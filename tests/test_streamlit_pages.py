from __future__ import annotations

from importlib import import_module, util
from pathlib import Path

PAGE_MODULES = [
    "ledger.ui.pages.import_center",
    "ledger.ui.pages.review_queue",
    "ledger.ui.pages.transactions",
    "ledger.ui.pages.overview",
    "ledger.ui.pages.categories",
    "ledger.ui.pages.accounts",
    "ledger.ui.pages.insights",
    "ledger.ui.pages.investments",
    "ledger.ui.pages.budgets_goals",
    "ledger.ui.pages.rules_settings",
    "ledger.ui.pages.admin_data",
]


def test_streamlit_page_modules_import_with_render_entrypoints() -> None:
    for module_name in PAGE_MODULES:
        module = import_module(module_name)

        assert callable(module.render), module_name


def test_streamlit_navigation_shell_imports() -> None:
    streamlit_app = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    spec = util.spec_from_file_location("streamlit_app", streamlit_app)
    assert spec is not None
    assert spec.loader is not None

    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.configure_page)
    assert callable(module.main)


def test_streamlit_navigation_registers_historical_investments() -> None:
    source = (Path(__file__).resolve().parents[1] / "streamlit_app.py").read_text(encoding="utf-8")

    assert "investments," in source
    assert "lambda: investments.render(ctx)" in source
    assert 'title="Investments"' in source
    assert 'icon=":material/candlestick_chart:"' in source
    assert 'url_path="investments"' in source
