import csv
import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account
from typer.testing import CliRunner

from ledger.audit import StatementAuditOptions, StatementAuditResult
from ledger.audit.statements import (
    AuditRow,
    StatementAuditService,
    analyze_mismatch_causes,
    audit_row_from_transaction,
    compare_audit_rows,
    detect_statement_continuity_gaps,
    inventory_row,
    logical_account_token,
)
from ledger.cli import app
from ledger.config import AppConfig
from ledger.core import Transaction
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult
from ledger.storage.database import AccountBalanceDB, init_database, session_scope


def test_logical_row_matching_links_renamed_account_identities():
    source = [
        AuditRow(
            posting_date=date(2025, 12, 31),
            institution="harbor",
            logical_account=logical_account_token("harbor", "harbor-chequing", "Chequing"),
            account="harbor-chequing",
            account_type="Chequing",
            amount=Decimal("2468.25"),
            description="PAYROLL DEPOSIT",
            source_file="2025-12.csv",
            source_kind="source_pdf",
        )
    ]
    db = [
        AuditRow(
            posting_date=date(2025, 12, 31),
            institution="harbor",
            logical_account=logical_account_token(
                "harbor",
                "2026-01-23-harbor-deposit-chequing-1001",
                "Chequing",
            ),
            account="2026-01-23-harbor-deposit-chequing-1001",
            account_type="Chequing",
            amount=Decimal("2468.25"),
            description="PAYROLL   DEPOSIT",
            source_file="ledger.db",
            source_kind="db_transaction",
        )
    ]

    assert compare_audit_rows(source, db) == []


def test_compare_audit_rows_does_not_collapse_same_family_accounts():
    source = [
        AuditRow(
            posting_date=date(2025, 12, 31),
            institution="harbor",
            logical_account="harbor:chequing",
            account="harbor-chequing-1001",
            account_type="Chequing",
            amount=Decimal("100.00"),
            description="PAYROLL DEPOSIT",
            source_file="statement.csv",
            source_kind="source_pdf",
            account_identity="harbor-chequing-1001",
        )
    ]
    db = [
        AuditRow(
            posting_date=date(2025, 12, 31),
            institution="harbor",
            logical_account="harbor:chequing",
            account="harbor-chequing-2002",
            account_type="Chequing",
            amount=Decimal("100.00"),
            description="PAYROLL DEPOSIT",
            source_file="ledger.db",
            source_kind="db_transaction",
            account_identity="harbor-chequing-2002",
        )
    ]

    mismatches = compare_audit_rows(source, db)

    assert {item["mismatch_type"] for item in mismatches} == {"source_only", "db_only"}


def test_compare_audit_rows_does_not_match_across_currencies():
    common = {
        "posting_date": date(2025, 12, 31),
        "institution": "harbor",
        "logical_account": "harbor:chequing",
        "account": "harbor-chequing-1001",
        "account_type": "Chequing",
        "amount": Decimal("100.00"),
        "description": "PAYROLL DEPOSIT",
        "account_identity": "harbor-chequing-1001",
    }
    source = AuditRow(
        **common,
        currency="CAD",
        source_file="statement.csv",
        source_kind="source_pdf",
    )
    db = AuditRow(
        **common,
        currency="USD",
        source_file="ledger.db",
        source_kind="db_transaction",
    )

    mismatches = compare_audit_rows([source], [db])

    assert {item["mismatch_type"] for item in mismatches} == {"source_only", "db_only"}
    assert {item["currency"] for item in mismatches} == {"CAD", "USD"}


def test_audit_row_uses_alias_normalized_canonical_account_identity():
    txn = Transaction.model_construct(
        posting_date=date(2025, 12, 31),
        institution="harbor",
        account="legacy-chequing",
        account_type="Chequing",
        amount_signed=Decimal("100.00"),
        amount_raw=Decimal("100.00"),
        description="PAYROLL DEPOSIT",
        source_file="statement.csv",
        currency="CAD",
    )

    row = audit_row_from_transaction(
        txn,
        "source_pdf",
        account_aliases={"legacy-chequing": "harbor-chequing-1001"},
    )

    assert row.account_identity == "harbor-chequing-1001"


def test_audit_row_preserves_normalized_transaction_currency():
    txn = Transaction.model_construct(
        posting_date=date(2025, 12, 31),
        institution="harbor",
        account="harbor-chequing-1001",
        account_type="Chequing",
        amount_signed=Decimal("100.00"),
        amount_raw=Decimal("100.00"),
        description="PAYROLL DEPOSIT",
        source_file="statement.csv",
        currency=" usd ",
    )

    row = audit_row_from_transaction(txn, "source_pdf")

    assert row.currency == "USD"


def test_load_db_rows_applies_configured_account_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    txn = Transaction.model_construct(
        posting_date=date(2025, 12, 31),
        institution="harbor",
        account="legacy-chequing",
        account_type="Chequing",
        amount_signed=Decimal("100.00"),
        amount_raw=Decimal("100.00"),
        description="PAYROLL DEPOSIT",
        source_file="ledger.db",
        currency="CAD",
    )

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args):
            return self

        def all(self):
            return self.rows

    class FakeSession:
        def query(self, model):
            return FakeQuery([object()] if model.__name__ == "TransactionDB" else [])

    class FakeScope:
        def __enter__(self):
            return FakeSession()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("ledger.audit.statements.session_scope", lambda _path: FakeScope())
    monkeypatch.setattr("ledger.audit.statements.transaction_from_db", lambda _row: txn)

    rows = StatementAuditService()._load_db_rows(
        tmp_path / "ledger.db",
        date(2025, 12, 1),
        date(2025, 12, 31),
        account_aliases={"legacy-chequing": "harbor-chequing-1001"},
    )

    assert rows[0].account_identity == "harbor-chequing-1001"


