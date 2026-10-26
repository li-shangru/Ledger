"""Admin and data export page."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ledger.adjustments import AdjustmentDetectionService
from ledger.config.repository import SQLiteConfigRepository
from ledger.evidence import EvidenceService
from ledger.exporting import ExportPackageService
from ledger.maintenance import DataCleanupService
from ledger.quality import DataQualityService
from ledger.reporting import ReportSnapshotService
from ledger.rules import RuleApplicationService
from ledger.storage.migrations import migration_status, run_migrations, validate_database
from ledger.storage.operations import backup_database, recent_db_operations
from ledger.ui.components import (
    CURRENCY_FORMAT,
    confirmation_ready,
    empty_state,
    page_header,
    period_status_strip,
    status_grid,
)
from ledger.ui.context import AppContext

APPLY_ADJUSTMENTS_CONFIRM = "apply true-cost adjustments"
EXECUTE_CLEANUP_CONFIRM = "execute cleanup"
CREATE_EXPORT_CONFIRM = "create export"

EVIDENCE_ROWS = [
    ("Statement Audit", "audit"),
    ("Import Freshness", "imports"),
    ("Review Readiness", "review"),
    ("Data Quality", "quality"),
    ("Report Snapshot", "snapshots"),
    ("Backup History", "backups"),
    ("Schema", "database"),
]


def _status_value(value: object) -> str:
    if value is None:
        return "Never"
    return str(value)


def _evidence_rows(evidence: dict) -> list[dict[str, str]]:
    return [
        {
            "Signal": label,
            "Status": str((evidence.get(key) or {}).get("status", "unknown")),
            "Detail": str((evidence.get(key) or {}).get("detail", "")),
        }
        for label, key in EVIDENCE_ROWS
    ]


def _evidence_status_items(evidence: dict) -> list[dict[str, str | None]]:
    return [
        {
            "label": label,
            "value": str((evidence.get(key) or {}).get("status", "unknown")),
            "detail": (evidence.get(key) or {}).get("detail"),
        }
        for label, key in EVIDENCE_ROWS
    ]


_ACTION_OUTCOME_KEY = "admin_action_outcome"


def _finish_action(kind: str, message: str) -> None:
    """Stash the outcome, then rerun.

    st.success/st.error immediately followed by st.rerun() rendered for one
    frame and was wiped — a failed cleanup's error vanished before it could be
    read. The outcome survives the rerun in session state and renders at the
    top of the next frame."""
    st.session_state[_ACTION_OUTCOME_KEY] = (kind, message)
    st.rerun()


def _render_pending_outcome() -> None:
    outcome = st.session_state.pop(_ACTION_OUTCOME_KEY, None)
    if outcome:
        (st.success if outcome[0] == "success" else st.error)(outcome[1])


def render(ctx: AppContext) -> None:
    page_header(
        "Admin / Data",
        "Create privacy-safe exports and inspect local data freshness.",
        "Local",
    )
    _render_pending_outcome()
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(ctx.service.compute_kpis(ctx.start_date, ctx.end_date)["transaction_count"]),
        source="Admin defaults",
    )

    repo_validation = SQLiteConfigRepository(ctx.db_path).validate()
    latest_rule_apply = RuleApplicationService(ctx.db_path).latest_run()
    latest_snapshot = ReportSnapshotService(ctx.db_path).latest_snapshot()
    db_validation = validate_database(ctx.db_path, record=False)
    migration = migration_status(ctx.db_path)

    st.subheader("Data Health")
    cols = st.columns(4)
    cols[0].metric("Transactions", f"{ctx.stats['total_transactions']:,}")
    cols[1].metric("Runtime Rules", "Valid" if repo_validation.ok else "Issues")
    cols[2].metric(
        "Last Rule Apply",
        _status_value(latest_rule_apply["applied_at"] if latest_rule_apply else None),
    )
    cols[3].metric(
        "Latest Snapshot",
        _status_value(latest_snapshot["created_at"] if latest_snapshot else None),
    )

    db_cols = st.columns(3)
    db_cols[0].metric("Schema", "Valid" if db_validation.ok else "Issues")
    db_cols[1].metric("Migrations", f"{migration['applied']}/{migration['total']}")
    db_cols[2].metric("Pending Migrations", f"{len(migration['pending']):,}")

    action_cols = st.columns(3)
    # build_app_context() runs ensure_schema on every rerun, so by the time this page
    # renders there is never anything pending -- the button was a guaranteed no-op and
    # "Pending Migrations" could never be non-zero. Keep it as an explicit re-check
    # rather than implying there is outstanding work.
    has_pending = bool(migration["pending"])
    if action_cols[0].button(
        "Run Migrations" if has_pending else "Re-check Migrations",
        help=(
            "Migrations are applied automatically when the dashboard loads, so this "
            "normally reports that everything is already up to date."
        ),
    ):
        result = run_migrations(ctx.db_path)
        if result.ok:
            applied = result.counts.get("applied", 0)
            _finish_action(
                "success",
                f"Applied {applied} migration(s)." if applied else "Schema is already up to date.",
            )
        else:
            _finish_action("error", "; ".join(result.errors))
    if action_cols[1].button("Validate DB"):
        result = validate_database(ctx.db_path)
        if result.ok:
            st.success("Database schema is valid.")
        else:
            st.error("; ".join(result.errors))
    if action_cols[2].button("Backup DB"):
        result = backup_database(ctx.db_path, reason="streamlit_admin")
        if result.ok:
            st.success(f"Backup created: {result.artifacts.get('backup')}")
        else:
            st.error("; ".join(result.errors))

    if not repo_validation.ok:
        with st.expander("Config validation issues", expanded=True):
            for issue in repo_validation.issues:
                st.caption(issue)

    st.subheader("Ledger Evidence")
    evidence = EvidenceService(ctx.db_path).summary(pending_reviews=ctx.pending_reviews)
    status_grid(_evidence_status_items(evidence))
    st.dataframe(
        pd.DataFrame(_evidence_rows(evidence)),
        width="stretch",
        hide_index=True,
        height=280,
    )
    if evidence["next_actions"]:
        st.caption("Evidence next actions")
        st.dataframe(
            pd.DataFrame({"Next Action": evidence["next_actions"]}),
            width="stretch",
            hide_index=True,
            height=min(180, 48 + len(evidence["next_actions"]) * 35),
        )

    st.subheader("True-Cost Adjustments")
    adjustment_service = AdjustmentDetectionService(ctx.db_path)
    st.text_input(
        "Confirm adjustment apply",
        key="admin_confirm_adjustments",
        placeholder=APPLY_ADJUSTMENTS_CONFIRM,
        help="Type the confirmation phrase before applying detected adjustments.",
    )
    adjustments_confirmed = confirmation_ready(
        st.session_state,
        "admin_confirm_adjustments",
        APPLY_ADJUSTMENTS_CONFIRM,
    )
    adjustment_cols = st.columns(3)
    if adjustment_cols[0].button("Preview Adjustments"):
        adj_result = adjustment_service.detect(execute=False)
        st.session_state["adjustment_preview"] = adj_result.counts
    if adjustment_cols[1].button(
        "Detect / Apply Adjustments",
        type="primary",
        disabled=not adjustments_confirmed,
    ):
        adj_result = adjustment_service.detect(execute=True)
        if adj_result.ok:
            snapshot = ReportSnapshotService(ctx.db_path).create_snapshot(
                source="streamlit_adjustments",
                start_date=ctx.start_date,
                end_date=ctx.end_date,
            )
            st.cache_data.clear()
            _finish_action(
                "success",
                "Applied adjustments: "
                f"{adj_result.counts.get('auto_applied', 0)} auto-applied, "
                f"{adj_result.counts.get('needs_review', 0)} review item(s), "
                f"snapshot #{snapshot['id']} refreshed.",
            )
        else:
            _finish_action("error", "; ".join(adj_result.errors))
    if adjustment_cols[2].button("Validate Adjustments"):
        result = adjustment_service.validate()
        if result.ok:
            st.success(
                f"{result.counts.get('treatments', 0)} treatment(s), "
                f"{result.counts.get('offsets', 0)} offset(s), no orphan offsets."
            )
        else:
            st.error("; ".join(result.errors))

    preview = st.session_state.get("adjustment_preview")
    if preview:
        st.caption(
            "Preview: "
            f"{preview.get('treatments', 0)} treatment(s), "
            f"{preview.get('offsets', 0)} offset(s), "
            f"{preview.get('needs_review', 0)} review item(s)."
        )
    treatment_rows = adjustment_service.treatment_rows(limit=100)
    if treatment_rows:
        df_treatments = pd.DataFrame(treatment_rows)
        df_treatments["Confidence"] = df_treatments["Confidence"] * 100
        st.dataframe(
            df_treatments,
            width="stretch",
            hide_index=True,
            height=300,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
                "Confidence": st.column_config.NumberColumn("Confidence", format="%.0f%%"),
            },
        )
    else:
        empty_state("No adjustment treatments have been saved yet.")

    st.subheader("Data Quality")
    if st.button("Run Data Quality Checks"):
        DataQualityService(ctx.db_path).run_checks()
        st.rerun()
    quality_rows = DataQualityService(ctx.db_path).latest()
    if quality_rows:
        quality_df = pd.DataFrame(quality_rows)
        st.dataframe(
            quality_df.drop(columns=["Details"], errors="ignore"),
            width="stretch",
            hide_index=True,
            height=260,
        )
        with st.expander("Quality checklist details"):
            for row in quality_rows[:12]:
                status = row["Status"]
                label = f"{row['Check']}: {status} ({row['Issues']} issue(s))"
                if status == "pass":
                    st.success(label)
                elif status == "fail":
                    st.error(label)
                else:
                    st.warning(label)
                details = row.get("Details") or {}
                if details:
                    st.json(details)
    else:
        empty_state("No data quality check results have been recorded yet.")

    st.subheader("DB Operation Audit")
    audit_rows = recent_db_operations(ctx.db_path)
    if audit_rows:
        st.dataframe(pd.DataFrame(audit_rows), width="stretch", hide_index=True, height=260)
    else:
        empty_state("No database operations have been recorded yet.")

    st.divider()
    st.subheader("Local Data Cleanup")
    cleanup_service = DataCleanupService(ctx.db_path)
    cleanup_plan = cleanup_service.dry_run()
    cleanup_cols = st.columns(4)
    cleanup_cols[0].metric("Filesystem Targets", cleanup_plan.counts["filesystem_targets"])
    cleanup_cols[1].metric("Delete Roots", cleanup_plan.counts["delete_roots"])
    cleanup_cols[2].metric("Stale Import Runs", cleanup_plan.counts["stale_import_runs"])
    cleanup_cols[3].metric("Stale Export Runs", cleanup_plan.counts["stale_export_runs"])

    with st.expander("Cleanup dry-run details"):
        targets = cleanup_plan.details.get("delete_roots", [])
        stale_imports = cleanup_plan.details.get("stale_import_run_ids", [])
        stale_exports = cleanup_plan.details.get("stale_export_run_ids", [])
        if targets:
            st.caption("Filesystem roots to delete")
            st.dataframe(pd.DataFrame({"Path": targets}), width="stretch", hide_index=True)
        if stale_imports or stale_exports:
            st.caption("Metadata rows to prune")
            st.json(
                {
                    "import_run_ids": stale_imports,
                    "export_run_ids": stale_exports,
                }
            )
        if not targets and not stale_imports and not stale_exports:
            empty_state("No cleanup targets found.")

    st.text_input(
        "Confirm cleanup",
        key="admin_confirm_cleanup",
        placeholder=EXECUTE_CLEANUP_CONFIRM,
        help="Type the confirmation phrase before deleting cleanup targets.",
    )
    if st.button(
        "Execute Cleanup",
        type="secondary",
        disabled=not confirmation_ready(
            st.session_state,
            "admin_confirm_cleanup",
            EXECUTE_CLEANUP_CONFIRM,
        ),
    ):
        result = cleanup_service.clean(execute=True)
        if result.ok:
            _finish_action(
                "success",
                "Cleanup complete. "
                f"Deleted {result.counts.get('deleted_paths', 0)} path(s), "
                f"pruned {result.counts.get('pruned_import_runs', 0)} import run(s), "
                f"and kept backup {result.artifacts.get('backup')}.",
            )
        else:
            _finish_action("error", "; ".join(result.errors))

    st.divider()
    st.subheader("Privacy-Safe Export")

    with st.form("admin_export_form"):
        preset = st.selectbox(
            "Preset",
            [
                "chatgpt-safe",
                "credit-card-optimization",
                "merchant-cleanup",
                "tax-category-review",
                "full-local-audit",
            ],
            index=0,
            help=(
                "chatgpt-safe exports are aggregates-only: they include category "
                "spending, monthly cashflow per account, recurring spend with NO "
                "payee/merchant name and amounts bucketed to the nearest $5, "
                "and category/account summaries. They OMIT all transaction-level "
                "data: no transactions.csv/jsonl, no per-transaction merchants, "
                "exact amounts, or transaction IDs, no merchant-by-merchant "
                "breakdown, and no raw account numbers or source paths. The "
                "account reference is a salted local hash. Other presets keep "
                "full transaction-level detail for local analysis."
            ),
        )
        export_format = st.selectbox("Format", ["zip", "csv", "jsonl", "parquet"], index=0)

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "From",
                value=ctx.start_date,
                min_value=ctx.earliest,
                max_value=ctx.latest,
                key="admin_export_start",
            )
            institutions = st.multiselect(
                "Institutions",
                ctx.service.get_institutions(),
                default=[],
                help="Leave empty to include all institutions.",
            )
            categories = st.multiselect(
                "Categories",
                ctx.service.get_categories(),
                default=[],
                help="Leave empty to include all categories.",
            )
        with col2:
            end_date = st.date_input(
                "To",
                value=ctx.end_date,
                min_value=ctx.earliest,
                max_value=ctx.latest,
                key="admin_export_end",
            )
            merchants = st.multiselect(
                "Merchants",
                ctx.service.get_merchants(),
                default=[],
                help="Leave empty to include all merchants.",
            )
            include_transfers = st.checkbox("Include transfers", value=False)

        dest_dir = Path(
            st.text_input("Destination directory", value=str(ctx.db_path.parent / "exports"))
        )
        export_confirm = st.text_input(
            "Confirm export",
            placeholder=CREATE_EXPORT_CONFIRM,
            help="Type the confirmation phrase before creating an export artifact.",
        )
        submitted = st.form_submit_button("Create Export", type="primary")

    if submitted:
        if export_confirm.strip() != CREATE_EXPORT_CONFIRM:
            st.error(f"Type `{CREATE_EXPORT_CONFIRM}` before creating an export.")
        else:
            try:
                export_result = ExportPackageService(ctx.db_path).create_package(
                    dest_dir=dest_dir,
                    preset=preset,
                    export_format=export_format,
                    start_date=start_date,
                    end_date=end_date,
                    institutions=institutions or None,
                    categories=categories or None,
                    merchants=merchants or None,
                    include_transfers=include_transfers,
                )
                st.success(
                    f"Created {export_result.path} with {export_result.rows_exported:,} rows."
                )
                if export_result.path.exists():
                    st.download_button(
                        "Download Export",
                        data=export_result.path.read_bytes(),
                        file_name=export_result.path.name,
                    )
            except Exception as exc:
                st.error(f"Could not create export: {exc}")

    st.divider()
    st.subheader("Export History")
    runs = ExportPackageService(ctx.db_path).recent_runs()
    if runs:
        st.dataframe(pd.DataFrame(runs), width="stretch", hide_index=True, height=320)
    else:
        empty_state("No export packages have been created yet.")

    st.subheader("Report Snapshots")
    snapshot_rows = ReportSnapshotService(ctx.db_path).list_snapshots(limit=20)
    if snapshot_rows:
        st.dataframe(pd.DataFrame(snapshot_rows), width="stretch", hide_index=True, height=260)
    else:
        empty_state("No report snapshots have been generated yet.")

    st.subheader("Latest Report Snapshot")
    if latest_snapshot:
        payload = latest_snapshot["payload"]
        st.json(
            {
                "id": latest_snapshot["id"],
                "created_at": str(latest_snapshot["created_at"]),
                "source": latest_snapshot["source"],
                "period": payload.get("period"),
                "kpis": payload.get("kpis"),
                "anomaly_count": payload.get("anomaly_count"),
            }
        )
    else:
        empty_state("No report snapshot has been generated yet.")
