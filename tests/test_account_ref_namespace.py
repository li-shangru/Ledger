"""One opaque account-reference namespace across every surface.

Fixtures vary parser labels, stored account types and free-text transaction types;
all must resolve through the account dimension to the same salted reference."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_UNCOMPUTED_CARD_NUMBER,
    PRIVATE_UNLIMITED_CARD_NUMBER,
    build_synthetic_mcp_database,
)

from ledger.exporting import ExportPackageService
from ledger.mcp.contracts import SERVER_INSTRUCTIONS
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import account_ref_map, banking_account_ref, unresolved_account_ref
from ledger.storage.database import AccountDB, StatementCoverageDB, TransactionDB, session_scope

_AS_OF = "2025-12-31"
# The sentinel namespace, spelled out here rather than imported, so a change to
# the minting code has to be restated in the test that pins it.
_UNRESOLVED_REF = re.compile(r"unresolved-[0-9a-f]{10}")

# A slug the `accounts` dimension has no row for. `statement_coverage` carries no
# foreign key -- rows land at PDF finalize, before account detection has seen a
# newly-imported account -- so this is a state production reaches, not a
# hypothetical.
UNJOINABLE_SLUG = "synthetic-private-number-0000"
SECOND_UNJOINABLE_SLUG = "synthetic-private-number-1111"


def _seed_transaction(
    db_path: Path,
    *,
    label: str,
    account: str,
    account_type: str,
    institution: str = CARD_INSTITUTION,
    posted: date = date(2025, 6, 15),
    amount: int = 10,
    exclude_from_spending: bool = False,
) -> None:
    """Insert one expense row on an arbitrary slug/type pair."""
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=label.ljust(40, "0"),
                posting_date=posted,
                transaction_date=posted,
                institution=institution,
                account=account,
                account_type=account_type,
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=exclude_from_spending,
                currency="CAD",
                amount_raw=amount,
                amount_signed=-amount,
                description="SYNTHETIC CARD ROW",
                merchant="SYNTHETIC CARD",
                category="Shopping",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
                parser_version="synthetic",
                txn_fingerprint=label.ljust(32, "0")[:32],
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _package_files(db_path: Path, dest_dir: Path, preset: str) -> dict[str, str]:
    result = ExportPackageService(db_path).create_package(
        dest_dir=dest_dir,
        preset=preset,
        export_format="zip",
    )
    with zipfile.ZipFile(result.path) as package:
        return {name: package.read(name).decode("utf-8") for name in package.namelist()}


def _rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content)))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


@contextmanager
def _open(db_path: Path) -> Iterator[LedgerMCPService]:
    """The fixture's lifetime, for a test that must seed the DB before opening."""
    built = LedgerMCPService(db_path)
    try:
        yield built
    finally:
        built.close()


def _entries(result: dict[str, object]) -> list[dict[str, object]]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]["entries"]  # type: ignore[index]


def _coverage_refs(service: LedgerMCPService) -> set[str]:
    """Every ref coverage emitted, unfiltered.

    Nothing is dropped for being null, because nothing may be null any more: a
    filter here is what would let a resolver that regressed to all-null refs pass
    a subset assertion against the empty set.
    """
    entries = _entries(call_tool(service, "ledger.coverage", {"limit": 100}))
    return {str(entry["account_ref"]) for entry in entries}


def _balances_refs(service: LedgerMCPService) -> set[str]:
    entries = _entries(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))
    return {str(entry["account_ref"]) for entry in entries}


def _projection_refs(service: LedgerMCPService) -> set[str]:
    entries = _entries(
        call_tool(
            service,
            "ledger.projection",
            {"as_of": _AS_OF, "target_date": _AS_OF, "limit": 100},
        )
    )
    return {str(entry["account_ref"]) for entry in entries}


def test_projection_emits_the_same_refs_balances_does(service: LedgerMCPService) -> None:
    """One account, one reference, on every tool that emits one.

    Asserted by VALUE rather than by shape: `ledger.projection` reads the same
    `accounts` rows `ledger.balances` reads, so a locally derived ref here would
    still look like a well-formed `acct-` value while joining to nothing.
    """
    projection_refs = _projection_refs(service)

    assert projection_refs, "fixture seeded no accounts"
    assert projection_refs == _balances_refs(service)


