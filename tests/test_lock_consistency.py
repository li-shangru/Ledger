"""Guard that requirements.lock pins satisfy the pyproject dependency constraints.

The lock is a full-environment freeze; whenever a pinned version drifts outside the
bounded range declared in [project].dependencies (or [project.optional-dependencies]),
reproducible installs from the lock silently diverge from what pyproject promises. The
freeze-check `lock-consistency` gate invokes this same checker so drift fails loudly.
"""

from ledger.maintenance.lock_check import check_lock_against_pyproject


def test_lock_pins_satisfy_pyproject_constraints():
    violations = check_lock_against_pyproject()
    assert violations == [], f"lock/pyproject drift: {violations}"


def test_checker_flags_out_of_range_pin():
    # rich>=15,<16 with a 14.x pin must be reported
    violations = check_lock_against_pyproject(
        pyproject_deps=["rich>=15.0,<16"],
        lock_pins={"rich": "14.3.4"},
    )
    assert any(v["name"] == "rich" for v in violations)


def test_checker_passes_in_range_pin():
    violations = check_lock_against_pyproject(
        pyproject_deps=["rich>=15.0,<16"],
        lock_pins={"rich": "15.0.0"},
    )
    assert violations == []


def test_checker_flags_applicable_deps_absent_from_lock():
    # A declared dependency missing from the reproducible lock must fail closed.
    violations = check_lock_against_pyproject(
        pyproject_deps=["rich>=15.0,<16"],
        lock_pins={},
    )
    assert violations == [{"name": "rich", "pin": "<missing>", "specifier": "<16,>=15.0"}]


def test_read_pyproject_deps_includes_upper_bounded_extras():
    """F30: the checker's docstring claims optional-dependencies are covered, but
    _read_pyproject_deps only returned [project].dependencies -- so an out-of-range
    streamlit/plotly extras pin (the only upper-bounded extras) was never checked."""
    from packaging.requirements import Requirement

    from ledger.maintenance.lock_check import _read_pyproject_deps

    names = {Requirement(d).name for d in _read_pyproject_deps()}
    assert "streamlit" in names
    assert "plotly" in names


def test_checker_flags_out_of_range_extras_pin():
    """An out-of-range pin for an extras package (streamlit<2) must be reported."""
    violations = check_lock_against_pyproject(
        pyproject_deps=_read_pyproject_deps_for_test(),
        lock_pins={"streamlit": "2.5.0"},
    )
    assert any(v["name"].lower() == "streamlit" for v in violations), violations


def _read_pyproject_deps_for_test():
    from ledger.maintenance.lock_check import _read_pyproject_deps

    return _read_pyproject_deps()
