"""Fictional parse-only adapters exercise core investment publication safeguards."""

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.adapters.contracts import AdapterError, InvestmentCapability, InvestmentFormat
from ledger.imports.pdf import files as pdf_files
from ledger.investments import workflow
from ledger.investments.models import (
    InvestmentAccount,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentAssetBalance,
    InvestmentCashBalance,
    InvestmentPerformance,
    InvestmentPosition,
    InvestmentStatement,
)
from ledger.storage.database import InvestmentImportRunDB, session_scope

START = date(2031, 4, 1)
END = date(2031, 4, 30)
TEXT = "FICTIONAL statement ACCOUNT-7 2031-04-01 2031-04-30"


def statement(content: bytes) -> InvestmentStatement:
    return InvestmentStatement(
        statement_id="fictional-statement",
        account=InvestmentAccount(
            account_id="fictional-account",
            institution="fictional",
            source_account_id="ACCOUNT-7",
            account_alias="SAVINGS",
            account_type="investment",
            base_currency="CAD",
            status="unknown",
        ),
        period_start=START,
        period_end=END,
        base_currency="CAD",
        total_nav=Decimal("17.00"),
        asset_balances=(
            InvestmentAssetBalance(asset_class="Cash", current_total=Decimal("17.00")),
        ),
        positions=(),
        activities=(),
        parsed_fingerprint=hashlib.sha256(content).hexdigest(),
    )


@pytest.fixture
def adapter(monkeypatch):
    binding = {"schema_version": 1, "core_sha256": "1" * 64, "adapters": [{"synthetic": "2" * 64}]}
    fmt = InvestmentFormat(
        format_id="fictional-investments",
        institution="fictional",
        source_layout="account_folders",
        capabilities=frozenset(
            {
                InvestmentCapability.COMPLETE_STATEMENT,
                InvestmentCapability.POSITIONS,
                InvestmentCapability.BALANCES,
            }
        ),
        recognizes_text=lambda text: text.startswith("FICTIONAL statement"),
        parse=statement,
        normalize_alias=lambda alias: (
            "SAVINGS" if alias.casefold() == "savings folder" else alias.upper()
        ),
        period_from_text=lambda text: (START, END) if "2031-04-01 2031-04-30" in text else None,
        matches_account=lambda text, account: account in text.split(),
        evidence_only_aliases=("SAVINGS",),
        default=True,
    )
    state = {"format": fmt, "binding": binding, "calls": []}

    def snapshot():
        state["calls"].append("snapshot")
        return deepcopy(state["binding"])

    def verify(value, *, execute=False):
        state["calls"].append("verify")
        if value != state["binding"]:
            raise AdapterError("import_binding_changed")

    def get(institution=None):
        state["calls"].append("load")
        assert institution in (None, "fictional", "fictional-investments")
        return state["format"]

    monkeypatch.setattr(workflow, "snapshot_bindings", snapshot, raising=False)
    monkeypatch.setattr(workflow, "verify_bindings", verify, raising=False)
    monkeypatch.setattr(workflow, "get_investment_format", get, raising=False)
    monkeypatch.setattr(workflow, "_extract_pinned_pdf_text", lambda content: content.decode())
    return state


def inputs(tmp_path: Path, *, flat=False):
    inbox = tmp_path / "inbox"
    source = inbox if flat else inbox / "savings folder"
    source.mkdir(parents=True)
    (source / "statement.pdf").write_text(TEXT)
    if not flat:
        (source / "statement.csv").write_text("fictional structured statement")
    return {
        "inbox_dir": inbox,
        "db_path": tmp_path / "live.db",
        "runs_dir": tmp_path / "runs",
        "processed_dir": tmp_path / "processed",
        "backup_dir": tmp_path / "backups",
    }


def stage(tmp_path, adapter, *, flat=False, **kwargs):
    if flat:
        adapter["format"] = replace(adapter["format"], source_layout="flat")
    args = inputs(tmp_path, flat=flat)
    lifecycle = (
        {}
        if flat
        else {
            "closed_aliases": ("savings folder",),
            "transfer_destination": "Fictional destination",
            "status_confirmed_on": date(2031, 5, 1),
        }
    )
    lifecycle.update(kwargs)
    run = workflow.stage_investment_import(**args, institution="fictional", **lifecycle)
    return run, args


def test_fictional_paired_adapter_preserves_archive_and_lifecycle(tmp_path, adapter):
    run, args = stage(tmp_path, adapter)
    manifest = workflow.investment_import_status(run)
    assert adapter["calls"][0] == "snapshot"
    assert manifest["institution"] == "fictional"
    assert manifest["source_layout"] == "account_folders"
    assert manifest["lifecycle"]["closed_aliases"] == ["SAVINGS"]
    assert not args["db_path"].exists()
    result = workflow.finalize_investment_import(run)
    assert result["status"] == "finalized"
    for source in result["sources"]:
        expected = f"fictional-savings-2031-04-01_2031-04-30-{source['sha256'][:12]}"
        suffix = ".csv" if source["role"] == "csv" else ".pdf"
        assert (
            source["archive_relative"] == f"investments/fictional/SAVINGS/2031/{expected}{suffix}"
        )
        assert (args["processed_dir"] / source["archive_relative"]).exists()


