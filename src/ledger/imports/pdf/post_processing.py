"""Post-processing orchestration for incremental PDF imports."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ledger.analysis import AnalysisRunner
from ledger.analysis.transfer_matcher import TransferMatcher
from ledger.core.account_identity import institution_from_statement_type
from ledger.core.models import Transaction
from ledger.storage.database import (
    StatementCoverageDB,
    StatementCoverageIntervalDB,
    TransferDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import upsert_statement_credit_limit, utc_now_dt
from ledger.utils.dates import year_month
from ledger.utils.text import clean_account_name


def _statement_date_from_csv_path(csv_path: Path) -> date | None:
    """Derive a statement date from a CSV path laid out as ``.../YYYY-MM/<slug>.csv``.

    Returns the first day of that month, or None if the parent folder is not a
    YYYY-MM month folder. Ordering by this date across months is what
    get_latest_statement_credit_limit relies on.
    """
    parent = csv_path.parent.name
    if not re.fullmatch(r"\d{4}-\d{2}", parent):
        return None
    year, month = (int(part) for part in parent.split("-"))
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def _manifest_entries(manifest_path: Path) -> list[dict]:
    """Return the extraction manifest's dict entries, or [] for anything unusable.

    A missing, unreadable, corrupt or non-list manifest degrades to "nothing to
    persist" rather than raising. Both persist_* helpers below run inside finalize and
    inside statement repair, where a problem with one derived artifact must never cost
    us the statements that did parse.
    """
    if not manifest_path.exists():
        return []
    try:
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def persist_printed_credit_limits(
    db_path: Path,
    manifest_path: Path,
    account_aliases: dict[str, str] | None = None,
) -> int:
    """Read printed credit limits from the extraction manifest into the DB.

    For each statement that produced a CSV, the manifest's canonical account slug and
    its ``YYYY-MM`` parent folder give the identity and statement month. Legacy
    manifests without ``account_slug`` retain the CSV-stem fallback. Non-empty
    ``validation_summary["printed_credit_limit"]`` values are upserted keyed by
    (slug, statement_date). Missing manifest, empty limits, unparseable amounts,
    skipped statements (no csv_path), and non-month CSV folders are all silently
    ignored. Returns the number of limits stored.

    The slug is normalized with ``clean_account_name(slug, account_aliases)`` — the
    SAME transform ingestion applies to ``TransactionDB.account`` (parsers/base.py) —
    so the persist key matches the key ``AccountDetector`` reads back with. Without
    this, a configured ``account_aliases`` entry (or even a stem with uppercase, since
    the transform lowercases) makes persist and read keys diverge and the printed limit
    is silently dropped to the deepest-balance estimate.
    """
    stored = 0
    for entry in _manifest_entries(manifest_path):
        csv_path_str = entry.get("csv_path") or ""
        if not csv_path_str:
            continue
        summary = entry.get("validation_summary") or {}
        raw_limit = str(summary.get("printed_credit_limit") or "").strip()
        if not raw_limit:
            continue
        try:
            credit_limit = Decimal(raw_limit)
        except InvalidOperation, ValueError:
            continue
        csv_path = Path(csv_path_str)
        statement_date = _statement_date_from_csv_path(csv_path)
        if statement_date is None:
            continue
        manifest_slug = entry.get("account_slug")
        raw_account_slug = (
            manifest_slug
            if isinstance(manifest_slug, str) and manifest_slug.strip()
            else csv_path.stem
        )
        account_slug = clean_account_name(raw_account_slug, account_aliases)
        upsert_statement_credit_limit(db_path, account_slug, credit_limit, statement_date)
        stored += 1
    return stored


@dataclass
class _CoverageAggregate:
    """Running totals for one account-month while the manifest is read."""

    institution: str = ""
    statement_type: str = ""
    row_count: int = 0
    statement_count: int = 0
    skipped_reason: str = ""


def persist_statement_coverage(
    db_path: Path,
    manifest_path: Path,
    account_aliases: dict[str, str] | None = None,
) -> int:
    """Record one ``statement_coverage`` row per account-month in the extraction manifest.

    Unlike persist_printed_credit_limits above, entries with NO csv_path are the whole
    point: finalize drops every item carrying a ``skipped_reason``, so a statement that
    yielded zero transactions leaves nothing behind and "imported, no activity" becomes
    indistinguishable from "never imported". Only the two primary-key parts can't be
    guessed, so an entry is skipped only when it has no usable account_slug or no
    period (an unsupported or investment-delegated statement has neither).

    Entries are aggregated per (account_slug, period): ``row_count`` sums and
    ``statement_count`` counts, because a month can legitimately be covered by more
    than one PDF. A ``skipped_reason`` is kept ONLY when no statement for that
    account-month produced rows — otherwise a skipped duplicate would report a
    genuinely imported month as skipped.

    Rows are merged rather than inserted, so re-importing (or repairing) a statement
    updates the account-month in place. Returns the number of account-month rows
    written.

    The slug is normalized with ``clean_account_name(slug, account_aliases)`` — the
    SAME transform ingestion applies to ``TransactionDB.account`` — so the coverage key
    names the account the transactions actually landed under. Without it, a configured
    alias makes coverage describe an account slug that holds no rows.
    """
    coverage: dict[tuple[str, str], _CoverageAggregate] = {}
    entries = _manifest_entries(manifest_path)
    for entry in entries:
        raw_slug = entry.get("account_slug")
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            continue
        raw_period = entry.get("period")
        if not isinstance(raw_period, str) or not raw_period.strip():
            continue
        account_slug = clean_account_name(raw_slug, account_aliases)
        if not account_slug:
            continue

        statement_type = str(entry.get("statement_type") or "")
        try:
            institution = str(
                entry.get("institution") or institution_from_statement_type(statement_type)
            )
        except ValueError:
            # A statement type the identity map does not know yet (or a synthetic
            # "parser_exception"/"unknown"). The account-month is still worth
            # recording; failing here would abort the whole import over a label.
            institution = ""
        try:
            row_count = int(entry.get("transaction_count") or 0)
        except TypeError, ValueError:
            row_count = 0

        aggregate = coverage.setdefault((account_slug, raw_period.strip()), _CoverageAggregate())
        aggregate.row_count += row_count
        aggregate.statement_count += 1
        # First non-empty wins for the descriptive fields: sibling statements for one
        # account-month come from the same issuer, and a later blank must not erase it.
        aggregate.institution = aggregate.institution or institution
        aggregate.statement_type = aggregate.statement_type or statement_type
        aggregate.skipped_reason = aggregate.skipped_reason or str(
            entry.get("skipped_reason") or ""
        )

    if not coverage:
        return 0

    init_database(db_path)
    imported_at = utc_now_dt()
    with session_scope(db_path) as session:
        for (account_slug, period), aggregate in coverage.items():
            session.merge(
                StatementCoverageDB(
                    account_slug=account_slug,
                    period=period,
                    institution=aggregate.institution,
                    statement_type=aggregate.statement_type,
                    row_count=aggregate.row_count,
                    statement_count=aggregate.statement_count,
                    skipped_reason="" if aggregate.row_count else aggregate.skipped_reason,
                    imported_at=imported_at,
                )
            )
            year, month = (int(value) for value in period.split("-"))
            if not 1 <= month <= 12 or not 1 <= year <= 9999:
                continue
            # Replace this source account-month's precise evidence as well. An
            # unsupported/reparsed narrower period must not leave old bounds active.
            session.query(StatementCoverageIntervalDB).filter(
                StatementCoverageIntervalDB.account_slug == account_slug,
                StatementCoverageIntervalDB.institution == aggregate.institution,
                StatementCoverageIntervalDB.end_date >= date(year, month, 1),
                StatementCoverageIntervalDB.end_date
                <= date(year, month, calendar.monthrange(year, month)[1]),
            ).delete(synchronize_session=False)
        for entry in entries:
            if entry.get("coverage_basis") != "printed_statement_period" or entry.get(
                "validation_errors"
            ):
                continue
            if entry.get("skipped_reason") not in (None, "", "no_transactions_extracted"):
                continue
            try:
                start = date.fromisoformat(entry["coverage_start_date"])
                end = date.fromisoformat(entry["coverage_end_date"])
                institution = str(
                    entry.get("institution")
                    or institution_from_statement_type(entry["statement_type"])
                )
                slug = clean_account_name(entry["account_slug"], account_aliases)
            except KeyError, TypeError, ValueError:
                continue
            if (
                not slug
                or not 0 <= (end - start).days <= 62
                or end.strftime("%Y-%m") != entry.get("period")
            ):
                continue
            identifier = hashlib.sha256(
                json.dumps([institution, slug, start.isoformat(), end.isoformat()]).encode()
            ).hexdigest()
            session.merge(
                StatementCoverageIntervalDB(
                    interval_id=identifier,
                    institution=institution,
                    account_slug=slug,
                    start_date=start,
                    end_date=end,
                    evidence_basis="printed_statement_period",
                    imported_at=imported_at,
                )
            )
    return len(coverage)


def minimal_transfer(row: TransferDB) -> Transaction:
    posting_date = row.posting_date
    return Transaction(
        txn_id=str(row.txn_id),
        txn_fingerprint="",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution=str(row.institution),
        account=str(row.account),
        account_type=str(row.account_type),
        flow=str(row.flow),
        cashflow_type="transfer",
        include_in_cashflow=False,
        # Preserve the row's native currency so the (currency-aware) matcher never
        # pairs a USD leg with a CAD leg on this rebuild path. Fall back to CAD only
        # when the column is absent/empty (legacy rows predate the currency column).
        currency=getattr(row, "currency", None) or "CAD",
        amount_raw=row.amount_raw,
        amount_signed=Decimal("0"),
        description=str(row.description),
        merchant="",
        category="Transfers (Excluded)",
        merchant_confidence="none",
        category_confidence="low",
        exclude_from_spending=True,
        merchant_raw="",
        category_raw="",
        source_file=str(row.source_file),
        source_path="",
        source_period=year_month(posting_date),
        source_row=0,
        imported_at=row.imported_at,
        parser_version="",
        day_of_week=posting_date.weekday(),
        day_of_month=posting_date.day,
        week_of_year=posting_date.isocalendar()[1],
        is_weekend=posting_date.weekday() >= 5,
        is_month_start=posting_date.day <= 3,
        is_month_end=posting_date.day >= 28,
    )


def refresh_transfer_matches(db_path: Path, config) -> dict[str, int]:
    with session_scope(db_path) as session:
        rows = session.query(TransferDB).order_by(TransferDB.posting_date).all()
        for row in rows:
            row.paired_transfer_id = None
            row.transfer_confidence = None
            row.counterparty_account = None
            row.counterparty_institution = None

        transfers = [minimal_transfer(row) for row in rows]
        matcher = TransferMatcher(
            date_window_days=config.ledger.general.transfer_match_window,
            amount_tolerance_pct=Decimal(str(config.ledger.general.transfer_amount_tolerance)),
            min_confidence=config.ledger.general.transfer_min_confidence,
        )
        matches = matcher.match_transfers(transfers)
        updated = {txn.txn_id: txn for txn in matcher.apply_matches(transfers, matches)}

        for row in rows:
            txn = updated[str(row.txn_id)]
            row.paired_transfer_id = txn.paired_transfer_id
            row.transfer_confidence = txn.transfer_confidence  # type: ignore[assignment]
            row.counterparty_account = txn.counterparty_account
            row.counterparty_institution = txn.counterparty_institution

    return {"transfers": len(transfers), "matched_pairs": len(matches)}


def run_post_processing(db_path: Path, config) -> dict:
    # Transfer matching needs the parsed config, so it stays here; everything after it
    # is the shared derived-analytics refresh (AnalysisRunner.run_all), which owns the
    # ordering — accounts first, adjustments before the snapshot, insights after it.
    transfer_matching = refresh_transfer_matches(db_path, config)
    results = AnalysisRunner(db_path).run_all(source="pdf_finalize", days=30, months=12)
    return {
        "transfer_matching": transfer_matching,
        "accounts": results.accounts,
        "patterns": results.patterns,
        "trends": results.trends,
        "health": results.health,
        "adjustments": results.adjustments,
        "report_snapshot": results.snapshot,
        "insights": results.insights.counts,
        "quality": {
            "checks": len(results.quality),
            "issues": sum(issue.issue_count for issue in results.quality),
        },
    }
