"""P5.1: the health score discloses two bases instead of one conflated number.

The frozen number is not the defect. The density gate is working: a day is
scoreable only when its inclusive trailing 30-day window carries at least
``MIN_ACTIVE_DAYS`` distinct posting dates with an income-or-spend row, and a
sparse tail genuinely cannot be scored. The defect is that FOUR surfaces render
that number and only ONE explained it -- the Overview caption and its warning --
while the Planning Readiness "Health Score" tile, the Budgets/Goals "Emergency
Fund" tile and `ledger health` presented a weeks-old score as current. The census
said three until it was rechecked, and the missed surface made the strongest false
claim of the four.

So the score's basis becomes two disclosed facts: the newest day a score is
stored for, and the statement-coverage frontier across the non-dormant accounts
-- the month every one of them has an imported statement for OR LATER, which is
the strongest claim the minimum of their newest covered periods supports. Both
travel on ``PlanningReadinessService.summary()["health_basis"]`` so every
consumer carries them.

CLOCK DISCIPLINE. Every fixture here is synthetic and anchored to constants in
the past. The statement-gap clock is the shared ``gap_evaluation_date`` seam; the scoring
anchor needs no patch, because ``default_anchor_date`` is
``min(today - 1, latest posting date)`` and every fixture's latest posting date
is in the past.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from account_fixtures import seed_account

from ledger.analysis.health_scorer import (
    FRONTIER_ALL_DORMANT,
    FRONTIER_BASES,
    FRONTIER_COVERED,
    MIN_ACTIVE_DAYS,
    SCORING_WINDOW_DAYS,
    FinancialHealthScorer,
    StatementFrontier,
    statement_frontier,
)
from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.planning.readiness import (
    SCORE_AT_NEWEST_IMPORTED_DAY,
    SCORE_BASES,
    PlanningReadinessService,
    build_health_basis,
)
from ledger.processing import ProcessingResult
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter

# The statement-gap evaluation date every fixture is judged against. Past, fixed,
# and after the fifth of the month so `DELIVERY_GRACE_DAYS` has elapsed for the
# month that just closed.
TODAY = date(2025, 10, 8)
INSTITUTION = "TestBank"
CHEQUING = "1234"
CARD = "5678"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cadence-basis.db"
    init_database(path)
    return path


@pytest.fixture(autouse=True)
def frozen_gap_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one seam every gap classifier reads (P2)."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)


def _transaction(
    posting_date: date,
    *,
    key: str,
    amount_signed: Decimal,
    cashflow_type: str,
    account: str,
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    currency: str = "CAD",
) -> Transaction:
    weekday = posting_date.weekday()
    return Transaction(
        txn_id=f"{key}_{posting_date.isoformat()}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution=INSTITUTION,
        account=account,
        account_type="checking" if account == CHEQUING else "credit_card",
        flow="income" if cashflow_type == "income" else "expense",
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency=currency,
        amount_raw=abs(amount_signed),
        amount_signed=amount_signed,
        description=f"{key} row",
        merchant="Employer" if cashflow_type == "income" else "Store",
        category="Income" if cashflow_type == "income" else "Shopping",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=exclude_from_spending or cashflow_type == "income",
        merchant_raw="Employer" if cashflow_type == "income" else "Store",
        category_raw="Income" if cashflow_type == "income" else "Shopping",
        source_file="test.csv",
        source_path="/test",
        source_period=posting_date.strftime("%Y-%m"),
        source_row=abs(hash(key)) % 10_000,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint=f"fp_{key}_{posting_date.isoformat()}",
        day_of_week=weekday,
        day_of_month=posting_date.day,
        week_of_year=posting_date.isocalendar()[1],
        is_weekend=weekday >= 5,
        is_month_start=posting_date.day <= 3,
        is_month_end=posting_date.day >= 28,
    )


def _active_day_rows(days: list[date]) -> list[Transaction]:
    """One income row and one spend row per named day: each day is an active day."""
    rows: list[Transaction] = []
    for day in days:
        rows.append(
            _transaction(
                day,
                key="income",
                amount_signed=Decimal("200.00"),
                cashflow_type="income",
                account=CHEQUING,
            )
        )
        rows.append(
            _transaction(
                day,
                key="spend",
                amount_signed=Decimal("-50.00"),
                cashflow_type="expense",
                account=CARD,
            )
        )
    return rows


def _write(db_path: Path, rows: list[Transaction]) -> None:
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=rows,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _insert_coverage(db_path: Path, slug: str, periods: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executemany(
            "INSERT OR REPLACE INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, ?, ?, 'synthetic_deposit', 4, 1, '', '2025-10-01 00:00:00')",
            [(slug, period, INSTITUTION) for period in periods],
        )


def _insert_lifecycle(db_path: Path, slug: str, *, status: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_lifecycle (account_id, status,"
            " status_confirmed_on, closed_on, note, created_at, updated_at)"
            " VALUES (?, ?, '2025-09-30', '2025-09-30', '', '2025-10-01 00:00:00',"
            " '2025-10-01 00:00:00')",
            (account_identity(INSTITUTION, slug), status),
        )


def _monthly(first: str, last: str) -> tuple[str, ...]:
    start_year, start_month = (int(part) for part in first.split("-"))
    end_year, end_month = (int(part) for part in last.split("-"))
    start = start_year * 12 + start_month - 1
    end = end_year * 12 + end_month - 1
    return tuple(f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(start, end + 1))


# --- fixture shapes ---------------------------------------------------------
#
# SPARSE: dense daily activity through 2025-08-20, then one active day a week.
# The window ending at the newest posting date (2025-09-24) holds 5 active days,
# far under the gate, so nothing after 2025-08-31 can be scored -- while the
# statements are imported through 2025-09. This fictional shape shows that the density
# collapse starts INSIDE the covered period, so the two bases disagree by a month
# and neither one is wrong.
DENSE_START = date(2025, 6, 1)
SPARSE_DENSE_END = date(2025, 8, 20)
SPARSE_TAIL = [date(2025, 8, 27), date(2025, 9, 3), date(2025, 9, 10), date(2025, 9, 17)]
SPARSE_LATEST = date(2025, 9, 24)
DENSE_LATEST = date(2025, 9, 24)


def _seed_sparse_tail(db_path: Path) -> None:
    _write(
        db_path,
        _active_day_rows(
            [*_dates(DENSE_START, SPARSE_DENSE_END), *SPARSE_TAIL, SPARSE_LATEST],
        ),
    )
    for slug in (CHEQUING, CARD):
        seed_account(db_path, INSTITUTION, slug, account_type="checking")
        _insert_coverage(db_path, slug, _monthly("2025-04", "2025-09"))


def _seed_dense(db_path: Path) -> None:
    _write(db_path, _active_day_rows(_dates(DENSE_START, DENSE_LATEST)))
    for slug in (CHEQUING, CARD):
        seed_account(db_path, INSTITUTION, slug, account_type="checking")
        _insert_coverage(db_path, slug, _monthly("2025-04", "2025-09"))


def _refresh_scores(db_path: Path) -> int:
    """Score every day the production refresh would, newest-anchored."""
    return FinancialHealthScorer(db_path).compute_recent_scores(days=30)


def _basis(db_path: Path) -> dict:
    summary = PlanningReadinessService(db_path).summary(date(2025, 9, 1), date(2025, 9, 30))
    return summary["health_basis"]


# --- 1. the coverage basis --------------------------------------------------


def test_the_coverage_basis_is_the_newest_month_every_non_dormant_account_covers(
    db_path: Path,
) -> None:
    _seed_sparse_tail(db_path)

    assert _basis(db_path)["covered_through"] == "2025-09"


def test_a_dormant_account_does_not_drag_the_coverage_basis_backwards(db_path: Path) -> None:
    """Two ways to be dormant, and neither may make the frontier lie.

    An account silent past a multiple of its own cadence, and one the operator
    recorded closed while it was still current. Both are excluded; a frontier
    that took the minimum over EVERY covered account would report 2024-06 and
    call the ledger a year behind.
    """
    _seed_sparse_tail(db_path)
    _insert_coverage(db_path, "long-silent", _monthly("2024-01", "2024-06"))
    _insert_coverage(db_path, "operator-closed", _monthly("2025-04", "2025-09"))
    _insert_lifecycle(db_path, "operator-closed", status="closed")

    basis = _basis(db_path)

    assert basis["covered_through"] == "2025-09"
    assert basis["dormant_accounts"] == 2
    assert basis["non_dormant_accounts"] == 2


def test_the_coverage_basis_never_consults_the_stored_active_flag(db_path: Path) -> None:
    """D8: `accounts.is_active` decays from the very silence being measured.

    This account's own record says inactive and its statements are one month
    behind the rest. Reading the flag would drop it and report a frontier of
    2025-09 that no statement supports; the cadence rule keeps it, and the
    frontier is the month every non-dormant account really covers.
    """
    _seed_sparse_tail(db_path)
    seed_account(db_path, INSTITUTION, "quiet-but-open", account_type="checking")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE accounts SET is_active = 0 WHERE account_id = ?",
            (account_identity(INSTITUTION, "quiet-but-open"),),
        )
    _insert_coverage(db_path, "quiet-but-open", _monthly("2025-04", "2025-08"))

    basis = _basis(db_path)

    assert basis["covered_through"] == "2025-08"
    assert basis["non_dormant_accounts"] == 3


def test_a_ledger_with_no_statement_coverage_says_so_rather_than_naming_a_month(
    db_path: Path,
) -> None:
    """An initialized ledger with nothing imported names no month and no score.

    Reachable only with no transactions at all: the coverage backfill migration
    seeds `statement_coverage` from every imported row's `source_period`, so a
    ledger with transactions always has coverage to assess.
    """
    basis = _basis(db_path)

    assert basis["covered_through"] is None
    assert basis["covered_through_basis"] == "no_statement_coverage_recorded"
    assert basis["scored_through"] is None
    assert basis["anchor_date"] is None
    assert basis["active_days"] is None
    assert basis["is_stale"] is False
    assert basis["statement"] == (
        "No health score is stored; no imported statement is recorded for any account."
    )


def test_an_all_dormant_ledger_names_both_routes_to_dormancy(db_path: Path) -> None:
    """`classify_gap` reaches `dormant` two ways, and only one is about cadence.

    Both accounts here are perfectly current under their own monthly cadence and
    are dormant solely because the operator recorded them closed --
    `SIGNAL_OPERATOR_CLOSURE`, which outranks every derivation. A sentence
    crediting the cadence for that asserts a derivation that was never applied.
    """
    for slug in (CHEQUING, CARD):
        seed_account(db_path, INSTITUTION, slug, account_type="checking")
        _insert_coverage(db_path, slug, _monthly("2025-04", "2025-09"))
        _insert_lifecycle(db_path, slug, status="closed")

    frontier = statement_frontier(db_path)
    basis = _basis(db_path)

    assert frontier.basis == FRONTIER_ALL_DORMANT
    assert frontier.period is None
    assert frontier.dormant_accounts == 2
    assert frontier.non_dormant_accounts == 0
    assert basis["covered_through"] is None
    assert basis["statement"].endswith(
        "every account with statements is dormant under its own cadence or recorded "
        "closed by the operator."
    )


def test_every_published_frontier_basis_is_reachable(tmp_path: Path) -> None:
    """Derived from the exported tuple, never from a hand-written list of cases.

    Two of the four bases had no test at all, and one of them was published as a
    possible answer while no shipped caller could produce it. Enumerating from
    `FRONTIER_BASES` means a basis added without a reachable branch -- or left
    published after its branch is gone -- fails here rather than shipping as a
    sentence nobody can trigger.
    """
    observed: dict[str, str] = {}

    empty = tmp_path / "empty.db"
    init_database(empty)
    observed[statement_frontier(empty).basis] = "no coverage rows at all"

    covered = tmp_path / "covered.db"
    init_database(covered)
    seed_account(covered, INSTITUTION, CHEQUING, account_type="checking")
    _insert_coverage(covered, CHEQUING, _monthly("2025-04", "2025-09"))
    observed[statement_frontier(covered).basis] = "one account, current under its cadence"

    closed = tmp_path / "closed.db"
    init_database(closed)
    seed_account(closed, INSTITUTION, CHEQUING, account_type="checking")
    _insert_coverage(closed, CHEQUING, _monthly("2025-04", "2025-09"))
    _insert_lifecycle(closed, CHEQUING, status="closed")
    observed[statement_frontier(closed).basis] = "the only covered account is closed"

    # A database from before `account_lifecycle` shipped. `statement_frontier` is
    # reached by `ledger health`, which deliberately does not migrate, so this is
    # a live path and not a hypothetical one.
    legacy = tmp_path / "legacy.db"
    init_database(legacy)
    seed_account(legacy, INSTITUTION, CHEQUING, account_type="checking")
    _insert_coverage(legacy, CHEQUING, _monthly("2025-04", "2025-09"))
    with closing(sqlite3.connect(legacy)) as conn, conn:
        conn.execute("DROP TABLE account_lifecycle")
    observed[statement_frontier(legacy).basis] = "no account_lifecycle table"

    assert set(observed) == set(FRONTIER_BASES), sorted(set(FRONTIER_BASES) - set(observed))


# --- 2. the two bases, disclosed separately ---------------------------------


def test_a_sparse_tail_reports_the_newest_stored_score_and_discloses_both_bases(
    db_path: Path,
) -> None:
    """A sparse tail reports the newest stored score and discloses both bases."""
    _seed_sparse_tail(db_path)
    _refresh_scores(db_path)

    basis = _basis(db_path)

    assert basis["scored_through"] == date(2025, 8, 31)
    assert basis["anchor_date"] == SPARSE_LATEST
    assert basis["covered_through"] == "2025-09"
    assert basis["is_stale"] is True
    assert basis["days_behind"] == 24
    # The gate input at the anchor, and both sides of the comparison.
    assert basis["active_days"] == 5
    assert basis["min_active_days"] == MIN_ACTIVE_DAYS
    assert basis["window_days"] == SCORING_WINDOW_DAYS
    # The sentence every surface renders, pinned in full.
    assert basis["statement"] == (
        "Health score as of 2025-08-31, 24 day(s) behind the newest imported day "
        "(2025-09-24); 2 of 2 accounts with imported statement coverage are not dormant, "
        "and every one of them has a statement for 2025-09 or later."
    )


def test_the_published_coverage_claim_is_true_of_every_non_dormant_account(
    db_path: Path,
) -> None:
    """ "Or later", never "covered through" -- and the difference is reachable.

    This account's cadence is two months, so it has no statement for 2025-08 at
    all while the frontier sits at 2025-09. "Covered through 2025-09" would be
    false for it; "has an imported statement for 2025-09 or later" is true for
    every non-dormant account by construction, and is checked here against the
    stored rows rather than against the sentence that makes the claim.
    """
    _seed_sparse_tail(db_path)
    seed_account(db_path, INSTITUTION, "two-month-cycle", account_type="checking")
    _insert_coverage(db_path, "two-month-cycle", ("2025-03", "2025-05", "2025-07", "2025-09"))

    frontier = statement_frontier(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        newest = dict(
            conn.execute(
                "SELECT account_slug, MAX(period) FROM statement_coverage GROUP BY account_slug"
            ).fetchall()
        )
        intervening = conn.execute(
            "SELECT COUNT(*) FROM statement_coverage"
            " WHERE account_slug = 'two-month-cycle' AND period = '2025-08'"
        ).fetchone()[0]

    assert frontier.period == "2025-09"
    assert frontier.non_dormant_accounts == 3
    assert newest, "no coverage rows to check the claim against"
    assert all(latest >= frontier.period for latest in newest.values()), newest
    # The month the stronger wording would have claimed and this account lacks.
    assert intervening == 0


def test_a_dense_ledger_has_the_two_bases_in_agreement(db_path: Path) -> None:
    _seed_dense(db_path)
    _refresh_scores(db_path)

    basis = _basis(db_path)

    assert basis["scored_through"] == DENSE_LATEST
    assert basis["anchor_date"] == DENSE_LATEST
    assert basis["covered_through"] == "2025-09"
    assert basis["is_stale"] is False
    assert basis["days_behind"] == 0


def test_a_transaction_posted_today_is_never_called_the_day_the_score_reached(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scorer's anchor and the newest imported day are TWO dates, always.

    ``default_anchor_date`` is ``min(today - 1, latest posting date)`` and
    ``compute_recent_scores`` prunes anything past it, so a ledger holding a
    transaction posted TODAY can never have a score for that day -- not after any
    number of refreshes. Measuring staleness against the anchor made the same
    page warn "transactions run to {today}" and caption "current with the newest
    imported day" about the same number, and the caption was the false one.

    This is the one fixture anchored to the real clock, because that is the
    condition: the defect is unreachable unless the newest posting date is today.
    """
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", date.today)
    today = date.today()
    _write(db_path, _active_day_rows(_dates(today - timedelta(days=44), today)))
    for slug in (CHEQUING, CARD):
        seed_account(db_path, INSTITUTION, slug, account_type="checking")
        _insert_coverage(db_path, slug, (f"{today.year:04d}-{today.month:02d}",))
    _refresh_scores(db_path)

    basis = _basis(db_path)

    assert basis["scored_through"] == today - timedelta(days=1)
    assert basis["latest_transaction"] == today
    assert basis["anchor_date"] == today - timedelta(days=1)
    # The two named dates are both stated, and neither is called the other.
    assert str(today) in basis["statement"]
    assert "current with the newest imported day" not in basis["statement"]
    assert "current with the data" not in basis["score_detail"]

    emitted = _render_overview_health(
        monkeypatch,
        health={"overall_score": 61, "score_date": basis["scored_through"]},
        data_through=today,
        basis=basis,
    )

    # The banner still fires -- the score is genuinely not for the newest day --
    # but the advice it gives cannot be the refresh this gap is immune to.
    assert len(emitted["warning"]) == 1
    assert "Run `ledger analysis refresh`" not in emitted["warning"][0]