def test_flat_adapter_uses_pdf_archive_template(tmp_path, adapter):
    run, args = stage(tmp_path, adapter, flat=True)
    result = workflow.finalize_investment_import(run)
    assert result["status"] == "finalized"
    (source,) = result["sources"]
    expected = f"investments/fictional/SAVINGS/2031/2031-04-30-fictional-savings-2031-04-01-{source['sha256'][:12]}.pdf"
    assert source["archive_relative"] == expected
    assert (args["processed_dir"] / expected).read_text() == TEXT


@pytest.mark.parametrize("flat", [False, True])
def test_manifest_hashes_source_names_without_disclosing_private_basename(tmp_path, adapter, flat):
    args = inputs(tmp_path, flat=flat)
    if flat:
        adapter["format"] = replace(adapter["format"], source_layout="flat")
    source_dir = args["inbox_dir"] if flat else args["inbox_dir"] / "savings folder"
    private_stem = "FICTIONAL-ACCOUNT-91827364-账单"
    expected = {}
    for source in source_dir.iterdir():
        renamed = source.with_name(private_stem + source.suffix)
        source.rename(renamed)
        expected[source.suffix] = hashlib.sha256(renamed.name.encode("utf-8")).hexdigest()

    run = workflow.stage_investment_import(**args, institution="fictional")
    manifest = workflow.investment_import_status(run)

    assert "91827364" not in (run / "manifest.json").read_text()
    for source in manifest["sources"]:
        assert "source_name" not in source
        suffix = ".csv" if source["role"] == "csv" else ".pdf"
        assert source["source_name_hash"] == expected[suffix]
    assert workflow.finalize_investment_import(run)["status"] == "finalized"


@pytest.mark.parametrize("invalid", [None, 123, "0" * 63, "g" * 64, "A" * 64])
def test_manifest_rejects_invalid_source_name_hash(tmp_path, adapter, invalid):
    run, _ = stage(tmp_path, adapter, flat=True)
    manifest = workflow.investment_import_status(run)
    manifest["sources"][0]["source_name_hash"] = invalid
    with pytest.raises(ValueError, match="source name"):
        workflow._manifest_source_layout(manifest)


def test_source_name_hash_is_bound_to_staging_receipt(tmp_path, adapter):
    run, args = stage(tmp_path, adapter, flat=True)
    manifest = workflow.investment_import_status(run)
    manifest["sources"][0]["source_name_hash"] = "0" * 64
    workflow._write_manifest(run, manifest)
    with pytest.raises(ValueError, match="source binding"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()


def test_flat_legacy_name_hash_absence_preserves_adapter_free_archive_recovery(
    tmp_path, adapter, monkeypatch
):
    run, _ = stage(tmp_path, adapter, flat=True)
    manifest = workflow.investment_import_status(run)
    for source in manifest["sources"]:
        source.pop("source_name_hash")
    manifest["lifecycle"]["source_binding_fingerprint"] = workflow._flat_source_binding_fingerprint(
        manifest
    )
    # Model the recorded receipt produced before optional name hashes existed.
    with session_scope(run / "staging.db") as session:
        session.get(InvestmentImportRunDB, run.name).lifecycle_inputs = manifest["lifecycle"]
    manifest["staging_import_owned"] = workflow.import_owned_fingerprint(run / "staging.db")
    workflow._write_manifest(run, manifest)
    archive = workflow._archive_source

    def fail_archive(*args, **kwargs):
        raise OSError("synthetic archive interruption")

    monkeypatch.setattr(workflow, "_archive_source", fail_archive)
    assert workflow.finalize_investment_import(run)["status"] == "archive_pending"
    monkeypatch.setattr(workflow, "_archive_source", archive)

    def unavailable(*args, **kwargs):
        pytest.fail("Legacy published sources recover without an adapter")

    monkeypatch.setattr(workflow, "get_investment_format", unavailable)
    monkeypatch.setattr(workflow, "verify_bindings", unavailable)
    monkeypatch.setattr(workflow, "_extract_pinned_pdf_text", unavailable)
    assert workflow.finalize_investment_import(run)["status"] == "finalized"


@pytest.mark.parametrize("replacement", [TEXT + " changed", "unrelated statement"])
def test_flat_tail_refuses_reused_source_basename(tmp_path, adapter, monkeypatch, replacement):
    run, args = stage(tmp_path, adapter, flat=True)
    source = next(args["inbox_dir"].glob("*.pdf"))
    renamed = source.with_name("renamed.pdf")
    source.rename(renamed)
    archive = workflow._archive_source

    def fail_archive(*args, **kwargs):
        raise OSError("synthetic stop")

    monkeypatch.setattr(workflow, "_archive_source", fail_archive)
    assert workflow.finalize_investment_import(run)["status"] == "archive_pending"
    monkeypatch.setattr(workflow, "_archive_source", archive)
    source.write_text(replacement)
    before = args["db_path"].read_bytes()
    with pytest.raises(RuntimeError, match="source"):
        workflow.finalize_investment_import(run)
    assert args["db_path"].read_bytes() == before
    assert source.read_text() == replacement
    assert renamed.read_text() == TEXT
    source.rename(tmp_path / "new-statement.pdf")
    assert workflow.finalize_investment_import(run)["status"] == "finalized"


def test_paired_import_without_closure_keeps_unknown_lifecycle(tmp_path, adapter):
    args = inputs(tmp_path)
    run = workflow.stage_investment_import(**args, institution="fictional")
    manifest = workflow.investment_import_status(run)
    assert manifest["lifecycle"]["closed_aliases"] == []
    assert manifest["lifecycle"]["transfer_destination"] is None
    assert manifest["lifecycle"]["status_confirmed_on"] is None
    workflow.finalize_investment_import(run)
    from ledger.storage.database import InvestmentAccountDB

    with session_scope(args["db_path"]) as session:
        account = session.query(InvestmentAccountDB).one()
        assert account.status == "unknown"
        assert account.closed_on is None


def test_parser_preserves_display_case_institution_without_changing_identity(adapter):
    expected = statement(b"fictional")
    mixed = expected.model_copy(
        update={"account": expected.account.model_copy(update={"institution": "Fictional"})}
    )
    fmt = replace(adapter["format"], parse=lambda content: mixed)
    result = workflow._parse_statement(b"fictional", fmt)
    assert result.account.institution == "Fictional"
    assert result.parsed_fingerprint == expected.parsed_fingerprint
    assert result.statement_id == expected.statement_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"closed_aliases": ("SAVINGS",)},
        {"transfer_destination": "elsewhere"},
        {"status_confirmed_on": END},
    ],
)
def test_flat_rejects_any_supplied_lifecycle(tmp_path, adapter, kwargs):
    with pytest.raises(ValueError, match="do not accept lifecycle"):
        stage(tmp_path, adapter, flat=True, **kwargs)
    assert not (tmp_path / "live.db").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"closed_aliases": ()},
        {"transfer_destination": None},
        {"status_confirmed_on": None},
    ],
)
def test_paired_requires_explicit_lifecycle(tmp_path, adapter, kwargs):
    with pytest.raises(ValueError):
        stage(tmp_path, adapter, **kwargs)
    assert not (tmp_path / "live.db").exists()