def test_coverage_and_balances_emit_the_same_ref_for_one_account(
    service: LedgerMCPService,
) -> None:
    coverage_refs = _coverage_refs(service)
    balances_refs = _balances_refs(service)

    assert coverage_refs, "fixture seeded no coverage rows"
    assert coverage_refs <= balances_refs, (
        f"disjoint namespaces: coverage={sorted(coverage_refs)} balances={sorted(balances_refs)}"
    )


def test_coverage_refs_are_minted_from_the_account_record_not_the_statement_type(
    service: LedgerMCPService,
) -> None:
    """The exact values, so a passing set comparison cannot hide a shared error."""
    assert _coverage_refs(service) == {
        banking_account_ref(ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"),
        banking_account_ref(ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"),
    }


def _seed_coverage(db_path: Path, *, slug: str, period: str) -> None:
    """One coverage row on a slug the `accounts` dimension may have no row for."""
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug=slug,
                period=period,
                institution=BANK_INSTITUTION,
                statement_type="synthetic_deposit",
                row_count=2,
                statement_count=1,
                skipped_reason="",
                imported_at=datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            )
        )


def test_a_coverage_row_with_no_account_record_mints_a_sentinel_and_says_why(
    db_path: Path,
) -> None:
    """D3: never a locally derived resolved-namespace ref -- a sentinel and a reason."""
    _seed_coverage(db_path, slug=UNJOINABLE_SLUG, period="2025-05")
    service = LedgerMCPService(db_path)
    try:
        entries = _entries(call_tool(service, "ledger.coverage", {"limit": 100}))
    finally:
        service.close()

    orphan = [entry for entry in entries if entry["period"] == "2025-05"]
    assert len(orphan) == 1
    assert orphan[0]["account_ref_status"] == "no_account_record"
    ref = str(orphan[0]["account_ref"])
    assert _UNRESOLVED_REF.fullmatch(ref), ref
    # And every joinable row still resolves, so the reason is a per-row fact and
    # not a whole-surface degradation.
    assert {str(entry["account_ref_status"]) for entry in entries} == {
        "resolved",
        "no_account_record",
    }


def test_two_unjoinable_coverage_rows_do_not_share_one_key(db_path: Path) -> None:
    """The wire had the defect the export already fixed: a shared blank key.

    Two coverage rows for two DIFFERENT accounts the `accounts` dimension has no
    row for, at the same institution, statement_type and period, emitted two
    byte-identical entries -- `{"account_ref": null, "account_ref_status":
    "no_account_record", ...}` twice -- so any consumer that groups by ref merged
    two real accounts' statements into one. `exporting/service.py` mints a salted
    `unresolved-<10hex>` sentinel for exactly this case and its docstring rejects
    the blank-key alternative; D3 requires the two surfaces to agree.
    """
    _seed_coverage(db_path, slug=UNJOINABLE_SLUG, period="2025-05")
    _seed_coverage(db_path, slug=SECOND_UNJOINABLE_SLUG, period="2025-05")

    service = LedgerMCPService(db_path)
    try:
        entries = _entries(call_tool(service, "ledger.coverage", {"limit": 100}))
    finally:
        service.close()

    orphans = [entry for entry in entries if entry["account_ref_status"] == "no_account_record"]
    assert len(orphans) == 2, entries
    refs = [str(orphan["account_ref"]) for orphan in orphans]
    assert len(set(refs)) == 2, f"two unjoinable accounts collapsed onto one key: {orphans}"
    # A visibly distinct namespace, so it can never be joined against a resolved
    # ref by mistake -- and never a raw slug or a blank.
    for ref in refs:
        assert _UNRESOLVED_REF.fullmatch(ref), ref
        assert not ref.startswith("acct-"), ref
        assert UNJOINABLE_SLUG not in ref and SECOND_UNJOINABLE_SLUG not in ref, ref
    with _open(db_path) as reopened:
        resolved = {
            str(entry["account_ref"])
            for entry in _entries(call_tool(reopened, "ledger.coverage", {"limit": 100}))
            if entry["account_ref_status"] == "resolved"
        }
    assert resolved, "the joinable coverage rows stopped resolving"
    assert set(refs).isdisjoint(resolved)