def test_compare_audit_rows_reports_missing_and_duplicate_counts():
    source = [
        AuditRow(
            posting_date=date(2025, 12, 1),
            institution="harbor",
            logical_account="harbor:spark",
            account="harbor-sparkplus",
            account_type="sparkplus Visa",
            amount=Decimal("-10.00"),
            description="APPLE",
            source_file="statement.csv",
            source_kind="source_pdf",
        )
    ]
    db = [
        *source,
        AuditRow(
            posting_date=date(2025, 12, 2),
            institution="harbor",
            logical_account="harbor:spark",
            account="harbor-sparkplus",
            account_type="sparkplus Visa",
            amount=Decimal("-12.00"),
            description="GOOGLE",
            source_file="ledger.db",
            source_kind="db_transaction",
        ),
    ]

    mismatches = compare_audit_rows(source, db)

    assert [item["mismatch_type"] for item in mismatches] == ["db_only"]
    assert mismatches[0]["description"] == "GOOGLE"


def test_mismatch_cause_summary_detects_near_date_matches():
    mismatches = [
        {
            "mismatch_type": "source_only",
            "posting_date": "2025-12-03",
            "logical_account": "harbor:spark",
            "amount": "-10.00",
            "description_key": "APPLE COM BILL",
        },
        {
            "mismatch_type": "db_only",
            "posting_date": "2025-12-01",
            "logical_account": "harbor:spark",
            "amount": "-10.00",
            "description_key": "APPLE COM BILL",
        },
        {
            "mismatch_type": "source_only",
            "posting_date": "2025-12-10",
            "logical_account": "cedar:cedar",
            "amount": "-20.00",
            "description_key": "COSTCO MARKHAM",
        },
        {
            "mismatch_type": "db_only",
            "posting_date": "2025-12-10",
            "logical_account": "cedar:cedar",
            "amount": "-20.00",
            "description_key": "COSTCO",
        },
    ]

    causes = analyze_mismatch_causes(mismatches)

    assert causes["near_date_matches_within_5_days"] == 1
    assert causes["same_date_description_variants"] == 1
    assert causes["near_date_matches_by_day_delta"] == {"-2": 1}


@pytest.mark.parametrize(
    ("db_account_identity", "db_currency"),
    [
        ("harbor-chequing-2002", "CAD"),
        ("harbor-chequing-1001", "USD"),
    ],
)
def test_mismatch_cause_summary_does_not_cross_account_or_currency_identity(
    db_account_identity: str,
    db_currency: str,
) -> None:
    source = AuditRow(
        posting_date=date(2025, 12, 3),
        institution="harbor",
        logical_account="harbor:chequing",
        account="harbor-chequing-1001",
        account_type="Chequing",
        amount=Decimal("-10.00"),
        description="APPLE COM BILL",
        source_file="statement.csv",
        source_kind="source_pdf",
        account_identity="harbor-chequing-1001",
        currency="CAD",
    )
    db = AuditRow(
        posting_date=date(2025, 12, 1),
        institution="harbor",
        logical_account="harbor:chequing",
        account=db_account_identity,
        account_type="Chequing",
        amount=Decimal("-10.00"),
        description="APPLE COM BILL",
        source_file="ledger.db",
        source_kind="db_transaction",
        account_identity=db_account_identity,
        currency=db_currency,
    )

    causes = analyze_mismatch_causes(compare_audit_rows([source], [db]))

    assert causes["near_date_matches_within_5_days"] == 0


def test_inventory_row_reads_statement_metadata(tmp_path: Path):
    csv_path = tmp_path / "statement.csv"
    csv_path.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-12-31,PAYROLL,2468.25,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )
    result = ExtractedCsv(
        pdf=tmp_path / "statement.pdf",
        csv_path=csv_path,
        processed_pdf=Path("2025-12-23-harbor-deposit-chequing-1001.pdf"),
        statement_type="harbor_deposit",
        institution="harbor",
        balance_spec={"printed_sign": 1},
        transaction_count=1,
        account_slug="harbor-chequing-1001",
        validation_summary={
            "opening_balance": "100.00",
            "total_deposits": "2468.25",
            "total_withdrawals": "0.00",
            "closing_balance": "2568.25",
        },
    )

    row = inventory_row(tmp_path / "statement.pdf", "abc", result, False)

    assert row["statement_end_date"] == "2025-12-23"
    assert row["institution"] == "harbor"
    assert row["logical_account"] == "harbor:chequing"
    assert row["balance_kind"] == "deposit"
    assert row["account_identity"] == "harbor-chequing-1001"
    assert row["statement_closing_balance"] == "2568.25"
    assert row["in_processed_files"] is False