def test_a_score_dated_past_the_newest_imported_day_never_renders_a_negative_day_count(
    db_path: Path,
) -> None:
    """The repair/rebuild window, which is exactly when the dashboard gets opened.

    ``repair statements``, ``rebuild-db``, or a narrower re-import can remove
    transaction rows while ``financial_health_scores`` still holds the day the
    previous refresh scored. Until the next ``analysis refresh`` runs its own
    prune, the stored score describes rows the database no longer has -- so the
    one thing no surface may do is present it as current, and the one number no
    surface may print is a negative day count.
    """
    _seed_dense(db_path)
    _refresh_scores(db_path)
    survivor = date(2025, 9, 4)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM transactions WHERE posting_date > ?", (survivor.isoformat(),))

    basis = _basis(db_path)

    assert basis["scored_through"] == DENSE_LATEST
    assert basis["latest_transaction"] == survivor
    assert basis["anchor_date"] == survivor
    assert basis["days_behind"] is None or basis["days_behind"] >= 0
    assert basis["is_stale"] is True
    for rendered in (basis["statement"], basis["score_detail"]):
        assert re.search(r"-\d+ day", rendered) is None, rendered
        assert "current with" not in rendered, rendered
    # And it says which way round the two dates sit, not just that they differ.
    assert str(survivor) in basis["statement"]


