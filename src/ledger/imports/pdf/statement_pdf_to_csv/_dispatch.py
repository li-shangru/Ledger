"""Core-owned extraction, validation, private CSV output and archival."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ledger.adapters.contracts import AdapterError, PdfFacts
from ledger.adapters.registry import (
    get_pdf_format,
    load_adapters,
    snapshot_bindings,
    verify_bindings,
)
from ledger.imports.pdf.statement_pdf_to_csv._common import (
    ExtractedCsv,
    move_pdf,
    output_path,
    processed_path,
    unique_path,
    write_csv,
)
from ledger.runtime import require_external_runtime_path, runtime_path
from ledger.utils.dates import year_month
from ledger.utils.json_io import write_json_atomic


def _public():
    """Return the public package module object.

    ``process_pdf``/``main`` dispatch to their patchable siblings
    (``run_pdftotext``, ``process_pdf``, ``write_manifest``) through this so that a
    ``monkeypatch.setattr(statement_pdf_to_csv, "<name>", ...)`` on the public
    package is honored — mirroring the original single-module namespace where these
    were plain global lookups. Resolved lazily via ``sys.modules`` to avoid a
    circular import at module load time.
    """
    return sys.modules["ledger.imports.pdf.statement_pdf_to_csv"]


def _summary_shows_movement(summary: dict[str, str]) -> bool:
    """True if the parsed statement summary indicates the period had transactions.

    Covers each canonical movement signal:
      * deposits: total_deposits / total_withdrawals
      * cards: purchases / payments / fees / interest / cash_advances
      * balances: a changed opening->closing balance (parsed_net is row-derived,
                     so it is 0 at zero rows and cannot signal movement here)
    Used only as a fallback: each type's validator already emits a blocking error at
    zero rows when its summary reconciles against a non-empty movement; a genuine
    double parse-failure (neither table NOR summary parsed) is not catchable here.
    """

    def _nonzero(key: str) -> bool:
        return summary.get(key) not in ("", "0", "0.0", "0.00", None)

    if any(
        _nonzero(key)
        for key in (
            "total_deposits",
            "total_withdrawals",
            "purchases",
            "payments",
            "fees",
            "interest",
            "cash_advances",
        )
    ):
        return True
    opening, closing = summary.get("opening_balance"), summary.get("closing_balance")
    return bool(opening not in ("", None) and closing not in ("", None) and opening != closing)


def write_manifest(path: Path, results: list[ExtractedCsv], dry_run: bool) -> None:
    if dry_run:
        return
    payload = [
        {
            "pdf": str(result.pdf),
            "csv_path": str(result.csv_path) if result.csv_path else "",
            "processed_pdf": str(result.processed_pdf) if result.processed_pdf else "",
            "statement_type": result.statement_type,
            "institution": result.institution,
            "balance_spec": result.balance_spec,
            "transaction_count": result.transaction_count,
            "skipped_reason": result.skipped_reason,
            "validation_errors": list(result.validation_errors),
            "validation_warnings": list(result.validation_warnings),
            "validation_summary": result.validation_summary or {},
            "account_slug": result.account_slug,
            "period": result.period,
            "coverage_start_date": result.coverage_start_date,
            "coverage_end_date": result.coverage_end_date,
            "coverage_basis": result.coverage_basis,
        }
        for result in results
    ]
    write_json_atomic(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True, help="Directory containing PDFs")
    parser.add_argument("--csv-root", type=Path, required=True, help="Ledger CSV source root")
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Manifest JSON path to write after extraction",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=runtime_path("processed"),
        help="Directory to move successfully extracted PDFs into",
    )
    parser.add_argument(
        "--keep-pdfs",
        action="store_true",
        help="Write CSVs and manifest without moving successfully extracted PDFs",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write files")
    args = parser.parse_args()

    # Validate all paths before extraction can write CSVs or move inbox PDFs.
    pdf_dir = require_external_runtime_path(args.pdf_dir).resolve()
    csv_root = require_external_runtime_path(args.csv_root).resolve()
    processed_dir = require_external_runtime_path(args.processed_dir).resolve()
    manifest = require_external_runtime_path(args.manifest).resolve()
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs found in {args.pdf_dir}")

    _pkg = _public()
    results: list[ExtractedCsv] = []
    for pdf in pdfs:
        try:
            results.append(
                _pkg.process_pdf(
                    pdf,
                    csv_root,
                    processed_dir,
                    args.dry_run,
                    archive_pdf=not args.keep_pdfs,
                )
            )
        except Exception:  # one corrupt PDF must not abort the batch
            # Mirror workflow.stage(): record the failure as a skipped result so the
            # manifest still captures the PDFs that did parse, instead of crashing the
            # whole batch (run_pdftotext raises on a corrupt/locked PDF or a timeout).
            results.append(ExtractedCsv(pdf, None, None, "unknown", 0, "extraction_error"))
    _pkg.write_manifest(manifest, results, args.dry_run)

    extracted = sum(result.transaction_count for result in results)
    skipped = [result for result in results if result.skipped_reason]
    print(f"Processed {len(results)} PDFs; extracted {extracted} transactions.")
    archived = [result for result in results if result.processed_pdf]
    if archived:
        if args.dry_run:
            print("Would archive:")
        elif args.keep_pdfs:
            print("Planned archive:")
        else:
            print("Archived:")
        for result in archived:
            print(f"  {result.pdf.name} -> {result.processed_pdf}")
    if skipped:
        print("Skipped:")
        for result in skipped:
            print(f"  {result.pdf.name}: {result.skipped_reason}")
    return 1 if skipped else 0


def detect_statement_type(text: str) -> str:
    matches: list[str] = []
    try:
        for loaded in load_adapters():
            matches.extend(
                fmt.format_id for fmt in loaded.definition.pdf_formats if fmt.recognizes(text)
            )
            matches.extend(
                "managed_by_investment_importer"
                for fmt in loaded.definition.investment_formats
                if fmt.recognizes_text(text)
            )
    except AdapterError:
        raise
    except Exception:
        raise AdapterError() from None
    if len(matches) > 1:
        raise AdapterError("adapter_ambiguous")
    return matches[0] if matches else "unsupported"


def process_pdf(
    pdf_path: Path, csv_root: Path, processed_dir: Path, dry_run: bool, archive_pdf: bool = True
) -> ExtractedCsv:
    binding = snapshot_bindings()
    # Adapters see only immutable extracted text and a basename, not an IO handle.
    content = pdf_path.read_bytes()
    text = _public().run_pdftotext_bytes(content)
    statement_type = detect_statement_type(text)
    if statement_type == "managed_by_investment_importer":
        return ExtractedCsv(
            pdf_path, None, None, statement_type, 0, "managed_by_investment_importer"
        )
    if statement_type == "unsupported":
        return ExtractedCsv(pdf_path, None, None, statement_type, 0, "unsupported_statement")
    fmt = get_pdf_format(statement_type)
    try:
        facts = fmt.parse(text, pdf_path.name)
        if not isinstance(facts, PdfFacts) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", facts.account_slug
        ):
            raise AdapterError("adapter_invalid_contract")
        if (
            not isinstance(facts.period, date)
            or type(facts.complete) is not bool
            or not isinstance(facts.rows, tuple)
            or not isinstance(facts.account_type, str)
            or not isinstance(facts.account_number, str)
            or not isinstance(facts.summary, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in facts.summary.items()
            )
            or not isinstance(facts.errors, tuple)
            or not isinstance(facts.warnings, tuple)
            or any(not isinstance(value, str) for value in (*facts.errors, *facts.warnings))
            or (
                facts.printed_interval is not None
                and (
                    not isinstance(facts.printed_interval, tuple)
                    or len(facts.printed_interval) != 2
                    or any(not isinstance(value, date) for value in facts.printed_interval)
                )
            )
        ):
            raise AdapterError("adapter_invalid_contract")
        if any(
            set(row) != set(fmt.headers)
            or any(not isinstance(value, str) for value in row.values())
            for row in facts.rows
        ):
            raise AdapterError("adapter_invalid_contract")
        if facts.account_slug != fmt.account_slug(facts.account_type, facts.account_number):
            raise AdapterError("adapter_invalid_contract")
        summary = dict(facts.summary)
        raw_limit = summary.get("printed_credit_limit", "").strip()
        try:
            summary_limit = Decimal(raw_limit) if raw_limit else None
        except InvalidOperation:
            raise AdapterError("adapter_invalid_contract") from None
        for limit in (facts.credit_limit, summary_limit):
            if limit is not None and (
                not isinstance(limit, Decimal) or not limit.is_finite() or limit < 0
            ):
                raise AdapterError("adapter_invalid_contract")
        if facts.credit_limit is not None:
            if summary_limit is not None and summary_limit != facts.credit_limit:
                raise AdapterError("adapter_invalid_contract")
            summary["printed_credit_limit"] = str(facts.credit_limit)
    except AdapterError:
        raise
    except Exception:
        raise AdapterError() from None
    verify_bindings(binding)
    if pdf_path.read_bytes() != content:
        raise AdapterError("import_binding_changed")
    errors = tuple("statement_validation_failed" for _ in facts.errors)
    warnings = tuple("statement_review_required" for _ in facts.warnings)
    if not facts.rows and _summary_shows_movement(summary) and not errors:
        errors = ("statement_rows_missing",)
    bounds = facts.printed_interval
    complete = bool(
        facts.complete
        and bounds
        and bounds[1] == facts.period
        and not errors
        and 0 <= (bounds[1] - bounds[0]).days <= 62
    )
    extra: dict[str, Any] = {
        "validation_errors": errors,
        "validation_warnings": warnings,
        "validation_summary": summary,
        "account_slug": facts.account_slug,
        "period": year_month(facts.period),
        "institution": fmt.institution,
        "balance_spec": asdict(fmt.balance_spec) if fmt.balance_spec else None,
        "coverage_start_date": bounds[0].isoformat() if complete and bounds else "",
        "coverage_end_date": bounds[1].isoformat() if complete and bounds else "",
        "coverage_basis": "printed_statement_period" if complete else "",
    }
    if not facts.rows:
        return ExtractedCsv(
            pdf_path, None, None, statement_type, 0, "no_transactions_extracted", **extra
        )
    csv_path = output_path(
        csv_root, pdf_path, facts.period, statement_type, facts.account_type, facts.account_number
    )
    if not dry_run:
        csv_path = unique_path(csv_path)
    write_csv(csv_path, list(facts.rows), list(fmt.headers), dry_run)
    archive_target = processed_path(
        processed_dir, statement_type, facts.account_type, facts.account_number, facts.period
    )
    archived = move_pdf(pdf_path, archive_target, dry_run) if archive_pdf else archive_target
    return ExtractedCsv(pdf_path, csv_path, archived, statement_type, len(facts.rows), **extra)
