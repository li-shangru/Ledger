"""Ledger Streamlit entrypoint."""

from __future__ import annotations

_UI_EXTRA_HELP = (
    "The Ledger UI requires the optional 'ui' extra (streamlit + plotly). Install it with:\n"
    "    pip install -e '.[ui]'\n"
    "(or 'uv pip install -e .[ui]'), then re-run the Streamlit app."
)

try:
    # Both streamlit and plotly (imported transitively via ledger.ui.pages) ship in the
    # 'ui' extra, so guard the whole UI import block: a missing OR partial ui install
    # then fails with actionable guidance rather than a raw ModuleNotFoundError.
    import streamlit as st

    from ledger.ui.context import build_app_context, choose_default_page
    from ledger.ui.pages import (
        accounts,
        admin_data,
        budgets_goals,
        categories,
        import_center,
        insights,
        investments,
        overview,
        plan_reconciliation,
        proposals,
        review_queue,
        rules_settings,
        transactions,
    )
except ImportError as exc:  # pragma: no cover - exercised via subprocess test
    # ImportError (not just ModuleNotFoundError, its subclass): a partial/broken ui
    # install -- a C-extension load failure, a "cannot import name X" -- raises a plain
    # ImportError, which must still surface the actionable message, not a raw traceback.
    raise SystemExit(_UI_EXTRA_HELP) from exc


