"""Change boundaries must distinguish new imports from restamped history.

Synthetic import timestamps exercise both cases and their published disclosure."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    PRIVATE_VALUES,
    build_synthetic_mcp_database,
)

from ledger.config import (
    AppConfig,
    CategoriesConfig,
    CategoryRule,
    ClassificationPatterns,
    GeneralConfig,
    LedgerConfig,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    CHANGES_BASIS_STATEMENT,
    CHANGES_COUNT_STATEMENT,
    SERVER_INSTRUCTIONS,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.storage.database import ConfigMetadataDB, StatementCoverageDB, session_scope
from ledger.storage.migrations import run_migrations
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY
from ledger.utils import naive_utc_now

# Before every fixture stamp (2030-01-01) and after nothing, so the synthetic
# database's own rows are never counted as "added" by these tests.
_FIXTURE_STAMP = "2030-01-01T00:00:00"
_HARBOR_HEADER = (
    "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\n"
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _structured(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]  # type: ignore[return-value]


def _generation(service: LedgerMCPService) -> str:
    return str(_structured(call_tool(service, "ledger.status", {}))["coverage"]["generation"])  # type: ignore[index]


def _changes(service: LedgerMCPService, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {"since": _FIXTURE_STAMP, "limit": 100}
    arguments.update(overrides)
    return _structured(call_tool(service, "ledger.changes", arguments))


def _changes_description() -> str:
    return str(
        next(tool["description"] for tool in tool_schemas() if tool["name"] == "ledger.changes")
    )


def _flat(text: str) -> str:
    """Collapse whitespace so a re-wrap does not read as a rewording.

    The published text is hard-wrapped prose. Comparing it verbatim would break on
    a line-break move, which is not a change to what the text SAYS; comparing it
    flattened still breaks on every change to what it says, which is the point.
    """
    return " ".join(text.split())


def _seed_expense(db_path: Path, *, label: str, imported_at: str, account: str) -> None:
    """One transaction row stamped at an explicit `imported_at`."""
    columns = (
        "txn_id, posting_date, institution, account, account_type, flow, cashflow_type, "
        "include_in_cashflow, currency, amount_raw, amount_signed, description, merchant, "
        "category, merchant_confidence, category_confidence, exclude_from_spending, "
        "merchant_raw, category_raw, source_file, source_path, source_period, source_row, "
        "imported_at, parser_version, txn_fingerprint, day_of_week, day_of_month, "
        "week_of_year, is_weekend, is_month_start, is_month_end"
    )
    identifier = label.ljust(40, "0")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            f"INSERT INTO transactions ({columns}) VALUES "
            "(?, '2030-02-02', 'synthetic-bank', ?, 'chequing', 'expense', 'expense', 1, "
            "'CAD', 5.00, -5.00, 'SEEDED', 'SEEDED MARKET', 'Groceries', 'high', 'high', 0, "
            "'', '', 'seed.csv', '/seed.csv', '2030-02', 1, ?, 'v1', ?, 3, 3, 9, 0, 0, 0)",
            (identifier, account, imported_at, identifier[:32]),
        )


def _seed_coverage(
    db_path: Path,
    *,
    slug: str,
    period: str,
    imported_at: datetime,
    row_count: int = 0,
    skipped_reason: str = "no_transactions_extracted",
    institution: str = BANK_INSTITUTION,
) -> None:
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug=slug,
                period=period,
                institution=institution,
                statement_type="synthetic_deposit",
                row_count=row_count,
                statement_count=1,
                skipped_reason=skipped_reason,
                imported_at=imported_at,
            )
        )


# --- the real-import harness for the reconciliation gate ---------------------


def _import_config() -> AppConfig:
    return AppConfig(
        ledger=LedgerConfig(
            general=GeneralConfig(strict=False),
            rules={"harbor": ClassificationPatterns(income_patterns=[r"(?i)\bpayroll\b"])},
        ),
        merchants=MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="COSTCO",
                    category="Groceries",
                    patterns=[r"(?i)\bCOSTCO\b"],
                )
            ],
        ),
        categories=CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[CategoryRule(name="Groceries", merchants=["COSTCO"])],
        ),
    )


def _real_import(tmp_path: Path, db_path: Path, *, period: str, rows: list[str]) -> int:
    """Run one statement through the real processing pipeline. Returns rows added.

    This is the whole point of the gate: `imported_at` is stamped by
    `ParseContext`, not by this test, so a change to how it is stamped surfaces
    here instead of in production.

    `persist_statement_coverage` runs afterwards, exactly as finalize runs it, so
    the ordering production always produces is the ordering under test: every
    transaction stamp comes from parsing, the single coverage stamp is taken at
    finalize, and the coverage stamp is therefore ALWAYS the newer of the two.
    A `latest_imported_at` computed over transactions alone is structurally older
    than the database's real newest import, and echoing it back re-reports the
    import that produced it, forever.
    """
    from ledger.imports.pdf.post_processing import persist_statement_coverage
    from ledger.imports.pdf.processing import write_result_to_db
    from ledger.processing.pipeline import process

    csv_root = tmp_path / f"csv-{period}"
    (csv_root / period).mkdir(parents=True)
    csv_path = csv_root / period / "harbor-chequing-1001.csv"
    csv_path.write_text(_HARBOR_HEADER + "".join(rows), encoding="utf-8")
    result = process(_import_config(), csv_root)
    assert not result.exceptions, result.exceptions
    write_result_to_db(db_path, csv_root, result)
    manifest = tmp_path / f"extraction-manifest-{period}.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "csv_path": str(csv_path),
                    "statement_type": "harbor_deposit",
                    "transaction_count": len(result.transactions),
                    "skipped_reason": "",
                    "account_slug": result.transactions[0].account,
                    "period": period,
                }
            ]
        ),
        encoding="utf-8",
    )
    persist_statement_coverage(db_path, manifest)
    return len(result.transactions)


def _imported_ledger(tmp_path: Path) -> tuple[Path, datetime, int]:
    """A database built by two real imports, with a boundary between them."""
    db_path = tmp_path / "imported.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    _real_import(
        tmp_path,
        db_path,
        period="2026-06",
        rows=[
            "ledger-mapped-v1,2026-06-02,COSTCO WHOLESALE,-50.00,CAD,,harbor,Chequing\n",
            "ledger-mapped-v1,2026-06-03,COSTCO GAS,-20.00,CAD,,harbor,Chequing\n",
            "ledger-mapped-v1,2026-06-04,PAYROLL DEPOSIT,2000.00,CAD,,harbor,Chequing\n",
        ],
    )
    boundary = naive_utc_now()
    added = _real_import(
        tmp_path,
        db_path,
        period="2026-07",
        rows=[
            "ledger-mapped-v1,2026-07-02,COSTCO WHOLESALE,-11.00,CAD,,harbor,Chequing\n",
            "ledger-mapped-v1,2026-07-03,COSTCO GAS,-22.00,CAD,,harbor,Chequing\n",
        ],
    )
    return db_path, boundary, added


def _status(service: LedgerMCPService) -> dict[str, object]:
    return _structured(call_tool(service, "ledger.status", {}))["data"]  # type: ignore[return-value]


# --- 0. THE DOCUMENTED LOOP --------------------------------------------------
#
# Quoted VERBATIM from the shipped text, so an edit to the text breaks the gate.
# The previous version of this gate claimed in its docstring to run "the loop the
# server instructions describe" and in fact re-read the generation at every call
# while holding a stale boundary -- a pairing no published document specifies.
# The documented pairing, stale boundary WITH the stale generation it was
# observed under, answered `basis: "rebuilt"` on every routine import, and that
# is how a surface which could never report an import survived three reviews.
_PUBLISHED_SEED_RULE = (
    "Seed `since` with the `latest_imported_at` field -- ledger.status and "
    "ledger.changes both emit it and both mean the same instant -- and echo back "
    "the `latest_imported_at` this tool returns as the next call's `since`."
)
_PUBLISHED_EXCLUSIVE_RULE = (
    "the comparison is exclusive, so echoing the field back reports nothing until "
    "something new arrives."
)
_PUBLISHED_DELTA_RULE = "`delta` means the counts are rows that arrived after your boundary."
_PUBLISHED_GENERATION_RULE = (
    "`client_generation` is an optional input, echoed back with "
    "`generation_changed` so you can see the data moved between your two calls. "
    "It never changes a count and never withholds one."
)
_PUBLISHED_DESCRIPTION_ECHO_RULE = (
    "echo back the `latest_imported_at` this tool returns as the next call's `since`"
)
_PUBLISHED_DESCRIPTION_GENERATION_RULE = (
    "`client_generation` is optional and informational: supply the generation you "
    "observed the boundary under and it is echoed back with `generation_changed`, "
    "which never changes a count and never withholds one."
)


def test_the_published_text_still_describes_the_loop_this_gate_runs() -> None:
    """The gate below is only worth anything if it runs the SHIPPED loop.

    So the shipped sentences are quoted here. Reword one and this fails, which
    forces the next author to re-derive the loop below from the new wording rather
    than leaving a passing test that exercises a pairing nothing documents.
    """
    instructions = _flat(SERVER_INSTRUCTIONS)
    description = _flat(_changes_description())

    for quoted in (
        _PUBLISHED_SEED_RULE,
        _PUBLISHED_EXCLUSIVE_RULE,
        _PUBLISHED_DELTA_RULE,
        _PUBLISHED_GENERATION_RULE,
    ):
        assert _flat(quoted) in instructions, quoted
    for quoted in (_PUBLISHED_DESCRIPTION_ECHO_RULE, _PUBLISHED_DESCRIPTION_GENERATION_RULE):
        assert _flat(quoted) in description, quoted
    # The withdrawn vocabulary is gone from both, not merely unreachable in code:
    # a caller reading `rebuilt` was told the ledger had been rebuilt after every
    # routine import.
    assert "rebuilt" not in instructions
    assert "rebuilt" not in description


def test_the_documented_loop_reports_an_import_once_and_then_reports_nothing(
    tmp_path: Path,
) -> None:
    """The loop the PUBLISHED TEXT describes, verbatim, across a real import.

    Every call below holds the boundary AND the generation exactly as the quoted
    sentences instruct: seed both from one observation, keep them until the answer
    hands back a newer `latest_imported_at`, then echo that. Nothing is re-read
    mid-loop, because nothing in the published text tells a caller to.

    Two properties this pins that nothing pinned before:

    * The import is reported ONCE. Under the old generation-equality gate round
      two answered `basis: "rebuilt"` with null counts, because the import that
      round two exists to detect had itself rotated the pin.
    * `ledger.status.latest_imported_at` and `ledger.changes.latest_imported_at`
      are ONE number, over transactions AND `statement_coverage`. Status once took
      `max(transactions.imported_at)` alone, which is structurally older than the
      finalize-time coverage stamp, so echoing it re-reported the last import
      forever.
    """
    db_path = tmp_path / "loop.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    _real_import(
        tmp_path,
        db_path,
        period="2026-06",
        rows=["ledger-mapped-v1,2026-06-02,COSTCO WHOLESALE,-50.00,CAD,,harbor,Chequing\n"],
    )

    service = LedgerMCPService(db_path)
    try:
        # Round one: ONE observation seeds both halves of the pairing.
        settled = str(_status(service)["latest_imported_at"])
        observed_generation = _generation(service)
        first = _changes(service, since=settled, client_generation=observed_generation)["data"]
        assert first["basis"] == "delta", first  # type: ignore[index]
        assert first["total_added"] == 0, first  # type: ignore[index]
        assert first["entries"] == [], first  # type: ignore[index]
        assert first["since_relation"] == "after_latest_import"  # type: ignore[index]
        # The two surfaces name the same instant.
        assert first["latest_imported_at"] == settled  # type: ignore[index]
        assert first["client_generation"] == observed_generation  # type: ignore[index]
        assert first["generation_changed"] is False, first  # type: ignore[index]

        # Round two: one real import arrives. The caller still holds the boundary
        # and the generation it observed together -- which is exactly the state
        # the old gate refused to answer from, on the one call that had something
        # to report.
        added = _real_import(
            tmp_path,
            db_path,
            period="2026-07",
            rows=["ledger-mapped-v1,2026-07-02,COSTCO GAS,-22.00,CAD,,harbor,Chequing\n"],
        )
        second = _changes(service, since=settled, client_generation=observed_generation)["data"]
        assert second["basis"] == "delta", second  # type: ignore[index]
        assert second["delta_available"] is True, second  # type: ignore[index]
        assert second["total_added"] == added, second  # type: ignore[index]
        assert [entry["period"] for entry in second["entries"]] == ["2026-07"]  # type: ignore[index]
        # The import moved the data, and the caller is TOLD so -- without that
        # fact being allowed to suppress the counts it accompanies.
        assert second["generation_changed"] is True, second  # type: ignore[index]

        # Round three: echo back the `latest_imported_at` this tool returned, with
        # the generation observed alongside it. Nothing new.
        settled = str(second["latest_imported_at"])
        assert settled == str(_status(service)["latest_imported_at"])
        observed_generation = _generation(service)
        third = _changes(service, since=settled, client_generation=observed_generation)["data"]
    finally:
        service.close()

    assert third["basis"] == "delta", third  # type: ignore[index]
    assert third["total_added"] == 0, third  # type: ignore[index]
    assert third["entries"] == [], third  # type: ignore[index]
    assert third["entry_count"] == 0  # type: ignore[index]
    assert third["since_relation"] == "after_latest_import"  # type: ignore[index]
    assert third["generation_changed"] is False, third  # type: ignore[index]


def test_overlapping_statement_keeps_existing_row_stamp_and_reports_only_new_statement(
    tmp_path: Path,
) -> None:
    """A repeated stable transaction is evidence coverage, not a newly added row."""
    db_path = tmp_path / "overlap.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    repeated = "ledger-mapped-v1,2026-06-02,COSTCO WHOLESALE,-50.00,CAD,,harbor,Chequing\n"
    _real_import(tmp_path, db_path, period="2026-06", rows=[repeated])

    service = LedgerMCPService(db_path)
    try:
        boundary = str(_status(service)["latest_imported_at"])
        _real_import(tmp_path, db_path, period="2026-07", rows=[repeated])
        data = _changes(service, since=boundary)["data"]
    finally:
        service.close()

    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["total_added"] == 0  # type: ignore[index]
    assert data["entry_count"] == 1  # type: ignore[index]
    [entry] = data["entries"]  # type: ignore[index]
    assert entry["period"] == "2026-07"
    assert entry["rows_added"] == 0
    assert entry["statement_count"] == 1


def test_the_loop_runs_without_a_generation_at_all_because_it_is_optional(
    tmp_path: Path,
) -> None:
    """`client_generation` is documented as optional, so the loop must close without it.

    Omitted, both echo fields are null rather than false: `generation_changed` is
    a comparison, and there was nothing to compare against. Every count is
    produced regardless, which is the whole of D13.
    """
    db_path = tmp_path / "no-generation.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    _real_import(
        tmp_path,
        db_path,
        period="2026-06",
        rows=["ledger-mapped-v1,2026-06-02,COSTCO WHOLESALE,-50.00,CAD,,harbor,Chequing\n"],
    )

    service = LedgerMCPService(db_path)
    try:
        settled = str(_status(service)["latest_imported_at"])
        added = _real_import(
            tmp_path,
            db_path,
            period="2026-07",
            rows=["ledger-mapped-v1,2026-07-02,COSTCO GAS,-22.00,CAD,,harbor,Chequing\n"],
        )
        data = _changes(service, since=settled)["data"]
    finally:
        service.close()

    assert data["basis"] == "delta", data  # type: ignore[index]
    assert data["total_added"] == added, data  # type: ignore[index]
    assert data["client_generation"] is None, data  # type: ignore[index]
    assert data["generation_changed"] is None, data  # type: ignore[index]


def test_the_status_snapshot_age_covers_statements_that_produced_no_rows(
    db_path: Path,
) -> None:
    """A statement that produced nothing still advances the newest import stamp.

    `ledger.status` reads that stamp from the same two tables `ledger.changes`
    does, because a status value that lags the changes value is a boundary that
    re-reports its own import.
    """
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-4321",
        period="2030-03",
        imported_at=datetime(2030, 5, 1),
    )
    service = LedgerMCPService(db_path)
    try:
        status_stamp = _status(service)["latest_imported_at"]
        changes_stamp = _changes(service, client_generation=_generation(service))["data"][  # type: ignore[index]
            "latest_imported_at"
        ]
    finally:
        service.close()

    assert status_stamp == "2030-05-01T00:00:00"
    assert changes_stamp == status_stamp


# --- 1. THE RECONCILIATION GATE ---------------------------------------------


def test_the_total_equals_the_rows_a_real_incremental_import_added(tmp_path: Path) -> None:
    """Exactly the second import's rows, and none of the first import's.

    A normal import preserves the stamps already in the database, so the answer
    must be the two new rows -- not the five the database now holds.
    """
    db_path, boundary, added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since=boundary.isoformat(),
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert added == 2
    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["delta_available"] is True  # type: ignore[index]
    assert data["total_added"] == added  # type: ignore[index]
    entries = data["entries"]  # type: ignore[index]
    assert [entry["period"] for entry in entries] == ["2026-07"]
    assert sum(int(entry["rows_added"]) for entry in entries) == added


def test_a_boundary_before_both_imports_reports_every_row_and_refuses_to_call_it_a_delta(
    tmp_path: Path,
) -> None:
    """A boundary before both imports reports every row and refuses to call it a delta."""
    db_path, _boundary, _added = _imported_ledger(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        stored = int(conn.execute("SELECT count(*) FROM transactions").fetchone()[0])
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since="2026-01-01T00:00:00",
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert stored == 5
    assert data["total_added"] == stored  # type: ignore[index]
    assert {entry["period"] for entry in data["entries"]} == {"2026-06", "2026-07"}  # type: ignore[index]
    # The count is the whole database, so it is not reported as a delta.
    assert data["basis"] == "whole_database"  # type: ignore[index]
    assert data["delta_available"] is False  # type: ignore[index]
    assert data["since_relation"] == "before_earliest_import"  # type: ignore[index]


def test_the_earliest_import_stamp_is_on_the_wire_so_the_disclosure_can_be_applied(
    tmp_path: Path,
) -> None:
    """`basis_semantics` says a boundary older than the last rebuild reports the
    whole database. Without the earliest stamp the caller has no datum to apply
    that to: the two bounds together are what make a subsecond imported history
    visible as one rebuild rather than as history."""
    db_path, _boundary, _added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since="2026-01-01T00:00:00",
            client_generation=_generation(service),
        )["data"]
        with closing(sqlite3.connect(db_path)) as conn:
            stored = str(conn.execute("SELECT min(imported_at) FROM transactions").fetchone()[0])
    finally:
        service.close()

    earliest = str(data["earliest_imported_at"])  # type: ignore[index]
    assert earliest == datetime.fromisoformat(stored).isoformat()
    assert earliest < str(data["latest_imported_at"])  # type: ignore[index]


def test_a_boundary_inside_the_imported_range_is_still_a_delta(tmp_path: Path) -> None:
    """The narrowing must not swallow the case the tool exists for: a boundary at
    or after the earliest stamp and before the latest one is a real delta."""
    db_path, boundary, added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since=boundary.isoformat(),
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["since_relation"] == "within_imported_range"  # type: ignore[index]
    assert data["total_added"] == added  # type: ignore[index]


def test_the_answer_vocabularies_are_the_ones_the_schema_publishes() -> None:
    """`_CHANGES_RELATIONS` is the vocabulary, not a comment about one.

    It was defined and never referenced while the three relation strings sat as
    literals two hundred lines away, which is exactly how a fourth value gets
    added to one and not the other. The named constants are now what `changes`
    builds its answer from, and this is the link that stops the two lists
    drifting: the query layer publishes nothing, and the wire contract computes
    nothing, so only a test can hold them together.
    """
    from ledger.query.agent import _CHANGES_BASES, _CHANGES_RELATIONS

    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.changes"
    )
    success = next(
        branch
        for branch in schema["oneOf"]
        if "data" in branch.get("properties", {})  # type: ignore[index]
    )
    properties = success["properties"]["data"]["properties"]

    assert frozenset(properties["since_relation"]["enum"]) == _CHANGES_RELATIONS
    assert "before_earliest_import" in _CHANGES_RELATIONS
    assert frozenset(properties["basis"]["enum"]) == _CHANGES_BASES
    assert "whole_database" in _CHANGES_BASES


def test_the_boundary_is_compared_as_a_timestamp_and_not_as_a_string(
    tmp_path: Path,
) -> None:
    """SQLite stores `imported_at` as "YYYY-MM-DD HH:MM:SS.ffffff".

    A raw string comparison against the ISO `T` form is silently wrong in one
    direction for every row -- ' ' sorts below 'T' -- which reads as "nothing was
    imported". The wire takes the `T` form, so the boundary must be parsed into a
    datetime before it reaches SQL.
    """
    db_path, boundary, added = _imported_ledger(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        naive_string_answer = int(
            conn.execute(
                "SELECT count(*) FROM transactions WHERE imported_at > ?",
                (boundary.isoformat(),),
            ).fetchone()[0]
        )
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since=boundary.isoformat(),
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert naive_string_answer == 0, "the string comparison stopped being wrong"
    assert data["total_added"] == added  # type: ignore[index]


# --- 2. the generation is reported, and never gates an answer ---------------


def test_a_generation_that_no_longer_matches_the_pin_still_reports_the_counts(
    tmp_path: Path,
) -> None:
    """A rotated generation is a FACT ABOUT THE DATA, not a reason to say nothing.

    It used to be the whole discriminator: mismatch meant `basis: "rebuilt"` with
    every count null. The generation hashes the pinned file's size, mtime and
    transaction count, so it rotates on any write at all -- which means it
    mismatches on precisely the calls that have an import to report, and matches
    only on the calls that have nothing. The counts are governed by the import
    stamps instead, and this comparison is reported alongside them.
    """
    db_path, boundary, added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        stale = "0" * 16
        assert stale != _generation(service)
        data = _changes(service, since=boundary.isoformat(), client_generation=stale)["data"]
    finally:
        service.close()

    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["delta_available"] is True  # type: ignore[index]
    assert data["total_added"] == added  # type: ignore[index]
    assert data["entry_count"] == 1  # type: ignore[index]
    assert data["entries"], data  # type: ignore[index]
    # Echoed and compared, so the caller learns the data moved -- which is the
    # entire yield of the input, and is not a count.
    assert data["client_generation"] == stale  # type: ignore[index]
    assert data["generation_changed"] is True  # type: ignore[index]


def test_the_answer_discloses_whether_it_withheld_an_identity(
    tmp_path: Path,
) -> None:
    """Two cases, not three. The salt was minted, or it was not.

    A third value (`not_computed`) belonged to the withdrawn rebuild branch, which
    returned before any entry existed. With that branch gone, a wire enum still
    carrying it would publish a state nothing can produce.
    """
    from ledger.query.agent import ACCOUNT_REFS_DISCLOSED, ACCOUNT_REFS_WITHHELD

    db_path, boundary, _added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        data = _changes(service, since=boundary.isoformat(), client_generation="0" * 16)["data"]
    finally:
        service.close()

    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.changes"
    )
    success = next(
        branch
        for branch in schema["oneOf"]
        if "data" in branch.get("properties", {})  # type: ignore[index]
    )
    published = success["properties"]["data"]["properties"]["account_refs"]["enum"]

    assert set(published) == {ACCOUNT_REFS_DISCLOSED, ACCOUNT_REFS_WITHHELD}
    assert "not_computed" not in published
    assert data["account_refs"] == "disclosed"  # type: ignore[index]
    assert data["account_refs_withheld_reason"] is None  # type: ignore[index]


def test_the_import_that_rotates_the_pin_is_the_one_that_gets_reported(
    db_path: Path,
) -> None:
    """The defect, stated as its own test. An import rotates the generation.

    A caller holding the generation it observed the boundary under therefore
    ALWAYS holds a stale one by the time there is something to report. The old
    gate answered `basis: "rebuilt"` with null counts here -- on the one call that
    had an arrival to name -- and told the operator the ledger had been rebuilt.
    """
    service = LedgerMCPService(db_path)
    try:
        before = _generation(service)
        _seed_expense(
            db_path,
            label="after-observation",
            imported_at="2030-06-01 00:00:00.000000",
            account="synthetic-private-number-4321",
        )
        data = _changes(service, client_generation=before)["data"]
        after = _generation(service)
    finally:
        service.close()

    assert after != before, "the write did not rotate the pin, so this proves nothing"
    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["total_added"] == 1  # type: ignore[index]
    assert data["generation_changed"] is True  # type: ignore[index]


def test_the_basis_rule_is_stated_on_the_wire_and_in_the_schema(
    service: LedgerMCPService,
) -> None:
    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.changes"
    )
    success = next(
        branch
        for branch in schema["oneOf"]
        if "data" in branch.get("properties", {})  # type: ignore[index]
    )
    properties = success["properties"]["data"]["properties"]

    assert properties["basis_semantics"] == {"const": CHANGES_BASIS_STATEMENT}
    assert properties["count_semantics"] == {"const": CHANGES_COUNT_STATEMENT}
    data = _changes(service)["data"]
    assert data["basis_semantics"] == CHANGES_BASIS_STATEMENT  # type: ignore[index]
    assert data["count_semantics"] == CHANGES_COUNT_STATEMENT  # type: ignore[index]
    # The facts a caller cannot reconstruct from the numbers, including the one
    # the wire text used to get backwards: the generation is not the verdict.
    assert "naive UTC" in CHANGES_BASIS_STATEMENT
    assert "rebuild" in CHANGES_BASIS_STATEMENT
    assert "rotates on every write" in CHANGES_BASIS_STATEMENT
    assert "transfers" in CHANGES_COUNT_STATEMENT.casefold()


# --- 3. the boundary itself -------------------------------------------------


def test_a_boundary_after_the_last_import_reports_zero_with_a_stated_relation(
    service: LedgerMCPService,
) -> None:
    data = _changes(service, since="2031-01-01T00:00:00")["data"]

    assert data["total_added"] == 0  # type: ignore[index]
    assert data["entries"] == []  # type: ignore[index]
    assert data["since_relation"] == "after_latest_import"  # type: ignore[index]
    # Reported so the caller can see WHY the answer is empty.
    assert data["latest_imported_at"] == "2030-01-01T00:00:00"  # type: ignore[index]


def test_the_boundary_is_exclusive_so_an_echoed_latest_import_reports_nothing(
    service: LedgerMCPService,
) -> None:
    """The natural client loop echoes `latest_imported_at` back.

    That row was already seen, so it must not come back as newly added.
    """
    data = _changes(service, since=_FIXTURE_STAMP)["data"]

    assert data["total_added"] == 0  # type: ignore[index]
    assert data["since_relation"] == "after_latest_import"  # type: ignore[index]


def test_the_relation_never_contradicts_the_entries_it_reports(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """A statement that produced no rows advances the newest import stamp without
    touching `transactions`. Taking the relation from transactions alone would
    report "past the last import" in the same answer that names an arrived
    statement, so it is taken over both tables.
    """
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-4321",
        period="2030-03",
        imported_at=datetime(2030, 5, 1),
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    assert data["entries"], "the statement did not arrive"  # type: ignore[index]
    assert data["since_relation"] == "within_imported_range"  # type: ignore[index]
    assert data["latest_imported_at"] == "2030-05-01T00:00:00"  # type: ignore[index]


@pytest.mark.parametrize(
    "since",
    [
        "2030-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00Z",
        "2030-01-01 00:00:00",
        "2030-01-01",
        "not-a-timestamp",
    ],
)
def test_a_boundary_that_is_not_naive_utc_is_refused(
    service: LedgerMCPService,
    since: str,
) -> None:
    """Storage is naive UTC. A locally-expressed boundary is four hours wrong on
    the operator's machine, and an offset-bearing one invites exactly that."""
    result = call_tool(
        service,
        "ledger.changes",
        {"since": since, "client_generation": _generation(service)},
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


# --- 4. statements, including the ones that produced no rows ----------------


def test_a_zero_row_statement_is_visible_even_though_it_added_no_rows(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The only way an empty statement is visible at all.

    Counting transactions alone reports nothing for a statement that produced no
    rows, which is indistinguishable from a month nobody imported.
    """
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-4321",
        period="2030-03",
        imported_at=datetime(2030, 5, 1),
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    entries = [entry for entry in data["entries"] if entry["period"] == "2030-03"]  # type: ignore[index]
    assert len(entries) == 1, data["entries"]  # type: ignore[index]
    assert entries[0]["rows_added"] == 0
    assert entries[0]["statement_status"] == "imported_no_activity"
    assert entries[0]["statement_count"] == 1
    assert entries[0]["account_ref_status"] == "resolved"


def test_rows_arriving_with_no_statement_row_in_the_window_say_so(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _seed_expense(
        db_path,
        label="row-without-statement",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    entries = [entry for entry in data["entries"] if entry["period"] == "2030-02"]  # type: ignore[index]
    assert len(entries) == 1, data["entries"]  # type: ignore[index]
    assert entries[0]["rows_added"] == 1
    assert entries[0]["statement_status"] == "no_statement_in_window"
    assert entries[0]["statement_count"] == 0


def test_two_accounts_with_no_stored_record_do_not_collapse_into_one_entry(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The P0 rule on a new surface: never a blank key for an unjoinable row."""
    import re

    for slug in ("synthetic-private-number-0000", "synthetic-private-number-1111"):
        _seed_coverage(
            db_path,
            slug=slug,
            period="2030-04",
            imported_at=datetime(2030, 5, 1),
        )
    data = _changes(service, client_generation=_generation(service))["data"]

    entries = [entry for entry in data["entries"] if entry["period"] == "2030-04"]  # type: ignore[index]
    assert len(entries) == 2, data["entries"]  # type: ignore[index]
    refs = {str(entry["account_ref"]) for entry in entries}
    assert len(refs) == 2, refs
    assert all(re.fullmatch(r"unresolved-[0-9a-f]{10}", ref) for ref in refs), refs
    assert all(entry["account_ref_status"] == "no_account_record" for entry in entries)


def test_without_a_minted_salt_the_entries_are_withheld_but_the_count_survives(
    tmp_path: Path,
) -> None:
    built = build_synthetic_mcp_database(tmp_path / "ledger.db", account_ref_salt=False)
    _seed_expense(
        built,
        label="withheld-row",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    service = LedgerMCPService(built)
    try:
        envelope = _changes(service, client_generation=_generation(service))
    finally:
        service.close()

    data = envelope["data"]
    assert data["entries"] == []  # type: ignore[index]
    assert data["account_refs"] == "withheld"  # type: ignore[index]
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"  # type: ignore[index]
    assert data["total_added"] == 1  # type: ignore[index]
    assert envelope["page"]["has_more"] is False  # type: ignore[index]
    assert envelope["page"]["next_cursor"] is None  # type: ignore[index]
    encoded = json.dumps(envelope, ensure_ascii=False)
    for private in PRIVATE_VALUES:
        assert private not in encoded, private


# --- 5. the cursor binds both new inputs ------------------------------------


def _seed_three_periods(db_path: Path) -> None:
    for index, period in enumerate(("2030-03", "2030-04", "2030-05")):
        _seed_coverage(
            db_path,
            slug="synthetic-private-number-4321",
            period=period,
            imported_at=datetime(2030, 6, 1) + timedelta(days=index),
        )


@pytest.mark.parametrize(
    "changed",
    [{"since": "2030-02-01T00:00:00"}, {"client_generation": "1" * 16}],
    ids=["since", "client_generation"],
)
def test_a_cursor_is_refused_when_either_new_input_changes(
    db_path: Path,
    service: LedgerMCPService,
    changed: dict[str, object],
) -> None:
    _seed_three_periods(db_path)
    base: dict[str, object] = {
        "since": _FIXTURE_STAMP,
        "client_generation": _generation(service),
        "limit": 1,
    }
    first = call_tool(service, "ledger.changes", base)
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor, first

    replayed = call_tool(service, "ledger.changes", {**base, **changed, "cursor": cursor})

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_an_unchanged_boundary_still_paginates_in_a_total_order(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _seed_three_periods(db_path)
    base: dict[str, object] = {
        "since": _FIXTURE_STAMP,
        "client_generation": _generation(service),
        "limit": 1,
    }
    seen: list[str] = []
    cursor: object = None
    for _ in range(3):
        arguments = base if cursor is None else {**base, "cursor": cursor}
        envelope = _structured(call_tool(service, "ledger.changes", arguments))
        seen.extend(str(entry["period"]) for entry in envelope["data"]["entries"])  # type: ignore[index]
        cursor = envelope["page"]["next_cursor"]  # type: ignore[index]

    assert seen == ["2030-03", "2030-04", "2030-05"]


# A second account-ref salt whose refs sort the fixture's accounts into a
# DIFFERENT order from the first's, so the old ref-ordered implementation really
# skips an entry rather than coincidentally addressing the same one.
_OTHER_SALT = "f" * 64


def _changes_page(agent, salt: str, cursor: str | None) -> tuple[list[str], str | None]:
    page = agent.changes(
        since=datetime.fromisoformat(_FIXTURE_STAMP),
        client_generation=agent._snapshot_generation,
        limit=1,
        cursor=cursor,
        account_ref_salt=salt,
    )
    return [entry.account_ref for entry in page.entries], page.next_cursor


def test_a_cursor_minted_under_one_salt_never_skips_an_entry_under_another(
    db_path: Path,
) -> None:
    """The cursor law on this surface, with the same fix the aggregate took.

    `account_ref_salt` is not bound into the cursor fingerprint, so a cursor
    minted under one salt and replayed under another is ACCEPTED. While the
    derived ref was the sort key that replay addressed a different entry, and the
    entry it displaced was never served. Ordering on the stored account slug --
    the P0 precedent that already makes ledger.coverage safe -- removes the
    coupling rather than binding one more input.
    """
    from ledger.query.account_refs import account_ref_map, resolve_account_ref
    from ledger.query.service import LedgerQueryService
    from ledger.storage.read_only import ReadOnlyDatabase

    for slug in (
        "synthetic-private-number-4321",
        "synthetic-private-number-0000",
        "synthetic-private-number-1111",
    ):
        _seed_coverage(db_path, slug=slug, period="2030-04", imported_at=datetime(2030, 5, 1))
    with session_scope(db_path) as session:
        slug_of = {
            salt: {
                resolve_account_ref(account_ref_map(session, salt), slug, salt)[0]: slug
                for slug in (
                    "synthetic-private-number-4321",
                    "synthetic-private-number-0000",
                    "synthetic-private-number-1111",
                )
            }
            for salt in (ACCOUNT_REF_SALT, _OTHER_SALT)
        }

    database = ReadOnlyDatabase(db_path)
    try:
        agent = LedgerQueryService(
            db_path,
            session_scope_factory=database.session_scope,
            snapshot_generation=database.preflight().generation,
        ).agent
        walked: dict[str, list[str]] = {}
        for salt in (ACCOUNT_REF_SALT, _OTHER_SALT):
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(3):
                refs, cursor = _changes_page(agent, salt, cursor)
                seen.extend(slug_of[salt][ref] for ref in refs)
            walked[salt] = seen
        first, cursor = _changes_page(agent, ACCOUNT_REF_SALT, None)
        assert cursor
        under_a, _ = _changes_page(agent, ACCOUNT_REF_SALT, cursor)
        under_b, _ = _changes_page(agent, _OTHER_SALT, cursor)
    finally:
        database.close()

    assert walked[ACCOUNT_REF_SALT] == sorted(slug_of[ACCOUNT_REF_SALT].values())
    assert walked[_OTHER_SALT] == walked[ACCOUNT_REF_SALT]
    assert first and under_a and under_b
    assert [slug_of[ACCOUNT_REF_SALT][ref] for ref in under_a] == [
        slug_of[_OTHER_SALT][ref] for ref in under_b
    ]


# --- 6. the basis is decided per COUNTED TABLE, never across both ------------


def _coverage_first_ledger(tmp_path: Path) -> tuple[Path, int]:
    """A ledger whose FIRST import produced no transaction rows at all.

    Precisely the case this surface joins `statement_coverage` to make visible.
    It puts the oldest stamp in the snapshot on `statement_coverage` and every
    transaction stamp after it, so the two counted tables have DIFFERENT earliest
    stamps and a boundary can sit between them.
    """
    db_path = tmp_path / "coverage-first.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    _seed_coverage(
        db_path,
        slug="harbor-chequing-1234",
        period="2026-05",
        imported_at=datetime(2026, 5, 31, 12, 0, 0),
    )
    added = _real_import(
        tmp_path,
        db_path,
        period="2026-06",
        rows=[
            "ledger-mapped-v1,2026-06-02,COSTCO WHOLESALE,-50.00,CAD,,harbor,Chequing\n",
            "ledger-mapped-v1,2026-06-03,COSTCO GAS,-20.00,CAD,,harbor,Chequing\n",
            "ledger-mapped-v1,2026-06-04,PAYROLL DEPOSIT,2000.00,CAD,,harbor,Chequing\n",
        ],
    )
    return db_path, added


def test_a_boundary_before_every_row_is_never_a_delta_because_a_statement_predates_it(
    tmp_path: Path,
) -> None:
    """The basis was decided from a different table than the count came from.

    `basis` read the earliest stamp over BOTH tables while `total_added` counts
    `transactions` ALONE, and the two coincide only while coverage is the newer
    table. Make the first thing ever imported a statement that produced no rows
    and they diverge: a boundary after the coverage stamp but before every
    transaction stamp is `within_imported_range` over the combined history, and
    the whole transactions table was reported as `basis: delta`.
    """
    db_path, added = _coverage_first_ledger(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        stored = int(conn.execute("SELECT count(*) FROM transactions").fetchone()[0])
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since="2026-06-01T00:00:00",
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert stored == added == 3
    # The count is exact and it is the ENTIRE transactions table.
    assert data["total_added"] == stored  # type: ignore[index]
    # So no part of the answer may frame it as newly arrived activity.
    assert data["basis"] == "whole_database"  # type: ignore[index]
    assert data["delta_available"] is False  # type: ignore[index]
    # The boundary really does sit inside the COMBINED history: the zero-row
    # statement is older than it. That is what made one collapsed verdict wrong.
    assert data["since_relation"] == "within_imported_range"  # type: ignore[index]


def test_each_counted_table_reports_its_own_bounds_and_its_own_verdict(
    tmp_path: Path,
) -> None:
    """Two counts, two tables, two earliest stamps -- so two verdicts.

    `rows_added`/`total_added` come from `transactions` and the statement counts
    come from `statement_coverage`. Each is 'the whole table' exactly when the
    boundary precedes THAT table's earliest stamp, and the top-level basis is the
    weaker of the two.
    """
    db_path, _added = _coverage_first_ledger(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        rows_earliest = str(conn.execute("SELECT min(imported_at) FROM transactions").fetchone()[0])
        statements_earliest = str(
            conn.execute("SELECT min(imported_at) FROM statement_coverage").fetchone()[0]
        )
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since="2026-06-01T00:00:00",
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    sources = data["count_sources"]  # type: ignore[index]
    assert sources["rows"]["basis"] == "whole_table"
    assert sources["rows"]["since_relation"] == "before_earliest_import"
    assert (
        sources["rows"]["earliest_imported_at"] == datetime.fromisoformat(rows_earliest).isoformat()
    )
    assert sources["statements"]["basis"] == "delta"
    assert sources["statements"]["since_relation"] == "within_imported_range"
    assert (
        sources["statements"]["earliest_imported_at"]
        == datetime.fromisoformat(statements_earliest).isoformat()
    )
    # The top-level pair still bounds BOTH tables: it is the value ledger.status
    # emits and the one a caller echoes back.
    assert data["earliest_imported_at"] == sources["statements"]["earliest_imported_at"]  # type: ignore[index]


def test_a_real_delta_still_reports_a_delta_on_both_counted_tables(tmp_path: Path) -> None:
    """The narrowing must not swallow the case the surface exists for."""
    db_path, boundary, added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since=boundary.isoformat(),
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["delta_available"] is True  # type: ignore[index]
    assert data["total_added"] == added  # type: ignore[index]
    sources = data["count_sources"]  # type: ignore[index]
    assert sources["rows"]["basis"] == "delta"
    assert sources["statements"]["basis"] == "delta"


def test_a_table_with_no_stamps_at_all_is_not_reported_as_a_whole_table_count(
    tmp_path: Path,
) -> None:
    """An empty table's zero is not "the whole database" in any useful sense, and
    saying so would make every ledger with no statement coverage refuse to report
    a delta forever."""
    db_path = tmp_path / "rolumen-only.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=ACCOUNT_REF_SALT))
    _seed_expense(
        db_path,
        label="rolumen-only",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    service = LedgerMCPService(db_path)
    try:
        data = _changes(
            service,
            since="2030-05-01T00:00:01",
            client_generation=_generation(service),
        )["data"]
    finally:
        service.close()

    assert data["count_sources"]["statements"]["basis"] == "no_imports"  # type: ignore[index]
    assert data["count_sources"]["statements"]["since_relation"] == "no_imports_recorded"  # type: ignore[index]
    assert data["basis"] == "delta"  # type: ignore[index]
    assert data["total_added"] == 0  # type: ignore[index]


def test_a_restamp_and_an_import_are_told_apart_by_the_stamps_under_one_boundary(
    tmp_path: Path,
) -> None:
    """The discriminator, demonstrated as a difference (D12).

    ONE boundary, held fixed, asked twice. First against a database built by two
    real incremental imports, where the first import's stamps SURVIVED because
    staging clones the live database: the boundary still precedes surviving rows,
    so the count is a delta. Then against the same database after every stamp in
    both tables has been rewritten forward, which is what `repair statements` and
    `rebuild-db` promote do: nothing older than the boundary survives, so the
    count is the whole table and is labelled that way.

    The generation rotated in BOTH cases -- an import rotates it exactly as much
    as a restamp -- which is precisely why it cannot be the discriminator and the
    stamps are.
    """
    db_path, boundary, added = _imported_ledger(tmp_path)
    service = LedgerMCPService(db_path)
    try:
        before_restamp = _changes(service, since=boundary.isoformat())["data"]

        # The restamp itself: every row in both counted tables, forward of the
        # boundary, exactly as a promote leaves them.
        restamped = "2027-01-01 00:00:00.000000"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("UPDATE transactions SET imported_at = ?", (restamped,))
            conn.execute("UPDATE statement_coverage SET imported_at = ?", (restamped,))
            conn.commit()
        after_restamp = _changes(service, since=boundary.isoformat())["data"]
    finally:
        service.close()

    assert before_restamp["basis"] == "delta", before_restamp  # type: ignore[index]
    assert before_restamp["total_added"] == added  # type: ignore[index]

    assert after_restamp["basis"] == "whole_database", after_restamp  # type: ignore[index]
    assert after_restamp["delta_available"] is False  # type: ignore[index]
    assert after_restamp["count_sources"]["rows"]["basis"] == "whole_table"  # type: ignore[index]
    assert after_restamp["count_sources"]["statements"]["basis"] == "whole_table"  # type: ignore[index]
    # Exact, and reported: the count is right, only its MEANING changed. Nulling
    # it would discard a true number to avoid mislabelling it.
    assert after_restamp["total_added"] > added  # type: ignore[index,operator]


# --- 7. one account-period is one entry, whatever the parser spelled ---------


def test_institution_drift_does_not_split_one_account_period_into_two_entries(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """`transactions.institution` is parser-supplied per row; the coverage row's
    is derived from the statement type. Bucketing on it put a statement and the
    very rows it produced into DIFFERENT entries, and the row entry reported
    `no_statement_in_window` -- whose published enum text says no statement for
    this account-period was stamped after the boundary. It was, and it is the one
    that produced those rows.

    `statement_coverage` is keyed on (account_slug, period) exactly; the entry
    identity now follows that key, and the institution follows the same authority
    the account_ref follows -- the stored `accounts` record.
    """
    _seed_expense(
        db_path,
        label="drifted-row",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-4321",
        period="2030-02",
        imported_at=datetime(2030, 5, 1),
        row_count=1,
        skipped_reason="",
        institution="Synthetic Bank",
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    entries = [entry for entry in data["entries"] if entry["period"] == "2030-02"]  # type: ignore[index]
    assert len(entries) == 1, data["entries"]  # type: ignore[index]
    assert entries[0]["rows_added"] == 1
    assert entries[0]["statement_count"] == 1
    assert entries[0]["statement_row_count"] == 1
    assert entries[0]["statement_status"] == "imported_with_activity"
    assert entries[0]["institution"] == BANK_INSTITUTION


def test_an_account_with_no_stored_record_reports_the_spelling_its_own_rows_carried(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """There is no accounts row to follow, so the observed spelling is all there
    is -- and it is chosen deterministically rather than by whichever row was
    read last."""
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-0000",
        period="2030-04",
        imported_at=datetime(2030, 5, 1),
        institution="drifted-bank",
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    entries = [entry for entry in data["entries"] if entry["period"] == "2030-04"]  # type: ignore[index]
    assert len(entries) == 1, data["entries"]  # type: ignore[index]
    assert entries[0]["account_ref_status"] == "no_account_record"
    assert entries[0]["institution"] == "drifted-bank"


# --- 8. the contract ---------------------------------------------------------


def test_the_tool_is_registered_with_an_inert_description_and_bounded_inputs() -> None:
    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.changes")

    assert "inert" in str(tool["description"])
    assert "rebuild" in str(tool["description"]).casefold()
    assert tool["inputSchema"]["additionalProperties"] is False  # type: ignore[index]
    assert set(tool["inputSchema"]["properties"]) == {  # type: ignore[index]
        "client_generation",
        "cursor",
        "limit",
        "since",
    }
    # The boundary alone. `client_generation` is accepted and informational, and
    # requiring it made the only documented loop unable to report an import.
    assert sorted(tool["inputSchema"]["required"]) == ["since"]  # type: ignore[index]
    assert tool["annotations"]["readOnlyHint"] is True  # type: ignore[index]


def test_the_reported_count_is_exactly_the_entries_the_cursor_walks(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """`entry_count` is now counted in SQL and the entries are bucketed in Python.

    Two computations of one number, so they can disagree -- and a caller pages
    until `offset` reaches `entry_count`, which means a disagreement is either an
    entry never served or a page requested past the end. Exercised over all three
    shapes at once: an account-period with rows only, one with a statement only,
    and one with both.
    """
    _seed_expense(
        db_path,
        label="rolumen-only",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-4321",
        period="2030-02",
        imported_at=datetime(2030, 5, 1),
        row_count=1,
        skipped_reason="",
    )
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-0000",
        period="2030-04",
        imported_at=datetime(2030, 5, 1),
    )
    base: dict[str, object] = {
        "since": _FIXTURE_STAMP,
        "client_generation": _generation(service),
        "limit": 1,
    }
    walked: list[tuple[str, str]] = []
    cursor: object = None
    for _ in range(10):
        arguments = base if cursor is None else {**base, "cursor": cursor}
        envelope = _structured(call_tool(service, "ledger.changes", arguments))
        walked.extend(
            (str(entry["account_ref"]), str(entry["period"]))
            for entry in envelope["data"]["entries"]  # type: ignore[index]
        )
        cursor = envelope["page"]["next_cursor"]  # type: ignore[index]
        if cursor is None:
            break

    assert cursor is None, "the walk did not terminate"
    assert len(walked) == len(set(walked)) == envelope["data"]["entry_count"]  # type: ignore[index]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"client_generation": "0" * 16},
        {"since": _FIXTURE_STAMP, "client_generation": "0" * 16, "account": "x"},
        {"since": _FIXTURE_STAMP, "client_generation": "not-a-generation"},
    ],
    ids=["empty", "no-since", "unknown-argument", "malformed-generation"],
)
def test_the_kernel_refuses_a_missing_boundary_or_unknown_argument(
    service: LedgerMCPService,
    arguments: dict[str, object],
) -> None:
    """The boundary is the contract: there is no server-chosen default for it.

    `client_generation` is not required -- it is informational -- but a SUPPLIED
    one is still validated against the published pattern rather than echoed
    unchecked onto the wire.
    """
    result = call_tool(service, "ledger.changes", arguments)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_a_change_set_larger_than_the_surface_serves_fails_closed(
    service: LedgerMCPService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both grouped sets are materialized to be merged in Python, so the merge is
    the scan. A boundary older than a rebuild is exactly the call that reaches
    this: after a promote every account-period in the database has changed."""
    monkeypatch.setattr("ledger.query.agent._MAX_CHANGE_ENTRIES", 1)

    result = call_tool(
        service,
        "ledger.changes",
        {"since": "2029-01-01T00:00:00", "client_generation": _generation(service)},
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]


def test_the_entry_cap_refuses_before_either_result_set_is_materialized(
    service: LedgerMCPService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tripwire that fires after the allocation it exists to bound is not one.

    The check ran after `.all()` on the grouped transaction rows AND on full
    `StatementCoverageDB` ORM objects. Its sibling `_MAX_ACCOUNT_GROUPS` counts
    the distinct accounts in SQL and raises before fetching anything; this counts
    the distinct account-periods the same way.
    """
    import sqlalchemy.orm

    generation = _generation(service)
    monkeypatch.setattr("ledger.query.agent._MAX_CHANGE_ENTRIES", 1)

    def _refuse(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("a result set was materialized before the cap was checked")

    monkeypatch.setattr(sqlalchemy.orm.Query, "all", _refuse)
    result = call_tool(
        service,
        "ledger.changes",
        {"since": "2029-01-01T00:00:00", "client_generation": generation},
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]


def test_the_surface_carries_the_two_namespace_rule_it_emits_refs_under(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """This surface emits BOTH ref namespaces and was the one that did not say so.

    A coverage row for a slug with no `accounts` record returns an `unresolved-`
    ref in a data block that carried no `account_ref_semantics` at all, while
    ledger.coverage and the account grouping -- which emit the same two
    namespaces -- both state the rule on the wire.
    """
    _seed_coverage(
        db_path,
        slug="synthetic-private-number-0000",
        period="2030-04",
        imported_at=datetime(2030, 5, 1),
    )
    data = _changes(service, client_generation=_generation(service))["data"]

    refs = {str(entry["account_ref"]) for entry in data["entries"]}  # type: ignore[index]
    assert any(ref.startswith("unresolved-") for ref in refs), refs
    assert data["account_ref_semantics"] == ACCOUNT_REF_SEMANTICS  # type: ignore[index]


def test_the_answer_validates_against_its_own_declared_schema(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    import jsonschema

    _seed_coverage(
        db_path,
        slug="synthetic-private-number-0000",
        period="2030-04",
        imported_at=datetime(2030, 5, 1),
        row_count=3,
        skipped_reason="",
    )
    _seed_expense(
        db_path,
        label="schema-row",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.changes"
    )

    envelope = _changes(service, client_generation=_generation(service))

    statuses = {str(entry["account_ref_status"]) for entry in envelope["data"]["entries"]}  # type: ignore[index]
    assert statuses == {"resolved", "no_account_record"}, statuses
    jsonschema.validate(envelope, schema)


def test_the_coverage_block_carries_the_pinned_generation_to_compare_against(
    service: LedgerMCPService,
) -> None:
    """The loop closes here: the pin the answer was served from is on the wire,
    so the next call can supply it."""
    envelope = _changes(service)

    assert envelope["coverage"]["generation"] == _generation(service)  # type: ignore[index]
    assert envelope["data"]["basis"] == "delta"  # type: ignore[index]


def test_the_surface_never_emits_row_content(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """Counts and coverage-style metadata only: no merchant, amount, or date."""
    _seed_expense(
        db_path,
        label="content-row",
        imported_at="2030-05-01 00:00:00.000000",
        account="synthetic-private-number-4321",
    )
    envelope = _changes(service, client_generation=_generation(service))

    encoded = json.dumps(envelope, ensure_ascii=False)
    for forbidden in ("SEEDED MARKET", "Groceries", "5.00", "seed.csv"):
        assert forbidden not in encoded, forbidden


def test_the_query_layer_refuses_a_timezone_aware_boundary(db_path: Path) -> None:
    """Defence in depth: the kernel's pattern is one gate, this is the other."""
    from ledger.query.agent import AgentQueryError
    from ledger.query.service import LedgerQueryService
    from ledger.storage.read_only import ReadOnlyDatabase

    database = ReadOnlyDatabase(db_path)
    generation = database.preflight().generation
    query = LedgerQueryService(
        db_path,
        session_scope_factory=database.session_scope,
        snapshot_generation=generation,
    )
    try:
        with pytest.raises(AgentQueryError, match="invalid_argument"):
            query.agent.changes(
                since=datetime(2030, 1, 1, tzinfo=UTC),
                client_generation=generation,
            )
        with pytest.raises(AgentQueryError, match="invalid_argument"):
            query.agent.changes(
                since=date(2030, 1, 1),  # type: ignore[arg-type]
                client_generation=generation,
            )
    finally:
        database.close()


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