def test_every_published_score_basis_is_reachable_and_renders_honestly() -> None:
    """Enumerated from `SCORE_BASES`, so no branch ships without a case.

    Two of the four `FRONTIER_BASES` values reached review with no test at all;
    this vocabulary is checked from its own exported tuple the day it lands. Each
    case is also held to the two properties the whole basis exists for: no
    negative day count anywhere in a published sentence, and "current with" said
    only where the scored day IS the newest imported day.
    """
    frontier = StatementFrontier(
        period="2025-09",
        basis=FRONTIER_COVERED,
        non_dormant_accounts=2,
        dormant_accounts=0,
        evaluated_on=TODAY,
    )
    scored = date(2025, 8, 31)
    cases = {
        "no score at all": (None, None, None),
        "score, no transactions left": (scored, None, None),
        "score past the data": (scored, date(2025, 8, 20), date(2025, 8, 20)),
        "score at the newest day": (scored, scored, scored),
        "score at the scorer's anchor": (scored, date(2025, 9, 1), scored),
        "score behind the anchor": (scored, date(2025, 9, 24), date(2025, 9, 24)),
    }

    observed: dict[str, str] = {}
    for label, (through, latest, anchor) in cases.items():
        basis = build_health_basis(
            scored_through=through,
            computed_at=None if through is None else datetime(2025, 9, 25, 3, 0),
            latest_transaction=latest,
            anchor_date=anchor,
            active_days=5,
            frontier=frontier,
        )
        observed[basis["score_basis"]] = label
        assert basis["days_behind"] is None or basis["days_behind"] >= 0, label
        for rendered in (basis["statement"], basis["score_detail"]):
            assert re.search(r"-\d+ day", rendered) is None, (label, rendered)
            if basis["score_basis"] != SCORE_AT_NEWEST_IMPORTED_DAY:
                assert "current with" not in rendered, (label, rendered)

    assert set(observed) == set(SCORE_BASES), sorted(set(SCORE_BASES) - set(observed))


