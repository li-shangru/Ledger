"""Tests for the shared account-reference salt and the CLI verb that seeds it.

The salt keys the HMAC behind every opaque ``acct-…`` reference. It used to be
minted only as a side effect of running an export (a WRITE), so a read-only
consumer could never obtain one. These tests pin the extracted helpers, the new
``config ensure-account-salt`` verb, and — most importantly — that the export
path still derives byte-identical refs whether the salt was seeded ahead of time
or minted by the export itself.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from account_fixtures import seed_account
from typer.testing import CliRunner

from ledger.cli import app
from ledger.exporting import ExportPackageService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.database import (
    ConfigMetadataDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import (
    ACCOUNT_REF_SALT_KEY,
    account_ref,
    ensure_account_ref_salt,
    read_account_ref_salt,
)

INSTITUTION = "harbor"
ACCOUNT = "4111111111111111"
ACCOUNT_TYPE = "visa"

# A fixed salt keeps producing the same generic and banking-domain references.
# These values independently pin both derivation contracts.
FIXED_SALT = "FIXED-TEST-SALT-0123456789abcdef"
GENERIC_GOLDEN_REF = "acct-7e00edce6d"
BANKING_GOLDEN_REF = "acct-f3eed95336"


def _seed_transaction(db_path: Path, txn_id: str = "txn001") -> None:
    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution=INSTITUTION,
                account=ACCOUNT,
                account_type=ACCOUNT_TYPE,
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=False,
                currency="CAD",
                amount_raw=Decimal("25.00"),
                amount_signed=Decimal("-25.00"),
                description="COFFEE SHOP",
                merchant="COFFEE SHOP",
                category="Food & Dining",
                source_file="statement.csv",
                source_path="/Users/example/private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _write_salt(db_path: Path, salt: str) -> None:
    """Plant a known salt the way a prior CLI run (or a promoted DB) would leave it."""
    with session_scope(db_path) as session:
        session.merge(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=salt))


def _ledger_with_transaction(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    init_database(db_path)
    _seed_transaction(db_path)
    # The export resolves account_ref through the `accounts` dimension, so the
    # dimension must exist for the same account the transaction posts to --
    # carrying the type the transaction carries, which is what detection derives.
    # The golden ref below is unchanged by this: the derivation reads the same
    # three values from a different table.
    seed_account(db_path, INSTITUTION, ACCOUNT, account_type=ACCOUNT_TYPE)
    return db_path


def _exported_account_refs(db_path: Path, dest_dir: Path) -> set[str]:
    """Run a share-safe export and return the account_ref values it emitted."""
    result = ExportPackageService(db_path).create_package(
        dest_dir=dest_dir,
        preset="chatgpt-safe",
        export_format="zip",
    )
    with zipfile.ZipFile(result.path) as package:
        rows = package.read("monthly_trend.csv").decode("utf-8").splitlines()
    index = rows[0].split(",").index("account_ref")
    return {line.split(",")[index] for line in rows[1:] if line.strip()}


# =============================================================================
# The shared helpers
# =============================================================================


def test_ensure_mints_once_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    first = ensure_account_ref_salt(db_path)
    second = ensure_account_ref_salt(db_path)

    assert first
    assert second == first
    # secrets.token_hex(32) — 64 lowercase hex characters, unchanged from the
    # format the export used to mint.
    assert re.fullmatch(r"[0-9a-f]{64}", first)


def test_read_never_mints(tmp_path: Path) -> None:
    """The read-only helper must return None rather than writing a salt."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    assert read_account_ref_salt(db_path) is None
    assert read_account_ref_salt(db_path) is None

    minted = ensure_account_ref_salt(db_path)
    assert read_account_ref_salt(db_path) == minted


def test_account_ref_is_stable_namespaced_and_salt_dependent() -> None:
    ref = account_ref(FIXED_SALT, INSTITUTION, ACCOUNT, ACCOUNT_TYPE)

    assert ref.startswith("acct-")
    assert re.fullmatch(r"acct-[0-9a-f]{10}", ref)
    # Stable: the same inputs always derive the same reference.
    assert account_ref(FIXED_SALT, INSTITUTION, ACCOUNT, ACCOUNT_TYPE) == ref
    # And byte-identical to what the pre-extraction derivation produced.
    assert ref == GENERIC_GOLDEN_REF
    # A different salt gives a different ref for the SAME account.
    assert account_ref("another-salt-entirely", INSTITUTION, ACCOUNT, ACCOUNT_TYPE) != ref