@pytest.mark.parametrize("flat", [False, True])
def test_adapter_cannot_supply_lifecycle_defaults(tmp_path, adapter, flat):
    def closed_statement(content):
        result = statement(content)
        return result.model_copy(
            update={"account": result.account.model_copy(update={"status": "closed"})}
        )

    adapter["format"] = replace(adapter["format"], parse=closed_statement)
    with pytest.raises(ValueError, match="unsupported lifecycle"):
        stage(tmp_path, adapter, flat=flat)
    assert not (tmp_path / "live.db").exists()


@pytest.mark.parametrize(
    "text",
    [
        TEXT.replace("ACCOUNT-7", "ACCOUNT-70"),
        TEXT.replace("2031-04-30", "2031-05-31"),
        TEXT.replace("FICTIONAL", "UNRELATED"),
    ],
)
def test_paired_pdf_requires_recognition_account_and_exact_period(tmp_path, adapter, text):
    args = inputs(tmp_path)
    (args["inbox_dir"] / "savings folder" / "statement.pdf").write_text(text)
    with pytest.raises(ValueError, match="does not match"):
        workflow.stage_investment_import(
            **args,
            closed_aliases=("SAVINGS",),
            transfer_destination="elsewhere",
            status_confirmed_on=END,
        )
    assert not args["db_path"].exists()


def test_only_declared_overlapping_alias_accepts_evidence_only_pdf(tmp_path, adapter):
    args = inputs(tmp_path)
    (args["inbox_dir"] / "savings folder" / "extra.pdf").write_text(TEXT + " extra evidence")
    adapter["format"] = replace(adapter["format"], evidence_only_aliases=())
    with pytest.raises(ValueError, match="permitted overlapping"):
        workflow.stage_investment_import(
            **args,
            closed_aliases=("SAVINGS",),
            transfer_destination="elsewhere",
            status_confirmed_on=END,
        )
    adapter["format"] = replace(adapter["format"], evidence_only_aliases=("SAVINGS",))
    run = workflow.stage_investment_import(
        **args,
        closed_aliases=("SAVINGS",),
        transfer_destination="elsewhere",
        status_confirmed_on=END,
    )
    assert workflow.investment_import_status(run)["counts"]["evidence_pdfs"] == 1


def test_unmatched_evidence_pdf_requires_the_overlapping_statement_account(tmp_path, adapter):
    args = inputs(tmp_path)
    (args["inbox_dir"] / "savings folder" / "extra.pdf").write_text(
        TEXT.replace("ACCOUNT-7", "ACCOUNT-70")
    )

    with pytest.raises(ValueError, match=r"evidence PDF.*account"):
        workflow.stage_investment_import(**args, institution="fictional")

    assert not args["db_path"].exists()
    assert not args["runs_dir"].exists()


def test_unmatched_evidence_pdf_refuses_multiple_distinct_overlapping_accounts(tmp_path, adapter):
    args = inputs(tmp_path)
    folder = args["inbox_dir"] / "savings folder"
    (folder / "other.csv").write_text("another fictional account")
    (folder / "other.pdf").write_text(TEXT.replace("ACCOUNT-7", "ACCOUNT-8"))
    (folder / "extra.pdf").write_text(TEXT + " extra evidence")

    def distinct_account(content):
        parsed = statement(content)
        if content == b"another fictional account":
            return parsed.model_copy(
                update={
                    "statement_id": "another-fictional-statement",
                    "account": parsed.account.model_copy(
                        update={
                            "account_id": "another-fictional-account",
                            "source_account_id": "ACCOUNT-8",
                        }
                    ),
                }
            )
        return parsed

    adapter["format"] = replace(adapter["format"], parse=distinct_account)
    with pytest.raises(ValueError, match=r"evidence PDF.*account"):
        workflow.stage_investment_import(**args, institution="fictional")

    assert not args["db_path"].exists()
    assert not args["runs_dir"].exists()


