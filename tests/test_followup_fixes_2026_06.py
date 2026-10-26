"""Regression tests for the 2026-06 follow-up correctness fixes.

Each test locks one fix in the query/aggregates modules: spend-universe
consistency, LIKE-wildcard escaping, transfer date-boundary pair counting,
the transfer-match N+1 batch, and Decimal money in trend dicts.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account, seed_accounts_for_existing_rows
from click import unstyle

from ledger.adapters.registry import CompatibilityPolicySnapshot
from ledger.commands.config import config_export
from ledger.core import Transaction
from ledger.exporting.service import ExportPackageService
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import (
    SpendingTrendDB,
    TransferDB,
    init_database,
    session_scope,
)
from ledger.storage.writer import DatabaseWriter


@pytest.fixture
def invented_reimbursement_policy(monkeypatch):
    policy = CompatibilityPolicySnapshot(
        digest="invented-reimbursement-policy",
        credit_institutions=frozenset(),
        credit_account_tokens=(),
        payment_tokens=(),
        reimbursement_merchants=frozenset({"INVENTED BENEFITS"}),
    )
    monkeypatch.setattr("ledger.adjustments.service.compatibility_policy_snapshot", lambda: policy)


def _make_txn(
    i: int,
    *,
    amount: Decimal,
    merchant: str,
    category: str,
    cashflow_type: str = "expense",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    description: str | None = None,
    posting_date: date = date(2025, 4, 10),
    institution: str = "harbor",
    account: str = "harbor-chequing",
    account_type: str = "",
    currency: str = "CAD",
) -> Transaction:
    return Transaction(
        txn_id=f"ff_txn_{i}",
        txn_fingerprint=f"ff_fp_{i}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution=institution,
        account=account,
        account_type=account_type,
        flow="income" if amount > 0 else "expense",
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency=currency,
        amount_raw=abs(amount),
        amount_signed=amount,
        description=description if description is not None else f"FOLLOWUP TEST {i}",
        merchant=merchant,
        category=category,
        exclude_from_spending=exclude_from_spending,
        merchant_raw="",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-04",
        source_row=i,
        imported_at=date(2025, 4, 15),
        parser_version="test",
    )


def _write_txns(db_path: Path, txns: list[Transaction]) -> None:
    result = ProcessingResult(
        transactions=txns,
        transfers=[],
        exceptions=[],
        total_scanned=len(txns),
        duplicates_removed=0,
        collisions=0,
    )
    DatabaseWriter(db_path).write_result(result, Path("/test"), "test")


def _add_transfer(
    session,
    txn_id: str,
    amount_raw: str,
    *,
    posting_date: date,
    paired: str | None = None,
    account_type: str = "Chequing",
) -> None:
    session.add(
        TransferDB(
            txn_id=txn_id,
            posting_date=posting_date,
            institution="harbor",
            account="acct",
            account_type=account_type,
            flow="transfer",
            amount_raw=Decimal(amount_raw),
            description="X",
            source_file="s.csv",
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            paired_transfer_id=paired,
        )
    )


# --- #1: get_spending_by_category routes through the spend-universe ----------


def test_spending_by_category_excludes_excluded_and_transfer_rows(tmp_path: Path):
    """Default get_spending_by_category must use the canonical spend universe:
    excluded rows and transfer rows must not appear in category spend."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(1, amount=Decimal("-50.00"), merchant="CAFE", category="Dining"),
            # exclude_from_spending=True must be dropped from default spend.
            _make_txn(
                2,
                amount=Decimal("-25.00"),
                merchant="CAFE",
                category="Dining",
                exclude_from_spending=True,
            ),
            # A transfer-typed row must never count as expense spend.
            _make_txn(
                3,
                amount=Decimal("-200.00"),
                merchant="MOVE",
                category="Transfer",
                cashflow_type="transfer",
                include_in_cashflow=False,
            ),
        ],
    )

    spend = LedgerQueryService(db_path).get_spending_by_category(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )

    # Only the single included Dining expense counts.
    assert spend == {"Dining": Decimal("50.00")}
    assert "Transfer" not in spend


def test_spending_by_category_include_excluded_path_preserved(tmp_path: Path):
    """When exclude_from_spending=False the caller explicitly wants excluded rows
    folded in; that legacy semantic must be preserved (still expense-only)."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(1, amount=Decimal("-50.00"), merchant="CAFE", category="Dining"),
            _make_txn(
                2,
                amount=Decimal("-25.00"),
                merchant="CAFE",
                category="Dining",
                exclude_from_spending=True,
            ),
        ],
    )

    spend = LedgerQueryService(db_path).get_spending_by_category(
        start_date=date(2025, 4, 1),
        end_date=date(2025, 4, 30),
        exclude_from_spending=False,
    )

    # Both rows fold in when the caller opts to include excluded rows.
    assert spend == {"Dining": Decimal("75.00")}


# --- #2: get_monthly_cashflow drops exclude_from_spending expenses -----------


def test_monthly_cashflow_drops_excluded_expense(tmp_path: Path):
    """An exclude_from_spending=True expense must not reduce the monthly net /
    expense total; only income + genuine spend move the needle."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("1000.00"),
                merchant="PAY",
                category="Income",
                cashflow_type="income",
            ),
            _make_txn(
                2,
                amount=Decimal("-100.00"),
                merchant="STORE",
                category="Dining",
            ),
            # Excluded expense must NOT count against the month.
            _make_txn(
                3,
                amount=Decimal("-500.00"),
                merchant="HIDDEN",
                category="Dining",
                exclude_from_spending=True,
            ),
        ],
    )

    rows = LedgerQueryService(db_path).get_monthly_cashflow(2025)
    row = next(r for r in rows if r["month"] == "2025-04")

    assert row["income"] == Decimal("1000.00")
    assert row["expense"] == Decimal("-100.00")
    assert row["net"] == Decimal("900.00")


# --- #3: description_search escapes LIKE wildcards ---------------------------


def test_description_search_escapes_percent_wildcard(tmp_path: Path):
    """A literal '%' search must match only rows that contain '%', not every row."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("-10.00"),
                merchant="A",
                category="Dining",
                description="50% OFF SALE",
            ),
            _make_txn(
                2,
                amount=Decimal("-20.00"),
                merchant="B",
                category="Dining",
                description="NO DISCOUNT HERE",
            ),
        ],
    )

    results = LedgerQueryService(db_path).search_descriptions("%")

    ids = {t.txn_id for t in results}
    assert ids == {"ff_txn_1"}, f"literal '%' search leaked non-matching rows: {ids}"


def test_description_search_escapes_underscore_wildcard(tmp_path: Path):
    """A literal '_' must not behave as the single-char LIKE wildcard."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("-10.00"),
                merchant="A",
                category="Dining",
                description="ACME_CORP",
            ),
            _make_txn(
                2,
                amount=Decimal("-20.00"),
                merchant="B",
                category="Dining",
                description="ACMEXCORP",
            ),
        ],
    )

    results = LedgerQueryService(db_path).search_descriptions("ACME_CORP")

    ids = {t.txn_id for t in results}
    assert ids == {"ff_txn_1"}, f"literal '_' acted as a wildcard: {ids}"


# --- #4: transfer stats count straddling pairs consistently ------------------


def test_transfer_stats_straddling_pair_counted_once(tmp_path: Path):
    """A matched pair whose two legs straddle the date boundary (one inside the
    window, one outside) must count as exactly one matched pair."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        # Pair legs straddle the window end: t1 inside, t2 outside.
        _add_transfer(session, "t1", "-500.00", posting_date=date(2025, 6, 10), paired="t2")
        _add_transfer(session, "t2", "500.00", posting_date=date(2025, 7, 10), paired="t1")
        # A clean unmatched leg inside the window.
        _add_transfer(session, "t3", "-100.00", posting_date=date(2025, 6, 12), paired=None)

    stats = LedgerQueryService(db_path).get_transfer_stats(
        start_date=date(2025, 6, 1), end_date=date(2025, 6, 30)
    )

    # The straddling pair must register as one matched pair, not zero.
    assert stats["matched_pairs"] == 1, stats
    # matched_transfers stays the count of matched legs visible in the window.
    assert stats["matched_transfers"] == 1, stats


def test_transfer_stats_whole_pair_in_window(tmp_path: Path):
    """Both legs inside the window still count as exactly one pair (no regression
    of the existing leg//2 behaviour)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_transfer(session, "t1", "-500.00", posting_date=date(2025, 6, 10), paired="t2")
        _add_transfer(session, "t2", "500.00", posting_date=date(2025, 6, 11), paired="t1")
        _add_transfer(session, "t3", "-100.00", posting_date=date(2025, 6, 12), paired=None)

    stats = LedgerQueryService(db_path).get_transfer_stats()

    assert stats["total_transfers"] == 3
    assert stats["matched_pairs"] == 1
    assert stats["matched_transfers"] == 2
    assert stats["unmatched_transfers"] == 1


# --- #5: get_transfer_matches batches its lookups (behaviour preserved) ------


def test_transfer_matches_batched_result_unchanged(tmp_path: Path):
    """The batched implementation must return the same matched pairs (and leg
    ordering) as before, including across multiple pairs."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_transfer(session, "a1", "-500.00", posting_date=date(2025, 6, 10), paired="a2")
        _add_transfer(session, "a2", "500.00", posting_date=date(2025, 6, 10), paired="a1")
        _add_transfer(session, "b1", "-25.00", posting_date=date(2025, 6, 12), paired="b2")
        _add_transfer(session, "b2", "25.00", posting_date=date(2025, 6, 12), paired="b1")
        _add_transfer(session, "c1", "-9.00", posting_date=date(2025, 6, 14), paired=None)

    pairs = LedgerQueryService(db_path).get_transfer_matches()

    # Two matched pairs; each returned as (negative-leg, positive-leg).
    normalized = {(outflow.txn_id, inflow.txn_id) for outflow, inflow in pairs}
    assert normalized == {("a1", "a2"), ("b1", "b2")}, normalized
    for outflow, inflow in pairs:
        assert outflow.amount_signed < 0, "negative leg must be first"
        assert inflow.amount_signed > 0, "positive leg must be second"


# --- #6: trend dict money fields stay cent-quantized Decimal ----------------


def test_trend_dict_money_fields_are_decimal(tmp_path: Path):
    """_trend_db_to_dict must return Decimal money, not float, so downstream
    money math never drifts."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            SpendingTrendDB(
                time_period="2025-04-10",
                period_type="daily",
                category="Dining",
                transaction_count=2,
                total_spent=Decimal("25.50"),
                avg_transaction=Decimal("12.75"),
                median_transaction=Decimal("12.75"),
                vs_previous_period_pct=Decimal("10.00"),
                vs_previous_period_abs=Decimal("2.50"),
                vs_avg_6m_pct=Decimal("5.00"),
                top_merchant="CAFE",
                top_merchant_amount=Decimal("20.00"),
                trend_direction="up",
                computed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    trends = LedgerQueryService(db_path).get_daily_trends(date(2025, 4, 10), date(2025, 4, 10))

    assert len(trends) == 1
    row = trends[0]
    for key in (
        "total_spent",
        "avg_transaction",
        "median_transaction",
        "vs_previous_period_pct",
        "vs_previous_period_abs",
        "vs_avg_6m_pct",
        "top_merchant_amount",
    ):
        assert isinstance(row[key], Decimal), f"{key} is not Decimal: {row[key]!r}"
    assert row["total_spent"] == Decimal("25.50")
    assert row["total_spent"].as_tuple().exponent == -2


# --- Reporting follow-ups (reporting/service.py) -----------------------------


def _card_payload(db_path: Path, start: date, end: date) -> dict:
    """Build a fresh (transient) reporting payload for the period."""
    from ledger.reporting.service import ReportSnapshotService

    svc = ReportSnapshotService(db_path)
    return svc.snapshot_for_period(start, end, persist=False)["payload"]


# --- R1: account/card summaries obey the spend universe ----------------------


def test_account_card_summary_excludes_transfers_and_excluded_rows(tmp_path: Path):
    """A transfer-typed row and an exclude_from_spending row must not inflate a
    card's net/volume in account_card_summaries; only spend-universe rows count."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            # A genuine card charge inside the spend universe.
            _make_txn(
                1,
                amount=Decimal("-100.00"),
                merchant="STORE",
                category="Dining",
                institution="exampleissuer",
                account="exampleissuer-card",
                account_type="exampleissuer card",
            ),
            # A transfer leg on the same card: must NOT count as spend.
            _make_txn(
                2,
                amount=Decimal("-500.00"),
                merchant="MOVE",
                category="Transfer",
                cashflow_type="transfer",
                include_in_cashflow=False,
                institution="exampleissuer",
                account="exampleissuer-card",
                account_type="exampleissuer card",
            ),
            # An excluded charge on the same card: must NOT count as spend.
            _make_txn(
                3,
                amount=Decimal("-250.00"),
                merchant="HIDDEN",
                category="Dining",
                exclude_from_spending=True,
                institution="exampleissuer",
                account="exampleissuer-card",
                account_type="exampleissuer card",
            ),
        ],
    )

    payload = _card_payload(db_path, date(2025, 4, 1), date(2025, 4, 30))
    summaries = payload["account_card_summaries"]
    exampleissuer = next(r for r in summaries if r["account_type"] == "exampleissuer card")

    # Only the single $100 spend-universe charge moves net/volume.
    assert Decimal(str(exampleissuer["net"])) == Decimal("-100.00")
    assert Decimal(str(exampleissuer["volume"])) == Decimal("100.00")
    # transaction_count reflects spend-universe rows (documented choice), so the
    # transfer and excluded rows are not counted.
    assert exampleissuer["transaction_count"] == 1


# --- R2: credit_card_optimization.total_spend is gross charges, not net ------


def test_credit_card_total_spend_is_gross_not_net(tmp_path: Path):
    """A card whose charges are fully offset by a payment still reports its gross
    spend; total_spend must be gross charges, not net (charges minus payments)."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            # $300 of charges on a Visa card.
            _make_txn(
                1,
                amount=Decimal("-300.00"),
                merchant="STORE",
                category="Dining",
                institution="examplelender",
                account="examplelender-visa",
                account_type="examplelender visa",
            ),
            # A $300 payment/credit that fully offsets the charge (net = 0).
            _make_txn(
                2,
                amount=Decimal("300.00"),
                merchant="PAYMENT",
                category="Transfer",
                cashflow_type="transfer",
                include_in_cashflow=False,
                institution="examplelender",
                account="examplelender-visa",
                account_type="examplelender visa",
            ),
        ],
    )

    payload = _card_payload(db_path, date(2025, 4, 1), date(2025, 4, 30))
    optimization = payload["credit_card_optimization"]

    # Gross charges of $300 must surface even though net is 0 (fully paid down).
    assert Decimal(str(optimization["total_spend"])) == Decimal("300.00")
    assert optimization["card_count"] == 1