def test_statement_audit_passes_extracted_account_slug_mapping_to_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_app_config: AppConfig,
):
    csv_root = tmp_path / "work" / "csv"
    csv_path = csv_root / "2025-12" / "harbor-chequing-1001-2.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("stub", encoding="utf-8")
    extraction = ExtractedCsv(
        pdf=tmp_path / "statement.pdf",
        csv_path=csv_path,
        processed_pdf=None,
        statement_type="harbor_deposit",
        institution="harbor",
        balance_spec={"printed_sign": 1},
        transaction_count=1,
        account_slug="harbor-chequing-1001",
    )
    audit_options = StatementAuditOptions(
        db_path=tmp_path / "ledger.db",
        pdf_dirs=[],
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
    )
    monkeypatch.setattr(
        "ledger.audit.statements.load_ledger_config", lambda _path: sample_app_config.ledger
    )
    monkeypatch.setattr(
        "ledger.audit.statements.load_runtime_app_config",
        lambda *_args: sample_app_config,
    )
    captured: dict[str, object] = {}

    def capture_process(**kwargs):
        captured.update(kwargs)
        return ProcessingResult()

    monkeypatch.setattr("ledger.audit.statements.process", capture_process)

    StatementAuditService()._process_extracted_csvs(audit_options, csv_root, [extraction])

    assert captured["account_slugs"] == {
        "2025-12/harbor-chequing-1001-2.csv": "harbor-chequing-1001"
    }


def test_statement_audit_inventory_applies_alias_to_extracted_account_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"synthetic statement")
    extraction = ExtractedCsv(
        pdf=pdf,
        csv_path=None,
        processed_pdf=None,
        statement_type="harbor_deposit",
        institution="harbor",
        balance_spec={"printed_sign": 1},
        transaction_count=0,
        account_slug="legacy-chequing",
    )
    monkeypatch.setattr(
        "ledger.audit.statements.statement_pdf_to_csv.process_pdf",
        lambda *_args, **_kwargs: extraction,
    )

    _results, inventory = StatementAuditService()._extract_pdfs(
        [pdf],
        tmp_path / "csv",
        tmp_path / "processed",
        set(),
        account_aliases={"legacy-chequing": "harbor-chequing-1001"},
    )

    assert inventory[0]["account_identity"] == "harbor-chequing-1001"


def test_statement_audit_run_threads_aliases_through_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace

    aliases = {"legacy-chequing": "harbor-chequing-1001"}
    txn = Transaction.model_construct(
        posting_date=date(2025, 12, 31),
        institution="harbor",
        account="legacy-chequing",
        account_type="Chequing",
        amount_signed=Decimal("100.00"),
        amount_raw=Decimal("100.00"),
        description="PAYROLL DEPOSIT",
        source_file="statement.csv",
        currency="CAD",
    )
    options = StatementAuditOptions(
        db_path=tmp_path / "ledger.db",
        pdf_dirs=[],
        start_date=date(2025, 12, 1),
        end_date=date(2025, 12, 31),
        audit_runs_dir=tmp_path / "audit-runs",
        ledger_config=tmp_path / "ledger.toml",
    )
    run_dir = tmp_path / "audit-runs" / "run"
    run_dir.mkdir(parents=True)
    captured: dict[str, object] = {}
    service = StatementAuditService()

    monkeypatch.setattr(
        "ledger.audit.statements.load_ledger_config",
        lambda _path: SimpleNamespace(general=SimpleNamespace(account_aliases=aliases)),
    )
    monkeypatch.setattr(service, "_make_run_dir", lambda _path: run_dir)
    monkeypatch.setattr(service, "_discover_pdfs", lambda _paths: [])
    monkeypatch.setattr(service, "_processed_hashes", lambda _path: set())

    def fake_extract(_pdfs, _csv_root, _processed, _hashes, account_aliases=None):
        captured["extract_aliases"] = account_aliases
        return [], []

    monkeypatch.setattr(service, "_extract_pdfs", fake_extract)
    monkeypatch.setattr(
        service,
        "_process_extracted_csvs",
        lambda _options, _csv_root, _results: ProcessingResult(transactions=[txn]),
    )

    def fake_load(_db, _start, _end, account_aliases=None):
        captured["db_aliases"] = account_aliases
        return []

    monkeypatch.setattr(service, "_load_db_rows", fake_load)

    def fake_balances(_db, _inventory, account_aliases=None):
        captured["balance_aliases"] = account_aliases
        return []

    monkeypatch.setattr(service, "_compare_balances", fake_balances)
    monkeypatch.setattr(service, "_account_alias_groups", lambda _db: {})
    monkeypatch.setattr(service, "_write_csv", lambda *_args: None)
    monkeypatch.setattr(service, "_write_json", lambda *_args: None)
    monkeypatch.setattr(service, "_write_findings", lambda *_args, **_kwargs: None)

    def fake_summary(_options, _pdfs, _inventory, source_rows, *_args, **_kwargs):
        captured["source_identity"] = source_rows[0].account_identity
        return {"status": "pass"}

    monkeypatch.setattr(service, "_summary", fake_summary)

    service.run(options)

    assert captured == {
        "extract_aliases": aliases,
        "db_aliases": aliases,
        "balance_aliases": aliases,
        "source_identity": "harbor-chequing-1001",
    }