@pytest.mark.parametrize("encoded_evidence", [False, True])
def test_unmatched_evidence_accepts_overlapping_reports_for_one_account(
    tmp_path, adapter, monkeypatch, encoded_evidence
):
    args = inputs(tmp_path)
    folder = args["inbox_dir"] / "savings folder"
    (folder / "ytd.csv").write_text("fictional year-to-date report")
    (folder / "ytd.pdf").write_text(TEXT.replace("2031-04-01", "2031-01-01"))
    evidence = (
        b"%PDF fictional encoded evidence" if encoded_evidence else (TEXT + " extra").encode()
    )
    (folder / "extra.pdf").write_bytes(evidence)
    monkeypatch.setattr(
        workflow,
        "_extract_pinned_pdf_text",
        lambda content: TEXT if content == b"%PDF fictional encoded evidence" else content.decode(),
    )

    def overlapping_report(content):
        parsed = statement(content)
        if content == b"fictional year-to-date report":
            return parsed.model_copy(
                update={
                    "statement_id": "fictional-ytd-statement",
                    "period_start": date(2031, 1, 1),
                }
            )
        return parsed

    adapter["format"] = replace(
        adapter["format"],
        parse=overlapping_report,
        period_from_text=lambda text: (date(2031, 1, 1) if "2031-01-01" in text else START, END),
    )
    run = workflow.stage_investment_import(**args, institution="fictional")

    assert workflow.investment_import_status(run)["counts"]["evidence_pdfs"] == 1
    assert workflow.finalize_investment_import(run)["status"] == "finalized"


@pytest.mark.parametrize("flat", [False, True])
def test_changed_binding_refuses_unpublished_promotion(tmp_path, adapter, flat):
    run, args = stage(tmp_path, adapter, flat=flat)
    adapter["binding"]["core_sha256"] = "3" * 64
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize_investment_import(run)
    assert workflow.investment_import_status(run)["status"] == "staged"
    assert not args["db_path"].exists()
    assert workflow.cancel_investment_import(run)["status"] == "cancelled"


@pytest.mark.parametrize("existing_target", [False, True])
@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize("drift", ["adapter", "core", "source", "source_identity", "source_set"])
def test_candidate_preparation_drift_refuses_publication(
    tmp_path, adapter, monkeypatch, existing_target, flat, drift
):
    args = inputs(tmp_path, flat=flat)
    if flat:
        adapter["format"] = replace(adapter["format"], source_layout="flat")
    if existing_target:
        baseline_content = b"fictional prior month"
        baseline_source = tmp_path / "prior-statement.pdf"
        baseline_source.write_bytes(baseline_content)
        baseline = statement(baseline_content).model_copy(
            update={
                "statement_id": "fictional-prior-statement",
                "period_start": date(2031, 3, 1),
                "period_end": date(2031, 3, 31),
            }
        )
        workflow.InvestmentRepository(args["db_path"]).store_statement(
            baseline,
            source_pdf_path=baseline_source,
            source_pdf_sha256=hashlib.sha256(baseline_content).hexdigest(),
            source_pdf_size=len(baseline_content),
            source_pdf_role="statement_pdf",
        )
    before_owned = workflow.import_owned_fingerprint(args["db_path"])
    before_preserved = workflow.investment_preserved_fingerprint(args["db_path"])
    run = workflow.stage_investment_import(**args, institution="fictional")
    original_binding = deepcopy(workflow.investment_import_status(run)["adapter_binding"])
    original_snapshot = pdf_files.snapshot_database
    source_dir = args["inbox_dir"] if flat else args["inbox_dir"] / "savings folder"
    source = source_dir / "statement.pdf"
    changed = []

    def drift_after_candidate_snapshot(source_db, destination_db, *positional, **keyword):
        result = original_snapshot(source_db, destination_db, *positional, **keyword)
        if ".promote-" in destination_db.name:
            changed.append(drift)
            if drift == "source":
                source.write_text(TEXT + " altered during candidate preparation")
            elif drift == "source_identity":
                replacement = source.with_name("replacement.pdf")
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
            elif drift == "source_set":
                (source_dir / "new-evidence.pdf").write_text(TEXT + " new evidence")
            elif drift == "core":
                adapter["binding"]["core_sha256"] = "9" * 64
            else:
                adapter["binding"]["adapters"][0]["synthetic"] = "8" * 64
        return result

    monkeypatch.setattr(pdf_files, "snapshot_database", drift_after_candidate_snapshot)
    adapter["calls"].clear()
    source_drift = drift.startswith("source")
    error = RuntimeError if source_drift else AdapterError
    match = "source changed" if source_drift else "import_binding_changed"
    with pytest.raises(error, match=match):
        workflow.finalize_investment_import(run)

    assert changed == [drift]
    assert "snapshot" not in adapter["calls"]
    assert args["db_path"].exists() is existing_target
    assert workflow.import_owned_fingerprint(args["db_path"]) == before_owned
    assert workflow.investment_preserved_fingerprint(args["db_path"]) == before_preserved
    assert source.is_file()
    assert not [path for path in args["processed_dir"].rglob("*") if path.is_file()]
    manifest = workflow.investment_import_status(run)
    assert manifest["status"] == "staged"
    assert not manifest["published"]
    assert manifest["adapter_binding"] == original_binding
    assert all(item["archive_status"] == "pending" for item in manifest["sources"])
    if existing_target:
        adapter["binding"] = deepcopy(original_binding)
        source.write_text(TEXT)
        (source_dir / "new-evidence.pdf").unlink(missing_ok=True)
        monkeypatch.setattr(pdf_files, "snapshot_database", original_snapshot)
        assert workflow.finalize_investment_import(run)["status"] == "finalized"
    else:
        assert workflow.cancel_investment_import(run)["status"] == "cancelled"


