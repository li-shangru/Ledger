"""Deterministic, explainable insight findings."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from ledger.core.results import RunResult
from ledger.finance_policy import is_cad_row, spend_sql_filters
from ledger.insights.preferences import SpendingPreferenceService, normalize_preference_name
from ledger.planning import BudgetService
from ledger.query import LedgerQueryService
from ledger.query.base import SessionScopeFactory
from ledger.reporting import ReportSnapshotService
from ledger.storage.database import (
    AccountDB,
    InsightFindingDB,
    RecurringPatternDB,
    TransactionDB,
    TransactionTreatmentDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.storage.snapshots import (
    audited_database_write_connection,
    database_target_lock,
    database_write_outcome,
)
from ledger.utils.json_io import json_safe

OPEN_STATUSES = {"open", "watch"}
VALID_STATUSES = {"open", "watch", "ignored", "reviewed"}


class StaleFindingError(ValueError):
    """The database or finding no longer matches the displayed action target."""


def _database_generation(db_path: Path) -> tuple[str, int, int, int, int]:
    path = db_path.expanduser().absolute()
    parent = path.parent.stat()
    main = path.stat()
    return str(path), parent.st_dev, parent.st_ino, main.st_dev, main.st_ino


def _finding_content_revision(
    finding_type: str,
    severity: str,
    title: str,
    explanation: str,
    evidence: dict[str, Any],
    suggested_action: str,
    status: str,
) -> str:
    """Bind an action to the evidence and status actually shown to the reviewer."""
    content = json.dumps(
        (finding_type, severity, title, explanation, evidence, suggested_action, status),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FindingActionTarget:
    """Internal UI binding; never included in ordinary finding/report results."""

    database_generation: tuple[str, int, int, int, int]
    finding_id: int
    fingerprint: str
    period_start: date
    period_end: date
    content_revision: str


@dataclass(frozen=True)
class GeneratedFinding:
    finding_type: str
    severity: str
    title: str
    explanation: str
    evidence: dict[str, Any]
    suggested_action: str
    action_priority: str = "normal"
    preference_context: dict[str, Any] | None = None
    effective_suggested_action: str | None = None

    def fingerprint(self, start_date: date, end_date: date) -> str:
        account_id = str(self.evidence.get("account_id") or "")
        canonical_card = self.finding_type == "credit_cards" and bool(account_id)
        if canonical_card:
            # Card display metadata can change without changing account identity.
            identity = account_id
            title = ""
        else:
            transaction_identity = "|".join(
                sorted(
                    str(value)
                    for value in (
                        self.evidence.get("txn_id"),
                        self.evidence.get("related_txn_id"),
                    )
                    if value
                )
            )
            identity = "|".join(
                str(self.evidence.get(key) or "")
                for key in [
                    "account_id",
                    "pattern_id",
                    "date",
                    "merchant",
                    "category",
                    "institution",
                    "account_type",
                    "amount",
                ]
            )
            identity = f"{transaction_identity}|{identity}"
            title = self.title
        raw = "|".join(
            [
                start_date.isoformat(),
                end_date.isoformat(),
                self.finding_type,
                title,
                identity,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class InsightEngine:
    """Generate and persist local rules-based finance findings."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
        prepare_schema: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self._session_scope_factory = session_scope_factory
        if prepare_schema:
            ensure_schema(self.db_path)
        self.preferences = SpendingPreferenceService(
            self.db_path,
            session_scope_factory=session_scope_factory,
            prepare_schema=False,
        )

    def _open_session(self) -> AbstractContextManager[Session]:
        return self._session_scope_factory(self.db_path)

    def refresh_period(self, start_date: date | None, end_date: date | None) -> RunResult:
        started_at = utc_now()
        if start_date is None or end_date is None:
            # A database with no transactions yields a snapshot with no period
            # (_compute_payload returns status="empty" and None dates). Every caller
            # passes those straight through unguarded, so a refresh on an empty
            # database used to crash deep inside budget_variance -- taking down
            # `config import --execute`, `rules apply --execute`, and import finalize.
            return RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"generated": 0, "created": 0, "updated": 0, "resolved": 0},
                warnings=["No transactions in range; no insights generated."],
                details={"period_start": None, "period_end": None},
            )
        generated = self._dedupe(self.generate(start_date, end_date), start_date, end_date)
        now = utc_now()
        created = 0
        updated = 0
        with self._open_session() as session:
            existing: dict[str, InsightFindingDB] = {
                str(row.fingerprint): row
                for row in session.query(InsightFindingDB)
                .filter(InsightFindingDB.period_start == start_date)
                .filter(InsightFindingDB.period_end == end_date)
                .all()
            }
            generated_fingerprints = {
                finding.fingerprint(start_date, end_date) for finding in generated
            }
            for finding in generated:
                fingerprint = finding.fingerprint(start_date, end_date)
                row = existing.get(fingerprint)
                if row is None:
                    session.add(
                        InsightFindingDB(
                            fingerprint=fingerprint,
                            period_start=start_date,
                            period_end=end_date,
                            finding_type=finding.finding_type,
                            severity=finding.severity,
                            title=finding.title,
                            explanation=finding.explanation,
                            evidence=finding.evidence,
                            suggested_action=finding.suggested_action,
                            status="open",
                            created_at=now,
                        )
                    )
                    created += 1
                else:
                    row_obj = cast(Any, row)
                    row_obj.finding_type = finding.finding_type
                    row_obj.severity = finding.severity
                    row_obj.title = finding.title
                    row_obj.explanation = finding.explanation
                    row_obj.evidence = finding.evidence
                    row_obj.suggested_action = finding.suggested_action
                    updated += 1

            # Reconcile orphans: a prior finding that no longer regenerates (the
            # underlying issue is gone, or its evidence changed and re-fingerprinted)
            # must not linger as "open" and inflate the summary counts. Auto-close the
            # still-open/watch orphans; preserve user-set "ignored"/"reviewed" states.
            resolved = 0
            for fingerprint, row in existing.items():
                if fingerprint in generated_fingerprints:
                    continue
                row_obj = cast(Any, row)
                if row_obj.status in OPEN_STATUSES:
                    row_obj.status = "resolved"
                    row_obj.reviewed_at = now
                    resolved += 1

        result = RunResult(
            status="success",
            started_at=started_at,
            finished_at=utc_now(),
            counts={
                "generated": len(generated),
                "created": created,
                "updated": updated,
                "resolved": resolved,
            },
            details={"period_start": start_date.isoformat(), "period_end": end_date.isoformat()},
        )
        record_db_operation(self.db_path, operation="insights_refresh", result=result)
        return result

    def generate(self, start_date: date, end_date: date) -> list[GeneratedFinding]:
        snapshot = ReportSnapshotService(self.db_path).snapshot_for_period(
            start_date,
            end_date,
            source="insight_engine",
            persist=True,  # insight engine run is an explicit refresh action
        )
        payload = snapshot.get("payload") or {}
        findings: list[GeneratedFinding] = []
        findings.extend(self._category_drift(payload))
        findings.extend(self._anomalies(payload))
        findings.extend(self._possible_duplicates(start_date, end_date))
        findings.extend(self._recurring_price_increases(start_date, end_date))
        findings.extend(self._unresolved_reimbursements(start_date, end_date))
        findings.extend(self._recurring(payload, start_date, end_date))
        findings.extend(self._budget_findings(start_date, end_date))
        findings.extend(self._transfer_quality(start_date, end_date))
        findings.extend(self._credit_cards(payload))
        findings.extend(self._cost_cutting(payload))
        # The cap chooses detected facts before user context changes presentation
        # order, so adding/removing a preference cannot orphan a stored finding.
        return self.rank_findings(
            findings[:60],
            start_date=start_date,
            end_date=end_date,
        )

    def rank_findings(
        self,
        findings: list[GeneratedFinding],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[GeneratedFinding]:
        """Apply reversible user context without changing detected financial facts."""
        with self._open_session() as session:
            preferences = SpendingPreferenceService.list_from_session(session)
            merchant_categories = self._merchant_categories(
                session,
                [finding.evidence for finding in findings],
                start_date=start_date,
                end_date=end_date,
            )
        adjusted = [
            self._apply_preference(
                finding,
                preferences,
                fallback_category=merchant_categories.get(
                    self._merchant_key(finding.evidence.get("merchant"))
                ),
            )
            for finding in findings
        ]
        priority_rank = {"normal": 0, "low": 1, "suppressed": 2}
        return [
            finding
            for _index, finding in sorted(
                enumerate(adjusted),
                key=lambda item: (priority_rank.get(item[1].action_priority, 0), item[0]),
            )
        ]

    @staticmethod
    def _merchant_key(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        try:
            return normalize_preference_name("merchant", value)
        except ValueError:
            return ""

    @classmethod
    def _merchant_categories(
        cls,
        session: Session,
        evidence_rows: list[dict[str, Any]],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> dict[str, str]:
        """Resolve a merchant-only finding only when its period category is unambiguous."""
        if start_date is None or end_date is None:
            return {}
        merchants = {cls._merchant_key(evidence.get("merchant")) for evidence in evidence_rows} - {
            ""
        }
        if not merchants:
            return {}
        rows = (
            session.query(
                TransactionDB.merchant,
                TransactionDB.category,
            )
            .filter(TransactionDB.posting_date >= start_date)
            .filter(TransactionDB.posting_date <= end_date)
            .filter(TransactionDB.merchant.isnot(None))
            .filter(*spend_sql_filters())
            .distinct()
            .all()
        )
        categories_by_merchant: dict[str, dict[str, str]] = defaultdict(dict)
        for merchant, category in rows:
            merchant_key = cls._merchant_key(merchant)
            if merchant_key not in merchants:
                continue
            category_name = str(category or "").strip()
            if merchant_key and category_name:
                try:
                    normalized_category = normalize_preference_name("category", category_name)
                except ValueError:
                    continue
                categories_by_merchant[merchant_key].setdefault(
                    normalized_category.casefold(), normalized_category
                )
        return {
            merchant: next(iter(categories.values()))
            for merchant, categories in categories_by_merchant.items()
            if len(categories) == 1
        }

    @staticmethod
    def _positive_change(value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().casefold()
        if text in {"true", "yes", "increase", "increased"}:
            return True
        try:
            amount = Decimal(text)
        except InvalidOperation, ValueError:
            return False
        return amount.is_finite() and amount > 0

    @classmethod
    def _material_evidence(
        cls,
        finding_type: str,
        title: str,
        evidence: dict[str, Any],
    ) -> bool:
        normalized_type = finding_type.strip().casefold()
        if normalized_type in {
            "anomalies",
            "anomaly",
            "category_drift",
            "budget",
            "duplicates",
            "possible_duplicates",
            "reimbursements",
            "unresolved_reimbursements",
        }:
            return True
        issue = (
            str(
                evidence.get("issue")
                or evidence.get("evidence_type")
                or evidence.get("change_type")
                or ""
            )
            .strip()
            .casefold()
        )
        if issue in {
            "possible_duplicate",
            "duplicate_possibility",
            "unresolved_reimbursement",
            "price_increase",
            "recurring_price_increase",
            "material_change",
        }:
            return True
        if any(
            cls._positive_change(evidence.get(key))
            for key in ("price_increase", "price_change", "material_change")
        ):
            return True
        return normalized_type == "recurring" and (
            title.casefold().startswith("new recurring cost:") or evidence.get("is_new") is True
        )

    @classmethod
    def _preference_effect(
        cls,
        *,
        finding_type: str,
        title: str,
        evidence: dict[str, Any],
        suggested_action: str,
        preferences: list[dict[str, Any]],
        fallback_category: str | None = None,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        match_evidence = evidence
        if fallback_category and not evidence.get("category"):
            match_evidence = {**evidence, "category": fallback_category}
        preference = SpendingPreferenceService.match(match_evidence, preferences)
        if preference is None:
            return "normal", None, suggested_action

        material = cls._material_evidence(finding_type, title, evidence)
        if material:
            priority = "normal"
            effect = "material_evidence_retained"
            effective_action: str | None = suggested_action
        elif finding_type == "cost_cutting":
            priority = "suppressed"
            effect = "size_only_reduction_suppressed"
            effective_action = None
        else:
            priority = "low"
            effect = "ordinary_recommendation_deprioritized"
            effective_action = suggested_action
        context = {
            "scope": preference["scope"],
            "name": preference["name"],
            "disposition": preference["disposition"],
            "note": preference.get("note", ""),
            "effect": effect,
        }
        return priority, context, effective_action

    @classmethod
    def _apply_preference(
        cls,
        finding: GeneratedFinding,
        preferences: list[dict[str, Any]],
        *,
        fallback_category: str | None = None,
    ) -> GeneratedFinding:
        priority, context, effective_action = cls._preference_effect(
            finding_type=finding.finding_type,
            title=finding.title,
            evidence=finding.evidence,
            suggested_action=finding.suggested_action,
            preferences=preferences,
            fallback_category=fallback_category,
        )
        return replace(
            finding,
            action_priority=priority,
            preference_context=context,
            effective_suggested_action=effective_action,
        )

    @staticmethod
    def _dedupe(
        findings: list[GeneratedFinding],
        start_date: date,
        end_date: date,
    ) -> list[GeneratedFinding]:
        seen: set[str] = set()
        result = []
        for finding in findings:
            fingerprint = finding.fingerprint(start_date, end_date)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append(finding)
        return result

    def latest_stored_period(self) -> tuple[date, date] | None:
        """The period key the newest stored findings carry.

        Newest ``period_end`` wins; among ties the earliest ``period_start`` (the
        widest window ending on that date) — the same selection the MCP read
        surface serves, so the dashboard and the agent surface agree on which
        stored analysis is "current"."""
        with self._open_session() as session:
            row = (
                session.query(InsightFindingDB.period_start, InsightFindingDB.period_end)
                .order_by(
                    InsightFindingDB.period_end.desc(),
                    InsightFindingDB.period_start.asc(),
                )
                .first()
            )
        if row is None:
            return None
        return (row[0], row[1])

    def served_period(self, start_date: date, end_date: date) -> tuple[date, date] | None:
        """The period ``list_findings(start_date, end_date)`` will actually serve.

        Findings are persisted under the exact window the refresh that wrote them
        analysed (an import finalize writes the run's trailing window), and no UI
        preset reproduces that pair — filtering on exact equality alone rendered
        "0 open finding(s)" beside a database holding dozens. A view period with
        stored rows of its own (even fully triaged ones) is served as-is; any
        other view falls back to the newest stored period. ``None`` means nothing
        is stored at all."""
        with self._open_session() as session:
            exists = (
                session.query(InsightFindingDB.id)
                .filter(InsightFindingDB.period_start == start_date)
                .filter(InsightFindingDB.period_end == end_date)
                .first()
            )
        if exists is not None:
            return (start_date, end_date)
        return self.latest_stored_period()

    def list_findings(
        self,
        start_date: date,
        end_date: date,
        *,
        finding_type: str | None = None,
        include_closed: bool = False,
        include_action_targets: bool = False,
    ) -> list[dict[str, Any]]:
        # Capture before row reads to detect file replacement. Normal publication
        # preserves the inode, so each target also binds the displayed content.
        generation = _database_generation(self.db_path) if include_action_targets else None
        served = self.served_period(start_date, end_date)
        if served is not None:
            start_date, end_date = served
        with self._open_session() as session:
            query = (
                session.query(InsightFindingDB)
                .filter(InsightFindingDB.period_start == start_date)
                .filter(InsightFindingDB.period_end == end_date)
            )
            if finding_type:
                query = query.filter(InsightFindingDB.finding_type == finding_type)
            if not include_closed:
                query = query.filter(InsightFindingDB.status.in_(OPEN_STATUSES))
            severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
            rows = query.order_by(InsightFindingDB.created_at.desc()).all()
            preferences = SpendingPreferenceService.list_from_session(session)
            result: list[dict[str, Any]] = [
                {
                    "id": row.id,
                    "fingerprint": row.fingerprint,
                    "type": row.finding_type,
                    "severity": row.severity,
                    "title": row.title,
                    "explanation": row.explanation,
                    "evidence": row.evidence or {},
                    "suggested_action": row.suggested_action,
                    "status": row.status,
                    "created_at": row.created_at,
                    "reviewed_at": row.reviewed_at,
                }
                for row in rows
            ]
            if generation is not None:
                for item, stored in zip(result, rows, strict=True):
                    item["_action_target"] = FindingActionTarget(
                        generation,
                        stored.id,
                        stored.fingerprint,
                        stored.period_start,
                        stored.period_end,
                        _finding_content_revision(
                            stored.finding_type,
                            stored.severity,
                            stored.title,
                            stored.explanation,
                            stored.evidence or {},
                            stored.suggested_action,
                            stored.status,
                        ),
                    )
            merchant_categories = self._merchant_categories(
                session,
                [row["evidence"] for row in result],
                start_date=start_date,
                end_date=end_date,
            )
        priority_rank = {"normal": 0, "low": 1, "suppressed": 2}
        for row in result:
            priority, context, effective_action = self._preference_effect(
                finding_type=row["type"],
                title=row["title"],
                evidence=row["evidence"],
                suggested_action=row["suggested_action"],
                preferences=preferences,
                fallback_category=merchant_categories.get(
                    self._merchant_key(row["evidence"].get("merchant"))
                ),
            )
            row["action_priority"] = priority
            row["preference_context"] = context
            row["effective_suggested_action"] = effective_action
        return sorted(
            result,
            key=lambda row: (
                priority_rank.get(row["action_priority"], 0),
                severity_rank.get(row["severity"], 5),
                row["title"],
            ),
        )

    def update_status(
        self,
        finding_id: int,
        status: str,
        *,
        expected: FindingActionTarget | None = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Unsupported finding status: {status}")
        if expected is not None:
            self._update_displayed_status(finding_id, status, expected)
            return
        with self._open_session() as session:
            row = session.get(InsightFindingDB, finding_id)
            if row is None:
                raise ValueError(f"Insight finding not found: {finding_id}")
            row.status = status
            row.reviewed_at = utc_now() if status in {"ignored", "reviewed"} else None

    def _update_displayed_status(
        self, finding_id: int, status: str, expected: FindingActionTarget
    ) -> None:
        message = (
            "The database or finding changed since it was displayed. "
            "Reopen the finding before changing its status."
        )
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Insight status update target"
            ) as connection:
                if (
                    finding_id != expected.finding_id
                    or _database_generation(self.db_path) != expected.database_generation
                ):
                    raise StaleFindingError(message)
                row = connection.execute(
                    "SELECT fingerprint, period_start, period_end, finding_type, severity, "
                    "title, explanation, evidence, suggested_action, status "
                    "FROM insight_findings WHERE id = ?",
                    (finding_id,),
                ).fetchone()
                if row is None or tuple(row[:3]) != (
                    expected.fingerprint,
                    expected.period_start.isoformat(),
                    expected.period_end.isoformat(),
                ):
                    raise StaleFindingError(message)
                if (
                    _finding_content_revision(
                        row[3], row[4], row[5], row[6], json.loads(row[7]) or {}, row[8], row[9]
                    )
                    != expected.content_revision
                ):
                    raise StaleFindingError(message)
                reviewed_at = (
                    utc_now().isoformat(sep=" ") if status in {"ignored", "reviewed"} else None
                )
                connection.execute(
                    "UPDATE insight_findings SET status = ?, reviewed_at = ? WHERE id = ?",
                    (status, reviewed_at, finding_id),
                )
            outcome.mark_committed()

    def summary(self, start_date: date, end_date: date) -> dict[str, int]:
        rows = self.list_findings(start_date, end_date, include_closed=False)
        return {
            "open": len(rows),
            "high": sum(1 for row in rows if row["severity"] in {"critical", "high"}),
            "watch": sum(1 for row in rows if row["status"] == "watch"),
        }

    @staticmethod
    def _severity(amount: float) -> str:
        if amount >= 1000:
            return "high"
        if amount >= 250:
            return "medium"
        return "low"

    def _cost_cutting(self, payload: dict[str, Any]) -> list[GeneratedFinding]:
        rows = []
        for item in payload.get("cost_cutting_candidates", [])[:8]:
            amount = float(item.get("amount") or 0)
            merchant = item.get("merchant") or "Unknown merchant"
            rows.append(
                GeneratedFinding(
                    finding_type="cost_cutting",
                    severity=self._severity(amount),
                    title=f"Review spend at {merchant}",
                    explanation=f"{merchant} is a top spend driver in this period.",
                    evidence=item,
                    suggested_action="Review whether this spend is necessary, recurring, or can be reduced.",
                )
            )
        return rows

    def _category_drift(self, payload: dict[str, Any]) -> list[GeneratedFinding]:
        rows = []
        for item in payload.get("category_drift", [])[:8]:
            change = float(item.get("change") or 0)
            if change <= 100:
                continue
            category = item.get("category") or "Unknown category"
            rows.append(
                GeneratedFinding(
                    finding_type="category_drift",
                    severity=self._severity(change),
                    title=f"{category} increased",
                    explanation=f"{category} spending increased by ${change:,.2f} versus the previous comparable period.",
                    evidence=item,
                    suggested_action="Open Categories to inspect the merchants driving this increase.",
                )
            )
        return rows

    def _credit_cards(self, payload: dict[str, Any]) -> list[GeneratedFinding]:
        summary = payload.get("credit_card_optimization") or {}
        rows = []
        for card in summary.get("cards", [])[:8]:
            net = float(card.get("net") or 0)
            if net >= 0:
                continue  # net-positive cards (paid down / refunds) are not spend
            spend = abs(net)
            rows.append(
                GeneratedFinding(
                    finding_type="credit_cards",
                    severity=self._severity(spend),
                    title=f"Card spend: {card.get('institution')} {card.get('account_type')}",
                    explanation="This card/account has material period spend and is a candidate for rewards optimization review.",
                    evidence=card,
                    suggested_action="Use the credit-card optimization export for external rewards/card comparison.",
                )
            )
        return rows

    def _anomalies(self, payload: dict[str, Any]) -> list[GeneratedFinding]:
        rows = []
        for item in payload.get("anomalies", [])[:8]:
            amount = float(item.get("amount") or 0)
            merchant = item.get("merchant") or "Unknown merchant"
            rows.append(
                GeneratedFinding(
                    finding_type="anomalies",
                    severity=self._severity(amount),
                    title=f"Unusual transaction at {merchant}",
                    explanation="This transaction is high relative to other transactions in its category.",
                    evidence=item,
                    suggested_action="Confirm this is expected and correctly categorized.",
                )
            )
        return rows

    def _possible_duplicates(self, start_date: date, end_date: date) -> list[GeneratedFinding]:
        """Surface guarded duplicate pairs as possibilities, never verdicts."""
        pairs = LedgerQueryService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
        ).get_duplicate_candidates(
            threshold_days=2,
            start_date=start_date,
            end_date=end_date,
        )
        rows: list[GeneratedFinding] = []
        for first, second in pairs:
            if (
                first.cashflow_type != "expense"
                or second.cashflow_type != "expense"
                or first.amount_signed >= 0
                or second.amount_signed >= 0
                or not first.include_in_cashflow
                or not second.include_in_cashflow
                or first.exclude_from_spending
                or second.exclude_from_spending
                or not is_cad_row(currency=first.currency)
                or not is_cad_row(currency=second.currency)
            ):
                continue
            evidence_ids = tuple(sorted((first.txn_id, second.txn_id)))
            amount = abs(first.amount_signed)
            rows.append(
                GeneratedFinding(
                    finding_type="possible_duplicates",
                    severity=self._severity(float(amount)),
                    title=f"Possible duplicate charge at {first.merchant}",
                    explanation=(
                        "Two imported charges share a merchant and amount within two days; "
                        "this is a detected possibility, not a confirmed duplicate."
                    ),
                    evidence={
                        "issue": "possible_duplicate",
                        "possibility": "detected",
                        "merchant": first.merchant,
                        "category": first.category,
                        "amount": format(amount, ".2f"),
                        "date": min(first.posting_date, second.posting_date).isoformat(),
                        "txn_id": evidence_ids[0],
                        "related_txn_id": evidence_ids[1],
                    },
                    suggested_action="Inspect both transactions before deciding whether either is duplicated.",
                )
            )
            if len(rows) == 8:
                break
        return rows

    def _recurring_price_increases(
        self, start_date: date, end_date: date
    ) -> list[GeneratedFinding]:
        """Compare the latest two observations for recurring costs."""
        rows: list[GeneratedFinding] = []
        with self._open_session() as session:
            patterns = (
                session.query(RecurringPatternDB)
                .filter(
                    RecurringPatternDB.is_active.is_(True),
                    RecurringPatternDB.pattern_type.in_(("bill", "subscription")),
                )
                .order_by(RecurringPatternDB.merchant, RecurringPatternDB.pattern_id)
                .all()
            )
            account_by_id = {
                str(row.account_id): row
                for row in session.query(AccountDB)
                .filter(AccountDB.account_id.in_([str(row.account_id) for row in patterns]))
                .all()
            }
            for pattern in patterns:
                account = account_by_id.get(str(pattern.account_id))
                if account is None:
                    continue
                observations = (
                    session.query(TransactionDB)
                    .filter(
                        TransactionDB.institution == str(account.institution),
                        TransactionDB.account == str(account.account_number),
                        TransactionDB.merchant == str(pattern.merchant),
                        TransactionDB.posting_date <= end_date,
                        *spend_sql_filters(),
                    )
                    .order_by(TransactionDB.posting_date.desc(), TransactionDB.txn_id.desc())
                    .limit(2)
                    .all()
                )
                if len(observations) != 2 or observations[0].posting_date < start_date:
                    continue
                current = abs(Decimal(str(observations[0].amount_signed)))
                previous = abs(Decimal(str(observations[1].amount_signed)))
                increase = current - previous
                if increase < Decimal("0.01"):
                    continue
                rows.append(
                    GeneratedFinding(
                        finding_type="recurring_price_increase",
                        severity=self._severity(float(increase)),
                        title=f"Recurring price increase possibility: {pattern.merchant}",
                        explanation=(
                            "The latest recurring observation is higher than the prior "
                            "one; the association does not prove a permanent price change."
                        ),
                        evidence={
                            "issue": "recurring_price_increase",
                            "possibility": "detected",
                            "merchant": str(pattern.merchant),
                            "category": str(pattern.category),
                            "amount": format(increase, ".2f"),
                            "current_amount": format(current, ".2f"),
                            "previous_amount": format(previous, ".2f"),
                            "date": observations[0].posting_date.isoformat(),
                            "previous_date": observations[1].posting_date.isoformat(),
                            "txn_id": str(observations[0].txn_id),
                            "related_txn_id": str(observations[1].txn_id),
                            "pattern_id": str(pattern.pattern_id),
                        },
                        suggested_action="Review the two observations and confirm whether the recurring amount changed.",
                    )
                )
                if len(rows) == 8:
                    break
        return rows

    def _unresolved_reimbursements(
        self, start_date: date, end_date: date
    ) -> list[GeneratedFinding]:
        """Expose reimbursement treatments that deterministic netting could not resolve."""
        rows: list[GeneratedFinding] = []
        with self._open_session() as session:
            candidates = (
                session.query(TransactionTreatmentDB, TransactionDB)
                .join(TransactionDB, TransactionDB.txn_id == TransactionTreatmentDB.txn_id)
                .filter(
                    TransactionTreatmentDB.treatment_type == "reimbursement",
                    TransactionTreatmentDB.status == "needs_review",
                    TransactionDB.posting_date >= start_date,
                    TransactionDB.posting_date <= end_date,
                )
                .order_by(TransactionDB.posting_date, TransactionDB.txn_id)
                .limit(8)
                .all()
            )
            for treatment, transaction in candidates:
                if not is_cad_row(currency=transaction.currency):
                    continue
                amount = abs(Decimal(str(transaction.amount_signed)))
                rows.append(
                    GeneratedFinding(
                        finding_type="unresolved_reimbursements",
                        severity=self._severity(float(amount)),
                        title=f"Unresolved reimbursement possibility at {transaction.merchant}",
                        explanation=(
                            "A reimbursement-like credit has no applied matching expense; "
                            "it remains a detected possibility requiring review."
                        ),
                        evidence={
                            "issue": "unresolved_reimbursement",
                            "possibility": "detected",
                            "merchant": str(transaction.merchant),
                            "category": str(treatment.target_category or transaction.category),
                            "amount": format(amount, ".2f"),
                            "date": transaction.posting_date.isoformat(),
                            "txn_id": str(transaction.txn_id),
                        },
                        suggested_action="Match this credit to an expense or leave it unresolved with review context.",
                    )
                )
        return rows

    def _recurring(
        self, payload: dict[str, Any], start_date: date, end_date: date
    ) -> list[GeneratedFinding]:
        rows = []
        for item in payload.get("subscriptions", [])[:8]:
            merchant = item.get("merchant") or "Unknown subscription"
            amount = float(item.get("expected_amount") or 0)
            first = item.get("first_occurrence")
            title = f"Recurring cost: {merchant}"
            explanation = (
                f"{merchant} is expected at ${amount:,.2f} on a {item.get('frequency')} cadence."
            )
            if first and start_date.isoformat() <= str(first) <= end_date.isoformat():
                title = f"New recurring cost: {merchant}"
                explanation = f"{merchant} first appeared as recurring during this selected period."
            rows.append(
                GeneratedFinding(
                    finding_type="recurring",
                    severity=self._severity(amount),
                    title=title,
                    explanation=explanation,
                    evidence=item,
                    suggested_action="Decide whether to keep, cancel, downgrade, or watch this recurring charge.",
                )
            )
        return rows

    def _budget_findings(self, start_date: date, end_date: date) -> list[GeneratedFinding]:
        rows = []
        for item in BudgetService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        ).budget_variance(start_date, end_date):
            if item["status"] != "over":
                continue
            over = abs(float(item["variance"]))
            rows.append(
                GeneratedFinding(
                    finding_type="budget",
                    severity=self._severity(over),
                    title=f"Budget overrun: {item['category']}",
                    explanation=f"{item['category']} is ${over:,.2f} over budget for the selected period.",
                    # budget_variance returns Decimal amounts by contract, and evidence
                    # lands in a JSON column. Every other producer feeds an already
                    # json-safe snapshot payload; this one is raw, so normalize it here
                    # as well as at the engine.
                    evidence=json_safe(item),
                    suggested_action="Open Budgets / Goals or Categories to adjust the target or reduce spend.",
                )
            )
        return rows

    def _transfer_quality(self, start_date: date, end_date: date) -> list[GeneratedFinding]:
        stats = LedgerQueryService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
        ).get_transfer_stats(start_date, end_date)
        unmatched = int(stats.get("unmatched_transfers", 0))
        if unmatched == 0:
            return []
        return [
            GeneratedFinding(
                finding_type="quality",
                severity="medium",
                title="Unmatched transfers need review",
                explanation=f"{unmatched} transfer transaction(s) are unmatched in this period.",
                evidence=stats,
                suggested_action="Open Transactions > Quality and inspect unmatched transfers.",
            )
        ]
