"""Generic statement extraction through explicitly enabled local adapters."""

import shutil as shutil
import subprocess as subprocess

from ledger.imports.pdf.statement_pdf_to_csv._common import (
    ExtractedCsv,
    StatementValidation,
    canonical_account_slug,
    decode_name,
    format_amount,
    move_pdf,
    output_path,
    parse_money,
    processed_path,
    row_reference,
    run_pdftotext,
    run_pdftotext_bytes,
    unique_path,
    write_csv,
)
from ledger.imports.pdf.statement_pdf_to_csv._dispatch import (
    detect_statement_type,
    main,
    process_pdf,
    write_manifest,
)

__all__ = [
    "ExtractedCsv",
    "StatementValidation",
    "canonical_account_slug",
    "decode_name",
    "format_amount",
    "move_pdf",
    "output_path",
    "parse_money",
    "processed_path",
    "row_reference",
    "run_pdftotext",
    "run_pdftotext_bytes",
    "unique_path",
    "write_csv",
    "detect_statement_type",
    "main",
    "process_pdf",
    "write_manifest",
]