def test_the_disclosed_scored_day_is_the_stored_selection_rule_not_a_refresh_time_claim(
    db_path: Path,
) -> None:
    """A read surface cannot recover what the refresh was handed.

    `scored_through` is defined as the newest STORED score's date and is
    published with the `computed_at` of that row. It is never published as "the
    newest scoreable day", which a narrower refresh would falsify permanently
    while the surface kept asserting it.
    """
    _seed_sparse_tail(db_path)
    # A deliberately narrow refresh: three days, none of them scoreable.
    FinancialHealthScorer(db_path).compute_recent_scores(days=3)

    basis = _basis(db_path)

    assert basis["scored_through"] is None
    assert basis["computed_at"] is None
    assert basis["covered_through"] == "2025-09"
    assert basis["covered_through_basis"] == "every_non_dormant_account_has_a_statement"


# --- 3. the gate input is the finance-policy predicate (spec 2.7) -----------


def test_the_disclosed_active_day_count_is_the_scorers_gate_input_not_a_posting_date_count(
    db_path: Path,
) -> None:
    """A raw COUNT(DISTINCT posting_date) reports a higher number than the scorer.

    Three rows that are stored transactions on days nothing else posted, and that
    `is_income_row`/`is_spend_row` both reject: a USD row, an
    `include_in_cashflow = 0` row, and an excluded-from-spending expense. A
    banner counting posting dates would say 8 where the gate saw 5, and read as
    if it were lying about the refusal it is explaining.
    """
    _seed_sparse_tail(db_path)
    _write(
        db_path,
        [
            _transaction(
                date(2025, 9, 20),
                key="usd",
                amount_signed=Decimal("-40.00"),
                cashflow_type="expense",
                account=CARD,
                currency="USD",
            ),
            _transaction(
                date(2025, 9, 21),
                key="outofcashflow",
                amount_signed=Decimal("-40.00"),
                cashflow_type="expense",
                account=CARD,
                include_in_cashflow=False,
            ),
            _transaction(
                date(2025, 9, 22),
                key="notspending",
                amount_signed=Decimal("-40.00"),
                cashflow_type="expense",
                account=CARD,
                exclude_from_spending=True,
            ),
        ],
    )

    with closing(sqlite3.connect(db_path)) as conn:
        posting_dates = conn.execute(
            "SELECT COUNT(DISTINCT posting_date) FROM transactions"
            " WHERE posting_date BETWEEN ? AND ?",
            (
                (SPARSE_LATEST - timedelta(days=SCORING_WINDOW_DAYS - 1)).isoformat(),
                SPARSE_LATEST.isoformat(),
            ),
        ).fetchone()[0]
    basis = _basis(db_path)

    assert posting_dates == 8
    assert basis["active_days"] == 5
    assert basis["active_days"] == FinancialHealthScorer(db_path).count_active_days(SPARSE_LATEST)