def test_account_ref_distinguishes_the_identity_fields() -> None:
    base = account_ref(FIXED_SALT, INSTITUTION, ACCOUNT, ACCOUNT_TYPE)

    assert account_ref(FIXED_SALT, "birch", ACCOUNT, ACCOUNT_TYPE) != base
    assert account_ref(FIXED_SALT, INSTITUTION, "4111111111111112", ACCOUNT_TYPE) != base
    assert account_ref(FIXED_SALT, INSTITUTION, ACCOUNT, "chequing") != base


# =============================================================================
# The CLI verb
# =============================================================================


def test_cli_mints_then_reports_the_existing_salt(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    first = runner.invoke(app, ["config", "ensure-account-salt", "--db", str(db_path)])
    assert first.exit_code == 0, first.output
    assert "Minted" in first.output

    salt = read_account_ref_salt(db_path)
    assert salt

    second = runner.invoke(app, ["config", "ensure-account-salt", "--db", str(db_path)])
    assert second.exit_code == 0, second.output
    assert "Minted" not in second.output
    assert "already" in second.output.lower()
    # Idempotent: the second run must not replace the salt, or every reference
    # already handed out would silently change.
    assert read_account_ref_salt(db_path) == salt


def test_cli_never_prints_the_salt(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    minting = runner.invoke(app, ["config", "ensure-account-salt", "--db", str(db_path)])
    salt = read_account_ref_salt(db_path)
    assert salt
    reporting = runner.invoke(app, ["config", "ensure-account-salt", "--db", str(db_path)])

    for output in (minting.output, reporting.output):
        assert salt not in output
        # Console wrapping could split the secret across lines, so check every
        # chunk rather than the whole string.
        for start in range(0, len(salt), 16):
            assert salt[start : start + 16] not in output


# =============================================================================
# Export behaviour is unchanged
# =============================================================================


def test_export_reuses_a_cli_seeded_salt_instead_of_minting(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = _ledger_with_transaction(tmp_path, "seeded.db")

    result = runner.invoke(app, ["config", "ensure-account-salt", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    seeded = read_account_ref_salt(db_path)
    assert seeded

    refs = _exported_account_refs(db_path, tmp_path / "exports-seeded")

    assert read_account_ref_salt(db_path) == seeded, "the export re-minted a pre-seeded salt"
    assert refs == {banking_account_ref(seeded, INSTITUTION, ACCOUNT, ACCOUNT_TYPE)}


def test_cli_seeded_export_matches_a_self_minted_export(tmp_path: Path) -> None:
    """Same salt, same account, same ref — regardless of which path minted it."""
    minting_db = _ledger_with_transaction(tmp_path, "minting.db")
    minted_refs = _exported_account_refs(minting_db, tmp_path / "exports-minting")
    salt = read_account_ref_salt(minting_db)
    assert salt

    seeded_db = _ledger_with_transaction(tmp_path, "preseeded.db")
    _write_salt(seeded_db, salt)
    seeded_refs = _exported_account_refs(seeded_db, tmp_path / "exports-preseeded")

    assert minted_refs
    assert seeded_refs == minted_refs


def test_export_ref_matches_the_shared_derivation_for_a_known_salt(tmp_path: Path) -> None:
    """The v1.6 banking namespace keeps producing its pinned stable reference."""
    db_path = _ledger_with_transaction(tmp_path, "golden.db")
    _write_salt(db_path, FIXED_SALT)

    assert _exported_account_refs(db_path, tmp_path / "exports-golden") == {BANKING_GOLDEN_REF}


def test_export_service_salt_is_the_shared_salt(tmp_path: Path) -> None:
    """ExportPackageService must read and write the same config_metadata row."""
    db_path = _ledger_with_transaction(tmp_path, "service.db")
    service = ExportPackageService(db_path)

    salt = service._account_salt()

    assert salt == read_account_ref_salt(db_path)
    # Cached on the instance and stable on re-read, exactly as before.
    assert service._account_salt() == salt
    assert ensure_account_ref_salt(db_path) == salt
