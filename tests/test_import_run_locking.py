"""Concurrency guards for import-run directory creation.

make_run_dir must hand each concurrent caller a distinct run dir and keep
latest-run.txt pointing at a real, created dir even under contention — otherwise two
imports started in the same second race on the timestamped name and the last writer
silently clobbers the pointer.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ledger.imports.pdf.files import make_run_dir


def test_concurrent_make_run_dir_yields_distinct_dirs(tmp_path: Path):
    runs = tmp_path / "import-runs"
    runs.mkdir()
    with ThreadPoolExecutor(max_workers=8) as pool:
        dirs = list(pool.map(lambda _: make_run_dir(runs), range(8)))
    resolved = {d.resolve() for d in dirs}
    assert len(resolved) == 8, "each concurrent run must get a unique run dir"
    for d in dirs:
        assert d.is_dir()


def test_latest_run_txt_points_at_a_real_created_dir(tmp_path: Path):
    runs = tmp_path / "import-runs"
    runs.mkdir()
    make_run_dir(runs)
    latest = (runs / "latest-run.txt").read_text(encoding="utf-8").strip()
    assert Path(latest).is_dir()


def test_make_run_dir_creates_runs_dir_when_absent(tmp_path: Path):
    runs = tmp_path / "import-runs"  # not created yet
    run_dir = make_run_dir(runs)
    assert run_dir.is_dir()
    assert run_dir.parent == runs