@pytest.mark.parametrize("existing_target", [False, True])
def test_prepublication_check_runs_without_other_fingerprints(tmp_path, existing_target):
    candidate = tmp_path / "candidate.db"
    target = tmp_path / "target.db"
    workflow.InvestmentRepository(candidate)
    if existing_target:
        workflow.InvestmentRepository(target)
    before = workflow.import_owned_fingerprint(target)
    calls = []

    def refuse_publication():
        calls.append("check")
        raise RuntimeError("synthetic interpretation drift")

    with pytest.raises(RuntimeError, match="synthetic interpretation drift"):
        pdf_files.promote_database(
            candidate,
            target,
            tmp_path / "backups",
            prepublication_check=refuse_publication,
        )
    assert calls == ["check"]
    assert target.exists() is existing_target
    assert workflow.import_owned_fingerprint(target) == before


def test_changed_binding_during_parse_refuses_staging_acceptance(tmp_path, adapter):
    def mutate_binding(content):
        adapter["binding"]["core_sha256"] = "4" * 64
        return statement(content)

    adapter["format"] = replace(adapter["format"], parse=mutate_binding)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        stage(tmp_path, adapter)
    assert not (tmp_path / "live.db").exists()
    assert not (tmp_path / "runs").exists()


def legacy_unbound_run(run):
    manifest = workflow.investment_import_status(run)
    manifest.pop("adapter_binding")
    manifest["lifecycle"].pop("adapter_binding_fingerprint")
    with session_scope(run / "staging.db") as session:
        record = session.get(InvestmentImportRunDB, run.name)
        record.lifecycle_inputs = manifest["lifecycle"]
    manifest["staging_import_owned"] = workflow.import_owned_fingerprint(run / "staging.db")
    workflow._write_manifest(run, manifest)
    return manifest


