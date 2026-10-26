"""Generate compact local reporting snapshots."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import case, func

from ledger.core.account_identity import account_identity
from ledger.core.results import RunResult
from ledger.finance_policy import spend_sql_filters
from ledger.query import LedgerQueryService
from ledger.storage.database import (
    AccountDB,
    DBOperationAuditDB,
    ReportSnapshotDB,
    TransactionDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import record_db_operation
from ledger.utils.dates import naive_utc_now
from ledger.utils.json_io import json_safe
from ledger.utils.money import to_cents

REPORT_SCHEMA_VERSION = 3


def _amount(value: Decimal | float | int) -> float:
    # float is safe here: every Decimal that reaches this function has already been
    # quantized to cents by the query layer (_money() / _cents()). A float64 can
    # represent any cent-quantized dollar value exactly up to ~$90 trillion
    # (2**53 / 100), which is unreachable in personal finance.  The cast is for
    # JSON serialisation; values are displayed only (never re-used in arithmetic).
    return float(value)


def _money(value: Decimal | float | int | None) -> Decimal:
    """Quantize a raw SQL aggregate to exact cents.

    ``func.sum``/``func.abs`` over a Numeric column loses SQLAlchemy's typing, so SQLite
    can hand back a float (e.g. 2999.9999999999995). Quantizing recovers the exact cent
    value before it enters the payload (which _json_safe later floats for JSON).
    """
    return to_cents(value)


# Shared with the insights engine and the SQLAlchemy JSON serializer so payloads,
# evidence dicts, and any other JSON column all normalize Decimal identically.
_json_safe = json_safe


def _recurring_payload(item: dict[str, Any]) -> dict[str, Any]:
    return _json_safe(
        {
            "pattern_id": item.get("pattern_id"),
            "pattern_type": item.get("pattern_type"),
            "merchant": item.get("merchant"),
            "category": item.get("category"),
            "institution": item.get("institution"),
            "frequency": item.get("frequency"),
            "expected_amount": item.get("expected_amount"),
            "confidence": item.get("confidence"),
            "occurrence_count": item.get("occurrence_count"),
            "first_occurrence": item.get("first_occurrence"),
            "last_occurrence": item.get("last_occurrence"),
            "next_expected_date": item.get("next_expected_date"),
            "is_active": item.get("is_active"),
            "missed_count": item.get("missed_count"),
        }
    )


def _health_payload(health: dict[str, Any] | None) -> dict[str, Any] | None:
    if not health:
        return None
    result = dict(health)
    score_date = result.get("score_date")
    if score_date is not None and hasattr(score_date, "isoformat"):
        result["score_date"] = cast(Any, score_date).isoformat()
    return result


def _movement(current: Decimal, previous: Decimal, *, comparable: bool = True) -> dict[str, float]:
    change = current - previous
    if not comparable:
        # There is no comparable prior period (e.g. the report window starts at the
        # earliest data date). A "new from zero" +100% would be spurious here -- the
        # baseline is unknown, not zero -- so report a neutral change and flag it.
        pct = Decimal("0")
        change = Decimal("0")
    elif previous:
        # abs(previous) so the % sign tracks the change direction even when the
        # baseline is negative (e.g. a net figure).
        pct = change / abs(previous) * 100
    elif current:
        pct = Decimal("100")  # new from zero -> +100%, not a misleading 0%
    else:
        pct = Decimal("0")
    result = {
        "current": _amount(current),
        "previous": _amount(previous),
        "change": _amount(change),
        "change_pct": _amount(pct),
    }
    if not comparable:
        result["comparable"] = False
    return result


# Fixed obligations are the biggest line items but not actionable cost-cutting targets,
# so they are excluded from cost-cutting suggestions.
_FIXED_OBLIGATION_KEYWORDS = (
    "mortgage",
    "rent",
    "insurance",
    "property tax",
    "condo",
    "hydro",
    "utility",
    "utilities",
    "loan",
    "lease",
)
# Match on WHOLE words so a discretionary merchant that merely contains a keyword as a
# substring (CURRENT RESTAURANT, PARENT ..., BRENT'S) is not wrongly dropped.
_FIXED_OBLIGATION_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _FIXED_OBLIGATION_KEYWORDS) + r")\b"
)


def _is_fixed_obligation(merchant: str) -> bool:
    return bool(_FIXED_OBLIGATION_RE.search(merchant.lower()))


class ReportSnapshotService:
    """Create and read cached dashboard snapshots."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        ensure_schema(self.db_path)

    def _compute_payload(
        self,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[dict[str, Any], date | None, date | None]:
        """Compute the snapshot payload without persisting anything.

        Returns ``(payload, report_start, report_end)``.  ``report_start`` /
        ``report_end`` are ``None`` when there are no transactions (empty DB).
        """
        service = LedgerQueryService(self.db_path)
        earliest, latest = service.get_date_range()
        if earliest is None or latest is None:
            return {"status": "empty"}, None, None

        report_end = end_date or latest
        report_start = start_date or max(earliest, report_end - timedelta(days=89))
        days = max((report_end - report_start).days + 1, 1)
        previous_end = report_start - timedelta(days=1)
        previous_start = max(earliest, previous_end - timedelta(days=days - 1))
        # When report_start is at/before the earliest data date, previous_start (clamped
        # to earliest) exceeds previous_end -> an inverted, always-empty window. Querying
        # it would return 0 for every previous KPI and make movement/drift read as a
        # spurious across-the-board "+100% new from zero". Treat the prior period as
        # ABSENT instead of comparing against an empty range.
        has_previous_period = previous_start <= previous_end
        # The clamp to `earliest` above SHORTENS the baseline whenever the report period
        # starts near the beginning of the data, and the guard only rejects a fully
        # inverted window. Comparing a shorter baseline to the full report window
        # would attribute the difference in window lengths to financial activity.
        # Only treat the baseline as comparable when it is the same length.
        baseline_days = (previous_end - previous_start).days + 1 if has_previous_period else 0
        baseline_comparable = has_previous_period and baseline_days == days
        kpis = service.compute_true_cost_kpis(report_start, report_end)
        category_details = service.get_true_cost_category_details(report_start, report_end)
        top_categories = {
            category: values["true_cost"] for category, values in category_details.items()
        }
        # Use the SAME window size on both sides so merchant_changes compares
        # like-for-like.  A mismatched window (current top-10 vs previous top-20)
        # made a steady merchant ranked just outside the narrower side read as a
        # spurious appear/disappear-to-zero.  We fetch top-20 on both sides for
        # the comparison and slice the emitted top_merchants list back to 10.
        top_merchants = service.get_spending_by_merchant(report_start, report_end, top_n=20)
        if has_previous_period:
            previous_kpis = service.compute_true_cost_kpis(previous_start, previous_end)
            previous_category_details = service.get_true_cost_category_details(
                previous_start,
                previous_end,
            )
            previous_categories = {
                category: values["true_cost"]
                for category, values in previous_category_details.items()
            }
            previous_merchants = service.get_spending_by_merchant(
                previous_start, previous_end, top_n=20
            )
        else:
            previous_kpis = {"income": Decimal("0"), "expenses": Decimal("0"), "net": Decimal("0")}
            previous_categories = {}
            previous_merchants = {}
        anomalies = service.detect_anomalies(report_start, report_end, threshold=2.0)
        # first_occurrence alone gates the window: these patterns are is_active, so
        # the detector says the commitment is live now. Requiring last_occurrence
        # inside the window dropped every active bill that had not recharged yet in
        # a young window — the same predicate defect fixed in planning readiness —
        # which understated the snapshot's recurring_costs and, downstream, the
        # recurring insight findings generated from this payload.
        recurring = [
            item
            for item in service.get_recurring_patterns(is_active=True, limit=200)
            if (not item["first_occurrence"] or item["first_occurrence"] <= report_end)
        ][:20]
        # Deferred: `planning.readiness` reaches `analysis.health_scorer`, whose
        # package imports `ledger.query`, which imports this module's siblings.
        from ledger.planning.readiness import health_basis

        latest_health = service.get_latest_health_score()
        # No comparable prior period -> emit empty drift/changes rather than reporting
        # every current category/merchant as "newly appeared".
        category_drift = (
            self._category_drift(top_categories, previous_categories) if has_previous_period else []
        )
        merchant_changes = (
            self._merchant_changes(top_merchants, previous_merchants) if has_previous_period else []
        )

        payload: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "fresh",
            "period": {
                "start": report_start.isoformat(),
                "end": report_end.isoformat(),
                "previous_start": previous_start.isoformat(),
                "previous_end": previous_end.isoformat(),
                # Consumers read defensively with .get(); the schema version still
                # rotates whenever cached identity or arithmetic semantics change.
                "days": days,
                "baseline_days": baseline_days,
                "baseline_comparable": baseline_comparable,
            },
            "data_freshness": {
                "earliest_transaction": earliest.isoformat(),
                "latest_transaction": latest.isoformat(),
                "snapshot_date": datetime.now(UTC).date().isoformat(),
            },
            "kpis": {
                "income": _amount(kpis["income"]),
                "expenses": _amount(kpis["expenses"]),
                "net": _amount(kpis["net"]),
                "transaction_count": int(kpis["transaction_count"]),
            },
            "true_cost": {
                "gross_spend": _amount(kpis["gross_spend"]),
                "offsets": _amount(kpis["offsets"]),
                "true_cost": _amount(kpis["true_cost"]),
                "gross_income": _amount(kpis["gross_income"]),
                "adjusted_income": _amount(kpis["income"]),
            },
            "income_spend_net_movement": {
                "income": _movement(
                    kpis["income"], previous_kpis["income"], comparable=baseline_comparable
                ),
                "spending": _movement(
                    kpis["expenses"], previous_kpis["expenses"], comparable=baseline_comparable
                ),
                "net": _movement(kpis["net"], previous_kpis["net"], comparable=baseline_comparable),
            },
            "top_categories": [
                {
                    "category": category,
                    "amount": _amount(amount),
                    "gross_spend": _amount(
                        category_details.get(category, {}).get("gross_spend", amount)
                    ),
                    "offsets": _amount(category_details.get(category, {}).get("offsets", 0)),
                }
                for category, amount in sorted(
                    top_categories.items(), key=lambda item: item[1], reverse=True
                )[:10]
            ],
            "top_merchants": [
                {"merchant": merchant, "amount": _amount(amount)}
                # top_merchants is fetched at top-20 for like-for-like
                # merchant_changes; the emitted list keeps its documented size 10.
                for merchant, amount in list(top_merchants.items())[:10]
            ],
            "recurring_costs": [
                _recurring_payload(item)
                for item in recurring
                if item.get("pattern_type") in {"subscription", "bill"}
            ][:10],
            "subscriptions": [
                _recurring_payload(item)
                for item in recurring
                if item.get("pattern_type") == "subscription"
            ][:10],
            "anomaly_count": len(anomalies),
            "anomalies": [
                {
                    "txn_id": txn.txn_id,
                    "date": txn.posting_date.isoformat(),
                    "merchant": txn.merchant,
                    "category": txn.category,
                    "amount": _amount(abs(txn.amount_signed)),
                }
                for txn in anomalies[:10]
            ],
            "cost_cutting_candidates": self._cost_cutting_candidates(top_merchants, recurring),
            "category_drift": category_drift,
            "merchant_changes": merchant_changes,
            "account_card_summaries": self._account_card_summaries(report_start, report_end),
            "credit_card_optimization": self._credit_card_summary(report_start, report_end),
            "health": _health_payload(latest_health),
            # Beside the score and never inside it, exactly as the readiness
            # summary carries it: `health` stays the stored row so a consumer
            # testing it for truthiness keeps getting the same answer. This
            # payload is PERSISTED, so a score embedded here with no basis
            # outlives every surface that could have explained it -- the same
            # unexplained frozen number, months later and with no way back to
            # the dates that would have placed it.
            "health_basis": None if not latest_health else health_basis(self.db_path),
        }
        return _json_safe(payload), report_start, report_end

    def create_snapshot(
        self,
        *,
        source: str = "manual",
        start_date: date | None = None,
        end_date: date | None = None,
        source_kind: str = "reporting",
    ) -> dict[str, Any]:
        payload, report_start, report_end = self._compute_payload(start_date, end_date)
        if payload.get("status") == "empty":
            return self._save_snapshot(
                source,
                start_date,
                end_date,
                payload,
                source_kind=source_kind,
                status="empty",
            )
        snapshot = self._save_snapshot(
            source,
            report_start,
            report_end,
            payload,
            source_kind=source_kind,
        )
        record_db_operation(
            self.db_path,
            operation="report_refresh",
            result=RunResult(
                status="success",
                started_at=snapshot["created_at"],
                finished_at=snapshot["created_at"],
                counts={"snapshot_id": int(snapshot["id"])},
                details={
                    "source": source,
                    "source_kind": source_kind,
                    "schema_version": payload["schema_version"],
                },
            ),
        )
        return snapshot

    def latest_snapshot(self) -> dict[str, Any] | None:
        with session_scope(self.db_path) as session:
            row = (
                session.query(ReportSnapshotDB).order_by(ReportSnapshotDB.created_at.desc()).first()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "created_at": row.created_at,
                "source": row.source,
                "source_kind": row.source_kind,
                "schema_version": row.schema_version,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "status": row.status,
                "payload": row.payload or {},
            }

    def snapshot_for_period(
        self,
        start_date: date,
        end_date: date,
        *,
        source: str = "period_view",
        source_kind: str = "reporting",
        persist: bool = False,
    ) -> dict[str, Any]:
        """Return a snapshot for the given period.

        Read-only by default (no snapshot/audit write): returns the freshest
        cached row if one exists and is up-to-date; otherwise computes the
        payload in-memory and returns a transient dict — no rows are written to
        ``report_snapshots`` or ``db_operations``.

        Pass ``persist=True`` only on an explicit refresh/apply action that
        intends to persist (e.g. an insight-engine run or an export package
        build).  Never pass it from a render/page path.
        """
        with session_scope(self.db_path) as session:
            row = (
                session.query(ReportSnapshotDB)
                .filter(ReportSnapshotDB.period_start == start_date)
                .filter(ReportSnapshotDB.period_end == end_date)
                .filter(ReportSnapshotDB.schema_version == REPORT_SCHEMA_VERSION)
                .filter(ReportSnapshotDB.source_kind == source_kind)
                .order_by(ReportSnapshotDB.created_at.desc())
                .first()
            )
            # A cached snapshot is stale if ANYTHING that changes the reported
            # numbers happened after it was written — not only an adjustments-detect
            # pass or a new import (import_finalize/import_stage, which adds
            # transactions the old snapshot never saw), but also any op that rewrites
            # merchant/category/exclusion on existing rows (review_apply,
            # category_override, category_optimize), promotes repaired statement rows
            # (statement_repair_promote), or deletes rows (data_cleanup). Gate on the
            # latest of all of them (an explicit allowlist, so a future non-mutating
            # op does not silently start busting the cache) so a post-mutation view
            # never serves pre-mutation KPIs.  'rules_apply' is the bulk runtime
            # rules-apply, which rewrites merchant/category/exclusion on existing rows.
            latest_change = (
                session.query(func.max(DBOperationAuditDB.finished_at))
                .filter(
                    DBOperationAuditDB.operation.in_(
                        [
                            "adjustments_detect",
                            "import_finalize",
                            "import_stage",
                            "review_apply",
                            "category_override",
                            "category_optimize",
                            "statement_repair_promote",
                            "data_cleanup",
                            "rules_apply",
                            "proposal_apply",
                        ]
                    )
                )
                .filter(DBOperationAuditDB.status == "success")
                .scalar()
            )
            if row is not None and (latest_change is None or row.created_at >= latest_change):
                return {
                    "id": row.id,
                    "created_at": row.created_at,
                    "source": row.source,
                    "source_kind": row.source_kind,
                    "schema_version": row.schema_version,
                    "period_start": row.period_start,
                    "period_end": row.period_end,
                    "status": row.status,
                    "payload": row.payload or {},
                }

        if not persist:
            # Read-only path: compute transiently, do not write anything.
            payload, report_start, report_end = self._compute_payload(start_date, end_date)
            return {
                "id": None,
                "created_at": naive_utc_now(),
                "source": source,
                "source_kind": source_kind,
                "schema_version": int(payload.get("schema_version", REPORT_SCHEMA_VERSION)),
                "period_start": report_start if report_start is not None else start_date,
                "period_end": report_end if report_end is not None else end_date,
                "status": payload.get("status", "fresh"),
                "payload": payload,
            }

        return self.create_snapshot(
            source=source,
            start_date=start_date,
            end_date=end_date,
            source_kind=source_kind,
        )

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        with session_scope(self.db_path) as session:
            rows = (
                session.query(ReportSnapshotDB)
                .order_by(ReportSnapshotDB.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "created_at": row.created_at,
                    "source": row.source,
                    "source_kind": row.source_kind,
                    "schema_version": row.schema_version,
                    "period_start": row.period_start,
                    "period_end": row.period_end,
                    "status": row.status,
                }
                for row in rows
            ]

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with session_scope(self.db_path) as session:
            row = session.get(ReportSnapshotDB, snapshot_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "created_at": row.created_at,
                "source": row.source,
                "source_kind": row.source_kind,
                "schema_version": row.schema_version,
                "period_start": row.period_start,
                "period_end": row.period_end,
                "status": row.status,
                "payload": row.payload or {},
            }

    def _save_snapshot(
        self,
        source: str,
        start_date: date | None,
        end_date: date | None,
        payload: dict[str, Any],
        *,
        source_kind: str = "reporting",
        status: str = "success",
    ) -> dict[str, Any]:
        created_at = naive_utc_now()
        with session_scope(self.db_path) as session:
            row = ReportSnapshotDB(
                created_at=created_at,
                source=source,
                source_kind=source_kind,
                schema_version=int(payload.get("schema_version", REPORT_SCHEMA_VERSION)),
                period_start=start_date,
                period_end=end_date,
                status=status,
                payload=payload,
            )
            session.add(row)
            session.flush()
            snapshot_id = int(row.id)
        return {
            "id": snapshot_id,
            "created_at": created_at,
            "source": source,
            "source_kind": source_kind,
            "schema_version": int(payload.get("schema_version", REPORT_SCHEMA_VERSION)),
            "period_start": start_date,
            "period_end": end_date,
            "status": status,
            "payload": payload,
        }

    @staticmethod
    def _category_drift(
        current: dict[str, Decimal],
        previous: dict[str, Decimal],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for category in sorted(set(current) | set(previous)):
            current_amount = current.get(category, Decimal("0"))
            previous_amount = previous.get(category, Decimal("0"))
            change = current_amount - previous_amount
            # A category that appears from nothing has no defined percentage change.
            # Reporting a flat 100 made it indistinguishable from a category that
            # merely doubled, so mark it instead and let the UI label it "new".
            is_new = not previous_amount and bool(current_amount)
            pct = change / abs(previous_amount) * 100 if previous_amount else Decimal("0")
            rows.append(
                {
                    "category": category,
                    "current": _amount(current_amount),
                    "previous": _amount(previous_amount),
                    "change": _amount(change),
                    "change_pct": _amount(pct),
                    "is_new": is_new,
                    "direction": "up" if change > 0 else "down" if change < 0 else "flat",
                }
            )
        return sorted(rows, key=lambda row: abs(row["change"]), reverse=True)[:10]

    @staticmethod
    def _merchant_changes(
        current: dict[str, Decimal],
        previous: dict[str, Decimal],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for merchant in sorted(set(current) | set(previous)):
            current_amount = current.get(merchant, Decimal("0"))
            previous_amount = previous.get(merchant, Decimal("0"))
            change = current_amount - previous_amount
            rows.append(
                {
                    "merchant": merchant,
                    "current": _amount(current_amount),
                    "previous": _amount(previous_amount),
                    "change": _amount(change),
                }
            )
        return sorted(rows, key=lambda row: abs(row["change"]), reverse=True)[:10]

    @staticmethod
    def _cost_cutting_candidates(
        top_merchants: dict[str, Decimal],
        recurring: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        recurring_merchants = {str(item.get("merchant", "")).strip().upper() for item in recurring}
        candidates = []
        for merchant, amount in top_merchants.items():
            # Skip fixed obligations (mortgage, insurance, property tax, condo fees, ...).
            if _is_fixed_obligation(merchant):
                continue
            is_recurring = merchant.strip().upper() in recurring_merchants
            candidates.append(
                {
                    "merchant": merchant,
                    "amount": _amount(amount),
                    "reason": "recurring high spend" if is_recurring else "top spend driver",
                }
            )
        return candidates[:8]

    def _account_card_summaries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        # Aggregate over the canonical spend universe only (see
        # ledger.finance_policy.spend_sql_filters): expense-only,
        # include_in_cashflow, exclude_from_spending == False.  Without this the
        # net/volume figures pulled in transfers and excluded rows, which then
        # leaked into the share-safe export and the credit-card summary.
        with session_scope(self.db_path) as session:
            rows = (
                session.query(
                    TransactionDB.institution,
                    TransactionDB.account,
                    TransactionDB.account_type,
                    func.max(TransactionDB.posting_date).label("latest_posting_date"),
                    func.count(TransactionDB.txn_id).label("transaction_count"),
                    func.sum(TransactionDB.amount_signed).label("net"),
                    func.sum(func.abs(TransactionDB.amount_signed)).label("volume"),
                )
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .group_by(
                    TransactionDB.institution,
                    TransactionDB.account,
                    TransactionDB.account_type,
                )
                .all()
            )
        summaries: dict[str, dict[str, Any]] = {}
        latest_metadata: dict[str, tuple[date, str, str]] = {}
        for row in rows:
            account_id = account_identity(str(row.institution), str(row.account))
            summary = summaries.setdefault(
                account_id,
                {
                    "account_id": account_id,
                    "institution": str(row.institution),
                    "account_type": str(row.account_type),
                    "transaction_count": 0,
                    "net": Decimal("0.00"),
                    "volume": Decimal("0.00"),
                },
            )
            # A transaction's free-text account_type is display metadata. Sum every
            # historical spelling under the stable institution/slug identity, then
            # retain the newest spelling as the useful current label.
            metadata = (
                row.latest_posting_date,
                str(row.account_type),
                str(row.institution),
            )
            if metadata > latest_metadata.get(account_id, (date.min, "", "")):
                latest_metadata[account_id] = metadata
                summary["institution"] = str(row.institution)
                summary["account_type"] = str(row.account_type)

            # transaction_count counts spend-universe rows only (transfers and
            # excluded rows are filtered out above), so it stays consistent with
            # the net/volume figures reported alongside it. SQL sums are restored
            # to cents before canonical groups are combined.
            summary["transaction_count"] += int(row.transaction_count or 0)
            summary["net"] = _money(summary["net"] + _money(row.net))
            summary["volume"] = _money(summary["volume"] + _money(row.volume))

        return sorted(
            summaries.values(),
            key=lambda row: (-abs(row["net"]), row["account_id"]),
        )

    def _card_gross_charges(self, start_date: date, end_date: date) -> dict[str, Decimal]:
        """Gross charges (sum of charge amounts, excluding inflows) per
        canonical account id over the canonical spend universe.

        Kept separate from ``_account_card_summaries`` so the per-card payload
        (which feeds the share-safe export verbatim) is not widened with an
        unbucketed money column.  Within the spend universe payments/credits are
        already excluded (they are transfers), so this is the card's real period
        spend even when the balance is fully paid down.
        """
        gross_charges = func.sum(
            case(
                (TransactionDB.amount_signed < 0, func.abs(TransactionDB.amount_signed)),
                else_=0,
            )
        )
        with session_scope(self.db_path) as session:
            rows = (
                session.query(
                    TransactionDB.institution,
                    TransactionDB.account,
                    gross_charges.label("gross_charges"),
                )
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .group_by(TransactionDB.institution, TransactionDB.account)
                .all()
            )
        by_account_id: dict[str, Decimal] = {}
        for row in rows:
            account_id = account_identity(str(row.institution), str(row.account))
            by_account_id[account_id] = _money(
                by_account_id.get(account_id, Decimal("0.00")) + _money(row.gross_charges)
            )
        return by_account_id

    @staticmethod
    def _is_card_account_type(account_type: Any) -> bool:
        text = str(account_type).lower()
        return "card" in text or "visa" in text or "master" in text

    def _canonical_card_statuses(self, account_ids: set[str]) -> dict[str, bool]:
        """Return authoritative card flags for identities present in accounts.

        Transaction ``account_type`` values are parser display metadata and can
        change over time. ``AccountDB.is_credit_card`` is the analyzed account
        classification used by Accounts and planning, so reporting must use the
        same canonical identity when that dimension row exists. The label
        heuristic remains only as a compatibility fallback for transaction-only
        databases that have not run account analysis yet.
        """
        if not account_ids:
            return {}
        with session_scope(self.db_path) as session:
            rows = (
                session.query(AccountDB.account_id, AccountDB.is_credit_card)
                .filter(AccountDB.account_id.in_(account_ids))
                .all()
            )
        return {str(account_id): bool(is_card) for account_id, is_card in rows}

    def _credit_card_summary(self, start_date: date, end_date: date) -> dict[str, Any]:
        summaries = self._account_card_summaries(start_date, end_date)
        canonical_status = self._canonical_card_statuses(
            {str(row["account_id"]) for row in summaries}
        )
        cards = []
        for row in summaries:
            account_id = str(row["account_id"])
            is_card = canonical_status.get(account_id)
            if is_card is None:
                is_card = self._is_card_account_type(row["account_type"])
            if is_card:
                cards.append(row)
        gross_by_key = self._card_gross_charges(start_date, end_date)
        return {
            "card_count": len(cards),
            # total_spend is GROSS charges (sum of charge amounts), not net
            # (charges minus payments).  A card fully paid down has net ~0 but its
            # real period spend is its gross charges -- using net dropped that card
            # out / understated it.  All values are cent-quantized Decimal;
            # _json_safe serialises them at the payload boundary.
            "total_spend": sum(
                (gross_by_key.get(row["account_id"], Decimal("0")) for row in cards),
                Decimal("0"),
            ).quantize(Decimal("0.01")),
            # Sum of the per-card spend-universe `net`. NOTE: card payments/credits are
            # transfers/income and are excluded by the spend universe, so every contributing
            # row is an expense (amount_signed <= 0) — this equals -total_spend, NOT a
            # charges-minus-payments balance movement. A real net-movement figure would need
            # a separate query outside the spend universe.
            "total_net": sum((row["net"] for row in cards), Decimal("0")).quantize(Decimal("0.01")),
            "cards": cards,
        }
