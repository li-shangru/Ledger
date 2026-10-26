"""Turn an explicitly bound CSV profile into core-owned staging artifacts."""

import hashlib
import json
import re
from pathlib import Path

from ledger.adapters.contracts import AdapterError
from ledger.adapters.csv_profile import load_profile, parse_csv_bytes
from ledger.imports.pdf.statement_pdf_to_csv._common import ExtractedCsv, write_csv
from ledger.parsers.canonical import CANONICAL_HEADERS, CanonicalParser
from ledger.utils.hashing import stable_id


def extract_mapped_export(
    source: Path,
    profile_path: Path,
    csv_root: Path,
    processed_root: Path,
    *,
    institution: str,
    account: str,
    account_type: str,
) -> ExtractedCsv:
    # Account binding is a separate explicit choice, never inferred from the profile.
    if (
        not all(
            isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", value)
            for value in (account, account_type)
        )
        or not isinstance(institution, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", institution)
    ):
        raise AdapterError("import_binding_invalid")
    profile = load_profile(profile_path)
    source_bytes = source.read_bytes()
    rows = parse_csv_bytes(source_bytes, profile)
    if not rows:
        raise AdapterError("adapter_unknown_format")
    canonical_rows = [
        {
            "Ledger Format": CanonicalParser.FORMAT_NAME,
            "Date": row.transaction_date.isoformat(),
            "Description": row.description,
            "Amount": format(row.amount, ".2f"),
            "Currency": row.currency,
            "Reference": row.reference or "",
            "Institution": institution,
            "Account Type": account_type,
        }
        for row in rows
    ]
    # An unreconciled export cannot establish absence of earlier transactions.
    # Give each bound set of facts its own replacement scope; overlapping facts
    # retain their financial IDs and are deduplicated by the normal row pipeline.
    batch = hashlib.sha256(
        json.dumps(
            [institution, account, account_type, canonical_rows],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    target = csv_root / "exports" / batch / f"{account}.csv"
    write_csv(
        target,
        canonical_rows,
        list(CANONICAL_HEADERS),
        dry_run=False,
    )
    archive = processed_root / f"mapped-{stable_id(source_bytes.hex())[:24]}.csv"
    return ExtractedCsv(
        pdf=source,
        csv_path=target,
        processed_pdf=archive,
        statement_type=CanonicalParser.FORMAT_NAME,
        transaction_count=len(rows),
        account_slug=account,
        institution=institution,
        # An export's observed transaction dates are not a complete statement period.
        period="",
        coverage_basis="",
    )