# --- 4. every surface carries the fact --------------------------------------
#
# THE BANNER IS DRIVEN THROUGH ITS RENDER FUNCTION, NEVER THROUGH THE STRING
# BUILDER. Calling `_health_staleness_notice` with hand-supplied keywords proves
# the builder can produce a sentence and nothing about whether the page passes it
# the basis: both wiring arguments could be deleted from `_render_health_score`
# with the whole suite green. Every case below therefore invokes the shipped
# render function, and every one gives `anchor_date` a DIFFERENT value from
# `data_through`, so a fallback to the wrong date cannot pass.


class _NullExpander:
    def __enter__(self) -> _NullExpander:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _render_overview_health(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health: dict,
    data_through: date,
    basis: dict,
) -> dict[str, list[str]]:
    """Invoke `overview._render_health_score` and collect everything it emitted."""
    from ledger.ui.pages import overview

    emitted: dict[str, list[str]] = {"caption": [], "warning": [], "markdown": []}
    monkeypatch.setattr(
        overview,
        "st",
        SimpleNamespace(
            caption=lambda text, **_k: emitted["caption"].append(str(text)),
            warning=lambda text, **_k: emitted["warning"].append(str(text)),
            markdown=lambda text, **_k: emitted["markdown"].append(str(text)),
            expander=lambda *_a, **_k: _NullExpander(),
            dataframe=lambda *_a, **_k: None,
        ),
    )

    overview._render_health_score(health, data_through=data_through, basis=basis)
    return emitted


