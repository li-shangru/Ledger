"""
Financial health scoring system.

Computes daily 0-100 health scores based on cashflow, spending consistency, and savings.
"""

import logging
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from ledger.core.analysis_models import FinancialHealthScore
from ledger.finance_policy import is_income_row, is_spend_row
from ledger.query import LedgerQueryService
from ledger.storage.database import FinancialHealthScoreDB, session_scope
from ledger.utils.dates import naive_utc_now

logger = logging.getLogger(__name__)

# The scoring gate, named once so callers can explain it instead of restating it.
# A day is scoreable only when its trailing SCORING_WINDOW_DAYS window carries at
# least MIN_ACTIVE_DAYS distinct posting dates with income/spend rows. The UI reads
# these to tell "analytics were never re-run" apart from "the recent data is too
# sparse to score" -- two states that look identical from score_date alone.
SCORING_WINDOW_DAYS = 30
MIN_ACTIVE_DAYS = 20

# Why the coverage frontier answered what it answered. A caller renders the
# period when there is one and this reason when there is not, so "no month" never
# has to be guessed at.
FRONTIER_COVERED = "every_non_dormant_account_has_a_statement"
FRONTIER_NO_COVERAGE = "no_statement_coverage_recorded"
FRONTIER_ALL_DORMANT = "every_covered_account_is_dormant"
FRONTIER_NO_LIFECYCLE = "account_lifecycle_unavailable"
FRONTIER_BASES = (
    FRONTIER_COVERED,
    FRONTIER_NO_COVERAGE,
    FRONTIER_ALL_DORMANT,
    FRONTIER_NO_LIFECYCLE,
)


@dataclass(frozen=True)
class StatementFrontier:
    """The month every account in the population has a statement for OR LATER.

    The second of the two bases the health score is disclosed against, and a
    different kind of fact from the first: this one is about statements, and the
    score's own date is about daily transaction density. Conflating them is what
    made the frozen banner feel like a lie -- statements really were arriving
    while no recent day could be scored.

    WHICH ACCOUNTS THE CLAIM IS ABOUT. ``statement_coverage`` and nothing else,
    which one producer writes: the PDF transaction-statement import. Bank and
    card accounts are in it; investment accounts are not, because they keep their
    own ``investment_accounts`` / ``investment_statements`` tables and their own
    ``status`` column, none of which is read here. Counts travel with the period
    so the claim explicitly quantifies accounts WITH IMPORTED STATEMENT COVERAGE
    rather than every account the operator owns.

    ``period`` is the MINIMUM of each non-dormant account's newest covered
    period. An overdue account remains in the population; an explicit dormant
    assessment removes it. ``None`` means no period can be named, and ``basis``
    identifies why.

    "Or later" is the whole claim and not a hedge. An account on a two-month
    cadence has no statement for the month between two of its own, so "covered
    through" would be false for it while this is true for every account in the
    population above.

    Dormancy comes from C1's rule (D8): the account's own observed cadence plus
    the operator's recorded closure. ``accounts.is_active`` is never read -- it
    is derived from "did a transaction post in the last 60 days" and decays from
    the very silence being measured, so it would drop accounts that are open and
    still receiving statements.
    """

    period: str | None
    basis: str
    non_dormant_accounts: int
    dormant_accounts: int
    evaluated_on: date


def _lifecycle_table_present(db_path: Path) -> bool:
    """Whether this database has the operator's closure record at all.

    ``account_lifecycle`` ships in 1.4.0, so every database from before it
    reaches this path, and a bare ``OperationalError`` traceback names nothing a
    reader can act on. Deliberately NOT ``ensure_schema``, for the reason
    ``commands/accounts.py:_require_lifecycle_table`` gives: applying every
    pending migration without taking a backup is a far larger action than a read
    asked for.

    NOT a claim that no caller migrates. ``PlanningReadinessService.__init__``
    constructs ``BudgetService``, whose ``__init__`` calls ``ensure_schema``
    unconditionally, so the dashboard has already migrated one frame up before
    this is ever entered -- an earlier version of this docstring asserted the
    opposite and was wrong. The guard is for the callers that have NOT, of which
    ``ledger health`` is one.

    Without the table the closure signal cannot be applied, and this surface says
    so rather than deriving dormancy from cadence alone and presenting the result
    as if the operator's record had been consulted.
    """
    with closing(sqlite3.connect(db_path)) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_lifecycle'"
            ).fetchone()
            is not None
        )


