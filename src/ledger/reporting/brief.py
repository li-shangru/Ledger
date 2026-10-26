"""Deterministic financial briefs over imported Ledger evidence."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func

from ledger.core.account_identity import lifecycle_account_ids
from ledger.query import LedgerQueryService
from ledger.query.account_refs import account_ref_map
from ledger.query.agent import InsightFinding
from ledger.query.base import SessionScopeFactory
from ledger.reporting.service import ReportSnapshotService
from ledger.storage.database import (
    AccountDB,
    AccountLifecycleDB,
    ConfigMetadataDB,
    StatementCoverageDB,
    StatementCoverageIntervalDB,
    TransactionDB,
)
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY
from ledger.utils.json_io import json_safe

_ZERO = Decimal("0.00")
_MAX_PAGE_SIZE = 100
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_ACTIVE_INSIGHT_STATUSES = {"open", "watch"}


def _decimal(value: object) -> Decimal:
    """Return a cent-quantized Decimal from a report/query boundary value."""
    if value is None:
        return _ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _money_text(value: Decimal) -> str:
    return f"CAD {value:,.2f}"


def _period(start_date: date, end_date: date, *, name: str) -> dict[str, object]:
    return {
        "name": name,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "days": (end_date - start_date).days + 1,
    }


class FinancialBriefService:
    """Build one bounded, deterministic financial brief.

    The ordinary application path reuses :class:`ReportSnapshotService`'s
    read-only period snapshot. A caller that already owns a pinned read-only
    session supplies ``session_scope_factory``; that path deliberately skips
    ``ReportSnapshotService`` construction because its constructor prepares the
    schema. All other reads use the same ``LedgerQueryService`` namespaces in
    both modes.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory | None = None,
    ) -> None:
        from ledger.planning.reconciliation import PlanReconciliationService

        self.db_path = Path(db_path)
        self._query = LedgerQueryService(
            self.db_path,
            session_scope_factory=session_scope_factory,
        )
        self._snapshot_service = (
            ReportSnapshotService(self.db_path) if session_scope_factory is None else None
        )
        self._reconciliation = PlanReconciliationService(
            self.db_path,
            session_scope_factory=self._query._session_scope_factory,
            prepare_schema=False,
        )

    def build_default(
        self, *, since: datetime | None = None, as_of: date | None = None
    ) -> dict[str, Any]:
        """Build at most 90 days through the latest precise imported evidence.

        Printed statement intervals include quiet days before the first or after
        the last activity. Legacy month-only coverage does not establish those
        dates, so it falls back to the transaction range.
        """
        earliest, latest = self._query.get_date_range()
        with self._query._open_session() as session:
            statement_start, statement_end = (
                session.query(
                    func.min(StatementCoverageIntervalDB.start_date),
                    func.max(StatementCoverageIntervalDB.end_date),
                )
                .filter(StatementCoverageIntervalDB.evidence_basis == "printed_statement_period")
                .one()
            )
        if statement_start is not None and statement_end is not None:
            earliest = min(earliest, statement_start) if earliest else statement_start
            latest = max(latest, statement_end) if latest else statement_end
        if earliest is None or latest is None:
            return self._empty_brief(
                period=None,
                comparison=None,
                start_date=None,
                end_date=None,
                earliest=None,
                latest=None,
                since=since,
                as_of=as_of,
            )
        start_date = max(earliest, latest - timedelta(days=89))
        return self.build(start_date, latest, since=since, as_of=as_of)

    def build(
        self,
        start_date: date,
        end_date: date,
        *,
        since: datetime | None = None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Build a JSON-safe brief for an inclusive selected period."""
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise TypeError("start_date and end_date must be dates")
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

        day_count = (end_date - start_date).days + 1
        comparison_end = start_date - timedelta(days=1)
        comparison_start = comparison_end - timedelta(days=day_count - 1)
        earliest, latest = self._query.get_date_range()
        comparison_comparable, comparison_reason = self._comparison_readiness(
            selected_start=start_date,
            selected_end=end_date,
            comparison_start=comparison_start,
            comparison_end=comparison_end,
        )

        period_block = _period(start_date, end_date, name="selected_period")
        comparison_block = {
            **_period(
                comparison_start,
                comparison_end,
                name="previous_equal_length_period",
            ),
            "comparable": comparison_comparable,
            "coverage_reason": comparison_reason,
        }

        current, current_activity = self._period_metrics(start_date, end_date)
        previous, _previous_activity = self._period_metrics(comparison_start, comparison_end)
        if earliest is None or latest is None or current_activity == 0:
            return self._empty_brief(
                period=period_block,
                comparison=comparison_block,
                start_date=start_date,
                end_date=end_date,
                earliest=earliest,
                latest=latest,
                since=since,
                as_of=as_of,
            )

        changes = self._changes(since)
        statements = self._statement_coverage(start_date, end_date)
        balances = self._balance_coverage(end_date)
        insights, insight_entries = self._insight_coverage(start_date, end_date, latest)
        freshness = self._freshness(earliest, latest, end_date)

        metrics = {
            "current": current,
            "comparison": previous,
            "change": {
                "income": current["income"] - previous["income"],
                "gross_spend": current["gross_spend"] - previous["gross_spend"],
                "offsets": current["offsets"] - previous["offsets"],
                "true_cost": current["true_cost"] - previous["true_cost"],
                "net": current["net"] - previous["net"],
                "transaction_count": (
                    int(current["transaction_count"]) - int(previous["transaction_count"])
                ),
            },
        }
        observations = self._observations(
            start_date=start_date,
            end_date=end_date,
            comparison_comparable=comparison_comparable,
            current=current,
            previous=previous,
            current_activity=current_activity,
            statements=statements,
            balances=balances,
            insights=insights,
            insight_entries=insight_entries,
            freshness=freshness,
            changes=changes,
            reconciliation=self._reconciliation_observations(start_date, end_date, as_of=as_of),
        )

        result: dict[str, Any] = {
            "schema_version": 1,
            "status": "ready",
            "period": period_block,
            "comparison": comparison_block,
            "currency": {
                "code": "CAD",
                "policy": "CAD reporting rows only; non-CAD activity is not converted or included.",
            },
            "metrics": metrics,
            "observations": observations,
            "coverage": {
                "completeness": {
                    "status": "not_asserted",
                    "statement": (
                        "Results cover imported records only; absence is not proof of real-world "
                        "absence."
                    ),
                },
                "freshness": freshness,
                "statements": statements,
                "balances": balances,
                "insights": insights,
                "changes": changes,
            },
        }
        return json_safe(result)

    def _period_metrics(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[dict[str, Decimal | int], int]:
        # Reuse the established reporting snapshot on ordinary application reads.
        # Its payload is already a reporting boundary, so immediately restore its
        # cent values to Decimal before deriving any brief arithmetic.
        if self._snapshot_service is not None:
            snapshot = self._snapshot_service.snapshot_for_period(start_date, end_date)
            payload = snapshot.get("payload") or {}
            kpis = payload.get("kpis") or {}
            true_cost = payload.get("true_cost") or {}
            metrics: dict[str, Decimal | int] = {
                "income": _decimal(kpis.get("income")),
                "gross_spend": _decimal(true_cost.get("gross_spend")),
                "offsets": _decimal(true_cost.get("offsets")),
                "true_cost": _decimal(true_cost.get("true_cost")),
                "net": _decimal(kpis.get("net")),
                "transaction_count": int(kpis.get("transaction_count") or 0),
            }
        else:
            kpis = self._query.compute_true_cost_kpis(start_date, end_date)
            metrics = {
                "income": _decimal(kpis["income"]),
                "gross_spend": _decimal(kpis["gross_spend"]),
                "offsets": _decimal(kpis["offsets"]),
                "true_cost": _decimal(kpis["true_cost"]),
                "net": _decimal(kpis["net"]),
                "transaction_count": int(kpis["transaction_count"]),
            }
        activity = int(self._query.compute_kpis(start_date, end_date)["activity_count"])
        return metrics, activity

    def _read_account_ref_salt(self) -> str | None:
        with self._query._open_session() as session:
            row = session.get(ConfigMetadataDB, ACCOUNT_REF_SALT_KEY)
            return None if row is None else str(row.value)

    def _comparison_readiness(
        self,
        *,
        selected_start: date,
        selected_end: date,
        comparison_start: date,
        comparison_end: date,
    ) -> tuple[bool, str]:
        """Require explicit statement coverage before interpreting a zero as inactivity."""
        required_periods = tuple(
            sorted(
                {
                    *self._month_labels(comparison_start, comparison_end),
                    *self._month_labels(selected_start, selected_end),
                }
            )
        )
        with self._query._open_session() as session:
            relevant_accounts = {
                str(account)
                for (account,) in session.query(TransactionDB.account)
                .filter(
                    TransactionDB.posting_date >= comparison_start,
                    TransactionDB.posting_date <= selected_end,
                )
                .distinct()
                .all()
            }
            dimension_accounts = (
                session.query(
                    AccountDB.account_id,
                    AccountDB.institution,
                    AccountDB.account_number,
                )
                .filter(AccountDB.first_transaction_date <= selected_end)
                .all()
            )
            coverage_rows = (
                session.query(
                    StatementCoverageDB.institution,
                    StatementCoverageDB.account_slug,
                    StatementCoverageDB.period,
                    StatementCoverageDB.statement_count,
                    StatementCoverageDB.row_count,
                    StatementCoverageDB.skipped_reason,
                )
                .filter(
                    StatementCoverageDB.period >= comparison_start.strftime("%Y-%m"),
                    StatementCoverageDB.period <= selected_end.strftime("%Y-%m"),
                )
                .all()
            )
            relevant_accounts.update(str(row.account_slug) for row in coverage_rows)

            stored_ids = {
                str(account_number): str(account_id)
                for account_id, _institution, account_number in dimension_accounts
            }
            lifecycle_candidates = {
                str(account_number): lifecycle_account_ids(
                    stored_ids,
                    str(account_number),
                    str(institution),
                )
                for _account_id, institution, account_number in dimension_accounts
            }
            candidate_ids = tuple(
                dict.fromkeys(
                    candidate
                    for candidates in lifecycle_candidates.values()
                    for candidate in candidates
                )
            )
            lifecycle_by_id = {
                str(row.account_id): row
                for row in (
                    session.query(AccountLifecycleDB)
                    .filter(AccountLifecycleDB.account_id.in_(candidate_ids))
                    .all()
                    if candidate_ids
                    else []
                )
            }
            for _account_id, _institution, account_number in dimension_accounts:
                account = str(account_number)
                lifecycle = next(
                    (
                        lifecycle_by_id[candidate]
                        for candidate in lifecycle_candidates[account]
                        if candidate in lifecycle_by_id
                    ),
                    None,
                )
                closed_before_comparison = (
                    lifecycle is not None
                    and str(lifecycle.status) == "closed"
                    and lifecycle.closed_on is not None
                    and lifecycle.closed_on < comparison_start
                )
                if not closed_before_comparison:
                    relevant_accounts.add(account)

        if not relevant_accounts:
            return False, "statement_coverage_cannot_establish_comparability"
        covered = {
            (str(account_slug), str(period))
            for _institution, account_slug, period, statement_count, row_count, skipped_reason in (
                coverage_rows
            )
            if int(statement_count) > 0
            and (int(row_count) > 0 or bool(str(skipped_reason).strip()))
        }
        complete = all(
            (account, period) in covered
            for account in relevant_accounts
            for period in required_periods
        )
        return (
            (
                True,
                "selected_and_comparison_periods_fully_covered_by_statements",
            )
            if complete
            else (False, "statement_coverage_cannot_establish_comparability")
        )

    @staticmethod
    def _month_labels(start_date: date, end_date: date) -> tuple[str, ...]:
        labels: list[str] = []
        current = start_date.replace(day=1)
        last = end_date.replace(day=1)
        while current <= last:
            labels.append(current.strftime("%Y-%m"))
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )
        return tuple(labels)

    def _statement_coverage(
        self,
        start_date: date | None,
        end_date: date | None,
    ) -> dict[str, Any]:
        salt = self._read_account_ref_salt()
        start_period = None if start_date is None else start_date.strftime("%Y-%m")
        end_period = None if end_date is None else end_date.strftime("%Y-%m")
        # Ask the public coverage query for bounded status counts, but compute its
        # account-level gap census against the brief's explicit end date. The
        # public weekly-review surface intentionally evaluates against today's
        # date; carrying that clock into a historical brief would make an
        # unchanged build produce different results next month.
        page = self._query.agent.statement_coverage(
            start_period=start_period,
            end_period=end_period,
            limit=1,
            account_ref_salt=None,
        )
        gap_census = page.gap_census
        gaps_evaluated_on: str | None = None
        if salt is not None and end_date is not None:
            with self._query._open_session() as session:
                refs = account_ref_map(session, salt)
                gap_census = self._query.agent._coverage_gaps(session, refs, salt, end_date)
            gaps_evaluated_on = end_date.isoformat()
        gaps = gap_census.counts
        return {
            "queried_periods": {
                "start": start_period,
                "end": end_period,
            },
            "observed_periods": {"start": page.observed_start, "end": page.observed_end},
            "matched_account_months": page.entry_count,
            "status_counts": {
                "imported_with_activity": page.status_counts.imported_with_activity,
                "imported_no_activity": page.status_counts.imported_no_activity,
                "imported_no_rows_unexplained": (page.status_counts.imported_no_rows_unexplained),
            },
            "gap_counts": {
                "overdue": gaps.overdue,
                "dormant": gaps.dormant,
                "unknown": gaps.unknown,
                "naming_a_missing_statement": gaps.naming_a_missing_statement,
            },
            "gaps_evaluated_on": gaps_evaluated_on,
            "gap_semantics": (
                "A missing period reflects the local import record and the account's observed "
                "cadence; it does not assert that an institution issued a statement."
            ),
            "account_refs": "available" if salt is not None else "withheld",
        }

    def _balance_coverage(self, as_of: date) -> dict[str, Any]:
        salt = self._read_account_ref_salt()
        cursor: str | None = None
        maximum_lag: int | None = None
        first_page = None
        while True:
            page = self._query.agent.account_balances(
                as_of=as_of,
                limit=_MAX_PAGE_SIZE,
                cursor=cursor,
                account_ref_salt=salt,
            )
            if first_page is None:
                first_page = page
            for entry in page.entries:
                lag = entry.level.as_of_lag_days
                if lag is not None:
                    maximum_lag = lag if maximum_lag is None else max(maximum_lag, lag)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert first_page is not None
        unavailable = (
            first_page.level_counts.no_absolute_anchor
            + first_page.level_counts.no_anchor_on_or_before_as_of
        )
        return {
            "as_of": as_of.isoformat(),
            "reported": first_page.level_counts.reported,
            "unavailable": unavailable,
            "maximum_anchor_lag_days": maximum_lag,
        }

    def _insight_coverage(
        self,
        start_date: date | None,
        end_date: date | None,
        latest_transaction: date | None,
    ) -> tuple[dict[str, Any], list[InsightFinding]]:
        salt = self._read_account_ref_salt()
        entries: list[InsightFinding] = []
        cursor: str | None = None
        first_page = None
        while True:
            page = self._query.agent.insight_findings(
                limit=_MAX_PAGE_SIZE,
                cursor=cursor,
                account_ref_salt=salt,
            )
            if first_page is None:
                first_page = page
            entries.extend(page.entries)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert first_page is not None

        if first_page.period_start is None or first_page.period_end is None:
            status = "absent"
            scoped_period = None
        else:
            scoped_period = {
                "start": first_page.period_start.isoformat(),
                "end": first_page.period_end.isoformat(),
            }
            if start_date is None or end_date is None:
                status = "available_without_requested_period"
            elif first_page.period_start != start_date or first_page.period_end != end_date:
                status = "stale_for_requested_period"
            elif latest_transaction is not None and first_page.period_end < latest_transaction:
                status = "stale_before_latest_transaction"
            else:
                status = "current_for_requested_period"

        return (
            {
                "status": status,
                "scoped_period": scoped_period,
                "computed_at": (
                    None if first_page.computed_at is None else first_page.computed_at.isoformat()
                ),
                "entry_count": first_page.entry_count,
                "unservable_count": first_page.unservable_count,
            },
            entries,
        )

    def _changes(self, since: datetime | None) -> dict[str, Any]:
        if since is None:
            return {"status": "not_requested"}
        salt = self._read_account_ref_salt()
        cursor: str | None = None
        first_page = None
        statement_count = 0
        while True:
            page = self._query.agent.changes(
                since=since,
                limit=_MAX_PAGE_SIZE,
                cursor=cursor,
                account_ref_salt=salt,
            )
            if first_page is None:
                first_page = page
            statement_count += sum(entry.statement_count for entry in page.entries)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert first_page is not None
        return {
            "status": "reported",
            "queried_since": since.isoformat(),
            "basis": first_page.basis,
            "delta_available": first_page.delta_available,
            "total_added": first_page.total_added,
            "statement_count": statement_count if salt is not None else None,
            "latest_imported_at": (
                None
                if first_page.latest_imported_at is None
                else first_page.latest_imported_at.isoformat()
            ),
        }

    @staticmethod
    def _freshness(earliest: date, latest: date, requested_end: date) -> dict[str, Any]:
        lag = max((requested_end - latest).days, 0)
        return {
            "earliest_transaction_date": earliest.isoformat(),
            "latest_transaction_date": latest.isoformat(),
            "requested_end": requested_end.isoformat(),
            "latest_transaction_lag_days": lag,
            "status": (
                "requested_period_extends_beyond_imported_transactions"
                if latest < requested_end
                else "within_imported_transaction_bounds"
            ),
        }

    def _observations(
        self,
        *,
        start_date: date,
        end_date: date,
        comparison_comparable: bool,
        current: dict[str, Decimal | int],
        previous: dict[str, Decimal | int],
        current_activity: int,
        statements: dict[str, Any],
        balances: dict[str, Any],
        insights: dict[str, Any],
        insight_entries: list[InsightFinding],
        freshness: dict[str, Any],
        changes: dict[str, Any],
        reconciliation: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for index, observation in enumerate(reconciliation):
            candidates.append((94, f"reconciliation:{index:02d}", observation))

        def add(
            priority: int,
            key: str,
            *,
            label: str,
            title: str,
            statement: str,
            dashboard_path: str,
            evidence_ids: list[str] | None = None,
            subject_kind: str = "none",
            subject: object | None = None,
            amount: Decimal | None = None,
            direction: str = "not_applicable",
        ) -> None:
            candidates.append(
                (
                    priority,
                    key,
                    {
                        "label": label,
                        "title": title,
                        "statement": statement,
                        "dashboard_path": dashboard_path,
                        "evidence_ids": evidence_ids or [],
                        "subject_kind": subject_kind,
                        "subject": None if subject is None else str(subject),
                        "amount": amount,
                        "direction": direction,
                    },
                )
            )

        gap_count = int(statements["gap_counts"]["naming_a_missing_statement"])
        if gap_count:
            add(
                100,
                "statement-gaps",
                label="coverage",
                title="Statement coverage gap",
                statement=(
                    f"The local import record has {gap_count} account(s) whose observed cadence "
                    "indicates at least one statement period is outstanding."
                ),
                dashboard_path="/import-center",
            )

        if freshness["status"] != "within_imported_transaction_bounds":
            add(
                95,
                "transaction-freshness",
                label="coverage",
                title="Transaction freshness boundary",
                statement=(
                    f"The latest imported transaction date is {freshness['latest_transaction_date']}, "
                    f"{freshness['latest_transaction_lag_days']} day(s) before the selected end date."
                ),
                dashboard_path="/import-center",
            )

        possibility = self._ranked_possibility(
            insight_entries,
            current_period=(start_date, end_date),
            insight_status=str(insights["status"]),
        )
        if possibility is not None:
            candidates.append((90, "insight-possibility", possibility))

        if comparison_comparable:
            current_categories = self._query.get_true_cost_category_details(start_date, end_date)
            previous_end = start_date - timedelta(days=1)
            previous_start = previous_end - timedelta(days=(end_date - start_date).days)
            previous_categories = self._query.get_true_cost_category_details(
                previous_start, previous_end
            )
            category, movement = self._largest_category_movement(
                current_categories,
                previous_categories,
            )
            if category is not None and movement != 0:
                direction = "higher" if movement > 0 else "lower"
                add(
                    85,
                    f"category:{category}",
                    label="arithmetic",
                    title="Largest category movement",
                    statement=(
                        f"{category} true cost was {_money_text(abs(movement))} {direction} than "
                        "the previous equal-length period."
                    ),
                    dashboard_path="/categories",
                    subject_kind="category",
                    subject=category,
                    amount=movement,
                    direction=direction,
                )
        else:
            add(
                85,
                "comparison-coverage",
                label="coverage",
                title="Comparison coverage",
                statement=(
                    "Statement coverage cannot establish complete selected and comparison "
                    "windows, so movement is not treated as comparable."
                ),
                dashboard_path="/overview",
            )

        add(
            80,
            "cashflow",
            label="arithmetic",
            title="Selected-period cash flow",
            statement=(
                f"Imported CAD reporting rows show {_money_text(_decimal(current['income']))} of "
                f"income, {_money_text(_decimal(current['true_cost']))} of true cost, and "
                f"{_money_text(_decimal(current['net']))} net."
            ),
            dashboard_path="/overview",
        )

        if changes.get("status") == "reported" and int(changes["total_added"]):
            label = "arithmetic" if changes.get("delta_available") else "coverage"
            add(
                78,
                "changes",
                label=label,
                title="Imported changes boundary",
                statement=(
                    f"The {changes['basis']} change count reports {changes['total_added']} "
                    "transaction row(s) after the supplied import-time boundary."
                ),
                dashboard_path="/import-center",
            )

        add(
            70,
            "activity",
            label="arithmetic",
            title="Imported activity in period",
            statement=f"The selected period contains {current_activity} imported transaction row(s).",
            dashboard_path="/transactions",
        )

        if int(balances["unavailable"]):
            add(
                68,
                "balance-coverage",
                label="coverage",
                title="Balance coverage",
                statement=(
                    f"{balances['unavailable']} account(s) have no statement-backed balance "
                    "available on or before the selected end date."
                ),
                dashboard_path="/accounts",
            )
        elif balances["maximum_anchor_lag_days"] is not None:
            add(
                68,
                "balance-freshness",
                label="coverage",
                title="Balance anchor freshness",
                statement=(
                    "The oldest served statement-backed balance is "
                    f"{balances['maximum_anchor_lag_days']} day(s) behind the selected end date."
                ),
                dashboard_path="/accounts",
            )

        add(
            10,
            "completeness",
            label="coverage",
            title="Imported-record boundary",
            statement=(
                "This brief covers imported records only and does not treat an absent record as "
                "proof that no real-world activity occurred."
            ),
            dashboard_path="/import-center",
        )

        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[:5]
        # Non-empty periods always have the cash-flow, activity, and completeness
        # fallbacks, so this assertion protects the public 3-5 contract.
        assert 3 <= len(selected) <= 5
        return [{"rank": rank, **item} for rank, (_priority, _key, item) in enumerate(selected, 1)]

    @staticmethod
    def _ranked_possibility(
        entries: list[InsightFinding],
        *,
        current_period: tuple[date, date],
        insight_status: str,
    ) -> dict[str, Any] | None:
        if insight_status != "current_for_requested_period":
            return None
        eligible = [
            entry
            for entry in entries
            if (entry.period_start, entry.period_end) == current_period
            and entry.status in _ACTIVE_INSIGHT_STATUSES
        ]
        if not eligible:
            return None
        non_suppressed = [entry for entry in eligible if entry.action_priority != "suppressed"]
        if non_suppressed:
            eligible = non_suppressed
        priority_rank = {"normal": 0, "low": 1, "suppressed": 2}
        entry = min(
            eligible,
            key=lambda item: (
                priority_rank.get(item.action_priority, 99),
                _SEVERITY_RANK.get(item.severity, 99),
                item.finding_type,
                tuple(sorted(item.scalars.items())),
            ),
        )
        amount = entry.scalars.get("amount")
        subject_kind = "none"
        subject = None
        for candidate_kind in ("category", "merchant", "institution"):
            candidate = entry.scalars.get(candidate_kind)
            if candidate is not None and str(candidate).strip():
                subject_kind = candidate_kind
                subject = candidate
                break
        amount_text = "" if amount is None else f" at {_money_text(_decimal(amount))}"
        subject_text = "" if subject is None else f" for {subject}"
        evidence_id = entry.scalars.get("evidence_id")
        related_evidence_id = entry.scalars.get("related_evidence_id")
        evidence_ids = [value for value in (evidence_id, related_evidence_id) if value is not None]
        return {
            "label": "possibility",
            "title": "Detected possibility for review",
            "statement": (
                f"A stored {entry.finding_type} finding{subject_text}{amount_text} is "
                "available for review; it is a detected possibility rather than a causal claim."
            ),
            "dashboard_path": "/insights",
            "evidence_ids": evidence_ids,
            "subject_kind": subject_kind,
            "subject": None if subject is None else str(subject),
            "amount": None if amount is None else _decimal(amount),
            "direction": "not_applicable",
        }

    @staticmethod
    def _largest_category_movement(
        current: dict[str, dict[str, Decimal]],
        previous: dict[str, dict[str, Decimal]],
    ) -> tuple[str | None, Decimal]:
        rows: list[tuple[Decimal, str, Decimal]] = []
        for category in sorted(set(current) | set(previous)):
            movement = _decimal(current.get(category, {}).get("true_cost")) - _decimal(
                previous.get(category, {}).get("true_cost")
            )
            rows.append((abs(movement), category, movement))
        if not rows:
            return None, _ZERO
        _magnitude, category, movement = max(rows, key=lambda item: (item[0], item[1]))
        return category, movement

    @staticmethod
    def _reconciliation_priority(finding: dict[str, Any]) -> tuple[int, Decimal, int, str, str]:
        # Verified differences and fully covered missing activity precede
        # unresolved matching/coverage questions. Amount changes use the actual
        # difference; missing or shifted activity uses the expected cash amount.
        # Amount and timing changes can coexist, so neither erases the other.
        uncertainty = {"ambiguous": 1, "coverage_gap": 2}.get(finding["status"], 0)
        expected_amount = abs(_decimal(finding["expected_amount"]))
        amount_impact = (
            abs(_decimal(finding["amount_difference"])) if finding["amount_changed"] else _ZERO
        )
        timing_impact = expected_amount if finding["date_changed"] else _ZERO
        magnitude = (
            max(amount_impact, timing_impact)
            if finding["amount_changed"] or finding["date_changed"]
            else expected_amount
        )
        return (
            uncertainty,
            -magnitude,
            -abs(int(finding["date_difference"] or 0)),
            finding["expected_date"],
            finding["occurrence_id"],
        )

    def _reconciliation_observations(
        self, start_date: date, end_date: date, *, as_of: date | None
    ) -> list[dict[str, Any]]:
        if (end_date - start_date).days >= 366:
            return [
                {
                    "label": "coverage",
                    "title": "Plan comparison period boundary",
                    "statement": "Plan comparisons need a selected period of at most 366 days. Choose a narrower period to review expected and actual activity.",
                    "dashboard_path": "/plan-reconciliation",
                    "evidence_ids": [],
                    "subject_kind": "none",
                    "subject": None,
                    "amount": None,
                    "direction": "not_applicable",
                    "as_of": (as_of or end_date).isoformat(),
                }
            ]
        eligible: list[dict[str, Any]] = []
        offset = 0
        while True:
            report = self._reconciliation.build(
                start_date,
                end_date,
                as_of=as_of or end_date,
                limit=100,
                offset=offset,
            )
            eligible.extend(
                finding
                for finding in report["findings"]
                if finding["actionable"] and not finding["reviewed"]
            )
            # Keep only four candidates between bounded pages, but inspect all
            # pages so a later material exception cannot be displaced by timing.
            eligible = sorted(eligible, key=self._reconciliation_priority)[:4]
            if not report["truncated"]:
                break
            offset += int(report["returned_count"])
        observations = []
        for finding in eligible[:4]:
            expected = (
                f"{finding['name']} was expected on {finding['expected_date']} "
                f"for {finding['currency']} {finding['expected_amount']}."
            )
            if finding["actual_date"] is not None:
                detail = (
                    f"Imported activity is dated {finding['actual_date']} for "
                    f"{finding['currency']} {finding['actual_amount']}. "
                    "Review the amount and posting-date differences independently."
                )
            elif finding["status"] == "missing":
                detail = "No matching activity appears in the fully covered posting window."
            elif finding["status"] == "coverage_gap":
                detail = "Statement coverage does not establish the complete posting window; a missing payment cannot be inferred."
            else:
                detail = "Multiple plausible matches need review before choosing an explanation."
            difference = (
                None
                if finding["amount_difference"] is None
                else _decimal(finding["amount_difference"])
            )
            observations.append(
                {
                    "label": "coverage" if finding["status"] == "coverage_gap" else "possibility",
                    "title": "Plan versus actual exception",
                    "statement": f"{expected} {detail}",
                    "dashboard_path": "/plan-reconciliation",
                    "evidence_ids": finding["evidence_ids"],
                    "subject_kind": "occurrence",
                    "subject": finding["occurrence_id"],
                    "as_of": report["as_of"],
                    "amount": difference,
                    "direction": (
                        "higher"
                        if difference is not None and difference > 0
                        else "lower"
                        if difference is not None and difference < 0
                        else "not_applicable"
                    ),
                }
            )
        return observations

    def _empty_brief(
        self,
        *,
        period: dict[str, object] | None,
        comparison: dict[str, object] | None,
        start_date: date | None,
        end_date: date | None,
        earliest: date | None,
        latest: date | None,
        since: datetime | None,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        statements = self._statement_coverage(start_date, end_date)
        balances = (
            self._balance_coverage(end_date)
            if end_date is not None
            else {
                "as_of": None,
                "reported": 0,
                "unavailable": 0,
                "maximum_anchor_lag_days": None,
            }
        )
        insights, _entries = self._insight_coverage(start_date, end_date, latest)
        freshness = (
            self._freshness(earliest, latest, end_date)
            if earliest is not None and latest is not None and end_date is not None
            else {
                "earliest_transaction_date": None,
                "latest_transaction_date": None,
                "requested_end": None if end_date is None else end_date.isoformat(),
                "latest_transaction_lag_days": None,
                "status": "no_imported_transactions",
            }
        )
        reconciliation = (
            self._reconciliation_observations(start_date, end_date, as_of=as_of)
            if start_date is not None and end_date is not None
            else []
        )
        return json_safe(
            {
                "schema_version": 1,
                "status": "empty",
                "period": period,
                "comparison": comparison,
                "currency": {
                    "code": "CAD",
                    "policy": "CAD reporting rows only; non-CAD activity is not converted or included.",
                },
                "metrics": None,
                "observations": [
                    *[
                        {"rank": rank, **observation}
                        for rank, observation in enumerate(reconciliation, 1)
                    ],
                    {
                        "rank": len(reconciliation) + 1,
                        "label": "coverage",
                        "title": "No imported activity in period",
                        "statement": (
                            "No imported transaction rows are available for the selected period; "
                            "completeness is not asserted."
                        ),
                        "dashboard_path": "/import-center",
                        "evidence_ids": [],
                        "subject_kind": "none",
                        "subject": None,
                        "amount": None,
                        "direction": "not_applicable",
                    },
                ],
                "coverage": {
                    "completeness": {
                        "status": "not_asserted",
                        "statement": (
                            "Results cover imported records only; absence is not proof of real-world "
                            "absence."
                        ),
                    },
                    "freshness": freshness,
                    "statements": statements,
                    "balances": balances,
                    "insights": insights,
                    "changes": self._changes(since),
                },
            }
        )


__all__ = ["FinancialBriefService"]