def test_the_coverage_sentinel_is_the_one_the_export_mints_for_that_account(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """One derivation, so the wire and the file cannot drift apart.

    Both surfaces mint through `query.account_refs.unresolved_account_ref` under
    the same per-database salt, so an unjoinable account carries ONE sentinel
    everywhere -- the same guarantee the resolved namespace already makes.
    """
    _seed_coverage(db_path, slug=UNJOINABLE_SLUG, period="2025-05")
    _seed_transaction(
        db_path, label="orphan-one", account=UNJOINABLE_SLUG, account_type="Synthetic Card"
    )

    service = LedgerMCPService(db_path)
    try:
        entries = _entries(call_tool(service, "ledger.coverage", {"limit": 100}))
    finally:
        service.close()
    from_wire = {
        str(entry["account_ref"])
        for entry in entries
        if entry["account_ref_status"] == "no_account_record"
    }

    files = _package_files(db_path, tmp_path / "exports", "full-local-audit")
    from_file = {
        row["account_ref"]
        for row in _rows(files["transactions.csv"])
        if row["account_ref_status"] == "no_account_record"
    }

    assert from_wire == from_file == {unresolved_account_ref(ACCOUNT_REF_SALT, UNJOINABLE_SLUG)}


def test_balances_never_emits_an_unresolved_ref(db_path: Path) -> None:
    """`ledger.balances` cannot reach the unjoinable case, and that is structural.

    Its entries ARE `accounts` rows -- it queries `AccountDB` and pages that
    result -- and `account_ref_map` is built by iterating the same table on the
    same key, so every lookup resolves by construction. This asserts the
    invariant rather than trusting the reasoning: a future page that sourced its
    rows from anywhere else would surface here instead of raising a KeyError in
    front of an operator.
    """
    _seed_coverage(db_path, slug=UNJOINABLE_SLUG, period="2025-05")
    _seed_transaction(
        db_path, label="orphan-one", account=UNJOINABLE_SLUG, account_type="Synthetic Card"
    )

    with _open(db_path) as service:
        refs = _balances_refs(service)

    assert refs, "fixture seeded no balance entries"
    assert all(ref.startswith("acct-") for ref in refs), sorted(refs)
    assert not any(_UNRESOLVED_REF.fullmatch(ref) for ref in refs), sorted(refs)


def _seed_three_spellings_of_one_card(db_path: Path) -> None:
    """One physical card whose statements spelled its type three ways."""
    for index, spelling in enumerate(
        ("Synthetic Card", "Synthetic Cardj", "Synthetic Card Terms And Conditions")
    ):
        _seed_transaction(
            db_path,
            label=f"corrupted-account-type-{index:02d}",
            account=PRIVATE_CARD_NUMBER,
            account_type=spelling,
        )


def test_the_export_resolves_through_accounts_instead_of_the_free_text_account_type(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """One physical card whose statements spelled its type three ways is ONE ref.

    `transactions.account_type` is uncontrolled PDF text. Hashing the synthetic
    variants would split a single card into one reference per spelling.
    """
    _seed_three_spellings_of_one_card(db_path)

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    exported = {row["account_ref"] for row in _rows(files["monthly_trend.csv"])}

    card_ref = banking_account_ref(
        ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"
    )
    assert card_ref in exported
    # The three spellings collapse to the one ref, and it is the SAME ref the
    # read-only surfaces emit for that account.
    service = LedgerMCPService(db_path)
    try:
        assert card_ref in _balances_refs(service)
    finally:
        service.close()


def test_one_physical_card_yields_one_account_ref_across_account_type_spellings(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Spec §5: `transactions.account_type` is never an identity input ANYWHERE.

    The card export consumes reporting's canonical account_id, resolves it to the
    same salted account_ref as the other account-bearing surfaces, and drops both
    the raw id and descriptive account labels.
    """
    _seed_three_spellings_of_one_card(db_path)

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    rows = _rows(files["card_account_summary.csv"])

    # The snapshot window (the 90 days ending at the fixture's last transaction)
    # contains exactly these three spend rows, so one physical card must produce
    # exactly one row carrying all three.
    refs = [row["account_ref"] for row in rows]
    assert len(refs) == len(set(refs)), f"one card split across rows: {refs}"
    assert len(rows) == 1, rows
    assert int(rows[0]["transaction_count"]) == 3, rows
    assert rows[0]["account_ref_status"] == "resolved"
    assert refs == [
        banking_account_ref(
            ACCOUNT_REF_SALT,
            CARD_INSTITUTION,
            PRIVATE_CARD_NUMBER,
            "credit_card",
        )
    ]


def test_two_accounts_under_one_free_text_group_are_not_published_as_one_row(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Canonical identities keep same-type accounts in separate resolved rows."""
    shared_type = "Synthetic Shared Spelling"
    _seed_transaction(
        db_path,
        label="merged-card-one",
        account=PRIVATE_CARD_NUMBER,
        account_type=shared_type,
        amount=400,
    )
    _seed_transaction(
        db_path,
        label="merged-card-two",
        account=PRIVATE_UNCOMPUTED_CARD_NUMBER,
        account_type=shared_type,
        amount=400,
    )
    _seed_transaction(
        db_path,
        label="solo-card",
        account=PRIVATE_UNLIMITED_CARD_NUMBER,
        account_type="Synthetic Solo Card",
        amount=40,
    )

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    rows = _rows(files["card_account_summary.csv"])
    refs = {row["account_ref"] for row in rows}
    assert len(rows) == 3, rows
    assert len(refs) == 3, rows
    assert {row["account_ref_status"] for row in rows} == {"resolved"}
    assert {int(row["transaction_count"]) for row in rows} == {1}
    assert {
        banking_account_ref(
            ACCOUNT_REF_SALT,
            CARD_INSTITUTION,
            PRIVATE_CARD_NUMBER,
            "credit_card",
        ),
        banking_account_ref(
            ACCOUNT_REF_SALT,
            CARD_INSTITUTION,
            PRIVATE_UNCOMPUTED_CARD_NUMBER,
            "credit_card",
        ),
        banking_account_ref(
            ACCOUNT_REF_SALT,
            CARD_INSTITUTION,
            PRIVATE_UNLIMITED_CARD_NUMBER,
            "credit_card",
        ),
    } == refs
    assert sum((Decimal(row["volume"]) for row in rows), Decimal("0")) == Decimal("840.0")


def test_a_canonical_card_on_no_stored_account_says_so_rather_than_looking_resolved(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """A canonical id absent from accounts is opaque and explicitly unresolved."""
    _seed_transaction(
        db_path,
        label="unjoinable-card",
        account=UNJOINABLE_SLUG,
        account_type="Synthetic Unjoinable Card",
    )

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    rows = _rows(files["card_account_summary.csv"])
    assert len(rows) == 1, rows
    assert rows[0]["account_ref_status"] == "no_account_record", rows[0]
    assert _UNRESOLVED_REF.fullmatch(rows[0]["account_ref"]), rows[0]


def test_card_export_preserves_the_snapshot_scope_with_canonical_identity(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """Out-of-window and excluded rows cannot add identities to the card file."""
    shared_type = "Synthetic Shared Spelling"
    # In window and in the spend universe: the only rows the summary reports.
    _seed_transaction(
        db_path,
        label="in-window-card",
        account=PRIVATE_CARD_NUMBER,
        account_type=shared_type,
    )
    # Out of the snapshot window (the fixture's window opens mid-2025-06).
    _seed_transaction(
        db_path,
        label="out-of-window-card",
        account=PRIVATE_UNCOMPUTED_CARD_NUMBER,
        account_type=shared_type,
        posted=date(2025, 1, 5),
    )
    # In window, but outside the spend universe.
    _seed_transaction(
        db_path,
        label="excluded-card",
        account=PRIVATE_UNLIMITED_CARD_NUMBER,
        account_type=shared_type,
        exclude_from_spending=True,
    )

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    rows = _rows(files["card_account_summary.csv"])

    assert len(rows) == 1, rows
    assert int(rows[0]["transaction_count"]) == 1, rows
    assert rows[0]["account_ref_status"] == "resolved", rows
    assert rows[0]["account_ref"] == banking_account_ref(
        ACCOUNT_REF_SALT,
        CARD_INSTITUTION,
        PRIVATE_CARD_NUMBER,
        "credit_card",
    )


def test_two_accounts_with_no_record_do_not_merge_into_one_exported_row(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """P0 traded over-splitting for silent under-merging.

    Before P0 an unjoinable row got its own (wrong-namespace) ref. P0 gave it a
    blank one, and `monthly_trend.csv` groups on ["month", "account_ref"], so two
    transactions on two DIFFERENT accounts the `accounts` dimension has no row for
    collapsed into ONE row with their money summed. D3 requires a stated reason,
    not a shared blank key.
    """
    _seed_transaction(
        db_path, label="orphan-one", account=UNJOINABLE_SLUG, account_type="Synthetic Card"
    )
    _seed_transaction(
        db_path,
        label="orphan-two",
        account=SECOND_UNJOINABLE_SLUG,
        account_type="Synthetic Card",
        amount=20,
    )

    files = _package_files(db_path, tmp_path / "exports", "chatgpt-safe")
    rows = _rows(files["monthly_trend.csv"])

    unresolved = [row for row in rows if row["account_ref_status"] == "no_account_record"]
    assert len(unresolved) == 2, (
        f"two unjoinable accounts collapsed into one grouped row: {files['monthly_trend.csv']}"
    )
    assert len({row["account_ref"] for row in unresolved}) == 2
    # The sentinel is in a visibly different namespace from a resolved ref, so it
    # can never be joined against one by mistake.
    assert not any(row["account_ref"].startswith("acct-") for row in unresolved)
    resolved = [row for row in rows if row["account_ref_status"] == "resolved"]
    assert resolved, "the joinable accounts stopped resolving"
    assert all(row["account_ref"].startswith("acct-") for row in resolved)


def test_the_row_level_export_states_why_an_account_ref_is_missing(
    db_path: Path,
    tmp_path: Path,
) -> None:
    """`ledger.coverage` discloses via `account_ref_status`; the export must too."""
    _seed_transaction(
        db_path, label="orphan-one", account=UNJOINABLE_SLUG, account_type="Synthetic Card"
    )

    files = _package_files(db_path, tmp_path / "exports", "full-local-audit")
    rows = _rows(files["transactions.csv"])

    statuses = {row["account_ref_status"] for row in rows}
    assert statuses == {"resolved", "no_account_record"}, statuses
    orphan = [row for row in rows if row["txn_id"].startswith("orphan-one")]
    assert len(orphan) == 1
    assert orphan[0]["account_ref_status"] == "no_account_record"
    assert not orphan[0]["account_ref"].startswith("acct-")


def test_the_synthetic_fixture_joins_every_transaction_to_an_account_record(
    db_path: Path,
) -> None:
    """A fixture whose transactions join to nothing cannot prove a ref claim.

    While `transactions.account` was a literal the `accounts` dimension had no row
    for, exporting the fixture produced one blank ref for every row -- so a correct
    `group_by: account_ref` and a broken one looked identical on it.
    """
    with session_scope(db_path) as session:
        refs = account_ref_map(session, ACCOUNT_REF_SALT)
        slugs = {str(row[0]) for row in session.query(TransactionDB.account).distinct().all()}

    assert slugs == {PRIVATE_ACCOUNT_NUMBER, PRIVATE_CARD_NUMBER}, slugs
    assert slugs <= set(refs), sorted(slugs - set(refs))


def test_server_instructions_ref_claim_is_true(service: LedgerMCPService) -> None:
    """`instructions=` is digest-neutral, so nothing else catches a false claim.

    This is the assertion that was missing while the text said coverage and
    balances shared a reference and the code minted two.
    """
    assert "the same opaque salted reference ledger.coverage uses" in SERVER_INSTRUCTIONS
    assert "minted from the stored account record" in SERVER_INSTRUCTIONS
    # The instructions also state the unjoinable disclosure, and that claim is
    # digest-neutral in exactly the same way.
    assert "`unresolved-` reference in its own namespace" in SERVER_INSTRUCTIONS
    assert "never joinable to an `acct-` one" in SERVER_INSTRUCTIONS

    coverage_refs = _coverage_refs(service)
    # Without this, a resolver that emitted nothing would pass: the empty set is a
    # subset of anything.
    assert coverage_refs, "fixture seeded no resolvable coverage refs"
    assert coverage_refs <= _balances_refs(service)


def test_the_resolver_is_the_only_derivation_and_is_empty_without_a_salt(
    db_path: Path,
) -> None:
    """Absent salt yields an empty map: callers withhold rather than degrade."""
    with session_scope(db_path) as session:
        assert account_ref_map(session, None) == {}
        assert account_ref_map(session, "") == {}
        resolved = account_ref_map(session, ACCOUNT_REF_SALT)

    assert resolved[PRIVATE_ACCOUNT_NUMBER] == banking_account_ref(
        ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"
    )
    assert UNJOINABLE_SLUG not in resolved


def test_banking_account_ref_survives_account_type_redetection(db_path: Path) -> None:
    """A mutable display classification cannot rename a canonical banking account."""
    with session_scope(db_path) as session:
        before = account_ref_map(session, ACCOUNT_REF_SALT)[PRIVATE_CARD_NUMBER]
        card = (
            session.query(AccountDB).filter(AccountDB.account_number == PRIVATE_CARD_NUMBER).one()
        )
        card.account_type = "renamed rewards product"

    with session_scope(db_path) as session:
        after = account_ref_map(session, ACCOUNT_REF_SALT)[PRIVATE_CARD_NUMBER]

    assert after == before
