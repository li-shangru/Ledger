"""Processing and database-write helpers for PDF import runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ledger.config import load_app_config, load_ledger_config
from ledger.config.repository import load_runtime_app_config
from ledger.imports.pdf.files import write_json
from ledger.imports.pdf.paths import Paths
from ledger.processing import ProcessingResult
from ledger.storage.database import (
    ProcessedFileDB,
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    TransferDB,
    session_scope,
)
from ledger.storage.overrides import collect_durable_overrides, normalize_source_path
from ledger.storage.writer import DatabaseWriter
from ledger.utils import naive_utc_now, script_version


def load_config(paths: Paths, runtime_db: Path | None = None):
    if runtime_db:
        ledger = load_ledger_config(paths.ledger_config.resolve())
        return load_runtime_app_config(
            runtime_db,
            ledger,
            paths.merchant_rules.resolve(),
            paths.categories.resolve(),
        )

    return load_app_config(
        paths.ledger_config.resolve(),
        paths.merchant_rules.resolve(),
        paths.categories.resolve(),
    )


def validate_processing_result(result: ProcessingResult, report_path: Path) -> None:
    if result.exceptions:
        payload = [exc.model_dump(mode="json") for exc in result.exceptions]
        write_json(report_path, payload)
        raise SystemExit(
            f"Import produced {len(result.exceptions)} parse exceptions: {report_path}"
        )

    if result.total_scanned == 0:
        raise SystemExit("Import produced zero scanned rows; refusing to write database.")


def write_result_to_db(db_path: Path, csv_root: Path, result: ProcessingResult) -> None:
    # Do the statement-row replacement (delete) and the writer upsert (re-insert) in ONE
    # transaction so a crash between them can't leave the statement's old rows deleted
    # with the new rows unwritten.
    writer = DatabaseWriter(db_path)
    with session_scope(db_path) as session:
        source_paths = {
            txn.source_path for txn in (*result.transactions, *result.transfers) if txn.source_path
        }
        durable_overrides = collect_durable_overrides(session, source_paths=source_paths)
        replace_statement_rows(db_path, result, session=session)
        writer.write_result(
            result,
            csv_root,
            script_version(),
            session=session,
            durable_overrides=durable_overrides,
        )


def replace_statement_rows(
    db_path: Path, result: ProcessingResult, session: Session | None = None
) -> None:
    """Make the writer authoritative for the statements it covers.

    The writer upserts by ``txn_id``, so a re-import whose rows changed ``txn_id``
    (e.g. a parser/normalization tweak altered a description that feeds the hash)
    would leave the old rows behind as duplicates. For every ``source_path`` in
    this import, delete existing transaction/transfer rows the new result no
    longer produces, scoped to those statement paths so other statements are
    untouched.

    Scoping uses the period-qualified ``source_path``, NOT the bare
    ``source_file`` basename — the same basename repeats across every month
    for an account, so scoping by it would delete other months' rows (data loss).
    This also subsumes cross-table moves: a row that became a transfer is dropped
    from ``transactions`` and re-inserted into ``transfers``.

    **Orphan prevention**: when a ``txn_id`` is removed from ``transactions``, any
    ``transaction_treatments`` / ``transaction_offsets`` rows referencing it are also
    deleted in the same transaction so they never become orphans.

    **Legacy NULL-source_path transfers**: pre-migration transfer rows have
    ``source_path = NULL`` and are invisible to the ``source_path IN (...)`` filter.
    A re-import that changes ``txn_id`` would leave them as duplicates.  We
    additionally delete NULL-source_path transfers whose ``(institution, account)``
    matches any row in this import, whose ``txn_id`` is NOT in the new transfer set,
    **and whose ``posting_date`` falls within the date range this import's transfers
    for that account cover**.  The date bound is essential: in a DB built before
    source_path tracking EVERY historical transfer has ``source_path = NULL``, so
    without it, importing one new statement for an account would delete that
    account's entire transfer history (only rows inside the new statement's coverage
    can be stale duplicates of the rows being rewritten).
    """
    from sqlalchemy import and_, or_

    source_paths = {
        txn.source_path for txn in (*result.transactions, *result.transfers) if txn.source_path
    }
    if not source_paths:
        return

    transaction_ids = [txn.txn_id for txn in result.transactions]
    transfer_ids = [txn.txn_id for txn in result.transfers]

    # Collect institution+account pairs for the NULL-source_path legacy transfer cleanup.
    # Only remove legacy transfers for accounts whose statements are being replaced.
    ia_pairs = {
        (getattr(txn, "institution", None), getattr(txn, "account", None))
        for txn in result.transfers
        if getattr(txn, "institution", None) and getattr(txn, "account", None)
    }

    def _apply(session: Session) -> None:
        normalized_source_paths = {normalize_source_path(path) for path in source_paths}
        transaction_source_paths = {
            stored_path
            for (stored_path,) in session.query(TransactionDB.source_path).distinct().all()
            if normalize_source_path(stored_path) in normalized_source_paths
        }
        transfer_source_paths = {
            stored_path
            for (stored_path,) in session.query(TransferDB.source_path).distinct().all()
            if stored_path is not None
            and normalize_source_path(stored_path) in normalized_source_paths
        }

        # --- Transactions ---
        txn_query = session.query(TransactionDB).filter(
            TransactionDB.source_path.in_(transaction_source_paths)
        )
        if transaction_ids:
            txn_query = txn_query.filter(TransactionDB.txn_id.notin_(transaction_ids))
        # Collect the txn_ids being removed so we can cascade to treatments/offsets.
        removed_txn_ids = [row.txn_id for row in txn_query.all()]
        txn_query.delete(synchronize_session=False)

        # --- Cascade: delete treatments for removed transaction ids ---
        if removed_txn_ids:
            session.query(TransactionTreatmentDB).filter(
                TransactionTreatmentDB.txn_id.in_(removed_txn_ids)
            ).delete(synchronize_session=False)
            # Delete offsets where either side references a removed txn_id
            session.query(TransactionOffsetDB).filter(
                or_(
                    TransactionOffsetDB.credit_txn_id.in_(removed_txn_ids),
                    TransactionOffsetDB.debit_txn_id.in_(removed_txn_ids),
                )
            ).delete(synchronize_session=False)

        # --- Transfers: normal source_path scoped delete ---
        transfer_query = session.query(TransferDB).filter(
            TransferDB.source_path.in_(transfer_source_paths)
        )
        if transfer_ids:
            transfer_query = transfer_query.filter(TransferDB.txn_id.notin_(transfer_ids))
        transfer_query.delete(synchronize_session=False)

        # --- Legacy NULL-source_path transfers: conservative institution+account scoped delete ---
        # Only touch rows where source_path IS NULL (pre-migration) for the same
        # institution+account being replaced.  Never touch rows from other accounts.
        if ia_pairs:
            # Bound the legacy cleanup to the DATE RANGE this import's transfers
            # actually cover, per (institution, account). Without this, a DB built
            # before source_path tracking (where EVERY historical transfer has
            # source_path IS NULL) loses its entire transfer history for an account
            # the first time any new statement for that account is imported — the
            # old rows' txn_ids aren't in the new set, so they'd all be deleted.
            # Only NULL rows WITHIN the re-imported statement's coverage can be stale
            # duplicates of the rows being rewritten; earlier statements' transfers
            # (outside this range) must be preserved.
            date_bounds: dict[tuple[Any, Any], tuple[Any, Any]] = {}
            for txn in result.transfers:
                ia = (getattr(txn, "institution", None), getattr(txn, "account", None))
                posted = txn.posting_date
                if ia in date_bounds:
                    low, high = date_bounds[ia]
                    date_bounds[ia] = (min(low, posted), max(high, posted))
                else:
                    date_bounds[ia] = (posted, posted)
            for institution, account in ia_pairs:
                low, high = date_bounds[(institution, account)]
                legacy_q = session.query(TransferDB).filter(
                    and_(
                        TransferDB.source_path.is_(None),
                        TransferDB.institution == institution,
                        TransferDB.account == account,
                        TransferDB.posting_date >= low,
                        TransferDB.posting_date <= high,
                    )
                )
                if transfer_ids:
                    legacy_q = legacy_q.filter(TransferDB.txn_id.notin_(transfer_ids))
                legacy_q.delete(synchronize_session=False)

    if session is not None:
        _apply(session)
    else:
        with session_scope(db_path) as own_session:
            _apply(own_session)


def record_processed_files(
    db_path: Path,
    pdfs: list[dict[str, Any]],
) -> None:
    """Record every distinct processed PDF so the content-hash dedup set is complete.

    The dedup set (see ``known_pdf_hashes``) is keyed on ``file_hash``, but the
    ``processed_files`` PRIMARY KEY is ``file_path``. Two DISTINCT PDFs that map to
    the same account + statement month resolve to the SAME intended ``processed_pdf``
    archive path, so a naive ``merge(file_path=processed_pdf)`` collapses them into
    one row — silently dropping one ``file_hash`` from the dedup set, so a later
    re-import of the dropped PDF is NOT recognised as already-processed.

    Key by actual file identity (content hash): when a ``file_path`` is already
    claimed by a DIFFERENT hash (within this batch OR already in the DB), give the
    colliding record a hash-disambiguated ``file_path`` so every distinct PDF keeps
    its own row and its hash survives in the dedup set.
    """
    now = naive_utc_now()
    with session_scope(db_path) as session:
        # path -> hash already recorded for that path (DB rows + earlier in this batch).
        path_to_hash: dict[str, str] = {
            row.file_path: row.file_hash
            for row in session.query(ProcessedFileDB.file_path, ProcessedFileDB.file_hash).all()
        }
        for item in pdfs:
            if item.get("skipped_reason"):
                continue
            processed_pdf = item.get("processed_pdf", "")
            file_hash = item.get("file_hash", "")
            if not processed_pdf or not file_hash:
                continue

            file_path = processed_pdf
            existing_hash = path_to_hash.get(file_path)
            if existing_hash is not None and existing_hash != file_hash:
                # This path is already claimed by a different PDF; disambiguate by
                # content hash so this distinct PDF keeps its own row (and its hash
                # stays in the dedup set) instead of overwriting the other one.
                file_path = f"{processed_pdf}#{file_hash[:16]}"

            path_to_hash[file_path] = file_hash
            session.merge(
                ProcessedFileDB(
                    file_path=file_path,
                    file_hash=file_hash,
                    processed_at=now,
                    transaction_count=int(item.get("transaction_count", 0)),
                )
            )


def result_counts(result: ProcessingResult) -> dict[str, int]:
    return {
        "rows_scanned": result.total_scanned,
        "transactions": len(result.transactions),
        "transfers": len(result.transfers),
        "duplicates_removed": result.duplicates_removed,
        "exceptions": len(result.exceptions),
    }