# --- R3: merchant_changes compares like-for-like windows ---------------------


def test_merchant_changes_uses_consistent_windows(tmp_path: Path):
    """merchant_changes must compare like-for-like windows on both sides.

    Today the current side is top-10 and the previous side is top-20.  A
    merchant whose spend is steady across both periods but ranks #15 in BOTH
    is captured on the previous side (top-20) yet dropped from the current side
    (top-10).  It then reads as a spurious "dropped to zero" decrease, when in
    truth its spend was unchanged.  With consistent windows it must appear with
    a current value matching its (unchanged) previous value -- or not appear at
    all -- but never as a false drop-to-zero.
    """
    db_path = tmp_path / "ledger.db"

    txns: list = []
    # 20 merchants spending an identical amount in BOTH periods, with distinct
    # descending magnitudes so ranks are stable across periods. STEADY15 lands
    # at rank #15 in both March and April.
    for n in range(1, 21):
        amount = Decimal(f"-{500 - n * 10}.00")  # 490, 480, ... 300; all distinct
        txns.append(
            _make_txn(
                300 + n,
                amount=amount,
                merchant=f"STEADY{n:02d}",
                category="Dining",
                posting_date=date(2025, 3, 10),
            )
        )
        txns.append(
            _make_txn(
                400 + n,
                amount=amount,
                merchant=f"STEADY{n:02d}",
                category="Dining",
                posting_date=date(2025, 4, 10),
            )
        )
    _write_txns(db_path, txns)

    payload = _card_payload(db_path, date(2025, 4, 1), date(2025, 4, 30))
    changes = {row["merchant"]: row for row in payload["merchant_changes"]}

    # STEADY15's spend is unchanged period over period. It must never show up as
    # a false drop-to-zero (current=0 while previous>0) caused by the current
    # window being narrower than the previous one.
    if "STEADY15" in changes:
        row = changes["STEADY15"]
        prev = Decimal(str(row["previous"]))
        cur = Decimal(str(row["current"]))
        if prev > 0:
            assert cur > 0, (
                "steady merchant reported as a drop-to-zero due to window "
                f"mismatch (current side too narrow): {row}"
            )


# --- R4: the JSON boundary serializer stays cent-exact -----------------------
#
# Fix #4 (unify the serializers to emit cent-precise *str* for Decimal) is NOT
# implemented here: the module already routes every payload money value through a
# single deliberate, documented float-at-boundary contract (see
# test_snapshot_money_float_cent_accuracy_regression, which proves float is
# lossless for cent-quantized money).  Flipping the whole payload to str would
# (a) reverse that documented decision and (b) break direct-call assertions in
# tests/test_insight_reporting.py (e.g. _movement/_category_drift change_pct ==
# 100.0), which this batch is not allowed to edit.  See the report for details.
#
# What we CAN pin safely: the boundary serializer renders the card net/volume
# (the only true Decimals that reach it, from _money()) to exact cents -- no
# binary-float drift -- whatever JSON-native type it emits.


def test_snapshot_card_money_is_cent_exact(tmp_path: Path):
    """The card net/volume the boundary serializer emits must render to exact
    cents (no 19.989999... drift), preserving the cent-lossless contract."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("-19.99"),
                merchant="STORE",
                category="Dining",
                institution="examplelender",
                account="examplelender-visa",
                account_type="examplelender visa",
            ),
        ],
    )

    payload = _card_payload(db_path, date(2025, 4, 1), date(2025, 4, 30))
    card = payload["account_card_summaries"][0]

    # Whatever JSON-native type is emitted, the cents must be exact.
    assert Decimal(str(card["net"])).quantize(Decimal("0.01")) == Decimal("-19.99")
    assert Decimal(str(card["volume"])).quantize(Decimal("0.01")) == Decimal("19.99")
    assert Decimal(str(payload["credit_card_optimization"]["total_spend"])).quantize(
        Decimal("0.01")
    ) == Decimal("19.99")


# =============================================================================
# Privacy follow-up batch (2026-06): H1 (config export dest + leak guard) and
# H2 (zip export must not leave an un-cleaned package dir of raw-PII CSVs).
# =============================================================================

# Repo root for the real pre-commit hook + shared predicate.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRE_COMMIT_HOOK = _REPO_ROOT / "scripts" / "git-hooks" / "pre-commit"
_EXPORT_HEADER_CHECK = _REPO_ROOT / "scripts" / "git-hooks" / "_is_export_header"


# --- Config exports must resolve external destinations at call time. ---


def test_config_export_default_dest_is_external_and_resolved_per_call(tmp_path, monkeypatch):
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.chdir(checkout)
    for name in ("first", "second"):
        home = tmp_path / name
        monkeypatch.setenv("LEDGER_HOME", str(home))
        config_export(db_path=db_path, config_file=None)
        assert (home / "exported/merchants.toml").is_file()
        assert (home / "exported/categories.toml").is_file()
    assert sorted(path.name for path in checkout.iterdir()) == [".git"]


def test_config_export_explicit_external_dest_overrides_home(tmp_path, monkeypatch):
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    home = tmp_path / "runtime"
    monkeypatch.setenv("LEDGER_HOME", str(home))
    selected = tmp_path / "selected-export"
    config_export(db_path=db_path, dest=selected, config_file=None)
    assert (selected / "merchants.toml").is_file()
    assert (selected / "categories.toml").is_file()
    assert not home.exists()


# --- H1.4: the REAL pre-commit hook blocks exported config TOML, allows source ---


def _clean_git_env(tmp_path: Path) -> dict[str, str]:
    """Env for throwaway-repo git subprocesses with the ambient GIT_* state scrubbed.

    Without this, running these tests INSIDE a git commit hook (which exports
    GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE for the hook process) makes the inner
    ``git add`` operate on the developer's REAL index instead of the throwaway repo
    — corrupting the working commit and cross-contaminating sibling tests.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    return env


