"""Review Queue page."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from ledger.import_review import (
    REVIEW_EXPECTED_FIELDS,
    REVIEW_MUTATION_FIELDS,
    ReviewConflictError,
    apply_review_mutations,
    get_review_rows,
)
from ledger.imports.pdf.files import FinalizeLockError, finalize_run_lock
from ledger.imports.pdf.readiness import (
    FINALIZABLE_STATUSES,
    LiveMutationDuringOpenImportRefused,
    live_mutation_boundary,
)
from ledger.storage.snapshots import (
    DatabaseWriteCommittedError,
    audited_database_write_connection,
    database_write_outcome,
)
from ledger.ui.components import (
    CURRENCY_FORMAT,
    confirmation_ready,
    format_currency_precise,
    metric_row,
    page_header,
)
from ledger.ui.context import (
    AppContext,
    load_review_category_config,
    review_amount,
    review_examples_text,
)
from ledger.utils.private_files import PinnedPrivateFile, pin_existing_directory

NEEDS_DECISION = "Needs decision"
STATUS_OPTIONS = ["pending", "resolved", "reviewed_as_is"]
SCOPE_OPTIONS = ["same_raw_merchant_in_run", "this_transaction_only"]
REVIEW_SAVE_CONFIRM = "save review decisions"
REVIEW_BULK_RESOLVE_CONFIRM = "resolve visible review rows"
REVIEW_BULK_AS_IS_CONFIRM = "review visible rows as-is"
REVIEW_UPDATE_UNAVAILABLE = (
    "Review update is temporarily unavailable; retry after the import state settles."
)
REVIEW_UPDATE_COMMITTED_WARNING = (
    "Review update commit succeeded, but final validation or cleanup did not complete; "
    "reload and inspect before retrying."
)
# run.json grows with the batch (one pdfs[] entry per statement, indent=2): a
# real ~50-PDF stage crosses 64 KiB, and the old 64 KiB cap made this validator
# refuse the pin — so the Review Queue could not save ANY decision for a large
# open run, failing with the generic "unavailable" error at save time. The cap
# exists only to bound a hostile file, not to police batch size; 8 MiB clears
# every plausible run (thousands of PDFs) while still refusing absurdity.
_RUN_STATE_MAX_BYTES = 8 * 1024 * 1024
_REVIEW_BASELINE_ACTIVE_KEY = "_ledger_review_baseline_active"
_REVIEW_BASELINE_KEY_PREFIX = "_ledger_review_baseline_"


def _absolute_lexical_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _review_source_digest(ctx: AppContext) -> str:
    """Return an opaque identity for the exact live or staged review source."""

    def source_path(value: object) -> str:
        if value is None or str(value) == "":
            return ""
        return os.fspath(_absolute_lexical_path(str(value)))

    identity = {
        "is_live_db": _writes_live_db(ctx.review_source),
        "review_db": source_path(ctx.review_db),
        "run_dir": source_path(ctx.review_source.get("run_dir")),
        "runs_dir": source_path(ctx.review_source.get("runs_dir")),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_baseline_key(ctx: AppContext) -> tuple[str, str]:
    source_digest = _review_source_digest(ctx)
    return source_digest, f"{_REVIEW_BASELINE_KEY_PREFIX}{source_digest}"


def _load_previous_render_baseline(ctx: AppContext) -> list[dict[str, Any]] | None:
    """Load only the prior completed render for this exact review source."""
    source_digest, baseline_key = _review_baseline_key(ctx)
    active_key = st.session_state.get(_REVIEW_BASELINE_ACTIVE_KEY)
    if active_key != baseline_key:
        if isinstance(active_key, str) and active_key.startswith(_REVIEW_BASELINE_KEY_PREFIX):
            st.session_state.pop(active_key, None)
        st.session_state.pop(_REVIEW_BASELINE_ACTIVE_KEY, None)
        return None

    payload = st.session_state.get(baseline_key)
    if not isinstance(payload, dict) or payload.get("source_digest") != source_digest:
        st.session_state.pop(baseline_key, None)
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        st.session_state.pop(baseline_key, None)
        return None
    review_keys = [str(row.get("review_key") or "") for row in rows]
    required_fields = {"review_key", *REVIEW_EXPECTED_FIELDS}
    if (
        any(not key for key in review_keys)
        or len(review_keys) != len(set(review_keys))
        or any(not required_fields.issubset(row) for row in rows)
    ):
        st.session_state.pop(baseline_key, None)
        return None
    return [dict(row) for row in rows]


def _store_completed_render_baseline(ctx: AppContext, rows: list[dict[str, Any]]) -> None:
    """Replace the active baseline only after a normal render reaches completion."""
    source_digest, baseline_key = _review_baseline_key(ctx)
    active_key = st.session_state.get(_REVIEW_BASELINE_ACTIVE_KEY)
    if (
        isinstance(active_key, str)
        and active_key.startswith(_REVIEW_BASELINE_KEY_PREFIX)
        and active_key != baseline_key
    ):
        st.session_state.pop(active_key, None)
    st.session_state[baseline_key] = {
        "source_digest": source_digest,
        "rows": [dict(row) for row in rows],
    }
    st.session_state[_REVIEW_BASELINE_ACTIVE_KEY] = baseline_key


@contextmanager
def _validated_staged_review_source(ctx: AppContext) -> Iterator[PinnedPrivateFile]:
    """Pin and validate the exact staged database while its finalize lock is held."""
    run_dir_value = ctx.review_source.get("run_dir")
    runs_dir_value = ctx.review_source.get("runs_dir")
    if not run_dir_value or not runs_dir_value:
        raise OSError("Staged review source is unavailable")
    run_dir = _absolute_lexical_path(Path(run_dir_value))
    runs_dir = _absolute_lexical_path(Path(runs_dir_value))
    if run_dir.parent != runs_dir:
        raise OSError("Staged review source is unavailable")

    with pin_existing_directory(runs_dir) as pinned_runs:
        pinned_runs.require_owner_only()
        with pinned_runs.pin_child_directory(run_dir.name) as pinned_run:
            pinned_run.require_owner_only()
            try:
                run_state = json.loads(
                    pinned_run.read_private_text("run.json", max_bytes=_RUN_STATE_MAX_BYTES)
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OSError("Staged review source is unavailable") from exc
            if not isinstance(run_state, dict):
                raise OSError("Staged review source is unavailable")
            if str(run_state.get("status", "")) not in FINALIZABLE_STATUSES:
                raise OSError("Staged review source is unavailable")

            expected_db = pinned_run.path / "staging.db"
            state_db = _absolute_lexical_path(str(run_state.get("staging_db_path", "")))
            review_db = _absolute_lexical_path(ctx.review_db)
            if state_db != expected_db or review_db != expected_db:
                raise OSError("Staged review source is unavailable")

            with pinned_run.pin_private_file("staging.db", mutable=True) as staging_file:
                yield staging_file


@contextmanager
def _validated_live_review_source(ctx: AppContext) -> Iterator[PinnedPrivateFile]:
    review_db = _absolute_lexical_path(ctx.review_db)
    with pin_existing_directory(review_db.parent) as parent:
        parent.require_owner_only()
        with parent.pin_private_file(review_db.name, mutable=True) as database_file:
            yield database_file


def _review_mutations(
    original_rows: list[dict[str, Any]],
    edited_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    original_by_key = {
        str(row.get("review_key") or ""): row for row in original_rows if row.get("review_key")
    }
    mutations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edited in edited_rows:
        key = str(edited.get("review_key") or "")
        if not key or key in seen or key not in original_by_key:
            raise ReviewConflictError("Review rows changed; reload and retry")
        seen.add(key)
        original = original_by_key[key]
        desired = {
            "status": str(edited.get("status") or "pending"),
            "merchant": str(edited.get("merchant") or ""),
            "category": str(edited.get("category") or "Other"),
            "note": str(edited.get("note") or ""),
            "scope": str(edited.get("scope") or "same_raw_merchant_in_run"),
        }
        expected = {
            "status": str(original.get("status") or "pending"),
            "merchant": str(original.get("merchant") or ""),
            "category": str(original.get("category") or "Other"),
            "note": str(original.get("note") or ""),
            "scope": str(original.get("scope") or "same_raw_merchant_in_run"),
            "txn_ids": str(original.get("txn_ids") or "[]"),
            "examples": str(original.get("examples") or "[]"),
            "transaction_count": int(original.get("transaction_count") or 0),
            "total_abs_amount": str(original.get("total_abs_amount") or "0"),
            "updated_at": str(original.get("updated_at") or ""),
        }
        if any(desired[field] != expected[field] for field in REVIEW_MUTATION_FIELDS):
            mutations.append({"review_key": key, "expected": expected, "desired": desired})
    if seen != set(original_by_key):
        raise ReviewConflictError("Review rows changed; reload and retry")
    return mutations


def _save_and_apply_review(
    ctx: AppContext,
    original_rows: list[dict[str, Any]] | None,
    edited_rows: list[dict[str, Any]],
) -> int | None:
    """Apply one UI action under the same exclusion boundary as finalize/stage."""
    try:
        if not isinstance(original_rows, list) or any(
            not isinstance(row, dict) for row in original_rows
        ):
            raise ReviewConflictError("Review rows changed; reload and retry")
        mutations = _review_mutations(original_rows, edited_rows)
        if _writes_live_db(ctx.review_source):
            with database_write_outcome() as outcome:
                with live_mutation_boundary(  # noqa: SIM117 - commit precedes outer exit
                    ctx.review_db,
                    operation="ui_review_apply",
                ):
                    with _validated_live_review_source(ctx) as database_file:
                        with audited_database_write_connection(
                            database_file,
                            label="Live review source",
                        ) as connection:
                            updated = apply_review_mutations(
                                connection,
                                ctx.review_db,
                                mutations,
                            )
                        outcome.mark_committed()
                return updated

        run_dir = Path(ctx.review_source["run_dir"])
        runs_dir = Path(ctx.review_source["runs_dir"])
        with database_write_outcome() as outcome:
            with finalize_run_lock(runs_dir, run_dir):  # noqa: SIM117 - ordered exits
                with _validated_staged_review_source(ctx) as database_file:
                    with audited_database_write_connection(
                        database_file,
                        label="Staged review source",
                    ) as connection:
                        updated = apply_review_mutations(connection, ctx.review_db, mutations)
                    outcome.mark_committed()
            return updated
    except DatabaseWriteCommittedError:
        st.error(REVIEW_UPDATE_COMMITTED_WARNING)
        return None
    except (
        FinalizeLockError,
        LiveMutationDuringOpenImportRefused,
        ReviewConflictError,
        SQLAlchemyError,
        KeyError,
        OSError,
        sqlite3.Error,
        ValueError,
    ):
        st.error(REVIEW_UPDATE_UNAVAILABLE)
        return None


def _writes_live_db(review_source: dict) -> bool:
    """Whether saving writes the LIVE ledger rather than a staged import copy.

    ``get_review_source`` sets ``is_live_db`` explicitly and it wins. The fallbacks
    exist for a context assembled elsewhere and resolve toward LIVE whenever the
    source cannot be identified: a spurious warning costs noise, a missed one costs a
    silent write to real transactions.
    """
    if "is_live_db" in review_source:
        return bool(review_source["is_live_db"])
    status = review_source.get("status")
    if status is None:
        return True
    return str(status) == "live_db"


def _write_target_notice(review_source: dict, review_db: Path) -> tuple[str, str]:
    """Return (streamlit severity, message) naming the database Save will write.

    The live path used to be indistinguishable from a staged one: with no active
    import run the context silently retargets ``data/ledger.db`` and Save applies the
    decisions straight to real transactions.
    """
    if _writes_live_db(review_source):
        return (
            "warning",
            f"**Saving writes the LIVE database — `{review_db}`.** No import run is "
            "active, so Save and the bulk actions change your real transactions "
            "immediately; there is no staging copy to discard. Confirmation phrases on "
            "this page therefore end in `to live db`.",
        )
    return (
        "info",
        f"Saving writes the staged import run **{review_source.get('label', '')}** — "
        f"`{review_db}`. Nothing reaches the live database until the run is finalized.",
    )


def _confirm_phrase(base: str, *, writes_live_db: bool) -> str:
    """Confirmation text for an action, distinct when the target is the live database."""
    return f"{base} to live db" if writes_live_db else base


def _category_options(ctx: AppContext) -> tuple[list[str], str | None]:
    """Selectable categories, read from the database the applier loads config from.

    The dropdown used to list ``ctx.category_config`` (the LIVE config) while
    ``apply_review_decisions`` loads the REVIEW database's config. A category added
    during the review window was therefore selectable and then unknown at apply time,
    where ``exclude_from_spending`` falls through to ``False`` and 'manual' confidence
    freezes it. Returns no options when the authoritative config is unavailable;
    every write action then remains disabled.
    """
    config = load_review_category_config(ctx.review_db)
    if config is None:
        return (
            [],
            "Review actions are disabled because authoritative category configuration "
            "is unavailable. Restore or migrate the review database, then reload.",
        )
    return sorted(cat.name for cat in config.categories), None


def _needs_decision(row: dict) -> bool:
    """Whether a review row still requires a human decision.

    Either it is still ``pending``, or it was marked ``resolved`` without actually
    supplying both a merchant and a category (which leaves the transaction unmapped).
    """
    if row["status"] == "pending":
        return True
    return row["status"] == "resolved" and (not row.get("merchant") or not row.get("category"))


def _editor_state_key(review_source: dict, rows: list[dict[str, Any]]) -> str:
    """Widget key for the decisions editor, bound to the VISIBLE row set.

    Streamlit stores a keyed data_editor's pending edits positionally and its
    widget identity by schema (columns, dtypes, row count) — not by cell values.
    Keyed only by source, a filter/search change that produced an equal-sized
    visible subset re-applied the stored edits to whatever rows now sat at those
    positions, and Save then wrote the decision onto the wrong review rows.
    Folding a digest of the visible review keys into the key mints a fresh
    editor whenever the visible set changes: pending unsaved edits are dropped
    (safe) instead of silently retargeted (not)."""
    identity = hashlib.sha256(
        "\n".join(str(row.get("review_key") or "") for row in rows).encode("utf-8")
    ).hexdigest()[:12]
    return f"review_queue_{review_source['source_kind']}_{review_source['label']}_{identity}"


def _visible_rows(
    rows: list[dict],
    status_filter: str | None,
    missing_filter: str | None,
    search: str | None,
) -> list[dict]:
    result = rows
    # "Needs decision" is a Status option (the default), not a literal status value —
    # matching it against row["status"] would match nothing and leave the landing page
    # permanently empty regardless of how many rows are pending.
    if status_filter == NEEDS_DECISION:
        result = [row for row in result if _needs_decision(row)]
    elif status_filter and status_filter != "All":
        result = [row for row in result if row["status"] == status_filter]
    if missing_filter == "Missing merchant":
        result = [row for row in result if not row.get("merchant")]
    elif missing_filter == "Missing category":
        result = [row for row in result if not row.get("category")]
    if search:
        needle = search.casefold()
        result = [
            row
            for row in result
            if needle in row["review_key"].casefold()
            or needle in (row.get("merchant") or "").casefold()
            or needle in (row.get("category") or "").casefold()
            or needle in (row.get("examples") or "").casefold()
        ]
    return result


def _review_dataframe(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "review_key": row["review_key"],
                "status": row["status"],
                "merchant": row["merchant"],
                "category": row["category"],
                "scope": row["scope"],
                "note": row["note"],
                "count": row["transaction_count"],
                "amount": float(Decimal(str(row["total_abs_amount"]))),
                "examples": review_examples_text(row["examples"]),
            }
            for row in rows
        ]
    )


def _records(rows: list[dict] | pd.DataFrame) -> list[dict]:
    if isinstance(rows, pd.DataFrame):
        return rows.to_dict("records")
    return rows


def _row_count(row: dict) -> int:
    return int(row.get("count") or row.get("transaction_count") or 0)


def _review_decision_preview(
    original_rows: list[dict] | pd.DataFrame,
    edited_rows: list[dict] | pd.DataFrame,
) -> dict:
    original_by_key = {
        str(row.get("review_key")): row
        for row in _records(original_rows)
        if row.get("review_key") is not None
    }
    records = _records(edited_rows)
    changed = 0
    resolved = 0
    reviewed_as_is = 0
    incomplete = 0
    affected_transactions = 0

    for row in records:
        key = str(row.get("review_key"))
        original = original_by_key.get(key, {})
        row_changed = any(
            str(row.get(field) or "") != str(original.get(field) or "")
            for field in REVIEW_MUTATION_FIELDS
        )
        if row_changed:
            changed += 1

        status = str(row.get("status") or "pending")
        merchant = str(row.get("merchant") or "").strip()
        category = str(row.get("category") or "").strip()
        count = _row_count(row)
        if status == "resolved":
            resolved += 1
            if not merchant or not category:
                # Only flag an incomplete row the user actually edited. A pre-existing
                # resolved-but-incomplete legacy row would otherwise populate errors and
                # disable Save / bulk actions for ALL rows, including valid untouched edits.
                if row_changed:
                    incomplete += 1
            else:
                affected_transactions += count
        elif status == "reviewed_as_is":
            reviewed_as_is += 1
            affected_transactions += count

    errors = []
    if incomplete:
        noun = "row" if incomplete == 1 else "rows"
        errors.append(f"{incomplete} resolved {noun} needs merchant and category before save.")

    return {
        "rows": len(records),
        "changed": changed,
        "resolved": resolved,
        "reviewed_as_is": reviewed_as_is,
        "incomplete": incomplete,
        "affected_transactions": affected_transactions,
        "errors": errors,
    }


def _review_action_ready(
    state: Any,  # Streamlit SessionStateProxy (dict-like) or a plain dict in tests
    key: str,
    expected: str,
    preview: dict,
) -> bool:
    return (
        int(preview.get("changed", 0)) > 0
        and not preview.get("errors")
        and confirmation_ready(state, key, expected)
    )


def _decision_help(row: dict | None) -> str:
    if not row:
        return "Select a row to inspect the examples and decision state."
    if row["status"] == "resolved":
        return "Resolved will apply the merchant and category to the grouped transactions."
    if row["status"] == "reviewed_as_is":
        return "Reviewed as-is stops the prompt; merchant, category, and confidence stay auto-derived and can still be updated by future rule passes."
    return "Pending rows block finalize until resolved or reviewed as-is."


def render(ctx: AppContext) -> None:
    page_header(
        "Review Queue",
        "Work through unresolved merchant and category decisions before finalizing imports.",
        status=f"{ctx.pending_reviews} pending",
    )

    writes_live_db = _writes_live_db(ctx.review_source)
    severity, notice = _write_target_notice(ctx.review_source, ctx.review_db)
    (st.warning if severity == "warning" else st.info)(notice)

    prior_rendered_rows = _load_previous_render_baseline(ctx)
    all_rows = get_review_rows(ctx.review_db)
    active_rows = [
        row
        for row in all_rows
        if row["status"] == "pending"
        or (row["status"] == "resolved" and (not row["merchant"] or not row["category"]))
    ]

    metric_row(
        [
            (ctx.review_source["source_kind"], str(ctx.review_source["label"]), None),
            ("Source Status", str(ctx.review_source["status"]), None),
            ("Pending", f"{len(active_rows):,}", None),
            ("Affected Amount", format_currency_precise(review_amount(active_rows)), None),
        ]
    )

    st.divider()

    if not all_rows:
        st.success("No review rows found.")
        _store_completed_render_baseline(ctx, [])
        return

    filter_col, detail_col = st.columns([0.72, 0.28], gap="large")
    with filter_col:
        c1, c2, c3 = st.columns([0.28, 0.28, 0.44])
        with c1:
            status_filter = st.segmented_control(
                "Status",
                [NEEDS_DECISION, *STATUS_OPTIONS, "All"],
                default=NEEDS_DECISION,
            )
        with c2:
            missing_filter = st.segmented_control(
                "Missing",
                ["Any", "Missing merchant", "Missing category"],
                default="Any",
            )
        with c3:
            search = st.text_input("Search", placeholder="Merchant, category, review key, examples")

    rows = _visible_rows(all_rows, status_filter, missing_filter, search)

    with detail_col:
        selected_key = st.selectbox(
            "Detail",
            options=[row["review_key"] for row in rows] if rows else [],
            index=0 if rows else None,
            placeholder="Select a row",
        )

    if not rows:
        # Don't dead-end. The common case is the default "Needs decision" filter on a
        # fully-resolved queue: saying only "no rows match" hides that the queue is
        # CLEAR, and leaves the user on a page with nothing to do and no next step.
        if status_filter == NEEDS_DECISION:
            st.success(
                f"Nothing needs a decision — all {len(all_rows):,} review row(s) are "
                "resolved or reviewed as-is. Showing them all so you can revisit any."
            )
            rows = all_rows
        else:
            st.info(
                f"No rows match this filter. The queue holds {len(all_rows):,} row(s) — "
                "switch Status to **All** to see them."
            )
            _store_completed_render_baseline(ctx, [])
            return

    selected_row = next((row for row in rows if row["review_key"] == selected_key), rows[0])

    left, right = st.columns([0.72, 0.28], gap="large")
    with left:
        category_options, category_warning = _category_options(ctx)
        category_config_available = bool(category_options)
        if category_warning:
            st.warning(category_warning)
        df_review = _review_dataframe(rows)
        with st.expander("Bulk actions"):
            complete = df_review[
                (df_review["merchant"].astype(str).str.len() > 0)
                & (df_review["category"].astype(str).str.len() > 0)
            ]
            st.caption(f"{len(complete):,} visible row(s) already have both merchant and category.")
            bulk = df_review.copy()
            mask = (bulk["merchant"].astype(str).str.len() > 0) & (
                bulk["category"].astype(str).str.len() > 0
            )
            resolve_bulk = bulk.copy()
            resolve_bulk.loc[mask, "status"] = "resolved"
            resolve_preview = _review_decision_preview(df_review, resolve_bulk)
            as_is_bulk = bulk.copy()
            as_is_bulk.loc[mask, "status"] = "reviewed_as_is"
            as_is_preview = _review_decision_preview(df_review, as_is_bulk)
            st.caption(
                "Bulk preview: "
                f"{resolve_preview['changed']:,} resolve change(s), "
                f"{as_is_preview['changed']:,} reviewed-as-is change(s)."
            )
            bulk_resolve_phrase = _confirm_phrase(
                REVIEW_BULK_RESOLVE_CONFIRM, writes_live_db=writes_live_db
            )
            bulk_as_is_phrase = _confirm_phrase(
                REVIEW_BULK_AS_IS_CONFIRM, writes_live_db=writes_live_db
            )
            c1, c2 = st.columns(2)
            c1.text_input(
                "Confirm bulk resolve",
                key="review_confirm_bulk_resolve",
                help=f"Type `{bulk_resolve_phrase}` before resolving visible rows.",
            )
            c2.text_input(
                "Confirm bulk reviewed as-is",
                key="review_confirm_bulk_as_is",
                help=f"Type `{bulk_as_is_phrase}` before marking visible rows reviewed.",
            )
            b1, b2 = st.columns(2)
            if b1.button(
                "Resolve complete visible rows",
                disabled=(
                    not category_config_available
                    or not _review_action_ready(
                        st.session_state,
                        "review_confirm_bulk_resolve",
                        bulk_resolve_phrase,
                        resolve_preview,
                    )
                ),
            ):
                bulk = resolve_bulk
                updated = _save_and_apply_review(ctx, prior_rendered_rows, bulk.to_dict("records"))
                if updated is not None:
                    st.cache_data.clear()
                    st.success(
                        f"Resolved visible complete rows and updated {updated} transactions."
                    )
                    st.rerun()
            if b2.button(
                "Review complete visible rows as-is",
                disabled=(
                    not category_config_available
                    or not _review_action_ready(
                        st.session_state,
                        "review_confirm_bulk_as_is",
                        bulk_as_is_phrase,
                        as_is_preview,
                    )
                ),
            ):
                bulk = as_is_bulk
                updated = _save_and_apply_review(ctx, prior_rendered_rows, bulk.to_dict("records"))
                if updated is not None:
                    st.cache_data.clear()
                    st.success(
                        f"Marked visible complete rows reviewed and updated {updated} transactions."
                    )
                    st.rerun()
        edited = st.data_editor(
            df_review,
            width="stretch",
            hide_index=True,
            column_order=[
                "status",
                "merchant",
                "category",
                "scope",
                "note",
                "count",
                "amount",
                "review_key",
                "examples",
            ],
            column_config={
                "status": st.column_config.SelectboxColumn("Status", options=STATUS_OPTIONS),
                "category": st.column_config.SelectboxColumn("Category", options=category_options),
                "scope": st.column_config.SelectboxColumn("Scope", options=SCOPE_OPTIONS),
                "amount": st.column_config.NumberColumn("Abs Amount", format=CURRENCY_FORMAT),
                "count": st.column_config.NumberColumn("Count", disabled=True),
                "review_key": st.column_config.TextColumn("Review Key", disabled=True),
                "examples": st.column_config.TextColumn("Examples", disabled=True),
            },
            disabled=["count", "amount", "review_key", "examples"],
            height=620,
            key=_editor_state_key(ctx.review_source, rows),
        )
        preview = _review_decision_preview(df_review, edited)
        metric_row(
            [
                ("Changed Rows", f"{preview['changed']:,}", None),
                ("Resolved", f"{preview['resolved']:,}", None),
                ("Reviewed As-Is", f"{preview['reviewed_as_is']:,}", None),
                ("Affected Txns", f"{preview['affected_transactions']:,}", None),
            ]
        )
        for error in preview["errors"]:
            st.error(error)
        save_phrase = _confirm_phrase(REVIEW_SAVE_CONFIRM, writes_live_db=writes_live_db)
        st.text_input(
            "Confirm review save",
            key="review_confirm_save",
            help=f"Type `{save_phrase}` before saving and applying review decisions.",
        )

        save_col, hint_col = st.columns([0.28, 0.72])
        with save_col:
            if st.button(
                "Save Review Decisions — LIVE DB" if writes_live_db else "Save Review Decisions",
                type="primary",
                width="stretch",
                disabled=(
                    not category_config_available
                    or not _review_action_ready(
                        st.session_state,
                        "review_confirm_save",
                        save_phrase,
                        preview,
                    )
                ),
            ):
                updated = _save_and_apply_review(
                    ctx, prior_rendered_rows, edited.to_dict("records")
                )
                if updated is not None:
                    st.cache_data.clear()
                    st.success(f"Saved review decisions and updated {updated} transactions.")
                    st.rerun()
        with hint_col:
            st.caption(
                "`resolved` applies merchant/category and locks confidence to manual. "
                "`reviewed_as_is` keeps current values and stops the prompt; confidence "
                "stays auto-derived and future rule passes can still update it."
            )

    with right:
        st.subheader("Selected Row")
        st.caption(selected_row["review_key"])
        st.metric("Transactions", f"{selected_row['transaction_count']:,}")
        st.metric(
            "Abs Amount", format_currency_precise(Decimal(str(selected_row["total_abs_amount"])))
        )
        st.write(_decision_help(selected_row))
        st.text_area(
            "Examples",
            value=review_examples_text(selected_row["examples"]),
            height=220,
            disabled=True,
        )

    _store_completed_render_baseline(ctx, rows)
