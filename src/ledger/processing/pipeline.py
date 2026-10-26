"""
Main processing pipeline.

Orchestrates parsing, deduplication, validation, and output.
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ledger.config import AppConfig
from ledger.core import (
    CollisionError,
    ExceptionRecord,
    Summary,
    Transaction,
)
from ledger.core.types import ConfigVersions, FilterDict
from ledger.finance_policy import is_income_row, is_spend_row
from ledger.parsers import parse_file
from ledger.processing.identity import (
    IdentityEvidence,
    require_unambiguous_cross_source_identity,
    transaction_identity,
    transfer_identity,
)
from ledger.utils import is_month_folder, naive_utc_now, year_quarter
from ledger.utils.hashing import fingerprint, stable_id

logger = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    """Result of processing all files."""

    transactions: list[Transaction] = field(default_factory=list)
    transfers: list[Transaction] = field(default_factory=list)
    exceptions: list[ExceptionRecord] = field(default_factory=list)

    total_scanned: int = 0
    duplicates_removed: int = 0
    collisions: int = 0
    # Internal source occurrences, bound to the final rows after deduplication.
    # They prove multiplicity without changing first-arrival stored provenance.
    identity_evidence: tuple[IdentityEvidence, ...] = field(default=(), repr=False)


def discover_files(
    source_root: Path,
    year: str | None = None,
    month: str | None = None,
    quarter: str | None = None,
) -> Iterator[Path]:
    """
    Discover CSV files to process.

    Filters by period if specified.
    """
    for path in sorted(source_root.rglob("*.csv")):
        # Skip hidden and temp files
        if path.name.startswith(".") or path.name.startswith("~$"):
            continue

        # Check period filter
        if year or month or quarter:
            # Try to detect period from folder structure
            period = ""
            for parent in (path.parent, path.parent.parent):
                if is_month_folder(parent.name):
                    period = parent.name
                    break

            if month and period:
                if period != month:
                    continue
            elif quarter and period:
                # Check quarter matches
                period_q = year_quarter(datetime.strptime(f"{period}-01", "%Y-%m-%d").date())
                if period_q != quarter:
                    continue
            elif year:
                if period:
                    if not period.startswith(f"{year}-"):
                        continue
                elif year not in str(path):
                    continue

        yield path


def validate_source_files(source_root: Path, source_files: Sequence[Path]) -> list[Path]:
    """Resolve and validate an explicit list of CSV inputs.

    Validation is completed for the whole list before any file is parsed. Resolved
    paths must be regular CSV files contained by the resolved source root; resolving
    first also rejects symlinks that escape the root.
    """
    resolved_root = source_root.expanduser().resolve()
    validated: list[Path] = []
    for source_file in source_files:
        resolved_file = Path(source_file).expanduser().resolve()
        try:
            resolved_file.relative_to(resolved_root)
        except ValueError:
            raise ValueError("Explicit source CSV must be contained by source_root") from None
        if resolved_file.suffix.lower() != ".csv":
            raise ValueError("Explicit source file must be a CSV")
        if not resolved_file.is_file():
            raise FileNotFoundError("Explicit source CSV does not exist")
        validated.append(resolved_file)
    return validated


def disambiguate_within_file(items: list[Transaction]) -> list[Transaction]:
    """Give genuinely-distinct identical-content rows within ONE source file distinct ids.

    The FIRST occurrence of a txn_id keeps its id (no migration for existing single rows).
    The Nth (N>=2) identical occurrence is re-hashed with its occurrence index,
    deterministically — re-importing the same file reproduces the same ids, and
    cross-file duplicates (overlapping statements) still collapse in deduplicate().
    """
    counts: dict[str, int] = {}
    out: list[Transaction] = []
    for txn in items:
        n = counts.get(txn.txn_id, 0)
        counts[txn.txn_id] = n + 1
        if n == 0:
            out.append(txn)
            continue
        new_id = stable_id(txn.txn_id, str(n))
        # Re-hash the fingerprint too: equal fingerprints would still collapse
        # as duplicates in deduplicate(), defeating the whole point of disambiguation.
        new_fp = fingerprint(txn.txn_fingerprint, str(n))
        out.append(txn.model_copy(update={"txn_id": new_id, "txn_fingerprint": new_fp}))
    return out


def deduplicate(
    items: list[Transaction],
    strict: bool = True,
    *,
    identity_evidence: list[IdentityEvidence] | None = None,
    defer_identity_check_to_writer: bool = False,
) -> tuple[list[Transaction], int, int]:
    """
    Remove duplicate transactions.

    TRUE DUPLICATE (same txn_id AND same fingerprint): collapsed to one row.
    COLLISION (same txn_id, DIFFERENT fingerprint): two genuinely distinct
    transactions that happen to hash to the same txn_id.

    - strict=True (default): collision raises CollisionError immediately.
    - strict=False: collision is disambiguated — both the first-seen row AND
      each subsequently-colliding row are re-keyed using their CONTENT
      (fingerprint) rather than a positional occurrence index.  The new id
      is ``stable_id(original_id, txn_fingerprint)``, making it a pure
      function of (id, fingerprint).

      This gives two important properties:
        1. Two rows with the same (id, fingerprint) hash to the SAME new id
           → a true duplicate of a colliding row still collapses (fixes [3]).
        2. The new id does NOT depend on processing order → re-importing with
           a different file selection produces the same ids (fixes [4]).

      NOTE: in practice, parsers derive txn_id from a superset of the fields
      that make up txn_fingerprint, so a genuine SHA-256 collision on txn_id
      between two structurally-different transactions is essentially
      unreachable.  This is a robustness/correctness refinement, not a live
      bug.

    The optional internal evidence sink retains source occurrences bound to the
    final IDs and financial fields. It does not change the three-result return.
    Pure processing cannot know which IDs are already stored. A database-bound
    caller may defer the identity check to its writer, which must validate using
    the actual database session before writing. Standalone callers stay strict.

    Returns: (unique_items, duplicates_removed, collisions)
    """
    # Within-file occurrences were disambiguated before this boundary. Never
    # guess whether a novel ID on an overlapping source is another occurrence or
    # the same fact under a changed source identity, including in non-strict mode.
    transactions, transfers = partition(items)
    if not defer_identity_check_to_writer:
        require_unambiguous_cross_source_identity(transaction_identity(row) for row in transactions)
        require_unambiguous_cross_source_identity(transfer_identity(row) for row in transfers)

    seen: dict[str, Transaction] = {}
    # Maps original txn_id → set of ORIGINAL fingerprints already encountered.
    # Keyed by original (pre-disambiguation) id and fingerprint so that:
    #   - a true duplicate of a disambiguated row is still recognised and
    #     collapsed (same original (id, fp) pair → same content-keyed id).
    #   - the disambiguated id is stable across processing orders.
    # Only populated once the first collision is detected for a given id; before
    # that, the row lives under its bare id in `seen`.
    collision_fps: dict[str, set[str]] = {}
    final_ids: dict[tuple[str, str], str] = {}
    duplicates = 0
    collisions = 0

    for txn in items:
        original_id = txn.txn_id
        original_fp = txn.txn_fingerprint
        final_ids.setdefault((original_id, original_fp), original_id)

        if original_id not in collision_fps:
            # No collision seen yet for this id.
            if original_id not in seen:
                # First occurrence — store bare.
                seen[original_id] = txn
            else:
                # Same id already in seen.  If fingerprints match it is a true
                # duplicate; drop.  If they differ it is a collision (handled
                # in the next block on the SECOND pass — but we need to check
                # now for the strict-raise case).
                existing = seen[original_id]
                if existing.txn_fingerprint == original_fp:
                    # True duplicate (same id AND fingerprint) → this row is
                    # dropped.  Count it and move on.
                    duplicates += 1
                else:
                    # Collision (same id, different fingerprint): both rows are
                    # kept (remapped to content-keyed ids), so NOTHING is
                    # dropped — do NOT count it as a duplicate.
                    collisions += 1
                    msg = (
                        f"Hash collision: {original_id} "
                        f"({existing.source_path}:{existing.source_row} vs "
                        f"{txn.source_path}:{txn.source_row})"
                    )
                    if strict:
                        raise CollisionError(original_id, existing.txn_fingerprint, original_fp)
                    logger.warning(msg)
                    # Retroactively remap the first-seen row to content-keyed id.
                    first_txn = seen.pop(original_id)
                    first_fp = first_txn.txn_fingerprint
                    first_new_id = stable_id(original_id, first_fp)
                    first_new_fp = fingerprint(first_fp, original_id)
                    # The existing row may itself be the result of an earlier
                    # collision. Move every original occurrence that resolves
                    # to it, so witnesses follow the actual final stored row.
                    for origin, current_id in final_ids.items():
                        if current_id == original_id:
                            final_ids[origin] = first_new_id
                    # A later input can repeat the derived pair itself, rather
                    # than its original ancestor. Keep that duplicate alias too.
                    final_ids[(original_id, first_fp)] = first_new_id
                    seen[first_new_id] = first_txn.model_copy(
                        update={"txn_id": first_new_id, "txn_fingerprint": first_new_fp}
                    )
                    # Remap incoming colliding row.
                    new_id = stable_id(original_id, original_fp)
                    new_fp = fingerprint(original_fp, original_id)
                    final_ids[(original_id, original_fp)] = new_id
                    seen[new_id] = txn.model_copy(
                        update={"txn_id": new_id, "txn_fingerprint": new_fp}
                    )
                    # Register collision tracking so future rows with this id
                    # are handled by the block below.
                    collision_fps[original_id] = {first_fp, original_fp}
            continue

        # We have seen at least one collision for this original_id already.
        existing_fps = collision_fps[original_id]

        if original_fp in existing_fps:
            # Same (id, fingerprint) pair already processed → true duplicate of
            # a disambiguated row; drop it.
            duplicates += 1
            continue

        # New fingerprint for a colliding id → another distinct transaction.
        collisions += 1
        msg = (
            f"Hash collision: {original_id} "
            f"(new fingerprint {original_fp!r} vs existing {sorted(existing_fps)!r}, "
            f"{txn.source_path}:{txn.source_row})"
        )
        if strict:
            first_fp = next(iter(existing_fps))
            raise CollisionError(original_id, first_fp, original_fp)
        logger.warning(msg)

        # Remap incoming row to content-keyed id (first row was already moved).
        new_id = stable_id(original_id, original_fp)
        new_fp = fingerprint(original_fp, original_id)
        final_ids[(original_id, original_fp)] = new_id
        seen[new_id] = txn.model_copy(update={"txn_id": new_id, "txn_fingerprint": new_fp})
        # Track the ORIGINAL fingerprint so future duplicates of this row are
        # recognised and collapsed.
        existing_fps.add(original_fp)

    unique = list(seen.values())
    check_final_identities = bool(collisions) and not defer_identity_check_to_writer
    if identity_evidence is not None or check_final_identities:
        final_transactions, final_transfers = partition(unique)
        final_evidence = {row.txn_id: transaction_identity(row) for row in final_transactions} | {
            row.txn_id: transfer_identity(row) for row in final_transfers
        }
        retained_evidence = []
        for rows, identity in (
            (transactions, transaction_identity),
            (transfers, transfer_identity),
        ):
            for row in rows:
                source = identity(row)
                target = final_evidence[final_ids[(row.txn_id, row.txn_fingerprint)]]
                source_key, target_key = source.financial_identity, target.financial_identity
                if source_key is None or target_key is None:
                    continue
                if source_key == target_key:
                    retained_evidence.append(
                        IdentityEvidence(
                            target.txn_id,
                            source.source_path,
                            target.financial_identity,
                            txn_fingerprint=target.txn_fingerprint,
                        )
                    )
        if check_final_identities:
            # A same-ID collision may create distinct final IDs for otherwise
            # identical facts. Check those outputs even without an evidence sink.
            require_unambiguous_cross_source_identity(
                (transaction_identity(row) for row in final_transactions),
                witnesses=retained_evidence,
            )
            require_unambiguous_cross_source_identity(
                (transfer_identity(row) for row in final_transfers),
                witnesses=retained_evidence,
            )
        if identity_evidence is not None:
            identity_evidence.extend(retained_evidence)
    return unique, duplicates, collisions


def partition(
    items: list[Transaction],
) -> tuple[list[Transaction], list[Transaction]]:
    """
    Partition transactions into regular and transfers.

    Returns: (transactions, transfers)
    """
    transactions = []
    transfers = []

    for txn in items:
        if txn.cashflow_type == "transfer" or txn.flow in ("payment", "transfer"):
            transfers.append(txn)
        else:
            transactions.append(txn)

    return transactions, transfers


def sort_transactions(items: list[Transaction]) -> list[Transaction]:
    """Sort transactions by date and other fields."""
    return sorted(
        items,
        key=lambda t: (
            t.posting_date or t.transaction_date,
            t.institution,
            t.account,
            t.cashflow_type,
            t.flow,
            t.amount_signed,  # Decimal sorts correctly; no float round-trip needed
            t.description.lower(),
        ),
    )


def validate_transaction(txn: Transaction, strict: bool = True) -> list[str]:
    """
    Validate transaction invariants.

    Returns list of warnings/errors.
    """
    errors = []

    # Amount invariants
    if not txn.include_in_cashflow and txn.amount_signed != Decimal("0"):
        errors.append(f"Non-cashflow transaction has non-zero amount: {txn.amount_signed}")

    if txn.include_in_cashflow:
        if txn.cashflow_type == "expense" and txn.amount_signed >= 0:
            errors.append(f"Expense must be negative: {txn.amount_signed}")
        if txn.cashflow_type == "income" and txn.amount_signed <= 0:
            errors.append(f"Income must be positive: {txn.amount_signed}")

    if txn.cashflow_type == "transfer" and txn.include_in_cashflow:
        errors.append("Transfer must not be included in cashflow")

    return errors


def process(
    config: AppConfig,
    source_root: Path,
    year: str | None = None,
    month: str | None = None,
    quarter: str | None = None,
    include_raw: bool = False,
    source_files: Sequence[Path] | None = None,
    account_slugs: Mapping[str, str] | None = None,
    *,
    defer_identity_check_to_writer: bool = False,
) -> ProcessingResult:
    """
    Process selected files from a source directory.

    When ``source_files`` is omitted, preserve the normal recursive discovery and
    period filters. An explicit list is authoritative and is fully validated before
    parsing, so other CSV files under ``source_root`` are ignored. ``account_slugs``
    optionally maps those files' relative ``source_path`` values to canonical account
    identities; missing entries preserve the legacy filename-stem fallback.

    Pure processing cannot infer persisted identities. Database-bound callers may
    set ``defer_identity_check_to_writer`` only when they validate against their
    explicitly selected database before presenting financial output, and the
    writer rechecks inside its write transaction. Standalone processing checks
    unresolved cross-source identity immediately by default.
    """
    result = ProcessingResult()
    all_transactions: list[Transaction] = []

    # Discover and parse files
    if source_files is None:
        files = list(discover_files(source_root, year, month, quarter))
    else:
        source_root = source_root.expanduser().resolve()
        files = validate_source_files(source_root, source_files)
    logger.info(f"Found {len(files)} files to process")

    for file_path in files:
        logger.debug(f"Parsing {file_path}")

        # Collect this file's transactions separately so identical-content rows
        # within a SINGLE source file can be disambiguated before they reach
        # deduplicate(). Cross-file duplicates (overlapping statements) are NOT
        # disambiguated here, so they still collapse in deduplicate().
        file_transactions: list[Transaction] = []

        # Materialize the file's parse inside a guard so a FILE-level read/decode
        # failure (e.g. a non-UTF-8 CSV) is isolated like the per-ROW failures
        # below — recorded as an exception and skipped — instead of aborting the
        # whole build with a raw traceback. Keep the reason generic: the exception
        # str can embed file bytes / row values (PII).
        try:
            source_path = str(file_path.relative_to(source_root))
            account_slug = account_slugs.get(source_path) if account_slugs is not None else None
            parsed_items = list(
                parse_file(
                    config,
                    source_root,
                    file_path,
                    include_raw,
                    account_slug=account_slug,
                )
            )
        except (UnicodeDecodeError, OSError) as exc:
            result.total_scanned += 1
            result.exceptions.append(
                ExceptionRecord(
                    source_file=file_path.name,
                    source_path=str(file_path),
                    source_row=0,
                    reason=f"file_read_error: {type(exc).__name__}",
                )
            )
            logger.warning("Skipping unreadable file %s (%s)", file_path, type(exc).__name__)
            continue

        for item in parsed_items:
            result.total_scanned += 1

            if isinstance(item, ExceptionRecord):
                result.exceptions.append(item)
            else:
                # Validate
                errors = validate_transaction(item, config.strict)
                if errors:
                    for err in errors:
                        # The violation type alone (e.g. "Expense must be negative") is
                        # safe; the value after the colon is the literal amount (PII).
                        violation_type = err.split(":")[0] if ":" in err else err
                        if config.strict:
                            # Strip the amount from the RAISED message too (the WARNING
                            # path below already does). If this propagates to a CLI
                            # traceback or higher-level log it must not carry the amount;
                            # the full detail stays at DEBUG for local debugging.
                            logger.debug(
                                "%s:%s: validation detail — %s",
                                item.source_path,
                                item.source_row,
                                err,
                            )
                            raise ValueError(
                                f"{item.source_path}:{item.source_row}: {violation_type}"
                            )
                        # Log violation type + source coordinates at WARNING but omit the
                        # literal amount value (financial PII).  The full error detail
                        # (including the amount) is available at DEBUG for local debugging.
                        logger.warning(
                            "%s:%s: validation violation — %s (cashflow_type=%s)",
                            item.source_path,
                            item.source_row,
                            violation_type,
                            item.cashflow_type,
                        )
                        logger.debug(
                            "%s:%s: validation detail — %s",
                            item.source_path,
                            item.source_row,
                            err,
                        )

                file_transactions.append(item)

        all_transactions.extend(disambiguate_within_file(file_transactions))

    # Deduplicate
    identity_evidence: list[IdentityEvidence] = []
    unique, dups, cols = deduplicate(
        all_transactions,
        config.strict,
        identity_evidence=identity_evidence,
        defer_identity_check_to_writer=defer_identity_check_to_writer,
    )
    result.identity_evidence = tuple(identity_evidence)
    result.duplicates_removed = dups
    result.collisions = cols

    logger.info(
        f"Processed {result.total_scanned} rows, "
        f"{len(unique)} unique, {dups} duplicates, {cols} collisions"
    )

    # Partition and sort
    transactions, transfers = partition(unique)

    # Match transfers across accounts (if enabled)
    if config.ledger.general.enable_transfer_matching and transfers:
        from ledger.analysis.transfer_matcher import TransferMatcher

        logger.info(f"Matching {len(transfers)} transfers...")

        matcher = TransferMatcher(
            date_window_days=config.ledger.general.transfer_match_window,
            amount_tolerance_pct=Decimal(str(config.ledger.general.transfer_amount_tolerance)),
            min_confidence=config.ledger.general.transfer_min_confidence,
        )

        matches = matcher.match_transfers(transfers)
        logger.info(f"Found {len(matches)} transfer pairs")

        # Apply matches to transfers
        transfers = matcher.apply_matches(transfers, matches)

    result.transactions = sort_transactions(transactions)
    result.transfers = sort_transactions(transfers)

    logger.info(
        f"Result: {len(result.transactions)} transactions, "
        f"{len(result.transfers)} transfers, "
        f"{len(result.exceptions)} exceptions"
    )

    return result


def compute_summary(
    result: ProcessingResult,
    config: AppConfig,
    source_root: Path,
    filters: FilterDict,
    config_versions: ConfigVersions,
    parser_version: str,
) -> Summary:
    """Compute summary statistics from processing result."""
    by_institution: dict[str, int] = {}
    by_flow: dict[str, int] = {}
    by_category: dict[str, Decimal] = {}
    by_quarter: dict[str, dict[str, Decimal]] = {}

    income = Decimal("0")
    expense = Decimal("0")

    for txn in result.transactions:
        is_income = is_income_row(
            amount_signed=txn.amount_signed,
            cashflow_type=txn.cashflow_type,
            include_in_cashflow=txn.include_in_cashflow,
            currency=txn.currency,
        )
        is_spend = is_spend_row(
            amount_signed=txn.amount_signed,
            cashflow_type=txn.cashflow_type,
            include_in_cashflow=txn.include_in_cashflow,
            exclude_from_spending=txn.exclude_from_spending,
            currency=txn.currency,
        )

        # By institution
        by_institution[txn.institution] = by_institution.get(txn.institution, 0) + 1

        # By flow
        by_flow[txn.flow] = by_flow.get(txn.flow, 0) + 1

        # By category (for expenses only)
        if is_spend:
            cat = txn.category
            by_category[cat] = by_category.get(cat, Decimal("0")) + abs(txn.amount_signed)

        # Cashflow totals
        if is_income or is_spend:
            if is_income:
                income += txn.amount_signed
            else:
                expense += txn.amount_signed

            # By quarter
            if txn.posting_date:
                q = year_quarter(txn.posting_date)
                if q not in by_quarter:
                    by_quarter[q] = {
                        "income": Decimal("0"),
                        "expense": Decimal("0"),
                        "net": Decimal("0"),
                    }

                if is_income:
                    by_quarter[q]["income"] += txn.amount_signed
                else:
                    by_quarter[q]["expense"] += txn.amount_signed
                by_quarter[q]["net"] = by_quarter[q]["income"] + by_quarter[q]["expense"]

    return Summary(
        generated_at=naive_utc_now(),
        parser_version=parser_version,
        config_versions=config_versions,
        source_path=str(source_root),
        filters=filters,
        total_scanned=result.total_scanned,
        total_processed=len(result.transactions) + len(result.transfers),
        duplicates_removed=result.duplicates_removed,
        collisions=result.collisions,
        exceptions=len(result.exceptions),
        transactions_count=len(result.transactions),
        transfers_count=len(result.transfers),
        by_institution=dict(sorted(by_institution.items())),
        by_flow=dict(sorted(by_flow.items())),
        by_category=dict(sorted(by_category.items())),
        cashflow_income=income,
        cashflow_expense=expense,
        cashflow_net=income + expense,
        by_quarter=dict(sorted(by_quarter.items())),
    )
