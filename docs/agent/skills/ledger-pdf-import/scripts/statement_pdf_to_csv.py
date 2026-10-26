#!/usr/bin/env python3
"""Entry point for the generic package-backed statement PDF extractor."""

from ledger.imports.pdf.statement_pdf_to_csv import main

if __name__ == "__main__":
    raise SystemExit(main())