def _sparse_basis(*, anchor: date, latest: date) -> dict:
    return build_health_basis(
        scored_through=date(2025, 8, 31),
        computed_at=datetime(2025, 9, 25, 3, 0),
        latest_transaction=latest,
        anchor_date=anchor,
        active_days=5,
        frontier=StatementFrontier(
            period="2025-09",
            basis=FRONTIER_COVERED,
            non_dormant_accounts=2,
            dormant_accounts=0,
            evaluated_on=TODAY,
        ),
    )


def test_the_rendered_banner_names_both_bases_and_attributes_the_count_to_its_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wording bug, checked on the wire the page actually renders.

    `count_active_days(D)` is the count for the 30-day window ENDING D, not a
    property of that one day; a day-level label would attribute the window count incorrectly. The
    anchor here is 2025-09-23 while the data runs to 2025-09-24, so a banner that
    fell back to the newest transaction date -- which is what dropping the
    `active_days_through` argument does -- names the wrong window and fails.
    """
    emitted = _render_overview_health(
        monkeypatch,
        health={"overall_score": 61, "score_date": date(2025, 8, 31)},
        data_through=date(2025, 9, 24),
        basis=_sparse_basis(anchor=date(2025, 9, 23), latest=date(2025, 9, 24)),
    )

    assert len(emitted["warning"]) == 1
    notice = emitted["warning"][0]
    assert "2025-08-31" in notice and "2025-09-24" in notice
    # The coverage basis, which only the `coverage_clause` wiring can supply, in
    # the SAME words the two tile captions render it in.
    assert "2 of 2 accounts with imported statement coverage are not dormant" in notice
    assert "has a statement for 2025-09 or later" in notice
    # The window the count belongs to, at the ANCHOR and not at the newest day.
    assert f"{SCORING_WINDOW_DAYS}-day window ending 2025-09-23" in notice
    assert "holds only 5" in notice
    assert str(MIN_ACTIVE_DAYS) in notice
    # The count is the finance-policy predicate, named as such.
    assert "income or spending" in notice
    # And the futile advice stays gone.
    assert "Run `ledger analysis refresh`" not in notice


def test_the_rendered_banner_does_not_promise_that_the_next_import_unfreezes_the_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered banner does not promise that the next import unfreezes the score."""
    emitted = _render_overview_health(
        monkeypatch,
        health={"overall_score": 61, "score_date": date(2025, 8, 31)},
        data_through=date(2025, 9, 24),
        basis=_sparse_basis(anchor=date(2025, 9, 23), latest=date(2025, 9, 24)),
    )

    notice = emitted["warning"][0]
    assert "Import newer statements to close the gap." not in notice
    assert "only" in notice.lower()


def test_the_planning_readiness_tile_carries_the_staleness_it_used_to_hide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.ui.pages import overview

    tiles: list[dict] = []
    captions: list[str] = []
    monkeypatch.setattr(overview, "status_grid", tiles.extend)
    monkeypatch.setattr(
        overview,
        "st",
        SimpleNamespace(
            subheader=lambda *a, **k: None,
            caption=lambda text, **k: captions.append(text),
            dataframe=lambda *a, **k: None,
        ),
    )

    overview._render_planning_readiness(_stale_summary())

    by_label = {item["label"]: item for item in tiles}
    assert by_label["Health Score"]["value"] == "61"
    assert "2025-08-31" in str(by_label["Health Score"]["detail"])
    assert "6.0 months emergency" in str(by_label["Health Score"]["detail"])
    assert any("2025-09" in text and "2025-08-31" in text for text in captions)