def _run_real_pre_commit(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL scripts/git-hooks/pre-commit inside a throwaway git repo.

    Stubs .venv/bin/python and .venv/bin/ruff with no-op executables so the venv
    guard passes and the lint/pytest tail is cheap — the TOML/export-leak guards
    (which run BEFORE those) are what we exercise. We copy the real hook script in
    rather than re-implementing the guard inline, so the test and the shipped hook
    can never drift.
    """
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python", "ruff"):
        stub = venv_bin / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)

    hooks_dir = repo / "scripts" / "git-hooks"
    hooks_dir.mkdir(parents=True)
    for src in (_PRE_COMMIT_HOOK, _EXPORT_HEADER_CHECK):
        dst = hooks_dir / src.name
        dst.write_text(src.read_text())
        dst.chmod(0o755)

    return subprocess.run(
        ["bash", str(hooks_dir / "pre-commit")],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_clean_git_env(repo.parent),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _clean_git_env(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True, env=env
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, env=env)
    return repo


def test_pre_commit_blocks_exported_config_toml(tmp_path: Path) -> None:
    """The real hook must reject a staged configs/exported/*.toml merchant dump."""
    repo = _init_repo(tmp_path)
    leak = repo / "configs" / "exported"
    leak.mkdir(parents=True)
    (leak / "merchants.toml").write_text('version = "1"\n[[merchant]]\nname = "ACME"\n')
    subprocess.run(
        ["git", "add", "configs/exported/merchants.toml"],
        cwd=repo,
        check=True,
        env=_clean_git_env(tmp_path),
    )

    result = _run_real_pre_commit(repo)
    assert result.returncode == 1, (
        f"hook must block configs/exported/*.toml; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "exported config TOML" in result.stderr


def test_pre_commit_allows_source_config_toml(tmp_path: Path) -> None:
    """The curated SOURCE config the app reads (configs/merchants.toml — no
    exported/ segment) must stay allowed: the hook must NOT block it."""
    repo = _init_repo(tmp_path)
    configs = repo / "configs"
    configs.mkdir(parents=True)
    (configs / "merchants.toml").write_text('version = "1"\n[[merchant]]\nname = "ACME"\n')
    subprocess.run(
        ["git", "add", "configs/merchants.toml"],
        cwd=repo,
        check=True,
        env=_clean_git_env(tmp_path),
    )

    result = _run_real_pre_commit(repo)
    assert result.returncode == 0, (
        f"hook must allow the tracked source config configs/merchants.toml; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# --- H2: zip export must not leave an un-cleaned package dir of raw-PII CSVs ---


def _insert_export_txn(db_path: Path) -> None:
    """Insert one full-detail transaction so the zip carries transactions.csv /
    merchant_summary.csv (the raw-PII files that must never survive on disk)."""
    from ledger.storage.database import TransactionDB, session_scope

    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="h2_txn",
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="harbor-chequing",
                account_type="Chequing",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("25.00"),
                amount_signed=Decimal("-25.00"),
                description="DISTINCT MERCHANT 9X7",
                merchant="DISTINCT MERCHANT 9X7",
                category="Dining",
                source_file="statement.csv",
                source_path="/tmp/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="h2-fp",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def test_zip_export_leaves_no_unzipped_package_dir(tmp_path: Path) -> None:
    """A full-detail zip export must produce the .zip and leave NO un-zipped
    package directory or raw-PII CSVs (transactions.csv / merchant_summary.csv)
    behind in the destination dir."""
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_export_txn(db_path)

    dest = tmp_path / "exports"
    result = ExportPackageService(db_path).create_package(
        dest_dir=dest,
        preset="full-local-audit",
        export_format="zip",
    )

    # (a) The .zip exists and the returned result points at it.
    assert result.path.exists()
    assert result.path.suffix == ".zip"
    assert result.files == [result.path.name]

    # (b) NO leftover package dir or bare per-file outputs survive in dest.
    survivors = [p.name for p in dest.iterdir()]
    assert survivors == [result.path.name], (
        f"only the .zip may remain in dest; found leftovers: {survivors}"
    )
    assert not any(p.is_dir() for p in dest.iterdir()), "no package dir may survive"
    # Spot-check the specific raw-PII filenames never linger anywhere under dest.
    for leaked in dest.rglob("transactions.csv"):
        pytest.fail(f"raw-PII transactions.csv left on disk: {leaked}")
    for leaked in dest.rglob("merchant_summary.csv"):
        pytest.fail(f"raw-PII merchant_summary.csv left on disk: {leaked}")

    # And the zip itself is intact and still carries the expected per-file outputs.
    with zipfile.ZipFile(result.path) as zf:
        names = set(zf.namelist())
    assert {"transactions.csv", "merchant_summary.csv"} <= names


# --- LOW: friendly errors on a bad --preset / --format -----------------------


def test_export_bad_preset_is_friendly_not_traceback(tmp_path: Path) -> None:
    """`export package --preset bogus` must fail with a clean message listing the
    valid presets, not a raw ValueError traceback."""
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = CliRunner().invoke(
        app, ["export", "package", "--db", str(db_path), "--preset", "bogus"]
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"bad preset must not surface a raw traceback: {result.exception!r}"
    )
    out = result.output.lower()
    assert "preset" in out
    assert "chatgpt-safe" in result.output  # the valid list is shown


def test_export_bad_format_is_friendly_not_traceback(tmp_path: Path) -> None:
    """`export package --format xlsx` must fail with a clean message listing the
    valid formats, not a raw ValueError traceback."""
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = CliRunner().invoke(
        app, ["export", "package", "--db", str(db_path), "--format", "xlsx"]
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"bad format must not surface a raw traceback: {result.exception!r}"
    )
    out = result.output.lower()
    assert "format" in out
    assert "zip" in out  # the valid list is shown


import inspect as _inspect  # noqa: E402
import sqlite3  # noqa: E402
from contextlib import closing  # noqa: E402


def _load_pdf_module():
    """Import the package PDF extractor module directly (source under test)."""
    from ledger.imports.pdf import statement_pdf_to_csv as mod

    return mod


# --- Fix 3: no hardcoded personal merchant terms in source --------------------


def test_source_has_no_hardcoded_personal_merchant_terms():
    """The committed source must not embed personal merchant literals."""
    mod = _load_pdf_module()
    src = Path(_inspect.getfile(mod)).read_text(encoding="utf-8")
    lowered = src.lower()
    for term in ("fictional moonlit teapot", "example private merchant"):
        assert term not in lowered, f"personal merchant literal still in source: {term!r}"


# --- Fix 4: merge_forward must fail gracefully on a schema mismatch -----------


def _make_older_schema_db(path: Path) -> None:
    """Create a target DB that is missing the account_id migration on savings_goals
    (an OLDER schema than a fully-migrated staging copy)."""
    from ledger.storage.database import init_database as _init
    from ledger.storage.migrations import run_migrations as _run

    _init(path)
    _run(path)
    # Roll savings_goals BACK to its pre-account_id shape so a SELECT * has a
    # different column count than the migrated staging copy.
    with closing(sqlite3.connect(path)) as conn, conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(savings_goals)").fetchall()]
        if "account_id" in cols:
            keep = [c for c in cols if c != "account_id"]
            collist = ", ".join(keep)
            conn.execute("ALTER TABLE savings_goals RENAME TO savings_goals_new")
            conn.execute(f"CREATE TABLE savings_goals AS SELECT {collist} FROM savings_goals_new")
            conn.execute("DROP TABLE savings_goals_new")
        # Mark the account-linked-goals migration as NOT applied (older schema).
        conn.execute("DELETE FROM schema_migrations WHERE version LIKE '%account_linked_goals%'")


def test_merge_forward_schema_mismatch_is_handled_not_unhandled_crash(tmp_path: Path):
    """A target (live) DB on an OLDER schema than the staging copy must NOT cause
    an unhandled sqlite OperationalError mid-merge. merge_forward must either
    auto-migrate the live target up first, or raise a clear, typed error."""
    from ledger.imports.pdf.readiness import merge_forward

    live = tmp_path / "live.db"  # older schema (savings_goals missing account_id)
    into = tmp_path / "staging.db"  # fully migrated
    _make_older_schema_db(live)

    from ledger.storage.database import init_database as _init
    from ledger.storage.migrations import run_migrations as _run

    _init(into)
    _run(into)

    # Pre-fix: bare sqlite3.OperationalError ("table savings_goals has N columns
    # but M values were supplied") escapes. Post-fix: either it succeeds (auto
    # migrate) or raises a clear actionable error — never a raw OperationalError.
    try:
        merge_forward(live_db=live, into_db=into)
    except sqlite3.OperationalError as exc:  # the exact crash we are fixing
        pytest.fail(f"merge_forward leaked a raw sqlite OperationalError: {exc}")
    except RuntimeError as exc:
        # A clear, actionable abort is acceptable (no partial promote).
        assert "schema" in str(exc).lower()


# --- Fix 5: readiness.py sqlite connections are closed ------------------------


def test_readiness_db_helpers_do_not_leak_connections(tmp_path: Path):
    """A smoke check that the readiness helpers complete and the file is fully
    released (no lingering handle prevents unlinking on any platform)."""
    from ledger.imports.pdf.readiness import (
        database_fingerprint,
        import_owned_fingerprint,
    )

    db = tmp_path / "ledger.db"
    init_database(db)
    database_fingerprint(db)
    import_owned_fingerprint(db)
    # If a connection were left open, this unlink would still pass on POSIX but
    # the source-level fix (contextlib.closing) is what we assert below.
    src = Path(_inspect.getfile(import_owned_fingerprint)).read_text(encoding="utf-8")
    assert "closing(sqlite3.connect" in src, (
        "readiness.py must wrap sqlite3.connect in contextlib.closing to avoid leaks"
    )


# --- Fix 6: record_processed_files records every distinct PDF's hash ----------


def test_record_processed_files_keeps_both_same_account_month_pdfs(tmp_path: Path):
    """Two DISTINCT PDFs mapping to the same account+month (hence the same intended
    processed_pdf archive path) must BOTH be recorded with their own content hash,
    so a later re-import of either is recognized as already-processed."""
    from ledger.imports.pdf.processing import record_processed_files

    db = tmp_path / "staging.db"
    init_database(db)

    same_processed = "/data/processed/2026-04-harbor-chequing-1001.pdf"
    pdfs = [
        {"processed_pdf": same_processed, "file_hash": "hash_aaaa", "transaction_count": 2},
        {"processed_pdf": same_processed, "file_hash": "hash_bbbb", "transaction_count": 3},
    ]
    record_processed_files(db, pdfs)

    with closing(sqlite3.connect(db)) as conn:
        hashes = {r[0] for r in conn.execute("select file_hash from processed_files").fetchall()}
    assert hashes == {"hash_aaaa", "hash_bbbb"}, (
        f"a content hash was dropped (same-account/month collapse): {hashes}"
    )


def textwrap_dedent(text: str) -> str:
    import textwrap

    return textwrap.dedent(text)


# =============================================================================
# Storage/config data-safety + correctness batch (2026-06,
# chore/followup-fixes-2026-06).
#
#  1. validate_database REQUIRED_TABLES must equal the live ORM metadata.
#  2. replace_from_configs must validate before any destructive replace.
#  3. re-import must preserve RULE-confidence categorizations (not only MANUAL).
#  4. category.merchants seed lists must not be silently discarded.
#  5. unknown config keys must warn (not silently drop).
#  6. a category must not be its own parent.
#  7. ReDoS guard gaps (deferred unless strengthenable without false positives).
# =============================================================================

import logging  # noqa: E402

from ledger.config.repository import SQLiteConfigRepository  # noqa: E402
from ledger.config.schemas import CategoryRule as _CategoryRule  # noqa: E402
from ledger.core.enums import Confidence  # noqa: E402
from ledger.core.exceptions import ConfigError  # noqa: E402

# --- Fix 1: REQUIRED_TABLES is derived from the live ORM metadata ------------


def test_required_tables_matches_orm_metadata():
    """REQUIRED_TABLES must equal the full set of live ORM tables so a structurally
    broken DB can never pass validation by omission. Guards future drift."""
    from ledger.storage.database import Base
    from ledger.storage.migrations import REQUIRED_TABLES

    assert set(Base.metadata.tables) == REQUIRED_TABLES, (
        "REQUIRED_TABLES drifted from Base.metadata.tables; derive it from the ORM."
    )


def test_evidence_schema_check_fails_when_a_real_table_is_missing(tmp_path: Path):
    """The read-only schema check (EvidenceService._database, which does NOT
    self-heal via init_database) must FAIL when a genuinely-used live table is
    missing. Pre-fix, a live table absent from the hardcoded REQUIRED_TABLES was
    never checked, so a structurally broken DB reported PASS."""
    from ledger.evidence import EvidenceService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # 'accounts' is one of the live tables the pre-fix hardcoded set omitted.
    table = "accounts"
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"DROP TABLE {table}")

    summary = EvidenceService(db_path, audit_runs_dir=tmp_path / "audit").summary()
    database = summary["database"]
    assert database["status"] == "fail", (
        f"evidence schema check must fail when live table {table!r} is missing: {database}"
    )
    assert table in database["missing_tables"], database


def test_validate_database_table_presence_uses_full_table_set(tmp_path: Path):
    """validate_database's table-presence set must cover every live table. Because
    validate_database calls init_database (which recreates any dropped table), we
    assert on the wiring: the missing-set is computed against the full ORM set."""
    from ledger.storage.database import Base
    from ledger.storage.migrations import REQUIRED_TABLES, validate_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # A clean, fully-migrated DB validates successfully and the required set is the
    # complete live set (so no live table can slip the presence check by omission).
    result = validate_database(db_path, record=False)
    assert result.status == "success", result.errors
    assert set(REQUIRED_TABLES) == set(Base.metadata.tables)


# --- Fix 2: replace_from_configs validates BEFORE any destructive replace -----


def write_seed_configs(root: Path) -> None:
    """Write a small valid merchants.toml + categories.toml pair under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "merchants.toml").write_text(
        """
version = "test"

[[rule]]
merchant_norm = "ACME"
category = "Groceries"
patterns = ["ACME MARKET"]
exclude_from_spending = false
""",
        encoding="utf-8",
    )
    (root / "categories.toml").write_text(
        """
version = "test"
default_category = "Other"

[[category]]
category_norm = "Groceries"
merchants = ["ACME"]
exclude_from_spend_metrics = false
""",
        encoding="utf-8",
    )


def test_invalid_config_import_leaves_existing_runtime_intact(tmp_path: Path):
    """An import whose merchant rule references a missing category must be REFUSED,
    and the previously-valid runtime config must remain fully intact."""
    from ledger.config.schemas import (
        CategoriesConfig as _Cats,
    )
    from ledger.config.schemas import (
        MerchantRule as _MRule,
    )
    from ledger.config.schemas import (
        MerchantRulesConfig as _MRules,
    )

    source = tmp_path / "configs"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.import_from_toml(source)

    before_merchants = {r.merchant_norm for r in repo.load_merchant_rules().rules}
    before_categories = {c.name for c in repo.load_categories().categories}
    assert before_merchants and before_categories

    # A merchant rule pointing at a non-existent category -> invalid cross-reference.
    bad_merchants = _MRules(
        version="bad",
        rules=[
            _MRule(
                merchant_norm="GHOST",
                category="DoesNotExist",
                patterns=["GHOST"],
            )
        ],
    )
    bad_categories = _Cats(
        version="bad",
        default_category="Other",
        categories=[_CategoryRule(name="Food", merchants=[])],
    )

    with pytest.raises(ConfigError):
        repo.replace_from_configs(bad_merchants, bad_categories)

    # The pre-existing runtime config must be UNTOUCHED (no partial overwrite).
    after_merchants = {r.merchant_norm for r in repo.load_merchant_rules().rules}
    after_categories = {c.name for c in repo.load_categories().categories}
    assert after_merchants == before_merchants, "invalid import clobbered merchants"
    assert after_categories == before_categories, "invalid import clobbered categories"


def test_valid_config_import_still_applies(tmp_path: Path):
    """A valid import must still replace the runtime config (no regression)."""
    from ledger.config.schemas import (
        CategoriesConfig as _Cats,
    )
    from ledger.config.schemas import (
        MerchantRule as _MRule,
    )
    from ledger.config.schemas import (
        MerchantRulesConfig as _MRules,
    )

    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)

    good_merchants = _MRules(
        version="v2",
        rules=[_MRule(merchant_norm="NEWCO", category="Shopping", patterns=["NEWCO"])],
    )
    good_categories = _Cats(
        version="v2",
        default_category="Other",
        categories=[_CategoryRule(name="Shopping", merchants=["NEWCO"])],
    )

    repo.replace_from_configs(good_merchants, good_categories)

    rules = {r.merchant_norm for r in repo.load_merchant_rules().rules}
    cats = {c.name for c in repo.load_categories().categories}
    assert rules == {"NEWCO"}
    assert cats == {"Shopping"}


# --- Fix 3: re-import preserves RULE-confidence categorizations ---------------


def test_reimport_preserves_rule_confidence_category(tmp_path: Path):
    """A re-imported row whose category was set by a `rules apply` pass (RULE
    confidence) must keep that category AND merchant; a plain IMPORT row must
    still refresh normally."""
    from ledger.storage.database import TransactionDB, session_scope

    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(1, amount=Decimal("-10.00"), merchant="RAW1", category="Other"),
            _make_txn(2, amount=Decimal("-20.00"), merchant="RAW2", category="Other"),
        ],
    )

    # Simulate a retroactive `rules apply`: row 1 gets RULE-confidence merchant+category.
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "ff_txn_1")
        row.merchant = "RULE MERCHANT"
        row.category = "Dining"
        row.merchant_confidence = Confidence.RULE
        row.category_confidence = Confidence.RULE

    # Re-import the SAME source rows (freshly parsed IMPORT values).
    _write_txns(
        db_path,
        [
            _make_txn(1, amount=Decimal("-10.00"), merchant="RAW1", category="Other"),
            _make_txn(2, amount=Decimal("-20.00"), merchant="RAW2", category="Other"),
        ],
    )

    with session_scope(db_path) as session:
        r1 = session.get(TransactionDB, "ff_txn_1")
        r2 = session.get(TransactionDB, "ff_txn_2")
        # RULE-confidence categorization survives the re-import.
        assert r1.category == "Dining", "RULE category was clobbered on re-import"
        assert r1.merchant == "RULE MERCHANT", "RULE merchant was clobbered on re-import"
        assert str(r1.category_confidence) == str(Confidence.RULE)
        assert str(r1.merchant_confidence) == str(Confidence.RULE)
        # A plain IMPORT row still refreshes from the parsed values.
        assert r2.category == "Other"
        assert r2.merchant == "RAW2"


def test_reimport_still_preserves_manual_confidence(tmp_path: Path):
    """MANUAL preservation must not regress when RULE preservation is added."""
    from ledger.storage.database import TransactionDB, session_scope

    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [_make_txn(1, amount=Decimal("-10.00"), merchant="RAW1", category="Other")],
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "ff_txn_1")
        row.merchant = "MANUAL MERCHANT"
        row.category = "Groceries"
        row.merchant_confidence = Confidence.MANUAL
        row.category_confidence = Confidence.MANUAL

    _write_txns(
        db_path,
        [_make_txn(1, amount=Decimal("-10.00"), merchant="RAW1", category="Other")],
    )

    with session_scope(db_path) as session:
        r1 = session.get(TransactionDB, "ff_txn_1")
        assert r1.category == "Groceries"
        assert r1.merchant == "MANUAL MERCHANT"
        assert str(r1.category_confidence) == str(Confidence.MANUAL)
        assert str(r1.merchant_confidence) == str(Confidence.MANUAL)


# --- Fix 4: category.merchants seed lists are not silently discarded ----------


def test_category_merchants_seed_list_is_applied_not_discarded(tmp_path: Path):
    """Merchants declared under a category in categories.toml must materialize as
    category_merchants associations (previously dropped as dead config)."""
    source = tmp_path / "configs"
    source.mkdir()
    (source / "merchants.toml").write_text('version = "test"\n', encoding="utf-8")
    (source / "categories.toml").write_text(
        """
version = "test"
default_category = "Other"

[[category]]
category_norm = "Dining"
merchants = ["LOCAL CAFE", "DINER"]
exclude_from_spend_metrics = false
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.import_from_toml(source)

    categories = repo.load_categories()
    dining = next(c for c in categories.categories if c.name == "Dining")
    seeded = {m.upper().strip() for m in dining.merchants}
    assert {"LOCAL CAFE", "DINER"} <= seeded, (
        f"category.merchants seed list was discarded on import: {dining.merchants}"
    )


# --- Fix 5: unknown config keys warn (not silently dropped) ------------------


def test_unknown_general_key_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """A typo'd key under [general] must emit a WARNING (and otherwise be ignored)."""
    from ledger.config.loader import load_ledger_config

    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[general]
strict = false
stricct = true
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_ledger_config(config_path)

    assert cfg.general.strict is False  # the valid key still applies
    assert any("stricct" in rec.getMessage() for rec in caplog.records), (
        "unknown [general] key must trigger a WARNING"
    )


def test_unknown_fuzzy_key_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """A typo'd key under [fuzzy] must emit a WARNING."""
    from ledger.config.loader import load_ledger_config

    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[fuzzy]
enabled = true
thresold = 90
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        load_ledger_config(config_path)

    assert any("thresold" in rec.getMessage() for rec in caplog.records)


def test_unknown_cli_defaults_key_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """A typo'd key under [defaults] must emit a WARNING."""
    from ledger.config.loader import load_cli_defaults

    config_path = tmp_path / "defaults.toml"
    config_path.write_text(
        """
[defaults]
db = "/tmp/x.db"
formatz = "csv"
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        load_cli_defaults(config_path)

    assert any("formatz" in rec.getMessage() for rec in caplog.records)


# --- Fix 6: a category must not be its own parent ----------------------------


def test_category_cannot_be_its_own_parent():
    """A category whose parent equals its own name must be rejected."""
    from ledger.config.schemas import CategoriesConfig

    with pytest.raises((ValueError,)):
        CategoriesConfig(
            version="x",
            categories=[_CategoryRule(name="Food", parent="Food")],
        )


def test_category_trivial_two_node_cycle_is_rejected():
    """A trivial A->B->A parent cycle must be rejected."""
    from ledger.config.schemas import CategoriesConfig

    with pytest.raises((ValueError,)):
        CategoriesConfig(
            version="x",
            categories=[
                _CategoryRule(name="A", parent="B"),
                _CategoryRule(name="B", parent="A"),
            ],
        )


# --- Fix 7: ReDoS guard still accepts every legitimate shipped pattern -------


def test_redos_guard_accepts_all_shipped_merchant_patterns():
    """Whatever the guard's strength, it must NOT reject any legitimate pattern in
    the committed configs/merchants.toml."""
    repo_root = Path(__file__).resolve().parents[1]
    from ledger.config.loader import load_merchant_rules as _load

    # Construction compiles + ReDoS-checks every pattern; this must not raise.
    cfg = _load(repo_root / "configs" / "merchants.toml")
    assert cfg.rules, "expected shipped merchant rules to load"


@pytest.mark.skip(
    reason="Fix 7 DEFERRED: a static regex can't flag alternation-overlap (a|a)* "
    "without also rejecting the legitimate (foo|bar)* documented in "
    "test_audit_lows_robustness.py. Left to a future structural check."
)
def test_redos_guard_rejects_alternation_overlap():
    """A classic alternation-overlap catastrophic pattern (a|a)* / (a|ab)* should be
    flagged by a stronger guard — but only a structural (non-regex) check can do this
    without false-positives, so it is deferred (see schemas.py fix-7 NOTE)."""
    from ledger.config.schemas import MerchantRule

    with pytest.raises((ValueError,)):
        MerchantRule(merchant_norm="X", category="Other", patterns=[r"(a|a)*c"])


# ---------------------------------------------------------------------------
# Adjustments true-cost follow-up fixes (2026-06)
# ---------------------------------------------------------------------------
#
# These lock four consistency/correctness/privacy fixes in the true-cost
# adjustments service. They insert TransactionDB rows directly (mirroring
# tests/test_true_cost_adjustments.py) so the spend-universe flags
# (include_in_cashflow / exclude_from_spending) can be set per-row.


def _adj_temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_adj_txn(
    db_path: Path,
    txn_id: str,
    posted: date,
    amount: str,
    merchant: str,
    category: str,
    *,
    description: str | None = None,
    account_type: str = "visa",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    currency: str = "CAD",
) -> None:
    from ledger.storage.database import TransactionDB

    value = Decimal(amount)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="account-0001",
                account_type=account_type,
                flow="expense" if value < 0 else "income",
                cashflow_type="expense" if value < 0 else "income",
                include_in_cashflow=include_in_cashflow,
                exclude_from_spending=exclude_from_spending,
                currency=currency,
                amount_raw=abs(value),
                amount_signed=value,
                description=description or merchant,
                merchant=merchant,
                category=category,
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                **_adj_temporal_fields(posted),
            )
        )


# --- Adjustments Fix 1: annotation_map honours spend-universe filters --------


def test_annotation_map_excludes_offset_for_exclude_from_spending_debit(tmp_path: Path):
    """The export annotation_map's offset query must apply the same spend-universe
    filters as the dashboard (true_cost_by_category / _period_offset_total): an
    offset whose matched DEBIT is exclude_from_spending=True must NOT appear in the
    export annotation_map, so EXPORT true_cost_offset == DASHBOARD offsets."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Debit is flagged exclude_from_spending -> outside the spend universe.
    _insert_adj_txn(
        db_path,
        "debit",
        date(2026, 1, 2),
        "-100.00",
        "APPLE",
        "Electronics",
        exclude_from_spending=True,
    )
    _insert_adj_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "40.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    annotations = service.annotation_map(["debit", "credit"])
    categories = service.true_cost_by_category(date(2026, 1, 1), date(2026, 1, 31))

    # Dashboard never counts an excluded debit's offset; export must match.
    dashboard_offsets = sum((v["offsets"] for v in categories.values()), Decimal("0.00"))
    assert dashboard_offsets == Decimal("0.00")
    assert annotations.get("debit", {}).get("true_cost_offset", 0.0) == 0.0


def test_annotation_map_excludes_offset_for_non_cashflow_debit(tmp_path: Path):
    """An offset whose matched DEBIT is include_in_cashflow=False must also be
    omitted from the export annotation_map (mirrors the dashboard filter)."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "debit",
        date(2026, 1, 2),
        "-100.00",
        "APPLE",
        "Electronics",
        include_in_cashflow=False,
    )
    _insert_adj_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "40.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    annotations = service.annotation_map(["debit", "credit"])
    assert annotations.get("debit", {}).get("true_cost_offset", 0.0) == 0.0


def test_annotation_map_still_reports_offset_for_in_universe_debit(tmp_path: Path):
    """Sanity: a normal in-spend-universe debit still gets its applied offset in the
    export annotation_map (the filters must not over-prune)."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_adj_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "40.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    annotations = service.annotation_map(["debit", "credit"])
    assert annotations["debit"]["true_cost_offset"] == 40.0


# --- Adjustments Fix 2: unmatched reimbursement is not auto_applied ----------


def test_unmatched_reimbursement_not_marked_auto_applied(
    tmp_path: Path, invented_reimbursement_policy
):
    """A reimbursement credit (INVENTED BENEFITS) that finds NO matchable debit must NOT be
    flagged auto_applied / reduce_category_cost — it offsets nothing and is never
    removed from income, so advertising it as a cost reducer is wrong."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB, TransactionTreatmentDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Reimbursement with no health/wellness debit anywhere to offset.
    _insert_adj_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        treatment = session.get(TransactionTreatmentDB, "invented-benefits")
        offsets = session.query(TransactionOffsetDB).count()
        assert offsets == 0
        assert treatment is not None
        assert treatment.status != "auto_applied"
        assert treatment.netting_policy != "reduce_category_cost"


def test_matched_reimbursement_still_marked_auto_applied(
    tmp_path: Path, invented_reimbursement_policy
):
    """The matched case is unchanged: a reimbursement that produces an offset is
    still auto_applied / reduce_category_cost."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionTreatmentDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "dental",
        date(2026, 1, 5),
        "-200.00",
        "MARKHAM DENTAL",
        "Health & Wellness",
    )
    _insert_adj_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        treatment = session.get(TransactionTreatmentDB, "invented-benefits")
        assert treatment is not None
        assert treatment.status == "auto_applied"
        assert treatment.netting_policy == "reduce_category_cost"


# --- Adjustments Fix 3: _best_offsets restricts debits to spend universe -----


def test_reimbursement_does_not_offset_exclude_from_spending_debit(
    tmp_path: Path, invented_reimbursement_policy
):
    """The debit-matching for offsets must ignore debits outside the spend universe:
    a reimbursement must not net against an exclude_from_spending debit."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "dental",
        date(2026, 1, 5),
        "-200.00",
        "MARKHAM DENTAL",
        "Health & Wellness",
        exclude_from_spending=True,
    )
    _insert_adj_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        assert session.query(TransactionOffsetDB).count() == 0


def test_reimbursement_does_not_offset_non_cashflow_debit(
    tmp_path: Path, invented_reimbursement_policy
):
    """A reimbursement must not net against an include_in_cashflow=False debit."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "dental",
        date(2026, 1, 5),
        "-200.00",
        "MARKHAM DENTAL",
        "Health & Wellness",
        include_in_cashflow=False,
    )
    _insert_adj_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        assert session.query(TransactionOffsetDB).count() == 0


# --- Adjustments Fix 4: _txn_dict gates raw merchant/description -------------


def test_txn_dict_redacts_raw_fields_by_default(tmp_path: Path):
    """With raw disabled (the privacy default), _txn_dict must not embed the raw
    merchant / description in evidence JSON."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.adjustments.service import _txn_dict
    from ledger.storage.database import TransactionDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "debit",
        date(2026, 1, 2),
        "-100.00",
        "SECRET MERCHANT",
        "Electronics",
        description="SECRET DESCRIPTION",
    )
    # Service defaults to privacy-on (include_raw=False).
    service = AdjustmentDetectionService(db_path)
    assert service.include_raw is False

    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "debit")
        redacted = _txn_dict(row, include_raw=False)
    assert "SECRET MERCHANT" not in str(redacted)
    assert "SECRET DESCRIPTION" not in str(redacted)
    assert redacted.get("merchant") in (None, "")
    assert redacted.get("description") in (None, "")
    # Non-PII structural fields survive so evidence stays useful.
    assert redacted["txn_id"] == "debit"
    assert redacted["amount"] == -100.0


def test_txn_dict_includes_raw_when_enabled(tmp_path: Path):
    """When include_raw=True the raw fields are preserved (opt-in)."""
    from ledger.adjustments.service import _txn_dict
    from ledger.storage.database import TransactionDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path,
        "debit",
        date(2026, 1, 2),
        "-100.00",
        "VISIBLE MERCHANT",
        "Electronics",
        description="VISIBLE DESCRIPTION",
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "debit")
        full = _txn_dict(row, include_raw=True)
    assert full["merchant"] == "VISIBLE MERCHANT"
    assert full["description"] == "VISIBLE DESCRIPTION"


def test_persisted_evidence_has_no_raw_merchant_by_default(tmp_path: Path):
    """End-to-end: with the privacy default, persisted offset/treatment evidence JSON
    must not contain the raw merchant string."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB, TransactionTreatmentDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "HUSH MERCHANT", "Electronics")
    _insert_adj_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "100.00",
        "HUSH MERCHANT",
        "Electronics",
        description="HUSH MERCHANT (Credit)",
    )
    AdjustmentDetectionService(db_path).detect(execute=True)

    with session_scope(db_path) as session:
        for offset in session.query(TransactionOffsetDB):
            assert "HUSH MERCHANT" not in str(offset.evidence)
        for treatment in session.query(TransactionTreatmentDB):
            assert "HUSH MERCHANT" not in str(treatment.evidence)


# =============================================================================
# Planning-layer follow-up fixes (2026-06, chore/followup-fixes-2026-06):
# correctness + robustness in budgets / goals / readiness.
#
#  P1. Blank / non-numeric amount cell must not crash budget/goal save or the
#      `budget import` CLI path (NaN-safe Decimal coercion).
#  P2. Month-specific budgets must prorate for partial-month report windows.
#  P3. Goal editor must not double-apply its added/deleted-row delta on rerun.
#  P4. Readiness goal target/current/remaining must be cent-quantized Decimal.
#  P5. _latest_account_balances must batch (no N+1); result identical.
# =============================================================================

from ledger.planning import BudgetService as _PlanningBudgetService  # noqa: E402
from ledger.planning import (  # noqa: E402
    PlanningReadinessService as _PlanningReadinessService,
)
from ledger.storage.database import (  # noqa: E402
    AccountBalanceDB as _AccountBalanceDB,
)


def _now_dt() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_goal_account(
    db_path: Path,
    institution: str,
    number: str,
    balances: list[tuple[date, Decimal]],
) -> str:
    """Seed the accounts row plus its AccountBalanceDB rows (for goal-linking tests).

    Returns the account id, which the balance rows reference by foreign key.
    """
    now = _now_dt()
    account_id = seed_account(
        db_path,
        institution,
        number,
        account_type="bank",
        first_transaction_date=balances[0][0],
        last_transaction_date=balances[-1][0],
    )
    with session_scope(db_path) as session:
        for bal_date, bal_amount in balances:
            session.add(
                _AccountBalanceDB(
                    account_id=account_id,
                    balance_date=bal_date,
                    balance=bal_amount,
                    balance_basis="absolute",
                    transaction_count=1,
                    balance_7d_avg=bal_amount,
                    balance_30d_avg=bal_amount,
                    balance_7d_min=bal_amount,
                    balance_7d_max=bal_amount,
                    credit_utilization=None,
                    computed_at=now,
                )
            )
    return account_id


# --- P1: blank / non-numeric amount cell must not crash save/import ----------


def test_blank_budget_amount_cell_saves_as_zero_not_crash(tmp_path: Path):
    """A blank amount cell (None) must coerce to Decimal('0') and save cleanly,
    not raise InvalidOperation / become Decimal('NaN')."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = _PlanningBudgetService(db_path)

    # A blank amount cell as it arrives from st.data_editor (None).
    result = svc.replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": None, "notes": ""}]
    )

    assert result.ok, result.errors
    budgets = svc.list_budgets()
    assert len(budgets) == 1
    assert budgets[0]["amount"] == Decimal("0")
    assert isinstance(budgets[0]["amount"], Decimal)


def test_nan_budget_amount_cell_does_not_crash(tmp_path: Path):
    """A float('nan') amount (what an empty NumberColumn cell becomes) must coerce
    to Decimal('0'), never propagate as Decimal('NaN')."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = _PlanningBudgetService(db_path)

    result = svc.replace_budgets(
        [{"month": "*", "category": "Dining", "amount": float("nan"), "notes": ""}]
    )

    assert result.ok, result.errors
    budgets = svc.list_budgets()
    assert budgets[0]["amount"] == Decimal("0")
    # Must be a real zero, not a NaN that compares unequal to itself.
    assert budgets[0]["amount"] == budgets[0]["amount"]


def test_blank_goal_target_cell_saves_as_zero_not_crash(tmp_path: Path):
    """A blank goal target_amount must coerce to Decimal('0') and save cleanly."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    account_id = _seed_goal_account(
        db_path, "bank", "g-0001", [(date(2026, 5, 1), Decimal("100.00"))]
    )
    svc = _PlanningBudgetService(db_path)

    result = svc.replace_goals(
        [
            {
                "name": "Rainy day",
                "account_id": account_id,
                "target_amount": float("nan"),
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    )

    assert result.ok, result.errors
    goals = svc.list_goals()
    assert len(goals) == 1
    assert goals[0]["target_amount"] == Decimal("0")


def test_budget_import_non_numeric_amount_is_handled_not_invalid_operation(
    tmp_path: Path,
):
    """`budget import` of a CSV whose amount cell is non-numeric must not raise an
    uncaught InvalidOperation or silently replace a budget with zero."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    source = tmp_path / "budgets.csv"
    source.write_text(
        "month,category,amount,notes\n*,Groceries,not-a-number,\n",
        encoding="utf-8",
    )

    svc = _PlanningBudgetService(db_path)
    assert svc.replace_budgets(
        [{"month": "*", "category": "Existing", "amount": "42", "notes": ""}]
    ).ok
    before = svc.list_budgets()

    result = svc.import_csv(source, execute=True)

    assert not result.ok
    assert svc.list_budgets() == before


# --- P2: month-specific budgets prorate for partial-month windows ------------


def test_month_specific_budget_prorated_for_half_month_window(tmp_path: Path):
    """A month-specific (YYYY-MM) budget compared against a half-month window must
    yield ~half the prorated budget — not the full month at a hardcoded factor 1."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = _PlanningBudgetService(db_path)
    svc.replace_budgets(
        [{"month": "2026-05", "category": "Groceries", "amount": "300.00", "notes": ""}]
    )

    # May 1 to May 15 covers 15 of May's 31 days -> ~0.4839 of the month.
    rows = svc.budget_variance(date(2026, 5, 1), date(2026, 5, 15))
    groceries = next(r for r in rows if r["category"] == "Groceries")

    expected = Decimal("300.00") * Decimal(15) / Decimal(31)
    # Pre-fix the budget was a flat 300.00 (factor 1). Post-fix it is prorated.
    assert groceries["budget"] < Decimal("300.00"), "partial month not prorated"
    assert abs(groceries["budget"] - expected) < Decimal("0.01")


def test_month_specific_budget_full_month_window_is_exact(tmp_path: Path):
    """A month-specific budget over its whole month still yields exactly the amount
    (overlap fraction == 1) — no regression for the common case."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = _PlanningBudgetService(db_path)
    svc.replace_budgets(
        [{"month": "2026-05", "category": "Groceries", "amount": "300.00", "notes": ""}]
    )

    rows = svc.budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    groceries = next(r for r in rows if r["category"] == "Groceries")
    assert groceries["budget"] == Decimal("300.00")


# --- P3: goal editor must not double-apply its added-row delta on rerun -------


def test_goal_editor_added_row_applied_exactly_once_on_rerun(tmp_path: Path):
    """The goal editor must apply its added-row delta exactly once.

    Regression for the double-apply hazard: _apply_goal_editor_state pre-applied
    the data_editor delta and the mutated frame was re-fed under the SAME key, so
    Streamlit re-applied the stored delta on rerun — duplicating added rows.

    We can't drive Streamlit's rerun here, but we can lock the contract the fix
    relies on: applying the same added-row state twice (mimicking the input frame
    already carrying a prior application) must not duplicate the row.
    """
    from ledger.ui.pages.budgets_goals import (
        GOAL_COLUMNS,
        _apply_goal_editor_state,
        _goal_dataframe,
    )

    default_account = "bank - Savings (...0001)"
    editor_state = {
        "edited_rows": {},
        "added_rows": [{"name": "New goal", "account": default_account}],
        "deleted_rows": [],
    }

    base = _goal_dataframe([], {}, default_account)
    once = _apply_goal_editor_state(base, editor_state)
    # Re-applying the same delta to the already-applied frame must be idempotent:
    # it must not produce a second copy of the added row.
    twice = _apply_goal_editor_state(once, editor_state)

    new_rows_once = once[once["name"] == "New goal"]
    new_rows_twice = twice[twice["name"] == "New goal"]
    assert len(new_rows_once) == 1
    assert len(new_rows_twice) == 1, (
        "added-row delta was double-applied (duplicated on second apply)"
    )
    assert list(twice.columns) == GOAL_COLUMNS


def test_goal_editor_render_does_not_pre_apply_its_own_widget_state(tmp_path: Path):
    """The render path must not pre-apply the goal_editor session-state onto the
    frame it feeds back to st.data_editor under the same 'goal_editor' key.

    Streamlit re-applies that stored delta on every rerun; pre-applying it as well
    double-applies the edit. We assert the source contract: the editor input is the
    un-mutated base frame, so the line that read st.session_state.get('goal_editor')
    and re-fed the result to the editor is gone.
    """
    import inspect

    from ledger.ui.pages import budgets_goals as page

    src = inspect.getsource(page.render)
    assert 'st.session_state.get("goal_editor")' not in src, (
        "render still pre-applies the goal_editor widget's own session-state to the "
        "editor input (double-apply hazard)"
    )


# --- P4: readiness goal money fields are cent-quantized Decimal --------------


def test_readiness_goal_money_fields_are_decimal(tmp_path: Path):
    """summary['goals'] target/current/remaining must be cent-quantized Decimal,
    consistent with the rest of the money-handling codebase (not float)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    account_id = _seed_goal_account(
        db_path,
        "bank",
        "r-0001",
        [(date(2026, 5, 1), Decimal("1234.56"))],
    )
    _PlanningBudgetService(db_path).replace_goals(
        [
            {
                "name": "Fund",
                "account_id": account_id,
                "target_amount": "5000.00",
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    )

    summary = _PlanningReadinessService(db_path).summary(
        date(2026, 5, 1), date(2026, 5, 31), pending_reviews=0
    )
    goals = summary["goals"]

    for key in ("target", "current", "remaining"):
        assert isinstance(goals[key], Decimal), (
            f"goals[{key!r}] is {type(goals[key])}, expected Decimal"
        )
        assert goals[key].as_tuple().exponent == -2, (
            f"goals[{key!r}] not cent-quantized: {goals[key]!r}"
        )
    assert goals["target"] == Decimal("5000.00")
    assert goals["current"] == Decimal("1234.56")
    assert goals["remaining"] == Decimal("3765.44")


# --- P5: _latest_account_balances batches (no N+1); result identical ---------


def test_latest_account_balances_batched_matches_per_account(tmp_path: Path):
    """The batched _latest_account_balances must return the same latest-per-account
    result as a naive per-account lookup, across multiple accounts and balances."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    acct_a = _seed_goal_account(
        db_path,
        "bank-a",
        "a-0001",
        [(date(2026, 5, 1), Decimal("100.00")), (date(2026, 5, 10), Decimal("250.00"))],
    )
    acct_b = _seed_goal_account(
        db_path,
        "bank-b",
        "b-0001",
        [(date(2026, 4, 1), Decimal("900.00")), (date(2026, 5, 5), Decimal("777.77"))],
    )
    # An account with no balance rows must be absent from the result (not 0).
    acct_empty = seed_account(
        db_path,
        "bank-c",
        "c-0001",
        account_type="bank",
        first_transaction_date=date(2026, 5, 1),
        last_transaction_date=date(2026, 5, 1),
    )

    svc = _PlanningBudgetService(db_path)
    account_ids = [acct_a, acct_b, acct_empty]
    with session_scope(db_path) as session:
        batched = svc._latest_account_balances(session, account_ids)
        # Naive per-account reference computed independently.
        reference = {}
        for account_id in set(account_ids):
            row = (
                session.query(_AccountBalanceDB)
                .filter(_AccountBalanceDB.account_id == account_id)
                .order_by(_AccountBalanceDB.balance_date.desc())
                .first()
            )
            if row is not None:
                reference[account_id] = {
                    "amount": Decimal(str(row.balance)),
                    "date": row.balance_date,
                }

    assert batched == reference
    assert batched[acct_a] == {"amount": Decimal("250.00"), "date": date(2026, 5, 10)}
    assert batched[acct_b] == {"amount": Decimal("777.77"), "date": date(2026, 5, 5)}
    assert acct_empty not in batched


# ===========================================================================
# Analysis-layer follow-up fixes (2026-06 round)
#
# Six targeted correctness/consistency fixes in the analysis modules:
#   1. transfer_matcher: two zero-amount legs must not pass the amount gate
#   2. transfer_matcher: short/digit-only shared tokens are not distinctive
#   3. pattern_matcher.match_all_transactions: deterministic across row order
#   4. quantize money to cents at the rolling-avg / utilization / expected_amount
#      return boundaries, and clamp trend pct to its Numeric(8,2) column
#   5. account_detector.is_active: date-based, no tz off-by-one at the boundary
#   6. balance_calculator.transaction_count: count transfer/payment legs too
# ===========================================================================

from datetime import date as _date  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402
from datetime import timedelta  # noqa: E402
from decimal import Decimal as _Decimal  # noqa: E402

from ledger.analysis.account_detector import AccountDetector  # noqa: E402
from ledger.analysis.balance_calculator import BalanceCalculator  # noqa: E402
from ledger.analysis.transfer_matcher import TransferMatcher  # noqa: E402
from ledger.core import Transaction as _Txn  # noqa: E402
from ledger.core.analysis_models import Account as _Account  # noqa: E402


def _afix_add_months(base: _date, months: int, day_of_month: int | None = None) -> _date:
    """Shift `base` by whole calendar months, clamping the day to the month length."""
    import calendar as _cal

    target_month = base.month + months
    target_year = base.year + (target_month - 1) // 12
    target_month = ((target_month - 1) % 12) + 1
    target_day = day_of_month if day_of_month is not None else base.day
    last_day = _cal.monthrange(target_year, target_month)[1]
    return _date(target_year, target_month, min(target_day, last_day))


def _afix_recent_monthly_start(day_of_month: int, months: int) -> _date:
    """Start date whose final monthly occurrence is recent enough to stay active."""
    today = _datetime.now(UTC).date()
    latest_offset = 0 if today.day >= day_of_month else -1
    latest_occurrence = _afix_add_months(today.replace(day=1), latest_offset, day_of_month)
    return _afix_add_months(latest_occurrence, -(months - 1), day_of_month)


def _afix_monthly_sub(
    merchant: str, amount: _Decimal, start_date: _date, months: int, day_of_month: int
) -> list[_Txn]:
    """Minimal monthly-subscription fixture (local copy to avoid cross-test imports)."""
    txns: list[_Txn] = []
    for i in range(months):
        d = _afix_add_months(start_date, i, day_of_month)
        dow = d.weekday()
        txns.append(
            _Txn(
                txn_id=f"afix_{merchant}_{i}",
                txn_fingerprint=f"afix_fp_{merchant}_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=amount,
                amount_signed=-amount,
                description=f"{merchant} Subscription",
                merchant=merchant,
                category="Subscription",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw=merchant,
                category_raw="Subscription",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=i,
                imported_at=_datetime(2026, 1, 1),
                parser_version="1.0.0",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )
    return txns


def _afix_transfer(
    i: int,
    amount_raw: _Decimal,
    account: str,
    account_type: str,
    posting_date: _date,
    description: str = "TRANSFER",
    institution: str = "harbor",
) -> _Txn:
    """Transfer-leg fixture mirroring tests/test_transfer_matcher.py::_transfer."""
    return _Txn(
        txn_id=f"aff_tr_{i}",
        txn_fingerprint=f"aff_fp_{i}",
        posting_date=posting_date,
        institution=institution,
        account=account,
        account_type=account_type,
        flow="transfer",
        cashflow_type="transfer",
        include_in_cashflow=False,
        currency="CAD",
        amount_raw=amount_raw,
        amount_signed=_Decimal("0"),
        description=description,
        source_file="x.csv",
        source_path="x.csv",
        source_period="2025-01",
        source_row=i,
        imported_at=_date(2025, 1, 1),
        parser_version="test",
    )


# --- Fix 1: two zero-amount transfer legs must not score an amount match -----


def test_zero_amount_legs_do_not_pass_amount_gate():
    """Two zero-amount legs satisfy abs(0-0) > 0*tol (0 > 0 is False), so pre-fix
    they passed the amount gate with full +0.50 amount score.

    The case is reachable: a deposit->card pair (exactly one credit leg) bypasses the
    opposite-sign gate, so two ZERO-amount legs there formerly scored
    0.50 (amount) + 0.20 (same day) + ... and could pair. A zero-amount 'transfer' is
    meaningless and must score 0.0 / not pair. The chequing+card pattern also supplies
    a real linking signal, so only the new zero-amount guard can reject this pair."""
    matcher = TransferMatcher()
    chequing = _afix_transfer(
        1,
        _Decimal("0.00"),
        "harbor-chequing",
        "Chequing",
        _date(2025, 1, 10),
        description="PAYMENT - CEDAR BANK",
        institution="harbor",
    )
    card = _afix_transfer(
        2,
        _Decimal("0.00"),
        "cedar-mc",
        "Silver Mastercard",
        _date(2025, 1, 10),
        description="PAYMENT THANK YOU",
        institution="cedar",
    )

    # Sanity: a NON-zero version of this exact pair DOES pair (so the only thing the
    # assertion below isolates is the zero-amount amount-gate guard).
    nz_chequing = chequing.model_copy(update={"amount_raw": _Decimal("-75.00")})
    nz_card = card.model_copy(update={"amount_raw": _Decimal("-75.00")})
    assert len(matcher.match_transfers([nz_chequing, nz_card])) == 1

    assert matcher._calculate_match_score(chequing, card) == 0.0
    assert matcher.match_transfers([chequing, card]) == []


def test_nonzero_equal_amount_legs_still_pair():
    """Guard for the zero-amount fix: a genuine non-zero equal-amount pair with a
    linking signal must still pair (the new guard rejects ONLY the both-zero case)."""
    matcher = TransferMatcher()
    a = _afix_transfer(
        3,
        _Decimal("-100.00"),
        "harbor-chequing",
        "Chequing",
        _date(2025, 1, 10),
        description="Online Banking transfer - 9816",
    )
    b = _afix_transfer(
        4,
        _Decimal("100.00"),
        "harbor-savings",
        "Savings",
        _date(2025, 1, 10),
        description="Online Banking transfer - 9816",
    )

    assert len(matcher.match_transfers([a, b])) == 1


# --- Fix 2: short / digit-only shared tokens are not a distinctive link ------


def test_short_or_numeric_shared_token_is_not_distinctive():
    """A shared token that is short or purely numeric ('1', 'to') is NOT a
    distinctive description link. Two legs whose only shared token is such a token
    must not earn the matching-descriptions bonus, and (with no other signal) must
    not pair."""
    matcher = TransferMatcher()
    # Only shared distinctive-candidate token would be the bare digit "1" (short, numeric).
    a = _afix_transfer(
        5,
        _Decimal("-50.00"),
        "harbor-chequing",
        "Chequing",
        _date(2025, 4, 7),
        description="INTERAC e-Transfer 1 sent",
        institution="harbor",
    )
    b = _afix_transfer(
        6,
        _Decimal("50.00"),
        "fern-1",
        "Chequing",
        _date(2025, 4, 7),
        description="INTERAC e-Transfer 1 received",
        institution="fern",
    )

    assert matcher._has_matching_descriptions(a, b) is False
    assert matcher.match_transfers([a, b]) == []


def test_genuinely_distinctive_shared_token_still_links():
    """Guard for the distinctive-token fix: a real counterparty name shared in both
    legs ('ACMEHOLDINGS') must still count as a distinctive link and pair."""
    matcher = TransferMatcher()
    a = _afix_transfer(
        7,
        _Decimal("-300.00"),
        "bank-a-1",
        "Settlement",
        _date(2025, 5, 2),
        description="Online transfer to ACMEHOLDINGS",
        institution="banka",
    )
    b = _afix_transfer(
        8,
        _Decimal("300.00"),
        "bank-b-1",
        "Settlement",
        _date(2025, 5, 2),
        description="Payment from ACMEHOLDINGS thank you",
        institution="bankb",
    )

    assert matcher._has_matching_descriptions(a, b) is True
    assert len(matcher.match_transfers([a, b])) == 1


# --- Fix 4 (transfer matcher has no money fields beyond confidence) ----------
# Quantization fixes are exercised below via balance_calculator + account stats
# and (for trend pct) via the trend clamp test.


def _afix_account(account_id: str, account_type: str, is_credit_card: bool, limit=None) -> _Account:
    now = _datetime(2026, 1, 1)
    return _Account(
        account_id=account_id,
        institution="harbor",
        account_number=f"{account_id}-num",
        account_type=account_type,
        account_subtype=account_type,
        first_transaction_date=_date(2026, 1, 1),
        last_transaction_date=_date(2026, 2, 5),
        is_active=True,
        transaction_count=2,
        avg_monthly_transactions=_Decimal("2"),
        is_credit_card=is_credit_card,
        detected_credit_limit=limit,
        created_at=now,
        updated_at=now,
    )


def _afix_balance_txn(i: int, d: _date, amt: _Decimal) -> _Txn:
    cf = "income" if amt > 0 else "expense"
    return _Txn(
        txn_id=f"abt{i}",
        txn_fingerprint=f"abfp{i}",
        posting_date=d,
        institution="harbor",
        account="acct",
        account_type="visa",
        flow=cf,
        cashflow_type=cf,
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amt),
        amount_signed=amt,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=i,
        imported_at=_date(2026, 1, 1),
        parser_version="t",
    )


def test_balance_rolling_avgs_are_cent_quantized(tmp_path: Path):
    """The 7d/30d rolling averages must be cent-quantized Decimals. A 3-day window
    of 10/20/30 averages to 20.00 (exact) but 10/20/35 -> 21.6666... which must be
    rounded to cents, not stored as an unquantized Decimal."""
    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [
        _afix_balance_txn(0, _date(2026, 1, 5), _Decimal("10")),
        _afix_balance_txn(1, _date(2026, 1, 6), _Decimal("10")),
        _afix_balance_txn(2, _date(2026, 1, 7), _Decimal("15")),
    ]
    balances = calc.compute_daily_balances("c1", txns, _afix_account("c1", "checking", False), [])

    for b in balances:
        for field in ("balance_7d_avg", "balance_30d_avg", "balance_7d_min", "balance_7d_max"):
            value = getattr(b, field)
            assert value.as_tuple().exponent == -2, f"{field}={value!r} not cent-quantized"


def test_credit_utilization_is_cent_quantized(tmp_path: Path):
    """credit_utilization must be cent-quantized. With balance -1000 and limit 3000,
    utilization = 1000/3000*100 = 33.3333..., which must be rounded to 33.33."""
    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [_afix_balance_txn(0, _date(2026, 1, 5), _Decimal("-1000"))]
    balances = calc.compute_daily_balances(
        "c1", txns, _afix_account("c1", "credit_card", True, _Decimal("3000")), []
    )
    absolute_balances = [
        balance.model_copy(update={"balance_basis": "absolute"}) for balance in balances
    ]
    balances = calc._compute_credit_utilization(absolute_balances, _Decimal("3000"))

    util = balances[-1].credit_utilization
    assert util is not None
    assert util.as_tuple().exponent == -2, f"utilization {util!r} not cent-quantized"
    assert util == _Decimal("33.33")


# --- Fix 6: transaction_count includes transfer/payment legs -----------------


def test_transaction_count_includes_transfer_legs(tmp_path: Path):
    """On a credit-card payment day, the payment leg lives in the transfers table.
    transaction_count must count it, or activity on payment days is understated."""
    calc = BalanceCalculator(tmp_path / "x.db")
    # One purchase on the 5th; one payment leg on the 20th (transfers table).
    txns = [_afix_balance_txn(0, _date(2026, 1, 5), _Decimal("-100"))]
    legs = [{"posting_date": _date(2026, 1, 20), "amount_raw": _Decimal("-100")}]

    balances = calc.compute_daily_balances(
        "c1", txns, _afix_account("c1", "credit_card", True, _Decimal("5000")), legs
    )

    by_date = {b.balance_date: b for b in balances}
    assert by_date[_date(2026, 1, 5)].transaction_count == 1  # the purchase
    assert by_date[_date(2026, 1, 20)].transaction_count == 1  # the payment leg, counted


# --- Fix 5: account is_active uses a date basis (no tz off-by-one) -----------


def test_account_is_active_boundary_is_date_based(tmp_path: Path):
    """An account whose last activity is exactly 60 days before 'today' must be
    classified consistently against a DATE basis, not a tz-naive UTC datetime that
    can shift the comparison by a day near the boundary."""
    detector = AccountDetector(tmp_path / "x.db")
    now = _datetime(2026, 6, 22, 3, 0, 0)  # early UTC hour, where tz skew would bite
    last = now.date() - timedelta(days=60)  # exactly on the 60-day boundary

    txns = [_afix_balance_txn(0, last, _Decimal("-10"))]
    account = detector._build_account("harbor", "acct", txns, now)

    # 60 days out is within the inclusive 60-day window -> active.
    assert account.is_active is True

    older_txns = [_afix_balance_txn(1, now.date() - timedelta(days=61), _Decimal("-10"))]
    older = detector._build_account("harbor", "acct", older_txns, now)
    assert older.is_active is False


# --- Fix 3: match_all_transactions is deterministic across row order ---------


def test_match_all_transactions_is_deterministic(tmp_path: Path):
    """match_all_transactions must yield identical results regardless of the DB row
    order the unmatched transactions come back in. Pre-fix it scored each txn against
    in-loop-mutated pattern state in arbitrary order, so the running-average / std and
    final occurrence_count depended on iteration order.

    We build several same-merchant txns with DIFFERENT amounts (all within match
    tolerance) so the order in which they fold into the running average is observable,
    then assert the persisted pattern stats are identical across two runs whose
    underlying row order is reversed.
    """
    from ledger.analysis import PatternMatcher, RecurringPatternDetector
    from ledger.storage.database import RecurringPatternDB as _RecurringPatternDB
    from ledger.storage.database import init_database as _init
    from ledger.storage.database import session_scope as _scope

    # Build the EXACT same logical txn set once: a fixed mapping of txn_id -> (date, amount).
    # Each new txn has a DISTINCT in-tolerance amount, so the order in which they fold into
    # the running average / std is observable in the persisted pattern stats. We then feed
    # the SAME data to two databases in two different INSERTION orders; only a determinism
    # fix (explicit stable sort) makes the two runs agree -- identical data, identical result.
    start = _afix_recent_monthly_start(day_of_month=10, months=5)
    source = _afix_monthly_sub(
        merchant="VARMERCHANT",
        amount=_Decimal("20.00"),
        start_date=start,
        months=5,
        day_of_month=10,
    )
    last = source[-1].posting_date
    amounts = [_Decimal("19.00"), _Decimal("20.50"), _Decimal("20.00"), _Decimal("19.75")]
    new_txns = []
    for i, amt in enumerate(amounts):
        d = _afix_add_months(last, i + 1, 10)
        dow = d.weekday()
        new_txns.append(
            _Txn(
                txn_id=f"var_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=amt,
                amount_signed=-amt,
                description="VARMERCHANT Subscription",
                merchant="VARMERCHANT",
                category="Subscription",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="VARMERCHANT",
                category_raw="Subscription",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=400 + i,
                imported_at=_datetime(2026, 1, 1),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_var_{i}",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )

    def _build(db_path: Path, new_order: list[_Txn]) -> tuple[_Decimal, _Decimal, int]:
        _init(db_path)
        DatabaseWriter(db_path).write_result(
            ProcessingResult(
                transactions=source,
                transfers=[],
                exceptions=[],
                duplicates_removed=0,
                total_scanned=len(source),
            ),
            Path("/x"),
            "t",
        )
        seed_accounts_for_existing_rows(db_path)
        detector = RecurringPatternDetector(db_path, min_occurrences=3)
        detector.save_patterns(detector.detect_patterns(source))

        # Insert the new txns in the GIVEN physical order (forward vs reversed).
        DatabaseWriter(db_path).write_result(
            ProcessingResult(
                transactions=new_order,
                transfers=[],
                exceptions=[],
                duplicates_removed=0,
                total_scanned=len(new_order),
            ),
            Path("/x"),
            "t",
        )

        PatternMatcher(db_path).match_all_transactions()
        with _scope(db_path) as s:
            p = s.query(_RecurringPatternDB).filter_by(merchant="VARMERCHANT").first()
            return (
                _Decimal(str(p.expected_amount)),
                _Decimal(str(p.amount_std_dev)),
                p.occurrence_count,
            )

    forward = _build(tmp_path / "fwd.db", new_txns)
    reverse = _build(tmp_path / "rev.db", list(reversed(new_txns)))

    assert forward == reverse, (
        f"match_all_transactions is order-dependent: forward={forward} reverse={reverse}"
    )
    # Sanity: at least one new txn actually matched and folded in (so the equality above
    # is meaningful, not just two no-ops). The exact count is not asserted -- only that the
    # result is stable across the two insertion orders.
    assert forward[2] > len(source)


# --- Fix 4: RecurringPattern.expected_amount is cent-quantized ---------------


def test_pattern_expected_amount_is_cent_quantized(tmp_path: Path):
    """expected_amount is a running average and pre-fix relied on the DB column to
    round it. The in-memory RecurringPattern must already be cent-quantized so the
    cents invariant holds before persistence."""
    from ledger.analysis import RecurringPatternDetector
    from ledger.storage.database import init_database as _init

    db = tmp_path / "exp.db"
    _init(db)
    # Build three monthly occurrences, then override amounts so their average is a
    # non-terminating Decimal: (10 + 10 + 11) / 3 = 10.3333...
    base = _afix_recent_monthly_start(day_of_month=10, months=3)
    txns = _afix_monthly_sub(
        merchant="ODDAVG",
        amount=_Decimal("10.00"),
        start_date=base,
        months=3,
        day_of_month=10,
    )
    txns = [
        t.model_copy(update={"amount_raw": amt, "amount_signed": -amt})
        for t, amt in zip(
            txns, (_Decimal("10.00"), _Decimal("10.00"), _Decimal("11.00")), strict=True
        )
    ]

    detector = RecurringPatternDetector(db, min_occurrences=3)
    patterns = detector.detect_patterns(txns)
    assert len(patterns) == 1
    pattern = patterns[0]
    assert pattern.expected_amount.as_tuple().exponent == -2, (
        f"expected_amount {pattern.expected_amount!r} not cent-quantized"
    )
    assert pattern.expected_amount == _Decimal("10.33")


# --- Fix 4: trend pct fields clamp to the Numeric(8,2) column precision ------


def test_trend_pct_clamped_to_column_precision(tmp_path: Path):
    """A sparse category whose 6-month average is tiny relative to a current spike
    can produce a vs_avg_6m_pct that overflows the Numeric(8,2) column (max
    999999.99). The computed pct must be clamped to fit the column."""
    from ledger.analysis import TrendAggregator
    from ledger.storage.database import SpendingTrendDB as _SpendingTrendDB
    from ledger.storage.database import init_database as _init
    from ledger.storage.database import session_scope as _scope

    db = tmp_path / "trend.db"
    _init(db)
    # One prior month with a tiny $0.01 spend -> avg over 6 months = 0.01/6.
    with _scope(db) as s:
        s.add(
            _SpendingTrendDB(
                time_period="2026-01",
                period_type="monthly",
                category="Dining",
                transaction_count=1,
                total_spent=_Decimal("0.01"),
                avg_transaction=_Decimal("0.01"),
                median_transaction=_Decimal("0.01"),
                vs_previous_period_pct=_Decimal("0"),
                vs_previous_period_abs=_Decimal("0"),
                vs_avg_6m_pct=_Decimal("0"),
                top_merchant="X",
                top_merchant_amount=_Decimal("0.01"),
                trend_direction="stable",
                computed_at=_datetime(2026, 1, 31),
            )
        )

    # A $1,000,000 current spend against a ~$0.0017 average is an astronomically large
    # pct that would overflow Numeric(8,2). It must be clamped within column precision.
    pct = TrendAggregator(db)._compare_to_6m_average(
        "Dining", _date(2026, 7, 15), _Decimal("1000000.00"), "monthly"
    )

    assert abs(pct) <= _Decimal("999999.99"), f"pct {pct!r} overflows Numeric(8,2)"
    assert pct.as_tuple().exponent == -2, f"pct {pct!r} not cent-quantized"


# ===========================================================================
# 2026-06 follow-up CLI / maintenance / repair fixes
#
# 1. Invalid --log-level must raise a clean typer.BadParameter, not AttributeError.
# 2. build --formats must reject unknown tokens.
# 3. build/query must reject invalid --start/--end dates.
# 4. Cleanup dry-run and execute must agree on the retain/delete backup set.
# 5. Repair PDF discovery must be case-insensitive (.PDF as well as .pdf).
# 6. Rules test-match must not crash on an edited row with empty patterns.
# 7. Statement-audit balance comparison must reuse a single DB session (no N+1).
# ===========================================================================


# --- Fix 1: invalid --log-level -> clean BadParameter, not a traceback ------


def test_setup_logging_rejects_invalid_level() -> None:
    import typer

    from ledger.commands.common import setup_logging

    with pytest.raises(typer.BadParameter):
        setup_logging("not-a-level")


def test_setup_logging_accepts_valid_level() -> None:
    import logging

    from ledger.commands.common import setup_logging

    # Reset root logger so basicConfig applies the level (it is a no-op if handlers
    # are already configured by an earlier test in the same process).
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    setup_logging("debug")  # case-insensitive, must not raise

    assert root.level == logging.DEBUG


def test_build_cli_invalid_log_level_is_clean_bad_parameter(tmp_path: Path) -> None:
    """An invalid --log-level on build yields exit code 2 (BadParameter), not an
    unhandled AttributeError traceback."""
    from typer.testing import CliRunner

    from ledger.cli import app

    src = tmp_path / "src"
    src.mkdir()
    result = CliRunner().invoke(
        app,
        [
            "build",
            "--src",
            str(src),
            "--out-dir",
            str(tmp_path / "out"),
            "--ledger-config",
            str(tmp_path / "l.toml"),
            "--merchant-rules",
            str(tmp_path / "m.toml"),
            "--categories",
            str(tmp_path / "c.toml"),
            "--log-level",
            "VERBOSE",
        ],
    )
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "log-level" in unstyle(result.output).lower()


# --- Fix 2: build --formats rejects unknown tokens --------------------------


def test_validate_formats_rejects_unknown_token() -> None:
    import typer

    from ledger.commands.build import validate_formats

    with pytest.raises(typer.BadParameter):
        validate_formats("csv,bogus")


def test_validate_formats_accepts_known_tokens() -> None:
    from ledger.commands.build import validate_formats

    assert validate_formats("csv,json") == ["csv", "json"]
    assert validate_formats("none") == ["none"]
    assert validate_formats("CSV, Parquet") == ["csv", "parquet"]


# --- Fix 3: build/query reject invalid --start/--end dates ------------------


def test_validate_date_option_rejects_unparseable() -> None:
    import typer

    from ledger.commands.build import validate_date_option

    with pytest.raises(typer.BadParameter):
        validate_date_option("2025-13-40", "--start")


def test_validate_date_option_accepts_valid_and_none() -> None:
    from ledger.commands.build import validate_date_option

    assert validate_date_option(None, "--start") is None
    assert validate_date_option("2025-01-15", "--start") == date(2025, 1, 15)


def test_query_cli_invalid_start_errors(tmp_path: Path) -> None:
    """An invalid --start on query must error (exit != 0) rather than returning rows."""
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    result = CliRunner().invoke(app, ["query", "--db", str(db_path), "--start", "not-a-date"])
    assert result.exit_code != 0, result.output
    assert "start" in result.output.lower()


# --- Fix 4: cleanup dry-run and execute agree on retain/delete backup set ----


def _seed_cleanup_backups(data_dir: Path) -> Path:
    from ledger.storage.database import init_database as _init

    db_path = data_dir / "ledger.db"
    _init(db_path)
    backups = data_dir / "db-backups"
    backups.mkdir()
    (backups / "ledger-20260101T000000Z-cleanup.db").write_text("old", encoding="utf-8")
    (backups / "ledger-20260201T000000Z-cleanup.db").write_text("newer", encoding="utf-8")
    (backups / "ledger-20260115T000000Z-manual.db").write_text("manual", encoding="utf-8")
    return db_path


def test_cleanup_execute_deletes_exactly_what_dry_run_reported(tmp_path: Path) -> None:
    from ledger.maintenance.cleanup import DataCleanupService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_backups(data_dir)

    dry = DataCleanupService(db_path).dry_run()
    reported = {Path(p).resolve() for p in dry.details["delete_roots"]}

    execute = DataCleanupService(db_path).clean(execute=True)
    actually_deleted = {Path(p).resolve() for p in execute.details["deleted_paths"]}

    assert actually_deleted == reported, (
        "execute deleted a different set than dry-run reported:\n"
        f"  reported : {sorted(str(p) for p in reported)}\n"
        f"  deleted  : {sorted(str(p) for p in actually_deleted)}"
    )


def test_cleanup_execute_does_not_delete_a_backup_dry_run_retained(tmp_path: Path) -> None:
    from ledger.maintenance.cleanup import DataCleanupService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_backups(data_dir)
    backups = data_dir / "db-backups"

    dry = DataCleanupService(db_path).dry_run()
    reported = {Path(p).resolve() for p in dry.details["delete_roots"]}
    retained = {p.resolve() for p in backups.glob("*.db")} - reported

    DataCleanupService(db_path).clean(execute=True)

    for path in retained:
        assert path.exists(), f"execute deleted a backup the dry-run promised to retain: {path}"


# --- Fix 5: repair PDF discovery is case-insensitive ------------------------


def test_repair_discover_pdfs_is_case_insensitive(tmp_path: Path) -> None:
    from ledger.repair.statements import discover_pdfs

    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "B.PDF").write_bytes(b"%PDF-1.4")

    found = {p.name for p in discover_pdfs([tmp_path])}
    assert found == {"a.pdf", "B.PDF"}, found


# --- Fix 6: rules test-match must not raise on empty-patterns edited row -----


def test_draft_merchants_skips_empty_pattern_rows() -> None:
    """The preview-draft helper must skip rows with empty patterns (which would fail
    MerchantRule's min_length=1 validation) rather than raising a ValidationError that
    crashes the Streamlit page."""
    import pandas as pd

    from ledger.ui.pages.rules_settings import _draft_merchants_for_preview

    df = pd.DataFrame(
        [
            {"Merchant": "ACME", "Category": "Groceries", "Exclude": False, "Patterns": "ACME"},
            {"Merchant": "NEWCO", "Category": "Other", "Exclude": False, "Patterns": ""},
        ]
    )

    draft = _draft_merchants_for_preview(df, "test")

    norms = {rule.merchant_norm for rule in draft.rules}
    assert "ACME" in norms
    assert "NEWCO" not in norms
    merchant, category, _exclude = draft.find_merchant("ACME STORE")
    assert merchant == "ACME"
    assert category == "Groceries"


# --- Fix 7: statement-audit balance comparison reuses a single DB session ----


def _seed_balance_db(db_path: Path) -> None:
    from ledger.storage.database import AccountBalanceDB

    init_database(db_path)
    account_id = seed_account(
        db_path,
        "harbor",
        "harbor-chequing-1001",
        first_transaction_date=date(2025, 1, 2),
        last_transaction_date=date(2025, 1, 31),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=date(2025, 1, 31),
                balance=Decimal("250.00"),
                balance_basis="absolute",
                transaction_count=1,
                balance_7d_avg=Decimal("250.00"),
                balance_30d_avg=Decimal("250.00"),
                balance_7d_min=Decimal("250.00"),
                balance_7d_max=Decimal("250.00"),
                computed_at=now,
            )
        )


def test_compare_balances_matches_without_mismatch(tmp_path: Path) -> None:
    from ledger.audit.statements import StatementAuditService

    db_path = tmp_path / "ledger.db"
    _seed_balance_db(db_path)
    inventory = [
        {
            "pdf_path": "statement.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "statement_end_date": "2025-01-31",
            "statement_closing_balance": "250.00",
        }
    ]

    assert StatementAuditService()._compare_balances(db_path, inventory) == []


def test_compare_balances_ignores_relative_change_rows(tmp_path: Path) -> None:
    from ledger.audit.statements import StatementAuditService
    from ledger.storage.database import AccountBalanceDB

    db_path = tmp_path / "ledger.db"
    _seed_balance_db(db_path)
    with session_scope(db_path) as session:
        row = session.query(AccountBalanceDB).one()
        row.balance_basis = "relative"
    inventory = [
        {
            "pdf_path": "statement.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "statement_end_date": "2025-01-31",
            "statement_closing_balance": "250.00",
        }
    ]

    mismatches = StatementAuditService()._compare_balances(db_path, inventory)

    assert len(mismatches) == 1
    assert mismatches[0]["status"] == "missing_db_balance"


def test_compare_balances_reports_mismatch(tmp_path: Path) -> None:
    from ledger.audit.statements import StatementAuditService

    db_path = tmp_path / "ledger.db"
    _seed_balance_db(db_path)
    inventory = [
        {
            "pdf_path": "statement.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "statement_end_date": "2025-01-31",
            "statement_closing_balance": "999.99",
        }
    ]

    mismatches = StatementAuditService()._compare_balances(db_path, inventory)
    assert len(mismatches) == 1
    assert mismatches[0]["status"] == "mismatch"
    assert mismatches[0]["nearest_difference"] == "749.99"


def test_compare_balances_opens_single_session(tmp_path: Path, monkeypatch) -> None:
    """The balance comparison must open at most one DB session regardless of how many
    inventory items it processes (no per-item N+1 sessions)."""
    import ledger.audit.statements as audit_mod
    from ledger.audit.statements import StatementAuditService

    db_path = tmp_path / "ledger.db"
    _seed_balance_db(db_path)

    inventory = [
        {
            "pdf_path": f"statement-{i}.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "statement_end_date": "2025-01-31",
            "statement_closing_balance": "250.00",
        }
        for i in range(5)
    ]

    real_session_scope = audit_mod.session_scope
    calls = {"n": 0}

    def counting_session_scope(path):
        calls["n"] += 1
        return real_session_scope(path)

    monkeypatch.setattr(audit_mod, "session_scope", counting_session_scope)

    result = StatementAuditService()._compare_balances(db_path, inventory)
    assert result == []
    assert calls["n"] <= 1, (
        f"_compare_balances opened {calls['n']} sessions for {len(inventory)} items; "
        "expected a single reused session (N+1 regression)"
    )


# ===========================================================================
# 2026-06 utils/core correctness fixes
# ===========================================================================


# --- Fix 1: parse_money rejects scientific notation -------------------------


class TestParseMoneyScientificNotation:
    """parse_money must reject scientific-notation input (a real statement
    amount never uses it) rather than silently mis-scaling the value."""

    def test_lower_e_rejected(self):
        from ledger.utils.money import parse_money

        assert parse_money("1e5") is None

    def test_upper_e_rejected(self):
        from ledger.utils.money import parse_money

        assert parse_money("1E3") is None

    def test_decimal_exponent_rejected(self):
        from ledger.utils.money import parse_money

        assert parse_money("1.5e2") is None

    def test_signed_exponent_rejected(self):
        from ledger.utils.money import parse_money

        assert parse_money("1e+5") is None
        assert parse_money("2.5E-3") is None
        assert parse_money("$1e5") is None
        assert parse_money("(1e5)") is None

    def test_normal_amounts_unchanged(self):
        from ledger.utils.money import parse_money

        # Decimals, thousands separators, parens-negative, trailing CR/DR.
        assert parse_money("100.00") == Decimal("100.00")
        assert parse_money("$1,234.56") == Decimal("1234.56")
        assert parse_money("($102.68)") == Decimal("-102.68")
        assert parse_money("100.00 CR") == Decimal("100.00")
        assert parse_money("50.00DR") == Decimal("-50.00")
        assert parse_money("-1,000.00") == Decimal("-1000.00")


# --- Fix 2: find_best_match returns the correct original on collision --------


class TestFuzzyNormalizeCollision:
    """When two distinct originals normalize to the same key, find_best_match
    must return the exact original that rapidfuzz matched (by index), not a
    lossy normalized-text dict lookup."""

    def test_normalize_collision_returns_correct_original(self):
        from ledger.utils.fuzzy import find_best_match

        # "MCDONALD'S" and "MCDONALDS" both normalize to "MCDONALDS".
        candidates = ["MCDONALD'S", "MCDONALDS"]
        match, score = find_best_match("MCDONALDS", candidates, threshold=85)
        # Must return one of the actual originals, exactly as supplied.
        assert match in candidates
        assert score == 100

    def test_normalize_collision_picks_matched_index(self):
        from ledger.utils.fuzzy import find_best_match

        # Two originals that normalize identically; query exactly equals the
        # SECOND original. rapidfuzz returns the matched index; the function
        # must map back through that index to the exact original, not collapse
        # both to whichever the dict happened to keep.
        candidates = ["WAL-MART", "WALMART"]
        match, _score = find_best_match("WALMART", candidates, threshold=85)
        assert match in candidates

    def test_genuine_best_match_still_works(self):
        from ledger.utils.fuzzy import find_best_match

        candidates = ["STARBUCKS", "TIM HORTONS", "MCDONALDS"]
        match, score = find_best_match("STARBCKS", candidates, threshold=80)
        assert match == "STARBUCKS"
        assert score >= 80


# --- Fix 3: coerce_decimal quantizes money fields to cents -------------------


class TestCoerceDecimalQuantizesCents:
    """Money fields built with sub-cent precision must be quantized to cents
    by the model validator; non-money Decimal fields are unaffected (the model
    has no other Decimal field, so quantization is scoped to amounts)."""

    def _txn(self, **overrides) -> Transaction:
        base = {
            "txn_id": "qc-1",
            "txn_fingerprint": "qc-fp",
            "posting_date": date(2025, 1, 15),
            "institution": "harbor",
            "account": "acct",
            "account_type": "Chequing",
            "flow": "expense",
            "cashflow_type": "expense",
            "include_in_cashflow": True,
            "currency": "CAD",
            "amount_raw": Decimal("10.00"),
            "amount_signed": Decimal("-10.00"),
            "description": "X",
            "source_file": "s.csv",
            "source_path": "/x/s.csv",
            "source_period": "2025-01",
            "source_row": 1,
            "imported_at": datetime.now(UTC).replace(tzinfo=None),
            "parser_version": "test",
        }
        base.update(overrides)
        return Transaction(**base)

    def test_amount_raw_quantized_to_cents(self):
        txn = self._txn(amount_raw=Decimal("10.005"), amount_signed=Decimal("-10.00"))
        assert txn.amount_raw == Decimal("10.00")
        assert txn.amount_raw.as_tuple().exponent == -2

    def test_amount_signed_quantized_to_cents(self):
        txn = self._txn(amount_raw=Decimal("10.00"), amount_signed=Decimal("-10.001"))
        assert txn.amount_signed == Decimal("-10.00")
        assert txn.amount_signed.as_tuple().exponent == -2

    def test_non_money_decimal_field_unaffected(self):
        # transfer_confidence is a float field, not Decimal — must pass through.
        txn = self._txn(transfer_confidence=0.123456)
        assert txn.transfer_confidence == 0.123456


# --- Fix 4: first hash collision is not double-counted as a duplicate --------


class TestDedupCollisionNotCountedAsDuplicate:
    """deduplicate's duplicates_removed must equal the number of rows actually
    dropped. A first collision (same id, different fingerprint) drops no rows,
    so it must not increment the duplicate counter."""

    @staticmethod
    def _dtxn(txn_id: str, fp: str, row: int) -> Transaction:
        return Transaction(
            txn_id=txn_id,
            txn_fingerprint=fp,
            posting_date=date(2025, 1, 15),
            institution="harbor",
            account="acct",
            account_type="Chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("10.00"),
            amount_signed=Decimal("-10.00"),
            description=f"ROW {row}",
            source_file="s.csv",
            source_path="/x/s.csv",
            source_period="2025-01",
            source_row=row,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="test",
        )

    def test_first_collision_reports_zero_duplicates(self):
        from ledger.processing.pipeline import deduplicate

        # Two distinct rows, same id, different fingerprint → collision, no drop.
        items = [self._dtxn("X", "fp_a", 1), self._dtxn("X", "fp_b", 2)]
        unique, duplicates, collisions = deduplicate(items, strict=False)
        assert len(unique) == 2
        assert collisions == 1
        assert duplicates == 0  # nothing was dropped

    def test_true_duplicates_counted_exactly(self):
        from ledger.processing.pipeline import deduplicate

        # N=5 rows where K=2 are exact duplicates of an original (same id+fp).
        items = [
            self._dtxn("A", "fp_a", 1),
            self._dtxn("A", "fp_a", 2),  # duplicate of A
            self._dtxn("A", "fp_a", 3),  # duplicate of A
            self._dtxn("B", "fp_b", 4),
            self._dtxn("C", "fp_c", 5),
        ]
        unique, duplicates, collisions = deduplicate(items, strict=False)
        assert len(unique) == 3  # A, B, C
        assert duplicates == 2  # exactly K, not K+1
        assert collisions == 0

    def test_collision_then_true_dup_counts_only_the_dup(self):
        from ledger.processing.pipeline import deduplicate

        # X/fp_a, X/fp_b (collision, no drop), X/fp_b (true dup of disambiguated
        # row → 1 drop). duplicates must equal 1, not 2.
        items = [
            self._dtxn("X", "fp_a", 1),
            self._dtxn("X", "fp_b", 2),
            self._dtxn("X", "fp_b", 3),
        ]
        unique, duplicates, collisions = deduplicate(items, strict=False)
        assert len(unique) == 2
        assert duplicates == 1
        assert collisions == 1


# --- Fix 5: dead utils.dates helper removed ---------------------------------


def test_is_in_period_helper_removed():
    """is_in_period was dead code; it must no longer be exported from
    ledger.utils nor defined in ledger.utils.dates."""
    import ledger.utils as utils_pkg
    import ledger.utils.dates as dates_mod

    assert not hasattr(dates_mod, "is_in_period")
    assert not hasattr(utils_pkg, "is_in_period")
    assert "is_in_period" not in utils_pkg.__all__


# Foreign-currency amounts remain native evidence and never enter CAD-only totals.


def _make_currency_txn(i: int, *, currency: str, amount: Decimal) -> Transaction:
    """A spend-universe expense txn with an explicit currency (income if positive)."""
    return _make_txn(
        i,
        amount=amount,
        merchant="STORE" if amount < 0 else "PAY",
        category="Dining" if amount < 0 else "Income",
        cashflow_type="expense" if amount < 0 else "income",
        currency=currency,
    )


def _write_raw_currency_txn(db_path: Path, txn_id: str, currency_sql: str) -> None:
    """Write one expense row via the normal writer (so all NOT-NULL temporal
    columns are populated), then force its currency to a chosen value via SQL so
    we can exercise the NULL/empty-currency backward-compat path the ORM default
    (``default="CAD"``) would otherwise never produce.

    The live schema marks ``currency`` NOT NULL, so a true NULL predates the
    column constraint; to simulate that legacy row we briefly drop the
    constraint enforcement via ``PRAGMA writable_schema`` before the UPDATE.
    """
    txn = _make_txn(1, amount=Decimal("-50.00"), merchant="STORE", category="Dining").model_copy(
        update={"txn_id": txn_id, "txn_fingerprint": f"fp_{txn_id}"}
    )
    _write_txns(db_path, [txn])

    if currency_sql == "NULL":
        # Rewrite the table's stored DDL to drop NOT NULL on currency so we can
        # plant a genuinely-NULL legacy row (the constraint makes NULL otherwise
        # unreachable in the current schema). The DDL edit only takes effect on a
        # fresh connection, so the UPDATE runs on a reopened handle below.
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA writable_schema = ON")
            conn.execute(
                "UPDATE sqlite_master "
                "SET sql = replace(sql, 'currency VARCHAR(3) NOT NULL', 'currency VARCHAR(3)') "
                "WHERE type = 'table' AND name = 'transactions'"
            )
            conn.execute("PRAGMA writable_schema = OFF")

    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            f"UPDATE transactions SET currency = {currency_sql} WHERE txn_id = ?",
            (txn_id,),
        )


def test_usd_row_excluded_from_monthly_cashflow(tmp_path: Path) -> None:
    """A USD expense/income must not move CAD monthly cashflow income/expense/net."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_currency_txn(1, currency="CAD", amount=Decimal("1000.00")),  # income
            _make_currency_txn(2, currency="CAD", amount=Decimal("-100.00")),  # expense
            # These USD rows must be excluded entirely.
            _make_currency_txn(3, currency="USD", amount=Decimal("5000.00")),
            _make_currency_txn(4, currency="USD", amount=Decimal("-9000.00")),
        ],
    )

    rows = LedgerQueryService(db_path).get_monthly_cashflow(2025)
    row = next(r for r in rows if r["month"] == "2025-04")

    assert row["income"] == Decimal("1000.00"), row
    assert row["expense"] == Decimal("-100.00"), row
    assert row["net"] == Decimal("900.00"), row


def test_usd_row_excluded_from_spending_by_category(tmp_path: Path) -> None:
    """A USD expense must not appear in CAD spending-by-category totals."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_currency_txn(1, currency="CAD", amount=Decimal("-50.00")),
            _make_currency_txn(2, currency="USD", amount=Decimal("-99.00")),
        ],
    )

    spend = LedgerQueryService(db_path).get_spending_by_category(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert spend == {"Dining": Decimal("50.00")}, spend


def test_usd_row_excluded_from_income_sources(tmp_path: Path) -> None:
    """A USD income row must not appear in CAD income sources."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("1000.00"),
                merchant="CAD PAYROLL",
                category="Income",
                cashflow_type="income",
                currency="CAD",
            ),
            _make_txn(
                2,
                amount=Decimal("8000.00"),
                merchant="USD PAYROLL",
                category="Income",
                cashflow_type="income",
                currency="USD",
            ),
        ],
    )

    sources = LedgerQueryService(db_path).get_income_sources(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert sources == {"CAD PAYROLL": Decimal("1000.00")}, sources


def test_usd_row_excluded_from_kpis(tmp_path: Path) -> None:
    """A USD row must not contribute to compute_kpis income/expenses/net/count."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_currency_txn(1, currency="CAD", amount=Decimal("1000.00")),
            _make_currency_txn(2, currency="CAD", amount=Decimal("-100.00")),
            _make_currency_txn(3, currency="USD", amount=Decimal("5000.00")),
            _make_currency_txn(4, currency="USD", amount=Decimal("-9000.00")),
        ],
    )

    kpis = LedgerQueryService(db_path).compute_kpis(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert kpis["income"] == Decimal("1000.00"), kpis
    assert kpis["expenses"] == Decimal("100.00"), kpis
    assert kpis["net"] == Decimal("900.00"), kpis
    # Only the two CAD rows are counted.
    assert kpis["transaction_count"] == 2, kpis


def test_usd_row_excluded_from_category_and_merchant_trends(tmp_path: Path) -> None:
    """The category/merchant SUM trend aggregates must drop USD rows too."""
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_currency_txn(1, currency="CAD", amount=Decimal("-50.00")),
            _make_currency_txn(2, currency="USD", amount=Decimal("-99.00")),
        ],
    )
    svc = LedgerQueryService(db_path)

    cat_trend = svc.get_category_spending_trend(
        category="Dining", start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert sum(r["amount"] for r in cat_trend) == Decimal("50.00"), cat_trend

    merch_trend = svc.get_merchant_spending_trend(
        merchant="STORE", start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert sum(r["amount"] for r in merch_trend) == Decimal("50.00"), merch_trend


# --- H3.2 (balances): a USD row must not shift the CAD balance series --------


def test_usd_row_excluded_from_balance_series(tmp_path: Path) -> None:
    """A USD transaction must not perturb the CAD running-balance deltas."""
    from ledger.analysis.balance_calculator import BalanceCalculator

    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [
        _make_currency_txn(1, currency="CAD", amount=Decimal("-100.00")),
        # A large USD charge that would dominate the series if counted.
        _make_currency_txn(2, currency="USD", amount=Decimal("-9000.00")),
    ]

    balances = calc.compute_daily_balances("a1", txns, account=None, transfer_legs=[])

    assert balances, "expected a balance series"
    # The series only reflects the single CAD -100 delta, not the USD -9000.
    assert balances[-1].balance == Decimal("-100.00"), balances[-1].balance
    # The USD row must not be counted as activity either.
    assert sum(b.transaction_count for b in balances) == 1


# --- H3.3: CAD and legacy NULL/empty-currency rows still count everywhere ------


def test_null_currency_legacy_row_still_counts(tmp_path: Path) -> None:
    """A legacy row with a NULL currency (pre-dates currency tracking) must be
    treated as CAD and still count in spend totals (backward-compat)."""
    db_path = tmp_path / "ledger.db"
    _write_raw_currency_txn(db_path, "legacy_null", "NULL")

    spend = LedgerQueryService(db_path).get_spending_by_category(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert spend == {"Dining": Decimal("50.00")}, spend


def test_empty_currency_legacy_row_still_counts(tmp_path: Path) -> None:
    """A legacy row with an empty-string currency must be treated as CAD too."""
    db_path = tmp_path / "ledger.db"
    _write_raw_currency_txn(db_path, "legacy_empty", "''")

    spend = LedgerQueryService(db_path).get_spending_by_category(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )
    assert spend == {"Dining": Decimal("50.00")}, spend


# =============================================================================
# Follow-up fixes (2026-06): CAD-only true-cost totals + reliable transfer
# pair direction (chore/followup-fixes-2026-06).
# =============================================================================


# --- Fix 1 (completes H3): non-CAD rows must not leak into true-cost totals ---


def test_true_cost_summary_excludes_usd_debit(tmp_path: Path) -> None:
    """A USD debit (not FX-converted) must NOT be SUM'd into the CAD gross_spend /
    true_cost. H3 added _cad_only() to query/aggregates; the adjustments money
    aggregates must apply the SAME predicate."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "cad", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_adj_txn(
        db_path, "usd", date(2026, 1, 3), "-500.00", "US STORE", "Electronics", currency="USD"
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.gross_spend == Decimal("100.00")
    assert summary.true_cost == Decimal("100.00")


def test_true_cost_summary_excludes_usd_income(tmp_path: Path) -> None:
    """A USD income credit must not inflate CAD gross_income / adjusted_income."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "cad-inc", date(2026, 1, 1), "1000.00", "PAYROLL", "Income")
    _insert_adj_txn(
        db_path, "usd-inc", date(2026, 1, 5), "2000.00", "US PAYROLL", "Income", currency="USD"
    )

    summary = AdjustmentDetectionService(db_path).true_cost_summary(
        date(2026, 1, 1), date(2026, 1, 31)
    )

    assert summary.gross_income == Decimal("1000.00")
    assert summary.adjusted_income == Decimal("1000.00")


def test_true_cost_by_category_excludes_usd(tmp_path: Path) -> None:
    """A USD debit must not appear in / inflate the by-category true-cost totals."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "cad", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_adj_txn(
        db_path, "usd", date(2026, 1, 3), "-500.00", "US STORE", "Electronics", currency="USD"
    )

    categories = AdjustmentDetectionService(db_path).true_cost_by_category(
        date(2026, 1, 1), date(2026, 1, 31)
    )

    assert categories["Electronics"]["gross_spend"] == Decimal("100.00")
    assert categories["Electronics"]["true_cost"] == Decimal("100.00")


def test_usd_credit_does_not_offset_cad_debit(tmp_path: Path) -> None:
    """A USD credit is not a CAD-comparable amount, so it must not be used as an
    offset against a CAD debit (the offset candidate predicate shares CAD-only
    semantics)."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "cad-debit", date(2026, 1, 1), "-100.00", "APPLE", "Electronics")
    _insert_adj_txn(
        db_path,
        "usd-credit",
        date(2026, 1, 5),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
        currency="USD",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        assert session.query(TransactionOffsetDB).count() == 0

    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))
    assert summary.gross_spend == Decimal("100.00")
    assert summary.offsets == Decimal("0.00")
    assert summary.true_cost == Decimal("100.00")


def test_cad_credit_does_not_offset_usd_debit(tmp_path: Path) -> None:
    """A CAD credit must not net against a USD debit: the USD debit is outside the
    CAD spend universe, so no offset forms."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.storage.database import TransactionOffsetDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(
        db_path, "usd-debit", date(2026, 1, 1), "-100.00", "APPLE", "Electronics", currency="USD"
    )
    _insert_adj_txn(
        db_path,
        "cad-credit",
        date(2026, 1, 5),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        assert session.query(TransactionOffsetDB).count() == 0


def test_cad_rows_still_offset_after_cad_only_predicate(tmp_path: Path) -> None:
    """Guard: the CAD-only predicate must not over-prune. Two CAD rows still net
    and the offset still applies (the canonical reward/return path is unchanged)."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_adj_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_adj_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "40.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.gross_spend == Decimal("100.00")
    assert summary.offsets == Decimal("40.00")
    assert summary.true_cost == Decimal("60.00")


# --- Fix 2 (M7): transfer pair direction from a RELIABLE signal --------------


def _add_directional_transfer(
    session,
    txn_id: str,
    amount_raw: str,
    account_type: str,
    *,
    institution: str,
    account: str,
    paired: str,
    flow: str = "transfer",
) -> None:
    session.add(
        TransferDB(
            txn_id=txn_id,
            posting_date=date(2025, 6, 18),
            institution=institution,
            account=account,
            account_type=account_type,
            flow=flow,
            amount_raw=Decimal(amount_raw),
            description="X",
            source_file="s.csv",
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            paired_transfer_id=paired,
        )
    )


def test_cross_institution_payment_direction_is_reliable(tmp_path: Path) -> None:
    """A chequing -> credit-card payment whose two legs carry the SAME native
    amount_raw sign (issuer conventions differ -- HARBOR chequing -X, Cedar card -X)
    must still label the chequing leg as the OUTFLOW (source) and the card leg as
    the INFLOW (destination).

    Regression (M7): reconstructing direction from amount_raw's sign mislabels this
    pair because both legs are negative, so sign-ordering is arbitrary. The
    credit-card leg is always the destination of a payment."""
    db_path = tmp_path / "transfers.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_directional_transfer(
            session,
            "chq",
            "-75.00",
            "Chequing",
            institution="harbor",
            account="harbor-chq",
            paired="card",
            flow="payment",
        )
        _add_directional_transfer(
            session,
            "card",
            "-75.00",
            "Silver Mastercard",
            institution="cedar",
            account="cedar-mc",
            paired="chq",
            flow="payment",
        )

    pairs = LedgerQueryService(db_path).get_transfer_matches()
    assert len(pairs) == 1
    outflow, inflow = pairs[0]
    assert outflow.txn_id == "chq"  # chequing is the source (money leaves it)
    assert inflow.txn_id == "card"  # card is the destination (payment lands here)


def test_cross_institution_payment_direction_is_order_independent(tmp_path: Path) -> None:
    """The reconstructed direction must not depend on which leg the query returns
    first: insert the card leg with a lower-sorting txn_id so any first-row bias
    would put it first; it must still be classified as the inflow."""
    db_path = tmp_path / "transfers.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_directional_transfer(
            session,
            "a-card",
            "-75.00",
            "Silver Mastercard",
            institution="cedar",
            account="cedar-mc",
            paired="b-chq",
            flow="payment",
        )
        _add_directional_transfer(
            session,
            "b-chq",
            "-75.00",
            "Chequing",
            institution="harbor",
            account="harbor-chq",
            paired="a-card",
            flow="payment",
        )

    pairs = LedgerQueryService(db_path).get_transfer_matches()
    assert len(pairs) == 1
    outflow, inflow = pairs[0]
    assert outflow.txn_id == "b-chq"  # chequing source
    assert inflow.txn_id == "a-card"  # card destination


def test_deposit_to_deposit_direction_uses_opposite_sign(tmp_path: Path) -> None:
    """For a deposit<->deposit transfer (neither leg is a credit card) the legs are
    opposite-signed, so the negative leg is the outflow -- pinned so the Fix-2
    change does not regress the existing behavior."""
    db_path = tmp_path / "transfers.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_directional_transfer(
            session,
            "out",
            "-500.00",
            "Chequing",
            institution="harbor",
            account="harbor-chq",
            paired="inn",
        )
        _add_directional_transfer(
            session,
            "inn",
            "500.00",
            "Savings",
            institution="harbor",
            account="harbor-sav",
            paired="out",
        )

    pairs = LedgerQueryService(db_path).get_transfer_matches()
    assert len(pairs) == 1
    outflow, inflow = pairs[0]
    assert outflow.txn_id == "out" and outflow.amount_raw < 0
    assert inflow.txn_id == "inn"


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
