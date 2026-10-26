"""Read-only planning readiness summaries for dashboard surfaces."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.analysis.health_scorer import (
    FRONTIER_ALL_DORMANT,
    FRONTIER_NO_COVERAGE,
    MIN_ACTIVE_DAYS,
    SCORING_WINDOW_DAYS,
    StatementFrontier,
)
from ledger.analysis.pattern_detector import RECURRING_COST_TYPES, monthly_equivalent
from ledger.planning.service import BudgetService
from ledger.quality import DataQualityService
from ledger.query import LedgerQueryService
from ledger.utils.money import to_cents


def _cents(value: Any) -> Decimal:
    """Quantize a money value to two-decimal Decimal cents.

    Goal target/current/remaining are money fields and must stay cent-quantized
    Decimal — consistent with the rest of the codebase — rather than leaking out
    as float.
    """
    return to_cents(value)


# WHERE THE STORED SCORE'S DAY SITS, as a closed vocabulary rather than as a
# boolean. There are TWO dates a reader can check it against and they are not the
# same date: the newest imported posting day, and the newest day the scorer will
# anchor on (`default_anchor_date` = min(today - 1, newest posting day)). A
# single `is_stale` flag cannot tell a gap a recompute would close from one it
# structurally cannot, and measuring against one date while the banner measured
# against the other is what let one page warn "transactions run to {today}" and
# caption "current with the newest imported day" about the same number.
SCORE_NO_SCORE = "no_score_stored"
SCORE_NO_TRANSACTIONS = "no_imported_transactions"
SCORE_AT_NEWEST_IMPORTED_DAY = "scored_through_the_newest_imported_day"
SCORE_AT_SCORING_ANCHOR = "scored_through_the_newest_scoring_anchor"
SCORE_BEHIND = "behind_the_newest_imported_day"
SCORE_AHEAD = "scored_past_the_newest_imported_day"
SCORE_BASES = (
    SCORE_NO_SCORE,
    SCORE_NO_TRANSACTIONS,
    SCORE_AT_NEWEST_IMPORTED_DAY,
    SCORE_AT_SCORING_ANCHOR,
    SCORE_BEHIND,
    SCORE_AHEAD,
)


def _score_basis(
    scored_through: date | None,
    latest_transaction: date | None,
    anchor_date: date | None,
) -> str:
    """Which of :data:`SCORE_BASES` describes this ledger, in precedence order.

    ``SCORE_AHEAD`` comes before every "behind" case and exists because
    ``repair statements``, ``rebuild-db``, and a narrower re-import all remove
    transaction rows while ``financial_health_scores`` keeps the day the previous
    refresh scored. ``_prune_scores_after`` fixes that on the NEXT refresh; until
    then the stored score describes rows the database no longer holds, and the
    window between the two is exactly when an operator opens the dashboard to
    check the damage.
    """
    if scored_through is None:
        return SCORE_NO_SCORE
    if latest_transaction is None:
        return SCORE_NO_TRANSACTIONS
    if scored_through > latest_transaction:
        return SCORE_AHEAD
    if scored_through == latest_transaction:
        return SCORE_AT_NEWEST_IMPORTED_DAY
    if anchor_date is not None and scored_through >= anchor_date:
        return SCORE_AT_SCORING_ANCHOR
    return SCORE_BEHIND


def build_health_basis(
    *,
    scored_through: date | None,
    computed_at: datetime | None,
    latest_transaction: date | None,
    anchor_date: date | None,
    active_days: int | None,
    frontier: StatementFrontier,
) -> dict[str, Any]:
    """The health score's two disclosed bases, plus the words for saying them.

    THE DEFECT THIS EXISTS FOR. Four surfaces render the stored score and only
    one of them explained it. The Overview caption carried the sparsity warning
    while the Planning Readiness "Health Score" tile, the Budgets/Goals
    "Emergency Fund" tile and ``ledger health`` presented a weeks-old number as
    current. Building the fact here means a consumer has to work to omit it
    rather than work to include it.

    WHAT EACH BASIS CLAIMS, EXACTLY.

    * ``scored_through`` is the SELECTION RULE "the newest stored daily score",
      published beside that row's ``computed_at``. It is deliberately not
      published as "the newest scoreable day": which days a refresh was handed is
      a refresh-time fact this read path cannot recover, and one narrower refresh
      would falsify the stronger claim permanently.
    * ``covered_through`` is the MINIMUM over the non-dormant accounts of each
      one's newest covered period, so the claim it supports is "every non-dormant
      account WITH IMPORTED STATEMENT COVERAGE has a statement for that month or
      later" (D8's dormancy rule; ``accounts.is_active`` is never consulted). The
      qualifier is not a hedge: ``statement_coverage`` has one producer and
      investment accounts are outside it, so the clause states the population
      size beside the month.

    ``active_days`` is the SCORER's own gate input for the window ENDING
    ``anchor_date`` -- rows passing ``is_income_row`` or ``is_spend_row``, not a
    ``COUNT(DISTINCT posting_date)``, which would report a higher number than the
    gate and make the explanation look like a lie.

    ONE REFERENCE FOR STALENESS, AND IT IS THE ONE THE BANNER USES.
    ``days_behind`` and ``is_stale`` are both measured against
    ``latest_transaction`` -- the newest imported posting day -- because that is
    what the Overview banner triggers on. Measured against ``anchor_date``
    instead, a ledger holding a transaction posted today warned that the score
    trailed the data and captioned the same number "current with the newest
    imported day" on one page. ``anchor_date`` is still published, and
    ``score_basis`` says when the gap is the anchor rather than an un-recomputed
    score, because those two need opposite advice.

    ``days_behind`` is NEVER negative. A score dated past the data is its own
    basis (``SCORE_AHEAD``) with no day count at all, rather than a negative
    "days behind" value rendered beside ``is_stale: False``.
    """
    score_basis = _score_basis(scored_through, latest_transaction, anchor_date)
    days_behind = (
        (latest_transaction - scored_through).days
        if scored_through is not None
        and latest_transaction is not None
        and scored_through <= latest_transaction
        else None
    )
    return {
        "scored_through": scored_through,
        "computed_at": computed_at,
        "covered_through": frontier.period,
        "covered_through_basis": frontier.basis,
        "non_dormant_accounts": frontier.non_dormant_accounts,
        "dormant_accounts": frontier.dormant_accounts,
        "anchor_date": anchor_date,
        "latest_transaction": latest_transaction,
        "active_days": active_days,
        "window_days": SCORING_WINDOW_DAYS,
        "min_active_days": MIN_ACTIVE_DAYS,
        # A statement of fact -- "the newest stored score is not for the newest
        # imported day" -- and never a prediction that a recompute would move it.
        # `score_basis` carries that distinction.
        "is_stale": score_basis
        in {SCORE_BEHIND, SCORE_AT_SCORING_ANCHOR, SCORE_AHEAD, SCORE_NO_TRANSACTIONS},
        "score_basis": score_basis,
        "days_behind": days_behind,
        # ONE builder for the coverage sentence, rendered by the caption, the
        # tiles and the Overview banner alike. Built here rather than restated
        # per surface: two spellings of one measured fact is how the surfaces
        # drifted apart in the first place.
        "coverage_clause": _coverage_clause(frontier),
        "score_detail": _score_detail(
            scored_through=scored_through,
            latest_transaction=latest_transaction,
            days_behind=days_behind,
            score_basis=score_basis,
        ),
        "statement": _basis_statement(
            scored_through=scored_through,
            latest_transaction=latest_transaction,
            days_behind=days_behind,
            anchor_date=anchor_date,
            score_basis=score_basis,
            frontier=frontier,
        ),
    }


def _score_detail(
    *,
    scored_through: date | None,
    latest_transaction: date | None,
    days_behind: int | None,
    score_basis: str,
) -> str:
    """The short form, for a tile that shows the number itself.

    Every branch that is not "the newest imported day" names that day, so a tile
    can never imply the score reached a day it did not.
    """
    if score_basis == SCORE_NO_SCORE:
        return "No score stored"
    if score_basis == SCORE_NO_TRANSACTIONS:
        return f"as of {scored_through}, with no imported transactions to place it against"
    if score_basis == SCORE_AHEAD:
        return f"as of {scored_through}, past the newest imported day ({latest_transaction})"
    if score_basis == SCORE_AT_NEWEST_IMPORTED_DAY:
        return f"as of {scored_through}, the newest imported day"
    return (
        f"as of {scored_through}, {days_behind} day(s) behind the newest imported day "
        f"({latest_transaction})"
    )


def _coverage_clause(frontier: StatementFrontier) -> str:
    """Exactly the fact computed, and no more.

    The frontier is the MINIMUM of each non-dormant account's newest covered
    period, so what is true of every one of them is "has a statement for that
    month or later". "Covered through" would claim more: an account on a
    two-month cadence legitimately has no statement for the month in between,
    and the stronger sentence would be false for it.
    """
    if frontier.period is not None:
        # The POPULATION SIZE, stated, because "every account" is not what was
        # measured. `statement_coverage` has one producer -- the PDF
        # transaction-statement import -- so investment accounts, which keep
        # their own tables and their own closure column, are outside it
        # entirely. The sentence must identify its covered population so a
        # reader comparing it with the total account count can see
        # that, and a reader given "every account" cannot.
        assessed = frontier.non_dormant_accounts + frontier.dormant_accounts
        return (
            f"{frontier.non_dormant_accounts} of {assessed} accounts with imported "
            f"statement coverage are not dormant, and every one of them has a "
            f"statement for {frontier.period} or later"
        )
    if frontier.basis == FRONTIER_NO_COVERAGE:
        return "no imported statement is recorded for any account"
    if frontier.basis == FRONTIER_ALL_DORMANT:
        # BOTH routes, because `classify_gap` has two. Silence past a multiple of
        # the account's own cadence is a cadence fact; `SIGNAL_OPERATOR_CLOSURE`
        # is not one at all -- it is the operator's record, and it outranks the
        # cadence even for an account that is perfectly current under it. Naming
        # only the cadence asserts a derivation that was never applied.
        return (
            "every account with statements is dormant under its own cadence or "
            "recorded closed by the operator"
        )
    return "the operator's account records are unavailable, so coverage is not assessed"


def _basis_statement(
    *,
    scored_through: date | None,
    latest_transaction: date | None,
    days_behind: int | None,
    anchor_date: date | None,
    score_basis: str,
    frontier: StatementFrontier,
) -> str:
    """Both bases in one sentence, because they are two facts and not one.

    Neither is derivable from the other: a month can be fully covered while no
    day inside it clears the density gate, which is exactly the state that made
    the previous single number read as stale-and-unexplained.

    "Current with the newest imported day" is said in exactly ONE branch -- the
    one where the scored day IS that day. Every other branch names both dates,
    because the scorer's anchor is at most the day before today and so can never
    reach a transaction posted today, however many times it is recomputed.
    """
    coverage = _coverage_clause(frontier)
    if score_basis == SCORE_NO_SCORE:
        return f"No health score is stored; {coverage}."
    if score_basis == SCORE_NO_TRANSACTIONS:
        return (
            f"Health score as of {scored_through}, with no imported transactions left to "
            f"compare it against; {coverage}."
        )
    if score_basis == SCORE_AHEAD:
        return (
            f"Health score as of {scored_through}, past the newest imported day "
            f"({latest_transaction}), so it was computed from rows this database no longer "
            f"holds — run `ledger analysis refresh` to supersede it; {coverage}."
        )
    if score_basis == SCORE_AT_NEWEST_IMPORTED_DAY:
        return f"Health score as of {scored_through}, current with the newest imported day; {coverage}."
    if score_basis == SCORE_AT_SCORING_ANCHOR:
        return (
            f"Health score as of {scored_through}, {days_behind} day(s) behind the newest "
            f"imported day ({latest_transaction}) because the scorer anchors no later than "
            f"{anchor_date}; {coverage}."
        )
    return (
        f"Health score as of {scored_through}, {days_behind} day(s) behind the newest "
        f"imported day ({latest_transaction}); {coverage}."
    )


def health_basis(db_path: Path, *, latest_transaction: date | None = None) -> dict[str, Any]:
    """The basis block, for a caller with no readiness summary to hang it on.

    FOUR SURFACES, NOT THREE. `ledger health` renders the same stored score the
    Overview caption, the Planning Readiness tile and the Emergency Fund tile do,
    and it had no basis at all -- the surface census that justified this change
    was short by one, and the missed surface made the strongest false claim of
    the four ("Last N Days" over rows weeks old). It reaches the identical
    sentence through this function.

    Deliberately NOT via `PlanningReadinessService`, whose `__init__` constructs
    `BudgetService`, whose `__init__` calls `ensure_schema` -- applying every
    pending migration without taking a backup is far more than a read command
    asked for. That is also what makes `FRONTIER_NO_LIFECYCLE` a reachable answer
    rather than a published branch nothing can trigger.
    """
    from ledger.analysis.health_scorer import FinancialHealthScorer, statement_frontier
    from ledger.storage.database import FinancialHealthScoreDB, session_scope

    query_service = LedgerQueryService(db_path)
    if latest_transaction is None:
        _earliest, latest_transaction = query_service.get_date_range()

    with session_scope(db_path) as session:
        # ONE read of the newest stored row, so the timestamp always belongs to
        # the date beside it.
        newest = (
            session.query(
                FinancialHealthScoreDB.score_date,
                FinancialHealthScoreDB.computed_at,
            )
            .order_by(FinancialHealthScoreDB.score_date.desc())
            .first()
        )

    anchor_date: date | None = None
    active_days: int | None = None
    if latest_transaction is not None:
        scorer = FinancialHealthScorer(db_path)
        # The anchor the scorer itself would use -- min(yesterday, latest
        # posting date) -- not the newest transaction date, so the count
        # explains the day that really failed the gate.
        anchor_date = scorer.default_anchor_date()
        try:
            active_days = scorer.count_active_days(anchor_date)
        except Exception:  # pragma: no cover - defensive
            # None means "not established", which every consumer renders as
            # "cannot claim sparsity". A count that failed must never become
            # a confident wrong diagnosis of why the score is frozen.
            active_days = None

    return build_health_basis(
        scored_through=None if newest is None else newest[0],
        computed_at=None if newest is None else newest[1],
        latest_transaction=latest_transaction,
        anchor_date=anchor_date,
        active_days=active_days,
        frontier=statement_frontier(db_path),
    )


class PlanningReadinessService:
    """Build advisor-style readiness summaries without mutating plan rows."""

    def __init__(self, db_path: Path, *, prepare_schema: bool = True):
        self.db_path = db_path
        self.prepare_schema = prepare_schema
        self.query_service = LedgerQueryService(db_path)
        self.budget_service = BudgetService(db_path, prepare_schema=prepare_schema)

    def summary(
        self,
        start_date: date,
        end_date: date,
        *,
        pending_reviews: int = 0,
    ) -> dict[str, Any]:
        budgets = self.budget_service.list_budgets()
        goals = self.budget_service.list_goals()
        variance = self.budget_service.budget_variance(start_date, end_date)
        recurring_monthly = self._recurring_monthly(start_date, end_date)
        latest_health = self.query_service.get_latest_health_score() or {}
        _earliest, latest = self.query_service.get_date_range()
        quality_warnings = self._quality_warnings()

        active_goals = [goal for goal in goals if goal["status"] == "active"]
        target = sum(Decimal(str(goal["target_amount"])) for goal in active_goals)
        goals_assessable = not active_goals or all(
            goal["current_amount"] is not None for goal in active_goals
        )
        current = (
            sum(
                (Decimal(str(goal["current_amount"])) for goal in active_goals),
                Decimal("0"),
            )
            if goals_assessable
            else None
        )
        active_budgets = [row for row in budgets if Decimal(str(row["amount"])) > 0]

        summary: dict[str, Any] = {
            "budgets": {
                "active": len(active_budgets),
                "categories": len({row["category"] for row in active_budgets}),
                "over_budget": sum(1 for row in variance if row["status"] == "over"),
            },
            "goals": {
                "active": len(active_goals),
                "target": _cents(target),
                "current": _cents(current) if current is not None else None,
                "remaining": _cents(target - current) if current is not None else None,
            },
            "recurring_monthly": _cents(recurring_monthly),
            "health": latest_health,
            # Beside the score, never inside it: `health` is the stored row and
            # `{}` when nothing is stored, and a consumer testing it for
            # truthiness must keep getting the same answer.
            "health_basis": self._health_basis(latest_transaction=latest),
            "data_freshness": {
                "latest_transaction": latest,
                "pending_reviews": pending_reviews,
                "quality_warnings": quality_warnings,
            },
            "next_actions": [],
        }
        summary["next_actions"] = self._next_actions(summary)
        return summary

    def _health_basis(self, *, latest_transaction: date | None) -> dict[str, Any]:
        """The shared builder, reached with this summary's own date range.

        `latest_transaction` is passed in rather than re-read so the block beside
        `data_freshness` describes the same newest day that block reports.
        """
        return health_basis(self.db_path, latest_transaction=latest_transaction)

    def _recurring_monthly(self, start_date: date, end_date: date) -> Decimal:
        """Monthly-equivalent total of active recurring COSTS overlapping the period.

        Non-monthly cadences are normalized rather than dropped: filtering on
        ``frequency == "monthly"`` excluded a biweekly mortgage entirely, understating
        stated fixed costs by most of their real value.
        """
        total = Decimal("0")
        for pattern in self.query_service.get_recurring_patterns(is_active=True, limit=200):
            if pattern["pattern_type"] not in RECURRING_COST_TYPES:
                continue
            first = pattern.get("first_occurrence")
            if first and first > end_date:
                continue
            # No last_occurrence test: these patterns are is_active, so the detector
            # says the commitment is live NOW. Requiring a charge inside the window
            # dropped every bill whose last occurrence posted before the window
            # opened — on a default mid-month period that erased the biweekly
            # mortgage this docstring exists to include, understating stated fixed
            # costs from thousands per month to a few dollars.
            total += monthly_equivalent(pattern["expected_amount"], pattern["frequency"])
        return total

    def _quality_warnings(self) -> int:
        rows = DataQualityService(self.db_path, prepare_schema=self.prepare_schema).latest(limit=20)
        if not rows:
            return 0
        # latest() returns rows ordered run_at desc and each run records ~9 rows, so a
        # flat count over 20 rows spans ~2 runs and double-counts persistent warnings.
        # Scope to the most recent run only (mirrors EvidenceService._quality).
        newest_run = rows[0]["Run At"]
        return sum(
            1 for row in rows if row["Run At"] == newest_run and row["Status"] in {"warn", "fail"}
        )

    @staticmethod
    def _next_actions(summary: dict[str, Any]) -> list[str]:
        actions: list[str] = []
        pending_reviews = int(summary["data_freshness"]["pending_reviews"])
        if pending_reviews:
            actions.append(f"Resolve {pending_reviews} review row(s).")
        if summary["budgets"]["active"] == 0:
            actions.append("Create monthly category budgets.")
        if summary["goals"]["active"] == 0:
            actions.append("Create account-linked savings goals.")
        if not summary["health"]:
            # THE SAME FUTILE INSTRUCTION THE BANNER STOPPED GIVING. "Refresh
            # reports" fires whenever no score is stored, and the commonest
            # reason none is stored is that no day clears the density gate --
            # which a refresh recomputes and still cannot move. The basis already
            # knows which of the two states this is, so the action says so
            # instead of sending the operator to a command that cannot work.
            basis = summary.get("health_basis") or {}
            active_days = basis.get("active_days")
            minimum = basis.get("min_active_days", MIN_ACTIVE_DAYS)
            if active_days is not None and active_days < minimum:
                actions.append(
                    f"No day yet carries the {minimum} active days scoring needs "
                    f"(the newest window holds {active_days}); import newer statements "
                    "rather than refreshing."
                )
            else:
                actions.append("Refresh reports to compute the latest health score.")
        if summary["data_freshness"]["quality_warnings"]:
            actions.append("Review data quality warnings.")
        return actions