def test_legacy_missing_binding_is_inspectable_cancellable_but_not_promotable(tmp_path, adapter):
    run, args = stage(tmp_path, adapter)
    legacy_unbound_run(run)
    assert workflow.investment_import_status(run)["status"] == "staged"
    with pytest.raises(RuntimeError, match=r"compatible original executable.*cancel and restage"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()
    assert workflow.cancel_investment_import(run)["status"] == "cancelled"


@pytest.mark.parametrize("flat", [False, True])
def test_manifest_cannot_substitute_current_binding_for_staged_receipt(tmp_path, adapter, flat):
    run, args = stage(tmp_path, adapter, flat=flat)
    manifest = workflow.investment_import_status(run)
    adapter["binding"]["core_sha256"] = "5" * 64
    manifest["adapter_binding"] = deepcopy(adapter["binding"])
    manifest["lifecycle"]["adapter_binding_fingerprint"] = (
        workflow._interpretation_binding_fingerprint(manifest)
    )
    workflow._write_manifest(run, manifest)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()


def test_manifest_cannot_add_current_pins_to_legacy_unbound_run(tmp_path, adapter):
    run, _ = stage(tmp_path, adapter)
    manifest = legacy_unbound_run(run)
    manifest["adapter_binding"] = deepcopy(adapter["binding"])
    manifest["lifecycle"]["adapter_binding_fingerprint"] = (
        workflow._interpretation_binding_fingerprint(manifest)
    )
    workflow._write_manifest(run, manifest)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize_investment_import(run)


@pytest.mark.parametrize("flat", [False, True])
def test_archive_resume_never_loads_or_verifies_adapter(tmp_path, adapter, monkeypatch, flat):
    run, args = stage(tmp_path, adapter, flat=flat)
    original_archive = workflow._archive_source

    def fail_archive(*args, **kwargs):
        raise OSError("synthetic archive failure")

    monkeypatch.setattr(workflow, "_archive_source", fail_archive)
    assert workflow.finalize_investment_import(run)["status"] == "archive_pending"
    monkeypatch.setattr(workflow, "_archive_source", original_archive)

    def unavailable(*args, **kwargs):
        pytest.fail("Published archive recovery must not load, verify, or parse an adapter")

    monkeypatch.setattr(workflow, "get_investment_format", unavailable)
    monkeypatch.setattr(workflow, "verify_bindings", unavailable)
    monkeypatch.setattr(workflow, "_extract_pinned_pdf_text", unavailable)
    if flat:
        (args["inbox_dir"] / "unrelated.pdf").write_text("unrelated new document")
    assert workflow.finalize_investment_import(run)["status"] == "finalized"


@pytest.mark.parametrize("flat", [False, True])
def test_positive_publication_proof_recovers_without_adapter(tmp_path, adapter, monkeypatch, flat):
    run, _ = stage(tmp_path, adapter, flat=flat)
    original_write = workflow._write_manifest

    def interrupt_receipt(path, manifest):
        if manifest.get("status") == "archive_pending":
            raise OSError("synthetic receipt interruption")
        return original_write(path, manifest)

    monkeypatch.setattr(workflow, "_write_manifest", interrupt_receipt)
    with pytest.raises(OSError, match="receipt interruption"):
        workflow.finalize_investment_import(run)
    monkeypatch.setattr(workflow, "_write_manifest", original_write)
    assert workflow.investment_import_status(run)["status"] == "finalizing"

    def unavailable(*args, **kwargs):
        pytest.fail("Proven publication recovery must not require the adapter")

    monkeypatch.setattr(workflow, "get_investment_format", unavailable)
    monkeypatch.setattr(workflow, "verify_bindings", unavailable)
    monkeypatch.setattr(workflow, "_extract_pinned_pdf_text", unavailable)
    result = workflow.finalize_investment_import(run)
    assert result["status"] == "finalized"
    assert result["publication_recovered"] is True


def test_inert_archive_validator_rejects_destination_substitution(tmp_path, adapter):
    run, args = stage(tmp_path, adapter)
    manifest = workflow.investment_import_status(run)
    source = manifest["sources"][0]
    source["archive_relative"] = source["archive_relative"].replace("SAVINGS/", "OTHER/")
    with pytest.raises(ValueError, match="destination binding"):
        workflow._archive_target(args["processed_dir"], source)


def test_pins_changed_during_finalizing_transition_abort_promotion(tmp_path, adapter, monkeypatch):
    run, args = stage(tmp_path, adapter)
    original_transition = workflow._transition_staging_run

    def change_after_transition(*args, **kwargs):
        result = original_transition(*args, **kwargs)
        if kwargs["to_status"] == "finalizing":
            adapter["binding"]["core_sha256"] = "6" * 64
        return result

    monkeypatch.setattr(workflow, "_transition_staging_run", change_after_transition)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()
    assert workflow.investment_import_status(run)["status"] == "staged"


def test_binding_digest_is_immutable_during_staging_transition(tmp_path, adapter, monkeypatch):
    run, args = stage(tmp_path, adapter)
    original_update = workflow.InvestmentRepository.update_import_run

    def corrupt_lifecycle(repository, run_id, status, **kwargs):
        original_update(repository, run_id, status, **kwargs)
        if status == "finalizing":
            with session_scope(repository.db_path) as session:
                record = session.get(InvestmentImportRunDB, run_id)
                lifecycle = dict(record.lifecycle_inputs)
                lifecycle["adapter_binding_fingerprint"] = "0" * 64
                record.lifecycle_inputs = lifecycle

    monkeypatch.setattr(workflow.InvestmentRepository, "update_import_run", corrupt_lifecycle)
    with pytest.raises(RuntimeError, match="audit has recovery drift"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()


def test_legacy_published_source_metadata_recovers_from_proven_database(
    tmp_path, adapter, monkeypatch
):
    run, _ = stage(tmp_path, adapter)
    manifest = legacy_unbound_run(run)
    manifest["lifecycle"].pop("source_binding_fingerprint")
    with session_scope(run / "staging.db") as session:
        session.get(InvestmentImportRunDB, run.name).lifecycle_inputs = manifest["lifecycle"]
    manifest["staging_import_owned"] = workflow.import_owned_fingerprint(run / "staging.db")
    workflow._write_manifest(run, manifest)
    original_write = workflow._write_manifest

    def interrupt_receipt(path, value):
        if value.get("status") == "archive_pending":
            raise OSError("synthetic legacy receipt interruption")
        return original_write(path, value)

    # Emulate a compatible historical executable publishing its reviewed unbound run.
    with monkeypatch.context() as old_executable:
        old_executable.setattr(workflow, "_verify_manifest_adapter", lambda value: None)
        old_executable.setattr(
            workflow, "_verified_investment_format", lambda value: adapter["format"]
        )
        old_executable.setattr(workflow, "_write_manifest", interrupt_receipt)
        with pytest.raises(OSError, match="legacy receipt interruption"):
            workflow.finalize_investment_import(run)
    manifest = workflow.investment_import_status(run)
    for value in [manifest, *manifest["sources"]]:
        value.pop("institution")
        value.pop("source_layout")
        value.pop("source_folder", None)
    workflow._write_manifest(run, manifest)

    def unavailable(*args, **kwargs):
        pytest.fail("Legacy publication recovery must use recorded database evidence")

    monkeypatch.setattr(workflow, "get_investment_format", unavailable)
    monkeypatch.setattr(workflow, "verify_bindings", unavailable)
    monkeypatch.setattr(workflow, "_extract_pinned_pdf_text", unavailable)
    result = workflow.finalize_investment_import(run)
    assert result["status"] == "finalized"
    assert result["institution"] == "fictional"
    assert result["sources"][0]["source_folder"] == "savings folder"


def test_flat_unpublished_source_set_rejects_new_relevant_document(tmp_path, adapter):
    run, args = stage(tmp_path, adapter, flat=True)
    (args["inbox_dir"] / "another.pdf").write_text(TEXT + " new document")
    with pytest.raises(RuntimeError, match="source set drifted"):
        workflow.finalize_investment_import(run)
    assert not args["db_path"].exists()


def test_flat_unrelated_document_is_left_untouched(tmp_path, adapter):
    run, args = stage(tmp_path, adapter, flat=True)
    unrelated = args["inbox_dir"] / "other.pdf"
    unrelated.write_text("FICTIONAL unrelated account document")
    assert workflow.finalize_investment_import(run)["status"] == "finalized"
    assert unrelated.read_text() == "FICTIONAL unrelated account document"


def test_conflict_reparse_uses_verified_selected_format(tmp_path, adapter):
    run, args = stage(tmp_path, adapter)
    assert workflow.finalize_investment_import(run)["status"] == "finalized"
    folder = args["inbox_dir"] / "savings folder"
    (folder / "changed.csv").write_text("revised fictional structured statement")
    (folder / "changed.pdf").write_text(TEXT + " revised")
    seen = []

    def record_parse(content):
        seen.append(content)
        assert adapter["calls"][-1] in {"verify", "load"}
        return statement(content)

    adapter["format"] = replace(adapter["format"], parse=record_parse)
    changed_run = workflow.stage_investment_import(
        **args,
        closed_aliases=("SAVINGS",),
        transfer_destination="elsewhere",
        status_confirmed_on=END,
        replace_conflicts=True,
    )
    assert workflow.investment_import_status(changed_run)["counts"]["replaced"] == 1
    assert workflow.finalize_investment_import(changed_run)["status"] == "finalized"
    assert seen == [b"revised fictional structured statement"] * 2


def test_conflict_reparse_cannot_change_reviewed_fingerprint(tmp_path, adapter):
    run, args = stage(tmp_path, adapter)
    workflow.finalize_investment_import(run)
    folder = args["inbox_dir"] / "savings folder"
    (folder / "changed.csv").write_text("revised fictional structured statement")
    (folder / "changed.pdf").write_text(TEXT + " revised")
    changed_run = workflow.stage_investment_import(
        **args,
        closed_aliases=("SAVINGS",),
        transfer_destination="elsewhere",
        status_confirmed_on=END,
        replace_conflicts=True,
    )
    adapter["format"] = replace(
        adapter["format"], parse=lambda content: statement(content + b" drift")
    )
    with pytest.raises(RuntimeError, match="conflict authorization changed"):
        workflow.finalize_investment_import(changed_run)
    assert workflow.investment_import_status(changed_run)["status"] == "staged"


def test_cli_default_format_selection_and_private_failure_redaction(tmp_path, adapter, monkeypatch):
    from typer.testing import CliRunner

    from ledger.commands import import_investment

    seen = []

    def failure(**kwargs):
        seen.append(kwargs["institution"])
        raise ValueError("private adapter details must not escape")

    monkeypatch.setattr(import_investment, "stage_investment_import", failure)
    result = CliRunner().invoke(import_investment.import_investment_app, ["stage"])
    assert result.exit_code == 1
    assert seen == [None]
    assert "private adapter details" not in result.output
    assert '"staging_failed"' in result.output


def canonical_activity(kind=InvestmentActivityType.DEPOSIT, **updates):
    return InvestmentActivity(
        activity_id="fictional-activity",
        activity_type=kind,
        activity_date=START,
        currency="CAD",
        amount=Decimal("2"),
        source_row=3,
    ).model_copy(update=updates)


def canonical_position(**updates):
    return InvestmentPosition(
        asset_category="Stocks",
        currency="CAD",
        symbol="EXAMPLE",
        discriminator="Summary",
        quantity=Decimal("1"),
        source_row=2,
    ).model_copy(update=updates)


@pytest.mark.parametrize(
    "invalid",
    [
        "institution",
        "institution_type",
        "account_type",
        "asset_class",
        "position_category",
        "discriminator",
        "currency",
        "symbol",
        "direction",
        "row_number",
        "nonfinite",
        "excess_precision",
    ],
)
def test_invalid_canonical_read_contract_is_refused_before_staging(tmp_path, adapter, invalid):
    parsed = statement(b"canonical query contract")
    if invalid in {"institution", "institution_type", "account_type"}:
        value = {
            "institution": "fictional.v2",
            "institution_type": None,
            "account_type": "unmapped/type",
        }[invalid]
        key = "institution" if invalid == "institution_type" else invalid
        parsed = parsed.model_copy(
            update={"account": parsed.account.model_copy(update={key: value})}
        )
    elif invalid == "asset_class":
        parsed = parsed.model_copy(
            update={
                "asset_balances": (
                    InvestmentAssetBalance(
                        asset_class="Unmapped Assets", current_total=Decimal("17")
                    ),
                )
            }
        )
    elif invalid in {"position_category", "discriminator", "currency", "symbol", "row_number"}:
        key, value = {
            "position_category": ("asset_category", "Unmapped Assets"),
            "discriminator": ("discriminator", "Unmapped Lot"),
            "currency": ("currency", "cad"),
            "symbol": ("symbol", "not a ticker"),
            "row_number": ("source_row", -1),
        }[invalid]
        parsed = parsed.model_copy(update={"positions": (canonical_position(**{key: value}),)})
    elif invalid == "direction":
        parsed = parsed.model_copy(
            update={"activities": (canonical_activity(direction="unmapped"),)}
        )
    else:
        parsed = parsed.model_copy(
            update={"total_nav": Decimal("NaN" if invalid == "nonfinite" else "1.0000000000001")}
        )
    adapter["format"] = replace(
        adapter["format"],
        institution=parsed.account.institution if invalid != "institution_type" else "fictional",
        parse=lambda content: parsed,
        capabilities=frozenset(InvestmentCapability),
    )
    args = inputs(tmp_path)
    with pytest.raises(AdapterError, match="adapter_invalid_contract"):
        workflow.stage_investment_import(**args, institution="fictional")
    assert not args["db_path"].exists()
    assert not args["runs_dir"].exists()


@pytest.mark.parametrize(
    "kind",
    [
        InvestmentActivityType.TRADE,
        InvestmentActivityType.DEPOSIT,
        InvestmentActivityType.WITHDRAWAL,
        InvestmentActivityType.DIVIDEND,
        InvestmentActivityType.INTEREST,
        InvestmentActivityType.FEE,
        InvestmentActivityType.WITHHOLDING_TAX,
        InvestmentActivityType.TRANSFER,
    ],
)
def test_cash_activity_without_amount_is_refused_before_staging(tmp_path, adapter, kind):
    parsed = statement(b"missing financial amount").model_copy(
        update={
            "activities": (canonical_activity(kind, amount=None),),
        }
    )
    adapter["format"] = replace(
        adapter["format"],
        parse=lambda content: parsed,
        capabilities=frozenset(InvestmentCapability),
    )
    args = inputs(tmp_path)
    with pytest.raises(AdapterError, match="adapter_invalid_contract"):
        workflow.stage_investment_import(**args, institution="fictional")
    assert not args["db_path"].exists()
    assert not args["runs_dir"].exists()


@pytest.mark.parametrize("domain", ["cash", "trade", "income", "fees", "performance", "lending"])
def test_retained_domain_requires_its_declared_capability(tmp_path, adapter, domain):
    parsed = statement(b"undeclared retained domain")
    if domain == "cash":
        parsed = parsed.model_copy(
            update={
                "cash_balances": (
                    InvestmentCashBalance(
                        currency="CAD",
                        opening_balance=Decimal("17"),
                        debits=Decimal(0),
                        credits=Decimal(0),
                        closing_balance=Decimal("17"),
                    ),
                )
            }
        )
    elif domain == "performance":
        parsed = parsed.model_copy(
            update={
                "performance": (
                    InvestmentPerformance(
                        asset_category="Total (All Assets)",
                        symbol="",
                        source_row=3,
                        total=Decimal("0"),
                    ),
                )
            }
        )
    else:
        kind = {
            "trade": InvestmentActivityType.TRADE,
            "income": InvestmentActivityType.DIVIDEND,
            "fees": InvestmentActivityType.FEE,
            "lending": InvestmentActivityType.STOCK_LOAN,
        }[domain]
        parsed = parsed.model_copy(update={"activities": (canonical_activity(kind),)})
    adapter["format"] = replace(adapter["format"], parse=lambda content: parsed)
    args = inputs(tmp_path)
    with pytest.raises(AdapterError, match="adapter_unsupported_capability"):
        workflow.stage_investment_import(**args, institution="fictional")
    assert not args["db_path"].exists()
    assert not args["runs_dir"].exists()


def test_known_zero_and_unavailable_noncash_fields_remain_distinct(tmp_path, adapter):
    from ledger.query import LedgerQueryService

    parsed = statement(b"exact noncash and zero boundaries").model_copy(
        update={
            "positions": (canonical_position(),),
            "activities": (
                canonical_activity(InvestmentActivityType.DEPOSIT, amount=Decimal("0")),
                canonical_activity(
                    InvestmentActivityType.TRANSFER,
                    activity_id="in-kind",
                    source_row=4,
                    symbol="EXAMPLE",
                    quantity=Decimal("1"),
                    amount=None,
                ),
                canonical_activity(
                    InvestmentActivityType.STOCK_LOAN,
                    activity_id="loan",
                    source_row=5,
                    symbol="EXAMPLE",
                    quantity=Decimal("1"),
                    amount=None,
                ),
            ),
        }
    )
    adapter["format"] = replace(
        adapter["format"],
        parse=lambda content: parsed,
        capabilities=frozenset(InvestmentCapability),
    )
    run, args = stage(tmp_path, adapter)
    workflow.finalize_investment_import(run)
    query = LedgerQueryService(args["db_path"])
    amounts = {row["activity_type"]: row["amount"] for row in query.get_investment_activities()}
    assert amounts == {"deposit": Decimal("0"), "transfer": None, "stock_loan": None}
    assert query.get_investment_positions(parsed.statement_id)[0]["cost_basis"] is None
    assert query.get_investment_activity_summary(currency="CAD") == {
        "net_deposits_withdrawals": Decimal("0"),
        "dividends_interest": Decimal("0"),
    }
    with session_scope(args["db_path"]) as session:
        from ledger.storage.database import InvestmentStatementDB

        assert (
            session.get(InvestmentStatementDB, parsed.statement_id).parsed_fingerprint
            == parsed.parsed_fingerprint
        )