def test_the_emergency_fund_tile_carries_the_same_staleness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.ui.pages import budgets_goals

    tiles: list[dict] = []
    captions: list[str] = []
    monkeypatch.setattr(budgets_goals, "status_grid", tiles.extend)
    monkeypatch.setattr(
        budgets_goals,
        "st",
        SimpleNamespace(
            caption=lambda text, **k: captions.append(text),
            info=lambda *a, **k: None,
            dataframe=lambda *a, **k: None,
        ),
    )

    budgets_goals._render_planning_readiness(_stale_summary())

    by_label = {item["label"]: item for item in tiles}
    assert by_label["Emergency Fund"]["value"] == "6.0 months"
    assert "2025-08-31" in str(by_label["Emergency Fund"]["detail"])
    assert any("2025-09" in text and "2025-08-31" in text for text in captions)


def _stale_summary() -> dict:
    """A readiness summary whose score is three weeks behind the data.

    The basis block is built by the SHIPPED builder rather than hand-written, so
    a tile test cannot pass against a block shape the service no longer emits.
    """
    return {
        "budgets": {"active": 1, "categories": 1, "over_budget": 0},
        "goals": {"active": 0, "target": Decimal("0.00"), "current": None, "remaining": None},
        "recurring_monthly": Decimal("0.00"),
        "health": {
            "overall_score": 61,
            "score_date": date(2025, 8, 31),
            "emergency_fund_months": Decimal("6.00"),
        },
        "health_basis": build_health_basis(
            scored_through=date(2025, 8, 31),
            computed_at=datetime(2025, 9, 25, 3, 0),
            latest_transaction=SPARSE_LATEST,
            anchor_date=SPARSE_LATEST,
            active_days=5,
            frontier=StatementFrontier(
                period="2025-09",
                basis=FRONTIER_COVERED,
                non_dormant_accounts=2,
                dormant_accounts=0,
                evaluated_on=TODAY,
            ),
        ),
        "data_freshness": {
            "latest_transaction": SPARSE_LATEST,
            "pending_reviews": 0,
            "quality_warnings": 0,
        },
        "next_actions": [],
    }


# --- 5. the CLI is the fourth surface ---------------------------------------