def test_deposit_balance_compare_uses_logical_account_identity(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    account_id = seed_account(
        db_path,
        "harbor",
        "2026-01-23-harbor-deposit-chequing-1001",
        account_type="Chequing",
        first_transaction_date=date(2025, 12, 24),
        last_transaction_date=date(2026, 1, 23),
    )
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=date(2025, 12, 31),
                balance=Decimal("864.20"),
                balance_basis="absolute",
                transaction_count=1,
                balance_7d_avg=Decimal("864.20"),
                balance_30d_avg=Decimal("864.20"),
                balance_7d_min=Decimal("864.20"),
                balance_7d_max=Decimal("864.20"),
                computed_at=now,
            )
        )

    inventory = [
        {
            "pdf_path": "statement.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "statement_end_date": "2025-12-31",
            "statement_closing_balance": "864.20",
        }
    ]

    assert StatementAuditService()._compare_balances(db_path, inventory) == []


def test_deposit_balance_compare_uses_exact_canonical_account_identity(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    seeded = [
        (
            seed_account(
                db_path,
                "harbor",
                account_number,
                account_type="Chequing",
                first_transaction_date=date(2025, 12, 1),
                last_transaction_date=date(2025, 12, 31),
            ),
            balance,
        )
        for account_number, balance in (
            ("harbor-chequing-1001", Decimal("100.00")),
            ("harbor-chequing-2002", Decimal("200.00")),
        )
    ]
    with session_scope(db_path) as session:
        for account_id, balance in seeded:
            session.add(
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=date(2025, 12, 31),
                    balance=balance,
                    balance_basis="absolute",
                    transaction_count=1,
                    balance_7d_avg=balance,
                    balance_30d_avg=balance,
                    balance_7d_min=balance,
                    balance_7d_max=balance,
                    computed_at=now,
                )
            )

    inventory = [
        {
            "pdf_path": "statement.pdf",
            "statement_type": "harbor_deposit",
            "balance_kind": "deposit",
            "logical_account": "harbor:chequing",
            "account_identity": "harbor-chequing-1001",
            "statement_end_date": "2025-12-31",
            "statement_closing_balance": "200.00",
        }
    ]

    mismatches = StatementAuditService()._compare_balances(db_path, inventory)

    assert len(mismatches) == 1
    assert mismatches[0]["status"] == "mismatch"
    assert mismatches[0]["db_balances"] == "harbor-chequing-1001=100.00"


def test_audit_statements_fail_on_issues_exits_nonzero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "ledger.db"
    db_path.touch()

    class FakeStatementAuditService:
        def run(self, _options):
            return StatementAuditResult(
                run_dir=tmp_path / "audit-run",
                status="issues_found",
                summary={"counts": {"row_mismatches": 1}},
            )

    monkeypatch.setattr("ledger.audit.StatementAuditService", FakeStatementAuditService)

    result = CliRunner().invoke(
        app,
        [
            "audit",
            "statements",
            "--db",
            str(db_path),
            "--pdf-dir",
            str(tmp_path),
            "--audit-runs-dir",
            str(tmp_path / "audit-runs"),
            "--fail-on-issues",
        ],
    )

    assert result.exit_code == 1
    assert "Statement audit: issues_found" in result.output
    assert "row_mismatches: 1" in result.output


def test_audit_statements_without_fail_on_issues_preserves_inspection_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "ledger.db"
    db_path.touch()

    class FakeStatementAuditService:
        def run(self, _options):
            return StatementAuditResult(
                run_dir=tmp_path / "audit-run",
                status="issues_found",
                summary={"counts": {"row_mismatches": 1}},
            )

    monkeypatch.setattr("ledger.audit.StatementAuditService", FakeStatementAuditService)

    result = CliRunner().invoke(
        app,
        [
            "audit",
            "statements",
            "--db",
            str(db_path),
            "--pdf-dir",
            str(tmp_path),
            "--audit-runs-dir",
            str(tmp_path / "audit-runs"),
        ],
    )

    assert result.exit_code == 0
    assert "Statement audit: issues_found" in result.output


def test_audit_statements_script_help_exits_successfully() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repo_root / "scripts" / "audit-statements"), "--help"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Audit source statement PDFs" in result.stdout