def statement_frontier(db_path: Path, *, today: date | None = None) -> StatementFrontier:
    """Compute the coverage basis over ``statement_coverage``.

    ``today`` defaults to the shared ``gap_evaluation_date`` seam rather than to
    ``date.today()``, so this surface and ``ledger.coverage`` cannot disagree
    about the same account on the same day. The cadence import is deferred to the
    function body for the same reason the query layer defers it: patching that
    attribute has to reach every caller.

    ONE POPULATION, GATHERED ONCE. ``gather_coverage_population`` is the same
    read ``ledger.coverage``'s ``gaps`` block runs, and ``classify_gap`` is the
    same verdict function -- because this surface's published sentence cites that
    tool's rule as its authority (spec §10), and two independent implementations
    of one published rule is how a dashboard and an MCP tool come to classify the
    same account differently. ``tests/test_coverage_gaps.py`` asserts the two
    partition one fixture identically.
    """
    from ledger.analysis.statement_cadence import (
        GAP_DORMANT,
        classify_gap,
        estimate_cadence,
        gap_evaluation_date,
    )
    from ledger.analysis.statement_population import gather_coverage_population

    evaluated_on = today or gap_evaluation_date()
    if not _lifecycle_table_present(db_path):
        return StatementFrontier(
            period=None,
            basis=FRONTIER_NO_LIFECYCLE,
            non_dormant_accounts=0,
            dormant_accounts=0,
            evaluated_on=evaluated_on,
        )

    with session_scope(db_path) as session:
        population = gather_coverage_population(session)

    if not population.periods:
        return StatementFrontier(
            period=None,
            basis=FRONTIER_NO_COVERAGE,
            non_dormant_accounts=0,
            dormant_accounts=0,
            evaluated_on=evaluated_on,
        )

    frontier: str | None = None
    non_dormant = 0
    dormant = 0
    for slug in population.slugs():
        covered = population.periods[slug]
        assessment = classify_gap(
            last_covered=covered[-1],
            cadence=estimate_cadence(covered),
            today=evaluated_on,
            lifecycle_status=population.status(slug),
        )
        if assessment.classification == GAP_DORMANT:
            dormant += 1
            continue
        non_dormant += 1
        frontier = covered[-1] if frontier is None else min(frontier, covered[-1])

    if frontier is None:
        return StatementFrontier(
            period=None,
            basis=FRONTIER_ALL_DORMANT,
            non_dormant_accounts=0,
            dormant_accounts=dormant,
            evaluated_on=evaluated_on,
        )
    return StatementFrontier(
        period=frontier,
        basis=FRONTIER_COVERED,
        non_dormant_accounts=non_dormant,
        dormant_accounts=dormant,
        evaluated_on=evaluated_on,
    )


def _is_income_transaction(txn) -> bool:
    return is_income_row(
        amount_signed=txn.amount_signed,
        cashflow_type=txn.cashflow_type,
        include_in_cashflow=txn.include_in_cashflow,
        currency=txn.currency,
    )


def _is_spend_transaction(txn) -> bool:
    return is_spend_row(
        amount_signed=txn.amount_signed,
        cashflow_type=txn.cashflow_type,
        include_in_cashflow=txn.include_in_cashflow,
        exclude_from_spending=txn.exclude_from_spending,
        currency=txn.currency,
    )


