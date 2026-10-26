"""Runtime merchant/category rules management."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ledger.config import load_categories, load_merchant_rules
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.rules import RuleApplicationService, RuntimeConfigMutationService
from ledger.runtime import seed_config_path
from ledger.ui.components import confirmation_ready, empty_state, page_header
from ledger.ui.context import AppContext, get_category_config, get_query_service

SAVE_MERCHANTS_CONFIRM = "save merchant profiles"
SAVE_CATEGORIES_CONFIRM = "save category taxonomy"
IMPORT_SEED_CONFIRM = "import seed config"
_ORIGINAL_CATEGORY_COLUMN = "_Original Category"


def _split_lines(value: object) -> list[str]:
    """Split a Patterns cell into individual regexes -- on NEWLINES ONLY.

    Commas are legal, load-bearing regex syntax: a bounded quantifier like ``\\d{3,4}``
    or ``\\*{2,}`` contains one. Treating a comma as a separator shattered such a
    pattern into two fragments that both still compile (``\\d{3`` and ``4}`` are valid
    regexes matching literals), so validate_patterns accepted them and the corruption
    persisted silently -- two live merchant rules were already in this state. The
    writer (_merchant_dataframe) joins with "\\n" only, so newline-only splitting also
    makes the round-trip symmetric.
    """
    if value is None:
        return []
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _merchant_dataframe(config: MerchantRulesConfig) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Merchant": rule.merchant_norm,
                "Category": rule.category,
                "Exclude": rule.exclude_from_spending,
                "Patterns": "\n".join(rule.patterns),
            }
            for rule in config.rules
        ]
    )


def _category_dataframe(config: CategoriesConfig) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Category": category.name,
                "Parent": category.parent or "",
                "Exclude": category.exclude_from_spending,
                _ORIGINAL_CATEGORY_COLUMN: category.name,
            }
            for category in config.categories
        ]
    )


def _dropped_merchant_rows(df: pd.DataFrame) -> list[str]:
    """Named profiles that a save would silently drop because they have no patterns.

    The save path is a full replace, so a skipped row is a DELETED merchant profile,
    not a preserved one. Clearing a Patterns cell to "reset" it therefore removes the
    profile outright -- worth warning about before the save, not after.
    """
    dropped = []
    for row in df.to_dict("records"):
        merchant = str(row.get("Merchant", "")).strip()
        if merchant and not _split_lines(row.get("Patterns")):
            dropped.append(merchant)
    return dropped


def _merchants_from_dataframe(df: pd.DataFrame, version: str) -> MerchantRulesConfig:
    rules = []
    for row in df.to_dict("records"):
        merchant = str(row.get("Merchant", "")).strip()
        patterns = _split_lines(row.get("Patterns"))
        # Skip an in-progress row that has a merchant but no patterns yet, instead of
        # letting MerchantRule (patterns min_length=1) raise a ValidationError that
        # rejects the WHOLE save. Mirrors _draft_merchants_for_preview.
        # _dropped_merchant_rows surfaces these to the user before they save.
        if not merchant or not patterns:
            continue
        rules.append(
            MerchantRule(
                merchant_norm=merchant,
                category=str(row.get("Category", "Other")).strip() or "Other",
                exclude_from_spending=bool(row.get("Exclude", False)),
                patterns=patterns,
            )
        )
    return MerchantRulesConfig(version=version, rules=rules)


def _draft_merchants_for_preview(df: pd.DataFrame, version: str) -> MerchantRulesConfig:
    """Build a draft MerchantRulesConfig for the preview-only "Test match" box.

    Unlike _merchants_from_dataframe (used on save), this tolerates an in-progress
    edited row that has a merchant but no patterns yet: such rows are skipped instead
    of being passed to MerchantRule, whose patterns field has min_length=1 and would
    raise a ValidationError that crashes the page. Mirrors the empty-merchant skip
    already done on save.
    """
    rules = []
    for row in df.to_dict("records"):
        merchant = str(row.get("Merchant", "")).strip()
        patterns = _split_lines(row.get("Patterns"))
        if not merchant or not patterns:
            continue
        rules.append(
            MerchantRule(
                merchant_norm=merchant,
                category=str(row.get("Category", "Other")).strip() or "Other",
                exclude_from_spending=bool(row.get("Exclude", False)),
                patterns=patterns,
            )
        )
    return MerchantRulesConfig(version=version, rules=rules)


def _categories_from_dataframe(
    df: pd.DataFrame,
    version: str,
    default_category: str,
    *,
    existing: CategoriesConfig | None = None,
) -> CategoriesConfig:
    existing_merchants = {
        category.name: list(category.merchants)
        for category in (existing or CategoriesConfig()).categories
    }
    categories = []
    for row in df.to_dict("records"):
        name = str(row.get("Category", "")).strip()
        if not name:
            continue
        parent = str(row.get("Parent", "") or "").strip()
        original_value = row.get(_ORIGINAL_CATEGORY_COLUMN)
        original_name = (
            str(original_value).strip()
            if original_value is not None and not pd.isna(original_value)
            else name
        )
        categories.append(
            CategoryRule(
                name=name,
                parent=parent or None,
                exclude_from_spending=bool(row.get("Exclude", False)),
                merchants=existing_merchants.get(
                    original_name,
                    existing_merchants.get(name, []),
                ),
            )
        )
    return CategoriesConfig(
        version=version,
        default_category=default_category,
        categories=categories,
    )


def _refresh_dashboard_caches() -> None:
    get_category_config.clear()
    get_query_service.clear()
    st.cache_data.clear()


def _result_message(result: dict) -> str:
    details = result.get("details") or {}
    insight_counts = details.get("insight_findings") or {}
    quality_issues = details.get("quality_issues", 0)
    return (
        f"{result['transactions_updated']:,} transactions updated across "
        f"{result['changed_merchants']:,} merchant profile(s). "
        f"Analytics refreshed: {result['analytics_refreshed']}. "
        f"Insights generated: {insight_counts.get('generated', 0)}. "
        f"Quality issues: {quality_issues}."
    )


def _changed_rows(original: pd.DataFrame, edited: pd.DataFrame, key_column: str) -> int:
    """Count edited/added/removed rows, matched BY KEY rather than by position.

    Positional zip reported every row after an insertion or deletion as changed, so
    removing one profile from the middle of the table claimed hundreds of edits.
    """
    original_map = _keyed_records(original, key_column)
    edited_map = _keyed_records(edited, key_column)
    changed = sum(1 for key, row in edited_map.items() if original_map.get(key) != row)
    removed = len(set(original_map) - set(edited_map))
    return changed + removed


def _keyed_records(df: pd.DataFrame, key_column: str) -> dict[str, dict]:
    if df.empty:
        return {}
    records = {}
    for row in df.fillna("").astype(str).to_dict("records"):
        key = str(row.get(key_column, "")).strip()
        if key:
            records[key] = row
    return records


def _changed_dataframe(
    original: pd.DataFrame, edited: pd.DataFrame, key_column: str
) -> pd.DataFrame:
    """Rows a save would change, INCLUDING removals.

    Iterating only the edited frame made deletions invisible: a profile present before
    the edit and absent after never appeared in the preview, so the two most
    destructive edit shapes -- deleting a row and renaming one (which is a delete plus
    an add) -- were exactly the ones the user could not see before confirming.
    """
    original_map = _keyed_records(original, key_column)
    edited_map = _keyed_records(edited, key_column)

    rows = []
    for key, row in edited_map.items():
        if original_map.get(key) != row:
            rows.append({**row, "Change": "added" if key not in original_map else "edited"})
    for key in original_map.keys() - edited_map.keys():
        rows.append({**original_map[key], "Change": "removed"})

    frame = pd.DataFrame(rows).drop(columns=[_ORIGINAL_CATEGORY_COLUMN], errors="ignore")
    if frame.empty:
        return frame
    return frame.sort_values(["Change", key_column], kind="stable").reset_index(drop=True)


def _save_apply_and_rerun(
    ctx: AppContext,
    merchants: MerchantRulesConfig,
    categories: CategoriesConfig,
    source: str,
) -> None:
    result = RuntimeConfigMutationService(ctx.db_path).replace_and_apply(
        merchants,
        categories,
        source=source,
    )
    st.session_state["rules_last_apply_result"] = {
        "status": result.status,
        "transactions_updated": result.transactions_updated,
        "changed_merchants": result.changed_merchants,
        "changed_categories": result.changed_categories,
        "analytics_refreshed": result.analytics_refreshed,
        "validation_issues": result.validation_issues,
        "details": result.details,
    }
    _refresh_dashboard_caches()
    st.rerun()


def render(ctx: AppContext) -> None:
    page_header(
        "Rules / Settings",
        "Manage canonical merchant profiles and the reporting category taxonomy.",
        "Local",
    )

    repo = SQLiteConfigRepository(ctx.db_path)
    merchants = repo.load_merchant_rules()
    taxonomy = repo.load_taxonomy()
    validation = repo.validate()

    if validation.ok:
        st.success("Runtime config is valid.")
    else:
        st.error(f"{len(validation.issues)} config issue(s) need attention.")
        with st.expander("Validation details", expanded=True):
            for issue in validation.issues:
                st.caption(f"Error: {issue}")

    last_result = st.session_state.get("rules_last_apply_result")
    if last_result:
        if last_result["status"] == "success":
            st.success(_result_message(last_result))
        else:
            issues = "; ".join(last_result["validation_issues"])
            st.error(
                f"Runtime configuration update failed ({last_result['status']})."
                + (f" {issues}" if issues else "")
            )

    stats = st.columns(4)
    stats[0].metric("Merchant Rules", validation.merchant_rules)
    stats[1].metric("Patterns", validation.merchant_patterns)
    stats[2].metric("Categories", validation.categories)
    stats[3].metric("Derived Map", validation.category_merchants)

    view = st.segmented_control(
        "View",
        ["Merchant Profiles", "Category Taxonomy", "Apply History", "Import / Export"],
        default="Merchant Profiles",
        label_visibility="collapsed",
    )

    if view == "Merchant Profiles":
        st.subheader("Merchant Profiles")
        original_df = _merchant_dataframe(merchants)
        edited = st.data_editor(
            original_df,
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            height=560,
            column_config={
                "Patterns": st.column_config.TextColumn(
                    "Patterns",
                    help=(
                        "One regex pattern per line. Commas are NOT separators — they "
                        "are kept, so quantifiers like \\d{3,4} survive a save."
                    ),
                ),
                "Exclude": st.column_config.CheckboxColumn("Exclude"),
            },
            key="rules_merchant_editor",
        )
        st.caption(f"Unsaved changed rows: {_changed_rows(original_df, edited, 'Merchant')}")
        dropped = _dropped_merchant_rows(edited)
        if dropped:
            st.warning(
                "These profiles have no patterns and will be **deleted** on save "
                f"(saving replaces the whole profile set): {', '.join(sorted(dropped))}.",
                icon="⚠️",
            )
        changed = _changed_dataframe(original_df, edited, "Merchant")
        if not changed.empty:
            with st.expander("Diff preview", expanded=True):
                st.dataframe(changed, width="stretch", hide_index=True, height=220)
                affected_merchants = changed["Merchant"].dropna().astype(str).tolist()
                affected_txns = ctx.service.get_transactions(
                    merchants=affected_merchants,
                    limit=5000,
                )
                st.caption(
                    f"Current merchant-name match preview: {len(affected_txns):,} "
                    "historical transaction(s). Pattern changes may affect additional rows."
                )
        test_text = st.text_input(
            "Test match",
            placeholder="Paste a transaction description to test the edited rules before saving.",
        )
        if test_text:
            draft_merchants = _draft_merchants_for_preview(edited, merchants.version)
            merchant, category, exclude = draft_merchants.find_merchant(test_text)
            if merchant:
                st.success(f"Matches {merchant} -> {category}; exclude={exclude}")
            else:
                st.warning("No edited merchant profile matches this text.")
        st.text_input(
            "Confirm merchant profile save",
            key="rules_confirm_merchants",
            placeholder=SAVE_MERCHANTS_CONFIRM,
            help="Type the confirmation phrase before saving and applying merchant profile changes.",
        )
        if st.button(
            "Save Merchant Profiles",
            type="primary",
            disabled=not confirmation_ready(
                st.session_state,
                "rules_confirm_merchants",
                SAVE_MERCHANTS_CONFIRM,
            ),
        ):
            try:
                _save_apply_and_rerun(
                    ctx,
                    _merchants_from_dataframe(edited, merchants.version),
                    taxonomy,
                    "streamlit_merchant_profiles",
                )
            except Exception as exc:
                st.error(f"Could not save merchant profiles: {exc}")

    elif view == "Category Taxonomy":
        st.subheader("Category Taxonomy")
        st.caption(
            "Merchant assignments are derived from merchant profiles. Change a merchant's "
            "category on the Merchant Profiles view."
        )
        original_df = _category_dataframe(taxonomy)
        edited = st.data_editor(
            original_df,
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            height=560,
            column_config={
                "Exclude": st.column_config.CheckboxColumn("Exclude"),
                _ORIGINAL_CATEGORY_COLUMN: None,
            },
            key="rules_category_editor",
        )
        st.caption(f"Unsaved changed rows: {_changed_rows(original_df, edited, 'Category')}")
        changed = _changed_dataframe(original_df, edited, "Category")
        if not changed.empty:
            with st.expander("Diff preview", expanded=True):
                st.dataframe(changed, width="stretch", hide_index=True, height=220)
        st.text_input(
            "Confirm category taxonomy save",
            key="rules_confirm_categories",
            placeholder=SAVE_CATEGORIES_CONFIRM,
            help="Type the confirmation phrase before saving and applying category taxonomy changes.",
        )
        if st.button(
            "Save Category Taxonomy",
            type="primary",
            disabled=not confirmation_ready(
                st.session_state,
                "rules_confirm_categories",
                SAVE_CATEGORIES_CONFIRM,
            ),
        ):
            try:
                _save_apply_and_rerun(
                    ctx,
                    merchants,
                    _categories_from_dataframe(
                        edited,
                        taxonomy.version,
                        taxonomy.default_category,
                        existing=taxonomy,
                    ),
                    "streamlit_category_taxonomy",
                )
            except Exception as exc:
                st.error(f"Could not save category taxonomy: {exc}")

    elif view == "Apply History":
        st.subheader("Apply History")
        history = RuleApplicationService(ctx.db_path).history()
        if history:
            st.dataframe(pd.DataFrame(history), width="stretch", hide_index=True)
        else:
            empty_state("No rule application runs have been recorded yet.")

    else:
        st.subheader("Import / Export")
        st.caption("TOML remains useful as a checked-in seed and snapshot format.")

        col1, col2 = st.columns(2)
        with col1:
            seed_dir = Path(
                st.text_input(
                    "Seed TOML directory",
                    value=str(seed_config_path("merchants.toml").parent),
                    key="rules_seed_dir",
                )
            )
            st.text_input(
                "Confirm seed import",
                key="rules_confirm_seed_import",
                placeholder=IMPORT_SEED_CONFIRM,
                help="Type the confirmation phrase before replacing runtime rules from TOML.",
            )
            if st.button(
                "Import Seed TOML",
                disabled=not confirmation_ready(
                    st.session_state,
                    "rules_confirm_seed_import",
                    IMPORT_SEED_CONFIRM,
                ),
            ):
                try:
                    _save_apply_and_rerun(
                        ctx,
                        load_merchant_rules(seed_dir / "merchants.toml"),
                        load_categories(seed_dir / "categories.toml"),
                        "streamlit_seed_import",
                    )
                except Exception as exc:
                    st.error(f"Could not import seed TOML: {exc}")

        with col2:
            export_dir = Path(
                # Default to the gitignored data/exported (matching commands/config.py),
                # not the tracked configs/ tree, so derived merchant PII does not land
                # inside the working tree by default.
                st.text_input(
                    "Export directory",
                    value=str(ctx.db_path.parent / "exported"),
                    key="rules_export_dir",
                )
            )
            if st.button("Export Runtime TOML"):
                try:
                    repo.export_to_directory(export_dir)
                    st.success(f"Exported runtime config to {export_dir}")
                except Exception as exc:
                    st.error(f"Could not export runtime TOML: {exc}")

        if not validation.issues:
            empty_state("No validation issues are present.")