def test_the_cli_renders_the_same_basis_the_dashboard_does(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ledger health` was a fourth surface printing the frozen number bare.

    Spec §10 counted three surfaces; the census was short by one, and the missed
    one made the STRONGER false claim: its header read "Last N Days" over rows
    weeks old, where the tiles had merely omitted the date. An operator who runs
    the CLI instead of opening Streamlit got exactly the unexplained frozen
    number this phase exists to eliminate.

    Asserted against the shipped builder's own output rather than against a
    retyped sentence, so the CLI cannot drift from the dashboard.
    """
    from typer.testing import CliRunner

    from ledger.cli import app
    from ledger.planning.readiness import health_basis

    _seed_sparse_tail(db_path)
    _refresh_scores(db_path)
    expected = health_basis(db_path)

    result = CliRunner().invoke(app, ["health", "--db", str(db_path), "--days", "3"])
    printed = " ".join(result.stdout.split())

    assert result.exit_code == 0, result.stdout
    assert expected["statement"] in printed
    # The header names the days it actually shows, not a window it does not have.
    assert "Last 3 Days" not in printed
    assert "2025-08-31" in printed


def test_the_cli_basis_does_not_migrate_the_database_it_reads(tmp_path: Path) -> None:
    """A read command must not apply pending migrations without a backup.

    `PlanningReadinessService` constructs `BudgetService`, whose `__init__` calls
    `ensure_schema` unconditionally -- fine for a dashboard that is already
    writing nothing else, wrong for a CLI read. `health_basis` therefore takes
    the non-migrating path, which is also what makes `FRONTIER_NO_LIFECYCLE`
    reachable rather than dead.
    """
    from ledger.planning.readiness import health_basis

    legacy = tmp_path / "legacy.db"
    init_database(legacy)
    with closing(sqlite3.connect(legacy)) as conn, conn:
        conn.execute("DROP TABLE account_lifecycle")

    basis = health_basis(legacy)

    with closing(sqlite3.connect(legacy)) as conn:
        present = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'account_lifecycle'"
        ).fetchone()[0]

    assert present == 0, "the read path migrated the database"
    assert basis["covered_through_basis"] == "account_lifecycle_unavailable"
    assert "coverage is not assessed" in basis["statement"]


def test_the_persisted_report_snapshot_carries_the_basis_beside_the_score(
    db_path: Path,
) -> None:
    """A stored payload outlives every surface that could explain it.

    The snapshot embeds the newest health score and is written to disk, so a
    score persisted with no basis is the unexplained frozen number again --
    months later, and with no way back to the dates that would place it. Beside
    the score and never inside it: `health` stays the stored row so a consumer
    testing it for truthiness keeps the same answer.
    """
    from ledger.reporting.service import ReportSnapshotService

    _seed_sparse_tail(db_path)
    _refresh_scores(db_path)

    payload = ReportSnapshotService(db_path).snapshot_for_period(
        date(2025, 9, 1), date(2025, 9, 30)
    )["payload"]

    assert payload["health"], "no score in the payload; the assertion below is vacuous"
    assert payload["health"]["score_date"] == "2025-08-31"
    assert payload["health_basis"]["statement"] == _basis(db_path)["statement"]
    # Serialized, because this payload is stored as JSON.
    assert payload["health_basis"]["scored_through"] == "2025-08-31"


def test_the_no_score_next_action_does_not_advise_a_refresh_sparsity_defeats(
    db_path: Path,
) -> None:
    """The same futile instruction the banner stopped giving, one panel down.

    "Refresh reports to compute the latest health score" fires whenever NO score
    is stored -- and the commonest reason none is stored is that no day clears
    the density gate, which a refresh recomputes and still cannot move. The user
    follows the instruction, watches nothing change, and learns nothing. The
    basis already knows which case this is, so the action names it.
    """
    _seed_sparse_tail(db_path)
    # A refresh narrow enough to store nothing: the state this action fires in.
    FinancialHealthScorer(db_path).compute_recent_scores(days=3)

    summary = PlanningReadinessService(db_path).summary(date(2025, 9, 1), date(2025, 9, 30))
    actions = " ".join(summary["next_actions"])

    assert not summary["health"], "a score is stored; this action would not fire"
    assert summary["health_basis"]["active_days"] < MIN_ACTIVE_DAYS
    assert "Refresh reports" not in actions
    assert str(MIN_ACTIVE_DAYS) in actions and "import" in actions.lower()


def test_the_no_score_next_action_still_advises_a_refresh_when_the_data_is_dense(
    db_path: Path,
) -> None:
    """Dense data with no stored score IS un-run analytics: keep the advice."""
    _seed_dense(db_path)

    summary = PlanningReadinessService(db_path).summary(date(2025, 9, 1), date(2025, 9, 30))
    actions = " ".join(summary["next_actions"])

    assert not summary["health"]
    assert summary["health_basis"]["active_days"] >= MIN_ACTIVE_DAYS
    assert "Refresh reports" in actions


def test_every_module_that_reads_a_stored_score_also_reaches_for_its_basis() -> None:
    """The census, DERIVED, because hand-counting it is what produced the defect.

    Spec §10 said three surfaces. There were four. A hand-written list of
    surfaces goes stale the moment someone adds a fifth, and the fifth is exactly
    the one nobody remembers to explain -- so the readers are found by searching
    the source for the two accessors that hand out a stored score, and every
    reader that is not the producer of those scores has to name `health_basis`.
    """
    accessors = ("get_latest_health_score(", "get_health_scores(")
    # The two modules that PRODUCE the score rather than render it: the query
    # layer that reads the table, and the scorer that writes it.
    producers = {
        Path("src/ledger/query/reports.py"),
        Path("src/ledger/analysis/health_scorer.py"),
    }
    root = Path(__file__).resolve().parents[1]

    readers = {
        path.relative_to(root)
        for path in (root / "src/ledger").rglob("*.py")
        if any(accessor in path.read_text(encoding="utf-8") for accessor in accessors)
    }
    rendering = sorted(readers - producers)

    assert len(rendering) >= 3, sorted(readers)
    for path in rendering:
        assert "health_basis" in (root / path).read_text(encoding="utf-8"), path


# --- 6. regression re-check (not a fix) -------------------------------------


def test_emergency_fund_and_debt_ratio_stay_non_null_after_an_analysis_refresh(
    db_path: Path,
) -> None:
    """Re-check, not a change: both are NULL unless every active account carries an
    absolute (statement-backed) anchor, and that gate has regressed before."""
    from ledger.query import LedgerQueryService
    from ledger.storage.database import AccountBalanceDB, session_scope

    _seed_dense(db_path)
    chequing = account_identity(INSTITUTION, CHEQUING)
    card = account_identity(INSTITUTION, CARD)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        for account_id, balance in ((chequing, Decimal("9000.00")), (card, Decimal("-1500.00"))):
            for day in _dates(DENSE_START, DENSE_LATEST):
                session.merge(
                    AccountBalanceDB(
                        account_id=account_id,
                        balance_date=day,
                        balance=balance,
                        balance_basis="absolute",
                        transaction_count=1,
                        balance_7d_avg=balance,
                        balance_30d_avg=balance,
                        balance_7d_min=balance,
                        balance_7d_max=balance,
                        credit_utilization=None,
                        computed_at=now,
                    )
                )

    _refresh_scores(db_path)

    stored = LedgerQueryService(db_path).get_health_scores(limit=5)
    assert stored, "the refresh produced no score at all"
    assert all(row["emergency_fund_months"] is not None for row in stored)
    assert all(row["debt_ratio"] is not None for row in stored)
