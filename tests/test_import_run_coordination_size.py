"""An oversized run.json must never permanently brick live mutation surfaces.

``run.json`` is read as a coordination record under
``_MAX_COORDINATION_RECORD_BYTES`` but written unbounded as the full run archive.
Its payload scales with the DATABASE (``finalize_tail``, the whole review queue,
DB-wide post-processing stats), not with how many PDFs a run contains, so even a
single-statement run can exceed the cap. Once one did, every live write refused
with "import-run coordination is unreadable" and never recovered on its own.

A registered coordination record already carries a bounded, independently
verifiable terminal claim. These tests pin what that claim may and may not buy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.imports.pdf.files import run_coordination_dir, write_run_state
from ledger.imports.pdf.readiness import (
    LiveMutationDuringOpenImportRefused,
    OpenImportCoordinationError,
    live_mutation_boundary,
    open_runs_for_target,
    repair_run_coordination,
)
from ledger.storage.database import init_database

_CAP_BYTES = 64 * 1024


def _oversized_archive() -> list[str]:
    """Padding that puts run.json well past the reader's cap (~100 KB)."""
    return ["x" * 512 for _ in range(200)]


def _make_run(tmp_path: Path, *, status: str) -> tuple[Path, Path]:
    """Write one run whose run.json exceeds the coordination read cap."""
    db_path = tmp_path / "data" / "ledger.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_database(db_path)

    run_dir = db_path.parent / "import-runs" / f"run-{status}"
    run_dir.mkdir(parents=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": status,
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
            "finalize_tail": _oversized_archive(),
        },
    )
    assert (run_dir / "run.json").stat().st_size > _CAP_BYTES, "run.json must exceed the cap"
    return db_path, run_dir


def _deregister(db_path: Path, run_dir: Path) -> None:
    """Drop the coordination record, leaving a legacy unregistered run."""
    coordination_dir = run_coordination_dir(db_path)
    removed = False
    for record in coordination_dir.glob("*.json"):
        if Path(json.loads(record.read_text())["run_dir"]) == run_dir.resolve():
            record.unlink()
            removed = True
    assert removed, "expected a coordination record to remove"


def test_oversized_terminal_run_does_not_block_live_writes(tmp_path: Path) -> None:
    """The bounded record proves 'finalized', so the unreadable archive is irrelevant."""
    db_path, _run_dir = _make_run(tmp_path, status="finalized")

    assert open_runs_for_target(db_path) == []


def test_oversized_cancelled_run_does_not_block_live_writes(tmp_path: Path) -> None:
    """'cancelled' is terminal on the same evidence as 'finalized'."""
    db_path, _run_dir = _make_run(tmp_path, status="cancelled")

    assert open_runs_for_target(db_path) == []


def test_oversized_open_run_still_blocks_live_writes(tmp_path: Path) -> None:
    """Fail-closed is preserved: an OPEN run must still refuse the live write.

    Its record says can_promote, so nothing here proves the run is terminal and
    admitting a writer could lose staged decisions.
    """
    db_path, _run_dir = _make_run(tmp_path, status="needs_review")

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)


def test_oversized_unregistered_run_blocks_until_repaired(tmp_path: Path) -> None:
    """A legacy run has no bounded record, so it must block until one is written."""
    db_path, run_dir = _make_run(tmp_path, status="finalized")
    _deregister(db_path, run_dir)

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)

    repair_run_coordination(run_dir)

    assert open_runs_for_target(db_path) == []


def test_repair_refuses_a_run_that_can_still_promote(tmp_path: Path) -> None:
    """Repair must not be a way to launder an open run into a terminal claim."""
    db_path, run_dir = _make_run(tmp_path, status="needs_review")
    _deregister(db_path, run_dir)

    with pytest.raises(ValueError):
        repair_run_coordination(run_dir)

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)


def test_oversized_terminal_run_does_not_block_non_import_surfaces(tmp_path: Path) -> None:
    """The guard is shared: runtime config edits were bricked by this too."""
    db_path, _run_dir = _make_run(tmp_path, status="finalized")

    with live_mutation_boundary(db_path, operation="runtime_config"):
        pass


def test_repair_command_unblocks_a_legacy_oversized_run(tmp_path: Path) -> None:
    """The documented remedy must actually work from the CLI."""
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path, run_dir = _make_run(tmp_path, status="finalized")
    _deregister(db_path, run_dir)

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)

    result = CliRunner().invoke(
        app,
        ["import", "pdf", "repair-coordination", "--run-dir", str(run_dir)],
    )

    assert result.exit_code == 0, result.output
    assert "repaired_coordination" in result.output
    assert open_runs_for_target(db_path) == []


def test_refusal_names_a_remedy_a_terminal_run_can_actually_use(tmp_path: Path) -> None:
    """The old message advised finalize/cancel, which a terminal run cannot do."""
    db_path, run_dir = _make_run(tmp_path, status="finalized")
    _deregister(db_path, run_dir)

    with (
        pytest.raises(LiveMutationDuringOpenImportRefused) as refusal,
        live_mutation_boundary(db_path, operation="runtime_config"),
    ):
        pass  # pragma: no cover - the boundary refuses before the body runs

    assert "repair-coordination" in str(refusal.value)


def test_written_coordination_record_stays_under_the_read_cap(tmp_path: Path) -> None:
    """The writer must never emit a record its own reader would refuse."""
    db_path, _run_dir = _make_run(tmp_path, status="finalized")

    records = list(run_coordination_dir(db_path).glob("*.json"))
    assert records, "expected a coordination record"
    for record in records:
        assert record.stat().st_size <= _CAP_BYTES