def _active_days(transactions) -> int:
    """Distinct posting dates carrying cashflow weight, in an already-loaded window.

    Takes a loaded list rather than querying: compute_daily_score holds the window's
    rows already and re-reading them per day would undo the pre-loading that keeps
    compute_recent_scores at one query per day.
    """
    return len(
        {
            txn.posting_date
            for txn in transactions
            if _is_income_transaction(txn) or _is_spend_transaction(txn)
        }
    )


def _interpolate(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation through (x, score) anchors, clamped at the ends.

    Gives a continuous sub-score instead of a step function, so a single transaction
    crossing a bucket boundary no longer jumps the daily score by 20-25 points.
    """
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in pairwise(points):
        if x0 <= x <= x1:
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return points[-1][1]


class FinancialHealthScorer:
    """
    Compute financial health scores.

    Evaluates cashflow, spending consistency, and savings rate to produce
    a daily 0-100 health score.
    """

    def __init__(self, db_path: Path):
        """
        Initialize health scorer.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
        self.query_service = LedgerQueryService(db_path)

    def count_active_days(self, target_date: date) -> int:
        """How many distinct active days the window ending `target_date` holds.

        The gate input, exposed so a caller can explain WHY a day is unscoreable.
        `compute_daily_score` returns a bare None, which leaves the dashboard unable
        to distinguish stale analytics from data too sparse to score -- and it used
        to resolve that ambiguity by always advising `ledger analysis refresh`, which
        cannot move the score when the cause is sparsity.
        """
        transactions = self.query_service.get_transactions(
            start_date=target_date - timedelta(days=SCORING_WINDOW_DAYS - 1),
            end_date=target_date,
            limit=10_000_000,
        )
        return _active_days(transactions)

    def compute_daily_score(
        self,
        target_date: date | None = None,
        *,
        _preloaded_accounts: list[dict] | None = None,
        _preloaded_balances: list[dict] | None = None,
    ) -> FinancialHealthScore | None:
        """
        Compute health score for a specific date.

        Args:
            target_date: Date to compute score for (default: yesterday)
            _preloaded_accounts: Optional pre-loaded accounts list (internal, from
                compute_recent_scores) to avoid repeated get_accounts() queries.
            _preloaded_balances: Optional pre-loaded full balance history (internal,
                from compute_recent_scores) to avoid repeated get_account_balances()
                queries.  The list may include rows beyond end_date; this method
                filters to <= end_date in Python to preserve per-day semantics.

        Returns:
            FinancialHealthScore or None if insufficient data
        """
        if target_date is None:
            target_date = self.default_anchor_date()

        # Need at least SCORING_WINDOW_DAYS of history
        start_date = target_date - timedelta(days=SCORING_WINDOW_DAYS - 1)
        end_date = target_date

        # Load transactions for this 30-day window once, then reuse the list for
        # _get_cashflow_period, the unique-days check, and _compute_spending_consistency.
        transactions = self.query_service.get_transactions(
            start_date=start_date,
            end_date=end_date,
            # get_transactions defaults to limit=1000; a high-volume 30-day window
            # would otherwise be silently truncated to the most-recent 1000 rows,
            # skewing the cashflow totals, the 20-unique-days gate, and consistency.
            limit=10_000_000,
        )

        # Get cashflow data for last 30 days (reuse the already-loaded list)
        cashflow_30d = self._get_cashflow_period(transactions)

        if cashflow_30d["transaction_count"] == 0:
            logger.info(f"Insufficient data to compute health score for {target_date}")
            return None

        # Check that we have enough days of data
        active_days = _active_days(transactions)
        if active_days < MIN_ACTIVE_DAYS:
            logger.info(
                f"Insufficient data to compute health score for {target_date}: "
                f"only {active_days} days with transactions (need {MIN_ACTIVE_DAYS}+)"
            )
            return None

        # Resolve per-day balance snapshot (filtered to <= end_date) from the
        # pre-loaded full history when available, otherwise query the DB.
        if _preloaded_balances is not None:
            balances_as_of = [
                row
                for row in _preloaded_balances
                if row["balance_date"] <= end_date and row.get("balance_basis") == "absolute"
            ]
        else:
            balances_as_of = self.query_service.get_account_balances(
                end_date=end_date,
                balance_basis="absolute",
            )

        # Resolve accounts from pre-loaded data or query.
        #
        # Active accounts only: _has_absolute_balance_anchor requires EVERY in-scope
        # account to have an absolute balance row, so including long-closed accounts
        # (which stop producing balance rows) permanently suppresses emergency-fund
        # and debt scoring for every day.
        if _preloaded_accounts is not None:
            accounts = _preloaded_accounts
        else:
            accounts = self.query_service.get_accounts(is_active=True)

        # Compute component scores
        cashflow_health = self._compute_cashflow_health(cashflow_30d)
        spending_consistency = self._compute_spending_consistency(transactions)
        savings_score = self._compute_savings_rate(cashflow_30d)  # 0-100 sub-score
        savings_rate_pct = self._compute_savings_rate_pct(cashflow_30d)  # real percentage

        # Emergency-fund (months of buffer) and debt ratio sub-metrics are only
        # meaningful against an ABSOLUTE balance. Calculator rows are relative
        # (seeded at 0 on the first transaction); statement repair rows are absolute.
        # Without an explicit absolute row these metrics are NOT ASSESSABLE. Report
        # them as None and drop
        # their sub-scores from the composite rather than blending in a misleading
        # 0 (which would silently tank the score of a perfectly solvent user).
        has_absolute_balance = self._has_absolute_balance_anchor(accounts, balances_as_of)
        if has_absolute_balance:
            emergency_fund_months: Decimal | None = self._compute_emergency_fund_months(
                cashflow_30d, accounts, balances_as_of
            )
            debt_ratio: Decimal | None = self._compute_debt_ratio(accounts, balances_as_of)
        else:
            emergency_fund_months = None
            debt_ratio = None

        # Weighted overall score across the five 0-100 sub-scores (cashflow 30 / savings 25
        # / emergency 20 / debt 15 / consistency 10). Emergency-fund and debt need an
        # ABSOLUTE balance anchor; when none exists, drop
        # them and renormalize so a not-assessable buffer can't wrongly tank the score.
        components: list[tuple[Decimal, int]] = [
            (Decimal("0.30"), cashflow_health),
            (Decimal("0.25"), savings_score),
            (Decimal("0.10"), spending_consistency),
        ]
        if emergency_fund_months is not None and debt_ratio is not None:
            components.append((Decimal("0.20"), self._emergency_fund_score(emergency_fund_months)))
            components.append((Decimal("0.15"), self._debt_score(debt_ratio)))
        total_weight = sum((weight for weight, _ in components), Decimal("0"))
        overall_score = int(
            sum((weight * score for weight, score in components), Decimal("0")) / total_weight
        )

        # Detect risk factors
        risk_factors = self._detect_risk_factors(
            cashflow_30d, spending_consistency, savings_rate_pct
        )

        # Compare to 7 days ago for trend (a real prior score of 0 is a valid baseline,
        # so test against None explicitly rather than truthiness).
        score_7d_ago = self._get_score_7d_ago(target_date)
        if score_7d_ago is not None:
            score_change_7d = overall_score - score_7d_ago
            if score_change_7d > 5:
                score_trend_7d = "improving"
            elif score_change_7d < -5:
                score_trend_7d = "declining"
            else:
                score_trend_7d = "stable"
        else:
            score_change_7d = 0
            score_trend_7d = "stable"

        return FinancialHealthScore(
            score_date=target_date,
            overall_score=overall_score,
            cashflow_health=cashflow_health,
            spending_consistency=spending_consistency,
            savings_rate=savings_rate_pct,
            emergency_fund_months=emergency_fund_months,
            debt_ratio=debt_ratio,
            score_trend_7d=score_trend_7d,
            score_change_7d=score_change_7d,
            risk_factors=risk_factors,
            computed_at=naive_utc_now(),
        )

    def save_score(self, score: FinancialHealthScore) -> bool:
        """
        Save health score to database.

        Args:
            score: FinancialHealthScore object

        Returns:
            True if saved successfully
        """
        with session_scope(self.db_path) as session:
            db_score = FinancialHealthScoreDB(
                score_date=score.score_date,
                overall_score=score.overall_score,
                cashflow_health=score.cashflow_health,
                spending_consistency=score.spending_consistency,
                savings_rate=score.savings_rate,
                emergency_fund_months=score.emergency_fund_months,
                debt_ratio=score.debt_ratio,
                score_trend_7d=score.score_trend_7d,
                score_change_7d=score.score_change_7d,
                risk_factors=",".join(score.risk_factors),  # Store as comma-separated
                computed_at=score.computed_at,
            )
            session.merge(db_score)  # Upsert

        return True

    def default_anchor_date(self) -> date:
        """The newest day worth scoring: yesterday, or the last day with data.

        Scoring is anchored to the DATA, not the wall clock. compute_daily_score needs
        20 distinct days with transactions inside a trailing 30-day window, so once the
        newest statement is more than ~30 days old EVERY wall-clock-anchored day fails
        that gate, compute_recent_scores returns 0, and the last stored score is frozen
        forever while the dashboard keeps presenting it as current.

        ``min`` is deliberate: a live-current database is unaffected (yesterday is at or
        before the latest transaction), and only a stale one re-anchors backwards.
        """
        yesterday = date.today() - timedelta(days=1)
        _earliest, latest = self.query_service.get_date_range()
        if latest is None:
            return yesterday
        return min(yesterday, latest)

    def compute_recent_scores(self, days: int = 7, *, today: date | None = None) -> int:
        """
        Compute health scores for recent days.

        Args:
            days: Number of recent days to compute
            today: Reference date to treat as "today" (defaults to the day after
                :meth:`default_anchor_date`, so the newest scored day is that anchor).
                Pass an explicit value in tests to freeze the clock.

        Returns:
            Number of scores computed
        """
        # The loop below scores today-1 .. today-days, so shifting the anchor forward
        # by a day makes default_anchor_date() the NEWEST scored day.
        today = today or self.default_anchor_date() + timedelta(days=1)
        scores_computed = 0

        # Pre-load accounts and the full balance history once for the entire run.
        # Each call to compute_daily_score will filter the balance list to its own
        # end_date in Python, preserving per-day balance semantics at zero extra I/O.
        preloaded_accounts = self.query_service.get_accounts(is_active=True)
        preloaded_balances = self.query_service.get_account_balances(balance_basis="absolute")

        # Iterate OLDEST-first: compute_daily_score reads the score from 7 days prior
        # (_get_score_7d_ago) out of the DB, so that older day must already be saved.
        # range(days) newest-first left score_change_7d permanently 0 on a full run.
        for i in reversed(range(days)):
            target_date = today - timedelta(days=i + 1)  # i=days-1 → oldest, i=0 → yesterday
            score = self.compute_daily_score(
                target_date,
                _preloaded_accounts=preloaded_accounts,
                _preloaded_balances=preloaded_balances,
            )

            if score:
                self.save_score(score)
                scores_computed += 1

        # Drop scores dated after this pass's newest day. save_score upserts by
        # score_date, so a previous run anchored further forward (e.g. to a wall clock
        # that has since outrun the data) leaves rows this pass never revisits -- and
        # get_latest_health_score returns the newest row, so the dashboard would keep
        # serving those superseded values no matter how often analytics are refreshed.
        self._prune_scores_after(today - timedelta(days=1))

        return scores_computed

    def _prune_scores_after(self, newest_scored: date) -> None:
        with session_scope(self.db_path) as session:
            session.query(FinancialHealthScoreDB).filter(
                FinancialHealthScoreDB.score_date > newest_scored
            ).delete(synchronize_session=False)

    def _get_cashflow_period(self, transactions: list) -> dict:
        """Get cashflow summary from an already-loaded list of transactions."""
        total_income = Decimal("0")
        total_expenses = Decimal("0")
        transaction_count = 0

        for txn in transactions:
            is_income = _is_income_transaction(txn)
            is_spend = _is_spend_transaction(txn)
            if not (is_income or is_spend):
                continue

            transaction_count += 1
            if is_income:
                total_income += txn.amount_signed
            else:
                total_expenses += abs(txn.amount_signed)

        return {
            "income": total_income,
            "expenses": total_expenses,
            "net": total_income - total_expenses,
            "transaction_count": transaction_count,
        }

    def _compute_cashflow_health(self, cashflow_30d: dict) -> int:
        """
        Compute cashflow health score (0-100).

        Based on income/expense ratio.
        - Score 100: Income >= 2x expenses (saving >50%)
        - Score 75: Income >= 1.5x expenses (saving 33%)
        - Score 50: Income = expenses (breaking even)
        - Score 25: Expenses > income (deficit)
        - Score 0: Severe deficit (expenses >= 2x income)
        """
        income = cashflow_30d["income"]
        expenses = cashflow_30d["expenses"]

        if expenses == 0:
            # No spend with income is healthy; no spend AND no income is no economic
            # activity (e.g. an all-transfer month), which is neutral, not "healthy".
            return 100 if income > 0 else 50

        if income == 0:
            return 0  # No income is critical

        # Smooth (piecewise-linear) instead of 25-point bucket jumps.
        ratio = float(income) / float(expenses)
        return int(
            _interpolate(ratio, [(0.0, 0.0), (0.5, 25.0), (1.0, 50.0), (1.5, 75.0), (2.0, 100.0)])
        )

    def _compute_spending_consistency(self, transactions: list) -> int:
        """
        Compute spending consistency score (0-100).

        Based on variance in daily spending.
        - Low variance = high consistency = high score
        - High variance = low consistency = low score
        """
        # Compute daily spending from the already-loaded transaction list
        daily_spending: defaultdict[date, Decimal] = defaultdict(Decimal)

        for txn in transactions:
            if _is_spend_transaction(txn):
                daily_spending[txn.posting_date] += abs(txn.amount_signed)

        if not daily_spending:
            return 50  # No data, neutral score

        amounts = list(daily_spending.values())
        mean = sum(amounts, Decimal("0")) / len(amounts)

        if mean == 0:
            return 100  # No spending is perfectly consistent

        # Calculate coefficient of variation (std dev / mean)
        variance = sum(((amt - mean) ** 2 for amt in amounts), Decimal("0")) / len(amounts)
        std_dev = Decimal(str(float(variance) ** 0.5))
        cv = std_dev / mean

        # Convert CV to score
        # CV < 0.3 = very consistent (100)
        # CV 0.3-0.6 = moderate (75)
        # CV 0.6-1.0 = inconsistent (50)
        # CV > 1.0 = highly inconsistent (0-25)

        if cv < Decimal("0.3"):
            return 100
        elif cv < Decimal("0.6"):
            return 75
        elif cv < Decimal("1.0"):
            return 50
        else:
            # Scale from 50 down to 0 as CV increases
            score = max(0, int(50 - (cv - 1) * 25))
            return score

    def _compute_savings_rate(self, cashflow_30d: dict) -> int:
        """
        Compute savings rate score (0-100).

        Based on percentage of income saved.
        - 50%+ savings = 100
        - 30-50% savings = 80
        - 20-30% savings = 60
        - 10-20% savings = 40
        - 0-10% savings = 20
        - Negative savings (deficit) = 0
        """
        income = cashflow_30d["income"]
        expenses = cashflow_30d["expenses"]

        if income == 0:
            return 0  # Can't save without income

        savings = income - expenses
        savings_rate_pct = float((savings / income) * 100)
        if savings_rate_pct < 0:
            return 0  # negative savings (deficit)
        # Smooth instead of 20-point bucket jumps.
        return int(
            _interpolate(
                savings_rate_pct,
                [(0.0, 20.0), (10.0, 40.0), (20.0, 60.0), (30.0, 80.0), (50.0, 100.0)],
            )
        )

    def _compute_savings_rate_pct(self, cashflow_30d: dict) -> Decimal:
        """Actual savings rate as a percentage of income: (income - expenses) / income.

        This is the user-facing percentage stored on the score; the 0-100 bucket from
        _compute_savings_rate is only an internal input to the overall score. Clamped to
        the Numeric(5,2) column range; 0 when there is no income."""
        income = Decimal(str(cashflow_30d["income"]))
        expenses = Decimal(str(cashflow_30d["expenses"]))
        if income <= 0:
            return Decimal("0.00")
        pct = (income - expenses) / income * 100
        clamped = max(Decimal("-999.99"), min(pct, Decimal("999.99")))
        return clamped.quantize(Decimal("0.01"))

    def _emergency_fund_score(self, months: Decimal) -> int:
        """0-100 score from months of liquid buffer (3-6 months is the standard guidance)."""
        return int(_interpolate(float(months), [(0.0, 0.0), (3.0, 60.0), (6.0, 100.0)]))

    def _debt_score(self, debt_ratio: Decimal) -> int:
        """0-100 score from debt-to-assets (0 = no debt -> 100; >= 1.0 -> 0)."""
        return int(_interpolate(float(debt_ratio), [(0.0, 100.0), (0.5, 50.0), (1.0, 0.0)]))

    def _has_absolute_balance_anchor(
        self,
        accounts: list[dict],
        balances_as_of: list[dict],
    ) -> bool:
        """Whether every in-scope account has an ABSOLUTE (true) balance.

        This is the single gate that keeps emergency_fund_months and debt_ratio from
        being derived from a relative-only series. Printed statement repair rows are
        explicit absolute anchors; calculator rows are explicit relative changes. A
        partial snapshot must fail closed because an uncovered account could hold
        assets or debt that the aggregate metrics would otherwise treat as zero.
        """
        account_ids = {str(row["account_id"]) for row in accounts}
        absolute_account_ids = {
            str(row.get("account_id"))
            for row in balances_as_of
            if row.get("balance_basis") == "absolute"
        }
        return bool(account_ids) and account_ids.issubset(absolute_account_ids)

    def _compute_emergency_fund_months(
        self,
        cashflow_30d: dict,
        accounts: list[dict],
        balances_as_of: list[dict],
    ) -> Decimal:
        """
        Compute emergency fund coverage in months.

        Uses the latest explicit absolute non-credit account balances at or before the score date
        (balances_as_of is already filtered to <= end_date by the caller).
        Callers gate this method on ``_has_absolute_balance_anchor``; the method also
        filters defensively so relative or unrelated rows cannot contribute.
        """
        monthly_expenses = Decimal(str(cashflow_30d["expenses"]))
        if monthly_expenses <= 0:
            return Decimal("0.0")

        accounts_by_id = {row["account_id"]: row for row in accounts}
        balances_as_of = [
            row
            for row in balances_as_of
            if row.get("balance_basis") == "absolute" and row.get("account_id") in accounts_by_id
        ]
        latest_by_account: dict[str, dict] = {}
        for row in balances_as_of:
            account_id = row["account_id"]
            previous = latest_by_account.get(account_id)
            if previous is None or row["balance_date"] > previous["balance_date"]:
                latest_by_account[account_id] = row

        liquid_balance = Decimal("0.0")
        for account_id, row in latest_by_account.items():
            account = accounts_by_id.get(account_id, {})
            if account.get("is_credit_card"):
                continue
            balance = Decimal(str(row["balance"] or 0))
            if balance > 0:
                liquid_balance += balance

        months = (liquid_balance / monthly_expenses).quantize(Decimal("0.01"))
        return min(months, Decimal("999.99"))  # fits Numeric(5, 2)

    def _compute_debt_ratio(
        self,
        accounts: list[dict],
        balances_as_of: list[dict],
    ) -> Decimal:
        """
        Compute debt payment ratio.

        Uses latest explicit absolute account balances when available (balances_as_of is already
        filtered to <= end_date by the caller). Credit-card balances and
        negative account balances are treated as debt; positive non-card
        balances are treated as assets. Callers gate this method on
        ``_has_absolute_balance_anchor``; it also ignores relative or unrelated rows.
        """
        accounts_by_id = {row["account_id"]: row for row in accounts}
        balances_as_of = [
            row
            for row in balances_as_of
            if row.get("balance_basis") == "absolute" and row.get("account_id") in accounts_by_id
        ]
        latest_by_account: dict[str, dict] = {}
        for row in balances_as_of:
            account_id = row["account_id"]
            previous = latest_by_account.get(account_id)
            if previous is None or row["balance_date"] > previous["balance_date"]:
                latest_by_account[account_id] = row

        assets = Decimal("0.0")
        debts = Decimal("0.0")
        for account_id, row in latest_by_account.items():
            account = accounts_by_id.get(account_id, {})
            balance = Decimal(str(row["balance"] or 0))
            # Debt is what is OWED, which under the stored convention is a
            # negative balance -- for a card as much as for anything else.
            # Keying on is_credit_card discarded the sign, so a card sitting in
            # credit after an overpayment or a refund was counted as debt of the
            # same magnitude. Positive card balances must retain that sign.
            if balance < 0:
                debts += -balance
            elif balance > 0 and not account.get("is_credit_card"):
                # A positive card balance is a prepayment, not a liquid asset.
                assets += balance

        if assets <= 0:
            return Decimal("999.99") if debts > 0 else Decimal("0.0")
        ratio = (debts / assets).quantize(Decimal("0.01"))
        return min(ratio, Decimal("999.99"))  # fits Numeric(5, 2)

    def _detect_risk_factors(
        self,
        cashflow_30d: dict,
        spending_consistency: int,
        savings_rate_pct: Decimal,
    ) -> list[str]:
        """Detect financial risk factors.

        Note: debt is deliberately not evaluated here because this helper does not
        receive the explicitly anchored ``debt_ratio`` computed by the caller.
        """
        risks = []

        # Check for deficit
        if cashflow_30d["net"] < 0:
            risks.append("Running deficit - expenses exceed income")

        # Check for low savings (real percentage of income)
        if savings_rate_pct < 10:
            risks.append("Low savings rate - saving less than 10% of income")

        # Check for inconsistent spending
        if spending_consistency < 50:
            risks.append("Highly inconsistent spending patterns")

        # Check for no income
        if cashflow_30d["income"] == 0:
            risks.append("No income detected in last 30 days")

        # Check for high expenses relative to income
        if cashflow_30d["income"] > 0:
            expense_ratio = cashflow_30d["expenses"] / cashflow_30d["income"]
            if expense_ratio > Decimal("0.9"):
                risks.append("High expense ratio - spending >90% of income")

        return risks

    def _get_score_7d_ago(self, target_date: date) -> int | None:
        """Get health score from 7 days ago."""
        date_7d_ago = target_date - timedelta(days=7)

        with session_scope(self.db_path) as session:
            db_score = (
                session.query(FinancialHealthScoreDB).filter_by(score_date=date_7d_ago).first()
            )

            if db_score:
                return db_score.overall_score

        return None