def configure_page() -> None:
    st.set_page_config(
        page_title="Ledger",
        page_icon="Ledger",
        layout="wide",
        initial_sidebar_state="auto",
    )

    st.markdown(
        """
        <style>
            :root {
                --ledger-border: rgba(120, 120, 120, 0.22);
                --ledger-muted: rgba(120, 120, 120, 0.82);
            }

            .block-container {
                padding-top: 0.85rem;
                padding-bottom: 3rem;
                max-width: 1440px;
            }

            h1, h2, h3 {
                letter-spacing: 0;
            }

            h1 {
                font-size: 1.65rem;
                font-weight: 700;
                margin: 0 0 0.15rem 0;
            }

            h2 {
                font-size: 1.25rem;
                margin-top: 1.6rem;
            }

            h3 {
                font-size: 1rem;
                margin-top: 1rem;
            }

            [data-testid="stMetric"] {
                border: 1px solid var(--ledger-border);
                border-radius: 8px;
                padding: 0.85rem 0.95rem;
                background: rgba(250, 250, 250, 0.035);
            }

            [data-testid="stMetricLabel"] {
                font-size: 0.78rem;
                font-weight: 600;
                text-transform: uppercase;
                color: var(--ledger-muted);
                white-space: normal;
                overflow: visible;
                text-overflow: clip;
                line-height: 1.25;
            }

            [data-testid="stMetricLabel"] * {
                white-space: normal !important;
                overflow: visible !important;
                text-overflow: clip !important;
            }

            [data-testid="stMetricValue"] {
                font-size: 1.55rem;
                font-weight: 700;
            }

            [data-testid="stMetricValue"],
            [data-testid="stMetricValue"] * {
                white-space: normal !important;
                overflow-wrap: anywhere;
                overflow: visible !important;
                text-overflow: clip !important;
            }

            [data-testid="stMetricDelta"] {
                font-size: 0.85rem;
            }

            .ledger-status-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 0.75rem;
                margin: 0.75rem 0 1rem;
            }

            .ledger-status-cell {
                border: 1px solid var(--ledger-border);
                border-radius: 8px;
                padding: 0.72rem 0.82rem;
                min-width: 0;
                background: rgba(250, 250, 250, 0.035);
            }

            .ledger-status-label {
                font-size: 0.72rem;
                font-weight: 700;
                text-transform: uppercase;
                color: var(--ledger-muted);
                margin-bottom: 0.2rem;
            }

            .ledger-status-value {
                font-size: 0.98rem;
                font-weight: 650;
                line-height: 1.3;
                white-space: normal;
                overflow-wrap: anywhere;
            }

            .ledger-status-detail {
                color: #16a34a;
                font-size: 0.78rem;
                margin-top: 0.28rem;
                overflow-wrap: anywhere;
            }

            .ledger-status-cell {
                transition: box-shadow 0.15s ease, border-color 0.15s ease;
            }

            .ledger-status-cell:hover,
            [data-testid="stMetric"]:hover {
                border-color: rgba(79, 70, 229, 0.4);
                box-shadow: 0 3px 12px rgba(15, 23, 42, 0.06);
            }

            .ledger-status-value.pos { color: #16a34a; }
            .ledger-status-value.neg { color: #dc2626; }

            .ledger-status-grid.is-primary {
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            }

            .ledger-status-grid.is-primary .ledger-status-cell {
                border-color: rgba(79, 70, 229, 0.28);
                background: rgba(79, 70, 229, 0.05);
                border-left: 3px solid #4f46e5;
            }

            .ledger-status-grid.is-primary .ledger-status-value {
                font-size: 1.5rem;
                font-weight: 700;
                line-height: 1.15;
            }

            div[data-testid="stDataFrame"] {
                border: 1px solid var(--ledger-border);
                border-radius: 8px;
            }

            hr {
                margin: 1.5rem 0;
                border-color: var(--ledger-border);
            }

            section[data-testid="stSidebar"] h1,
            section[data-testid="stSidebar"] h2,
            section[data-testid="stSidebar"] h3 {
                font-size: 0.95rem;
            }

            .ledger-page-caption {
                color: var(--ledger-muted);
                font-size: 0.95rem;
                margin-bottom: 0.45rem;
            }

            .ledger-page-header {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 1rem;
                margin-bottom: 0.8rem;
            }

            .ledger-status {
                display: inline-flex;
                align-items: center;
                gap: 0.35rem;
                padding: 0.15rem 0.5rem;
                border: 1px solid var(--ledger-border);
                border-radius: 999px;
                font-size: 0.78rem;
                color: var(--ledger-muted);
                white-space: nowrap;
            }

            @media (max-width: 760px) {
                .block-container {
                    padding-top: 0.65rem;
                    padding-left: 1rem;
                    padding-right: 1rem;
                }

                .ledger-page-header {
                    display: block;
                }

                .ledger-status {
                    margin-top: 0.35rem;
                    white-space: normal;
                }

                [data-testid="stMetricValue"] {
                    font-size: 1.25rem;
                }

                /* Wrap long CLI snippets instead of overflowing the viewport. */
                code,
                [data-testid="stCode"] pre,
                [data-testid="stCode"] code {
                    white-space: pre-wrap !important;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    configure_page()
    ctx = build_app_context()
    default_page = choose_default_page(
        ctx.pending_reviews, has_transactions=bool(ctx.stats["total_transactions"])
    )

    def render_data_page(renderer) -> None:
        if not ctx.stats["total_transactions"]:
            st.info("Import statements to view this page.")
            st.page_link(
                st.session_state["ledger_pages"]["import-center"], label="Open Import Center"
            )
            return
        renderer(ctx)

    pages = {
        "Work": [
            st.Page(
                lambda: import_center.render(ctx),
                title="Import Center",
                icon=":material/upload_file:",
                url_path="import-center",
            ),
            st.Page(
                lambda: review_queue.render(ctx),
                title="Review Queue",
                icon=":material/rate_review:",
                url_path="review",
            ),
            st.Page(
                lambda: transactions.render(ctx),
                title="Transactions",
                icon=":material/search:",
                url_path="transactions",
            ),
        ],
        "Dashboard": [
            st.Page(
                lambda: render_data_page(overview.render),
                title="Overview",
                icon=":material/monitoring:",
                url_path="overview",
            ),
            st.Page(
                lambda: render_data_page(categories.render),
                title="Categories",
                icon=":material/category:",
                url_path="categories",
            ),
            st.Page(
                lambda: render_data_page(accounts.render),
                title="Accounts",
                icon=":material/account_balance:",
                url_path="accounts",
            ),
            st.Page(
                lambda: investments.render(ctx),
                title="Investments",
                icon=":material/candlestick_chart:",
                url_path="investments",
            ),
        ],
        "Planning": [
            st.Page(
                lambda: render_data_page(insights.render),
                title="Insights",
                icon=":material/insights:",
                url_path="insights",
            ),
            st.Page(
                lambda: render_data_page(budgets_goals.render),
                title="Budgets / Goals",
                icon=":material/savings:",
                url_path="budgets-goals",
            ),
            st.Page(
                lambda: plan_reconciliation.render(ctx),
                title="Plan versus actual",
                icon=":material/compare_arrows:",
                url_path="plan-reconciliation",
            ),
            st.Page(
                lambda: proposals.render(ctx),
                title="Proposals",
                icon=":material/fact_check:",
                url_path="proposals",
            ),
        ],
        "Settings": [
            st.Page(
                lambda: rules_settings.render(ctx),
                title="Rules / Settings",
                icon=":material/tune:",
                url_path="rules",
            ),
            st.Page(
                lambda: admin_data.render(ctx),
                title="Admin / Data",
                icon=":material/database:",
                url_path="admin-data",
            ),
        ],
    }

    page_by_path = {page.url_path: page for group in pages.values() for page in group}
    st.session_state["ledger_pages"] = page_by_path
    # Keep every named URL stable as review/import state changes. Only the hidden
    # root route redirects to the current landing page.
    pages["Work"].insert(
        0,
        st.Page(
            lambda: st.switch_page(page_by_path[default_page]),
            title="Home",
            default=True,
            visibility="hidden",
        ),
    )
    selected = st.navigation(pages, position="sidebar", expanded=True)
    selected.run()


if __name__ == "__main__":
    main()
