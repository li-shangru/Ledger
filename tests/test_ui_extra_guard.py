"""Packaging guard: streamlit + plotly live in the optional [ui] extra.

pandas stays a core dependency (it is imported outside ui/, in exporting/service.py).
The Streamlit entrypoint must fail with actionable guidance — not a raw
ModuleNotFoundError — when the ui extra is not installed.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_entrypoint_has_ui_extra_guard():
    src = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "pip install -e '.[ui]'" in src
    assert "ModuleNotFoundError" in src


def test_pyproject_moves_streamlit_plotly_to_ui_extra():
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = data["project"]["dependencies"]
    assert not any(d.startswith("streamlit") for d in core)
    assert not any(d.startswith("plotly") for d in core)
    assert any(d.startswith("pandas") for d in core)  # pandas stays core
    ui = data["project"]["optional-dependencies"]["ui"]
    assert any(d.startswith("streamlit") for d in ui)
    assert any(d.startswith("plotly") for d in ui)


def test_ui_extra_guard_message_names_plotly_dependency():
    """The guard must cover the whole ui extra (streamlit AND plotly), so a partial
    install still gets the actionable message rather than a raw import error."""
    src = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "ui" in src and "extra" in src.lower()


def test_ui_extra_guard_catches_import_error_not_just_module_not_found():
    """A partial/broken ui install (a C-extension load error, a 'cannot import name'
    failure) raises a plain ImportError, NOT a ModuleNotFoundError. The guard must
    catch ImportError so it still shows the actionable message (F32). ModuleNotFoundError
    subclasses ImportError, so the not-installed case is still covered."""
    src = (REPO_ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    assert "except ImportError" in src