def test_resolve_end_defaults_to_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_end(None) must return the module-level date.today().

    Uses a frozen date to avoid TOCTOU flakiness near midnight: without the
    monkeypatch the test captured today() before calling _resolve_end, and
    _resolve_end captured it after — two independent reads that can disagree
    when a day rolls over between them.
    """
    import ledger.commands.audit as _audit_mod

    frozen = date(2026, 1, 15)

    class _FakeDate(date):
        @classmethod
        def today(cls) -> date:  # type: ignore[override]
            return frozen

    monkeypatch.setattr(_audit_mod, "date", _FakeDate)
    from ledger.commands.audit import _resolve_end

    assert _resolve_end(None) == frozen


def test_resolve_end_uses_explicit_date() -> None:
    from ledger.commands.audit import _resolve_end

    assert _resolve_end("2025-06-30") == date(2025, 6, 30)


def test_resolve_end_returns_none_for_malformed_date() -> None:
    from ledger.commands.audit import _resolve_end

    assert _resolve_end("not-a-date") is None


def test_audit_statement_scripts_have_valid_shell_syntax() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            "bash",
            "-n",
            str(repo_root / "scripts" / "audit-statements"),
            str(repo_root / "scripts" / "freeze-check"),
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_continuity_detects_missing_statement_month():
    inventory = [
        {"logical_account": "harbor:orbit", "statement_month": "2026-02"},
        {"logical_account": "harbor:orbit", "statement_month": "2026-05"},  # gap: 03, 04 missing
        {"logical_account": "harbor:meadow", "statement_month": "2026-01"},
        {"logical_account": "harbor:meadow", "statement_month": "2026-02"},  # contiguous
    ]
    gaps = detect_statement_continuity_gaps(inventory)
    orbit = next(g for g in gaps if g["logical_account"] == "harbor:orbit")
    assert orbit["missing_months"] == ["2026-03", "2026-04"]
    assert all(g["logical_account"] != "harbor:meadow" for g in gaps)  # no gap reported


def test_continuity_does_not_merge_distinct_same_family_accounts():
    inventory = [
        {
            "logical_account": "harbor:chequing",
            "account_identity": "harbor-chequing-1001",
            "statement_month": "2026-01",
        },
        {
            "logical_account": "harbor:chequing",
            "account_identity": "harbor-chequing-2002",
            "statement_month": "2026-03",
        },
    ]

    assert detect_statement_continuity_gaps(inventory) == []


def test_continuity_gap_describes_supplied_evidence_only():
    inventory = [
        {"logical_account": "harbor:orbit", "statement_month": "2026-02"},
        {"logical_account": "harbor:orbit", "statement_month": "2026-05"},
    ]
    gaps = detect_statement_continuity_gaps(inventory)
    note = gaps[0]["note"].lower()
    assert "supplied statement inventory" in note
    assert "no recorded transactions" not in note and "no transactions" not in note
    assert "no activity" not in note and "downloaded" not in note and "closed" not in note
    assert "2026-03" in gaps[0]["note"] and "2026-04" in gaps[0]["note"]


def test_continuity_ignores_rows_without_month():
    inventory = [
        {"logical_account": "harbor:spark", "statement_month": ""},
        {"logical_account": "harbor:spark", "statement_month": "2026-04"},
    ]
    assert detect_statement_continuity_gaps(inventory) == []


def test_continuity_spans_year_boundary():
    inventory = [
        {"logical_account": "harbor:visa", "statement_month": "2025-11"},
        {"logical_account": "harbor:visa", "statement_month": "2026-02"},  # 12, 01 missing
    ]
    gaps = detect_statement_continuity_gaps(inventory)
    assert gaps[0]["missing_months"] == ["2025-12", "2026-01"]


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")


def _invented_finding(posted: date, label: str = "INVENTED ROW", **changes):
    return {
        "posting_date": posted.isoformat(),
        "institution": "invented",
        "logical_account": "invented:chequing",
        "account_identity": "invented-main",
        "currency": "CAD",
        "amount": "-17.25",
        "description": label,
        "description_key": label,
        "mismatch_type": "source_only",
        "source_count": 1,
        "db_count": 0,
    } | changes


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2038, 4, 7), date(2038, 4, 19)),
        (date(2041, 12, 28), date(2042, 1, 4)),
        (date(2045, 8, 3), date(2045, 8, 3)),
    ],
)
def test_findings_use_selected_interval_and_only_overlapping_identity_rows(tmp_path, start, end):
    rows = [
        _invented_finding(start, "INVENTED START ROW"),
        _invented_finding(end, "INVENTED END ROW"),
        _invented_finding(start - timedelta(days=1), "OUTSIDE ROW BEFORE"),
        _invented_finding(end + timedelta(days=1), "OUTSIDE ROW AFTER"),
    ]
    balances = [
        {
            "statement_date": posted.isoformat(),
            "logical_account": label,
            "status": "mismatch",
            "statement_closing_balance": "246.80",
            "db_balances": "invented-main=230.00",
        }
        for posted, label in (
            (start, "INVENTED BALANCE"),
            (end + timedelta(days=1), "OUTSIDE BALANCE"),
        )
    ]

    def identity(number, first, last):
        return {
            "account_number": number,
            "first": first.isoformat(),
            "last": last.isoformat(),
            "transactions": 7,
            "is_active": True,
        }

    aliases = {
        "invented:shared": [
            identity("INVENTED ACTIVE A", start - timedelta(days=8), start),
            identity("INVENTED ACTIVE B", end, end + timedelta(days=8)),
            identity("OUTSIDE IDENTITY", start - timedelta(days=8), start - timedelta(days=1)),
        ],
        "invented:single-overlap": [
            identity("SINGLE OVERLAP", start, end),
            identity("OUTSIDE SINGLE PARTNER", end + timedelta(days=1), end + timedelta(days=8)),
        ],
    }
    path = tmp_path / "findings.md"
    StatementAuditService()._write_findings(
        path, rows, balances, aliases, start_date=start, end_date=end
    )
    text = path.read_text()
    assert f"Selected interval: {start.isoformat()} to {end.isoformat()} (inclusive)." in text
    assert "Row mismatch groups: 2" in text
    assert "Balance mismatches: 1" in text
    assert "Logical account groups with multiple recorded identities: 1" in text
    assert "INVENTED START ROW" in text and "INVENTED END ROW" in text
    assert "INVENTED ACTIVE A" in text and "INVENTED ACTIVE B" in text
    assert "OUTSIDE" not in text and "SINGLE OVERLAP" not in text
    assert "do not establish that records belong to the same real account" in text
    assert "supplied statement evidence" in text
    assert "Diagnosis" not in text and "cliff" not in text and "caused" not in text


def test_findings_limits_preserve_full_counts_and_identify_truncation(tmp_path):
    start, end = date(2044, 2, 1), date(2044, 2, 29)
    rows = [_invented_finding(start, f"INVENTED DETAIL ROW {i:02}") for i in range(30)]
    balances = [
        {
            "statement_date": start.isoformat(),
            "logical_account": f"INVENTED BALANCE {i:02}",
            "status": "mismatch",
            "statement_closing_balance": "246.80",
            "db_balances": "230.00",
        }
        for i in range(28)
    ]
    aliases = {
        f"invented:identity-group-{i:02}": [
            {
                "account_number": f"INVENTED ACCOUNT {i:02}-{j:02}",
                "first": start.isoformat(),
                "last": end.isoformat(),
                "transactions": 1,
                "is_active": False,
            }
            for j in range(27)
        ]
        for i in range(26)
    }
    path = tmp_path / "findings.md"
    StatementAuditService()._write_findings(
        path, rows, balances, aliases, start_date=start, end_date=end
    )
    text = path.read_text()
    assert "Row mismatch groups: 30" in text and "Balance mismatches: 28" in text
    assert "Logical account groups with multiple recorded identities: 26" in text
    assert "Showing 25 of 30 row mismatch groups" in text
    assert "Showing 25 of 28 balance mismatches" in text
    assert "Showing 25 of 26 account identity groups" in text
    assert "Showing 25 of 27 recorded identities" in text
    assert "INVENTED DETAIL ROW 24" in text and "INVENTED DETAIL ROW 25" not in text
    assert "INVENTED BALANCE 24" in text and "INVENTED BALANCE 25" not in text
    assert "invented:identity-group-25" not in text
    assert "INVENTED ACCOUNT 00-25" not in text
    assert "row_mismatches.csv" in text and "balance_mismatches.csv" in text


def test_mismatch_patterns_scope_both_legs_to_selected_interval():
    start, end = date(2047, 7, 10), date(2047, 7, 20)
    rows = [
        _invented_finding(start, "NEAR DATE"),
        _invented_finding(start + timedelta(days=2), "NEAR DATE", mismatch_type="db_only"),
        _invented_finding(end, "BOUNDARY"),
        _invented_finding(end + timedelta(days=1), "BOUNDARY", mismatch_type="db_only"),
        _invented_finding(start, "DESCRIPTION A", amount="-21.75"),
        _invented_finding(start, "DESCRIPTION B", amount="-21.75", mismatch_type="db_only"),
    ]
    result = analyze_mismatch_causes(rows, start_date=start, end_date=end)
    assert result == {
        "near_date_matches_within_5_days": 1,
        "same_date_description_variants": 1,
        "near_date_matches_by_day_delta": {"2": 1},
    }
    assert analyze_mismatch_causes(rows)["near_date_matches_within_5_days"] == 2


def test_audit_run_writes_neutral_findings_and_scopes_balance_counts(tmp_path, monkeypatch):
    from types import SimpleNamespace

    start, end = date(2046, 5, 1), date(2046, 5, 31)
    options = StatementAuditOptions(
        db_path=tmp_path / "unused.db",
        pdf_dirs=[],
        start_date=start,
        end_date=end,
        audit_runs_dir=tmp_path / "audit",
        ledger_config=tmp_path / "synthetic.toml",
    )
    service = StatementAuditService()
    monkeypatch.setattr(
        "ledger.audit.statements.load_ledger_config",
        lambda _p: SimpleNamespace(general=SimpleNamespace(account_aliases={})),
    )
    monkeypatch.setattr(service, "_discover_pdfs", lambda _p: [])
    monkeypatch.setattr(service, "_processed_hashes", lambda _p: set())
    monkeypatch.setattr(service, "_extract_pdfs", lambda *_a: ([], []))
    monkeypatch.setattr(service, "_process_extracted_csvs", lambda *_a: ProcessingResult())
    monkeypatch.setattr(service, "_load_db_rows", lambda *_a: [])
    monkeypatch.setattr(service, "_account_alias_groups", lambda *_a: {})
    monkeypatch.setattr(
        service,
        "_compare_balances",
        lambda *_a: [
            {
                "statement_date": posted.isoformat(),
                "logical_account": "invented:chequing",
                "status": "mismatch",
                "statement_closing_balance": "246.80",
                "db_balances": "230.00",
            }
            for posted in (start - timedelta(days=1), start, end + timedelta(days=1))
        ],
    )
    result = service.run(options)
    assert result.status == "issues_found"
    assert result.summary["counts"]["balance_mismatches"] == 1
    assert result.summary["mismatch_patterns"] == {
        "near_date_matches_within_5_days": 0,
        "same_date_description_variants": 0,
        "near_date_matches_by_day_delta": {},
    }
    assert {p.name for p in result.run_dir.glob("*.md")} == {"findings.md"}
    text = (result.run_dir / "findings.md").read_text()
    assert "Balance mismatches: 1" in text
    assert start.isoformat() in text and end.isoformat() in text
    assert (start - timedelta(days=1)).isoformat() not in text
    assert (end + timedelta(days=1)).isoformat() not in text
    assert not (result.run_dir / "work").exists()


def _invented_inventory_month(month, **changes):
    return {
        "institution": "invented-a",
        "account_identity": "invented-main",
        "logical_account": "invented-a:chequing",
        "currency": "CAD",
        "statement_month": month,
        "statement_end_date": month + "-01",
        "pdf_path": "invented.pdf",
        "folder": "invented",
        "in_processed_files": False,
    } | changes


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2054, 10, 1), date(2054, 10, 31), ["2054-10"]),
        (date(2054, 10, 31), date(2054, 11, 1), ["2054-10", "2054-11"]),
        (date(2054, 10, 15), date(2054, 10, 15), ["2054-10"]),
        (date(2054, 7, 1), date(2054, 8, 1), []),
        (date(2054, 12, 1), date(2055, 1, 31), []),
        (date(2054, 10, 31), None, ["2054-10", "2054-11"]),
        (None, date(2054, 10, 1), ["2054-09", "2054-10"]),
    ],
)
def test_continuity_scopes_missing_months_to_selected_interval(start, end, expected):
    inventory = [_invented_inventory_month("2054-08"), _invented_inventory_month("2054-12")]
    gaps = detect_statement_continuity_gaps(inventory, start_date=start, end_date=end)
    assert [month for gap in gaps for month in gap["missing_months"]] == expected
    assert detect_statement_continuity_gaps(inventory)[0]["missing_months"] == [
        "2054-09",
        "2054-10",
        "2054-11",
    ]


@pytest.mark.parametrize(
    "invalid", ["2054-00", "2054-13", "2054-99", "0000-12", "2054-2", "not-a-month"]
)
def test_continuity_ignores_invalid_calendar_months(invalid):
    inventory = [_invented_inventory_month("2054-08"), _invented_inventory_month(invalid)]
    assert detect_statement_continuity_gaps(inventory) == []


@pytest.mark.parametrize(
    "changes",
    [{"institution": "invented-b"}, {"account_identity": "invented-second"}, {"currency": "USD"}],
)
def test_continuity_preserves_institution_account_and_currency_boundaries(changes):
    inventory = [
        _invented_inventory_month("2054-08"),
        _invented_inventory_month("2054-12", **changes),
    ]
    assert detect_statement_continuity_gaps(inventory) == []


def test_continuity_missing_identity_does_not_join_unattributed_evidence():
    inventory = [
        _invented_inventory_month(month, account_identity="", logical_account="")
        for month in ("2054-08", "2054-12")
    ]
    assert detect_statement_continuity_gaps(inventory) == []


def _invented_audit_row(posted, *, description="INVENTED PAYMENT", **changes):
    return AuditRow(
        **(
            {
                "posting_date": posted,
                "institution": "invented-a",
                "logical_account": "invented-a:chequing",
                "account_identity": "invented-main",
                "account": "invented-main",
                "account_type": "Chequing",
                "currency": "CAD",
                "amount": Decimal("-9.31"),
                "description": description,
                "source_file": "invented.csv",
                "source_kind": "source_pdf",
            }
            | changes
        )
    )


@pytest.mark.parametrize("kind", ["near-date", "description"])
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"institution": "invented-b"}, 0),
        ({"account_identity": "invented-second"}, 0),
        ({"currency": "USD"}, 0),
        (
            {
                "institution": " INVENTED_A ",
                "account_identity": "INVENTED_MAIN",
                "currency": " cad ",
            },
            1,
        ),
        ({}, 1),
    ],
)
def test_pattern_pairing_respects_strict_identity_boundaries(kind, changes, expected):
    posted = date(2054, 10, 15)
    source = _invented_audit_row(posted)
    db = _invented_audit_row(
        posted + timedelta(days=1 if kind == "near-date" else 0),
        description="INVENTED PAYMENT" if kind == "near-date" else "INVENTED VARIANT",
        source_kind="db_transaction",
        **changes,
    )
    mismatches = compare_audit_rows([source], [db])
    assert len(mismatches) == 2
    patterns = analyze_mismatch_causes(mismatches)
    key = (
        "near_date_matches_within_5_days"
        if kind == "near-date"
        else "same_date_description_variants"
    )
    assert patterns[key] == expected


@pytest.mark.parametrize("kind", ["near-date", "description"])
@pytest.mark.parametrize("db_groups", [1, 2])
def test_pattern_pairing_consumes_each_mismatch_group_once(kind, db_groups):
    posted = date(2054, 10, 15)
    source = [
        _invented_audit_row(
            posted + timedelta(days=i if kind == "near-date" else 0),
            description="INVENTED PAYMENT" if kind == "near-date" else f"INVENTED SOURCE {i}",
        )
        for i in range(2)
    ]
    db = [
        _invented_audit_row(
            posted + timedelta(days=3 + i if kind == "near-date" else 0),
            description="INVENTED PAYMENT" if kind == "near-date" else f"INVENTED DB {i}",
            source_kind="db_transaction",
        )
        for i in range(db_groups)
    ]
    # Repeated occurrences remain a comparison group, not extra candidate matches.
    mismatches = compare_audit_rows([*source, source[0]], db)
    assert sum(item["source_count"] for item in mismatches) == 3
    patterns = analyze_mismatch_causes(mismatches)
    key = (
        "near_date_matches_within_5_days"
        if kind == "near-date"
        else "same_date_description_variants"
    )
    assert patterns[key] == db_groups


def _run_invented_audit(tmp_path, monkeypatch, *, inventory, db_rows, transactions=()):
    from types import SimpleNamespace

    service = StatementAuditService()
    options = StatementAuditOptions(
        db_path=tmp_path / "unused.db",
        pdf_dirs=[],
        start_date=date(2054, 10, 1),
        end_date=date(2054, 10, 31),
        audit_runs_dir=tmp_path / "audit",
        ledger_config=tmp_path / "invented.toml",
    )
    monkeypatch.setattr(
        "ledger.audit.statements.load_ledger_config",
        lambda _p: SimpleNamespace(general=SimpleNamespace(account_aliases={})),
    )
    monkeypatch.setattr(service, "_discover_pdfs", lambda _p: [])
    monkeypatch.setattr(service, "_processed_hashes", lambda _p: set())
    monkeypatch.setattr(service, "_extract_pdfs", lambda *_a: ([], inventory))
    monkeypatch.setattr(
        service,
        "_process_extracted_csvs",
        lambda *_a: ProcessingResult(transactions=list(transactions)),
    )
    monkeypatch.setattr(service, "_load_db_rows", lambda *_a: db_rows)
    monkeypatch.setattr(service, "_compare_balances", lambda *_a: [])
    monkeypatch.setattr(service, "_account_alias_groups", lambda *_a: {})
    return service.run(options)


def test_service_continuity_summary_and_csv_only_describe_selected_supplied_gaps(
    tmp_path, monkeypatch
):
    inventory = [_invented_inventory_month("2054-08"), _invented_inventory_month("2054-12")]
    result = _run_invented_audit(
        tmp_path,
        monkeypatch,
        inventory=inventory,
        db_rows=[_invented_audit_row(date(2054, 10, 15), source_kind="db_transaction")],
    )
    assert result.summary["counts"]["db_rows"] == 1
    gaps = result.summary["statement_continuity_gaps"]["gaps"]
    assert len(gaps) == 1 and gaps[0]["missing_months"] == ["2054-10"]
    assert "2054-09" not in gaps[0]["note"] and "2054-11" not in gaps[0]["note"]
    assert "supplied statement inventory" in gaps[0]["note"]
    assert "no recorded transactions" not in gaps[0]["note"]
    assert "no activity" not in gaps[0]["note"] and "downloaded" not in gaps[0]["note"]
    saved_summary = json.loads((result.run_dir / "summary.json").read_text())
    assert saved_summary["statement_continuity_gaps"]["gaps"] == gaps
    with (result.run_dir / "statement_continuity_gaps.csv").open(newline="") as stream:
        saved_gaps = list(csv.DictReader(stream))
    assert len(saved_gaps) == 1 and saved_gaps[0]["missing_months"] == "2054-10"
    assert saved_gaps[0]["note"] == gaps[0]["note"]


@pytest.mark.parametrize("kind", ["near-date", "description"])
def test_service_summary_and_findings_do_not_pair_distinct_institutions(
    tmp_path, monkeypatch, kind
):
    txn = Transaction.model_construct(
        posting_date=date(2054, 10, 15),
        institution="invented-a",
        account="invented-main",
        account_type="Chequing",
        currency="CAD",
        amount_signed=Decimal("-9.31"),
        amount_raw=Decimal("9.31"),
        description="INVENTED PAYMENT",
        source_file="invented.csv",
    )
    db = _invented_audit_row(
        date(2054, 10, 16 if kind == "near-date" else 15),
        institution="invented-b",
        source_kind="db_transaction",
        description="INVENTED PAYMENT" if kind == "near-date" else "INVENTED VARIANT",
    )
    result = _run_invented_audit(
        tmp_path, monkeypatch, inventory=[], db_rows=[db], transactions=[txn]
    )
    assert result.summary["counts"]["row_mismatches"] == 2
    assert result.summary["mismatch_patterns"] == {
        "near_date_matches_within_5_days": 0,
        "same_date_description_variants": 0,
        "near_date_matches_by_day_delta": {},
    }
    findings = (result.run_dir / "findings.md").read_text()
    assert "Candidate near-date row pairs within five days: 0" in findings
    assert "Candidate same-date description variants: 0" in findings


def test_continuity_present_selected_month_does_not_report_external_gaps():
    inventory = [_invented_inventory_month(month) for month in ("2054-08", "2054-10", "2054-12")]
    assert (
        detect_statement_continuity_gaps(
            inventory, start_date=date(2054, 10, 1), end_date=date(2054, 10, 31)
        )
        == []
    )
    assert detect_statement_continuity_gaps(inventory)[0]["missing_months"] == [
        "2054-09",
        "2054-11",
    ]


def test_continuity_legacy_logical_account_preserves_institution_boundary():
    inventory = [
        _invented_inventory_month("2054-08", institution="", logical_account="invented-a:chequing"),
        _invented_inventory_month("2054-12", institution="", logical_account="invented-b:chequing"),
    ]
    assert detect_statement_continuity_gaps(inventory) == []


def test_continuity_unknown_currency_remains_unavailable():
    inventory = [_invented_inventory_month(month, currency="") for month in ("2054-08", "2054-12")]
    gap = detect_statement_continuity_gaps(inventory)[0]
    assert gap["currency"] == ""
    assert gap["institution"] == "invented-a"
    assert gap["first_month"] == "2054-08" and gap["last_month"] == "2054-12"
