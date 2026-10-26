"""Generic private artifact IO and core extraction evidence types."""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

from ledger.utils.dates import year_month
from ledger.utils.hashing import stable_id
from ledger.utils.private_files import (
    copy_private_file,
    tighten_private_file,
    write_private_text_atomic,
)

HEADERS_BY_STATEMENT_TYPE: dict[str, list[str]] = {}
LARGE_TRANSACTION_THRESHOLD = Decimal("1000")


@dataclass(frozen=True)
class StatementValidation:
    blocking_errors: list[str]
    warnings: list[str]
    summary: dict[str, str]


@dataclass(frozen=True)
class ExtractedCsv:
    pdf: Path
    csv_path: Path | None
    processed_pdf: Path | None
    statement_type: str
    transaction_count: int
    skipped_reason: str = ""
    validation_errors: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()
    validation_summary: dict[str, str] | None = None
    account_slug: str = ""
    # Statement month as "YYYY-MM" (the statement END date's month, matching the CSV's
    # parent folder). Carried explicitly because downstream code otherwise derives the
    # month from that folder, which a zero-row statement never gets: without this,
    # "imported, no activity" is indistinguishable from "never imported". Empty only
    # when the statement returned before a period could be determined (unsupported /
    # delegated / extraction error). Kept LAST among the defaults so the several call
    # sites that build ExtractedCsv positionally keep working.
    period: str = ""
    coverage_start_date: str = ""
    coverage_end_date: str = ""
    coverage_basis: str = ""
    institution: str = ""
    balance_spec: dict | None = None


def decode_name(path: Path) -> str:
    name = path.name
    for _ in range(3):
        decoded = unquote(name)
        if decoded == name:
            break
        name = decoded
    return name


def parse_money(raw: str) -> Decimal:
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        result = Decimal(cleaned)
    except Exception as exc:
        # Keep the raw amount token out of the message: this error can flow into the
        # extraction manifest's validation_errors, which is a persisted artifact.
        raise ValueError("Cannot parse amount token") from exc
    if not result.is_finite():
        raise ValueError("Non-finite amount token")
    return result


def format_amount(amount: Decimal) -> str:
    return f"{amount:.2f}"


def run_pdftotext(pdf_path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is required. Install Poppler first.")

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{pdf_path}: pdftotext timed out")
    return result.stdout


def run_pdftotext_bytes(pdf_bytes: bytes) -> str:
    """Extract PDF text from immutable bytes without reopening a source pathname."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext is required. Install Poppler first.")

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=pdf_bytes,
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Pinned PDF extraction timed out")
    return result.stdout.decode("utf-8", errors="replace")


def _discrepancy_bucket(delta: Decimal) -> str:
    """Describe the magnitude of a reconciliation discrepancy WITHOUT leaking the
    raw figure.

    Returns a coarse order-of-magnitude bucket (e.g. ``"<$1"``, ``"$1-10"``,
    ``"$100-1k"``) so an operator can gauge severity from the manifest while the
    exact balance/amount (financial PII) stays out of statement-validation.json.
    """
    magnitude = abs(delta)
    if magnitude == 0:
        return "$0"
    if magnitude < Decimal("1"):
        return "<$1"
    if magnitude < Decimal("10"):
        return "$1-10"
    if magnitude < Decimal("100"):
        return "$10-100"
    if magnitude < Decimal("1000"):
        return "$100-1k"
    if magnitude < Decimal("10000"):
        return "$1k-10k"
    return ">$10k"


def row_reference(
    account_number: str,
    end: date,
    *,
    posting_date: date,
    description: str,
    amount: Decimal,
    occurrence: int = 0,
) -> str:
    """Synthesize the reference number for a card statement row.

    Card PDFs carry no bank reference, so the parsers key ``txn_id`` on this value.

    It used to be purely POSITIONAL -- ``last4-YYYYMMDD-NNNN`` where N was the row's
    ordinal among accepted rows -- and therefore contained no transaction content at
    all. Any change to the extraction regex or the table-boundary filters renumbered
    every subsequent row, so re-extracting a statement silently re-pointed MANUAL and
    RULE overrides onto different transactions. Given this repo's history of
    table-boundary fixes, that is a realistic event rather than a theoretical one.

    The reference is now derived from the row's own date, amount, and description, so
    it is stable under any re-extraction that still yields the same row. ``occurrence``
    disambiguates genuinely identical same-day rows (two $30 purchases at the same
    merchant): it counts prior rows with the SAME content, not the row's position, so
    an unrelated row appearing earlier in the table does not shift it.
    """
    prefix = f"{account_suffix(account_number)}-{end.strftime('%Y%m%d')}"
    content = stable_id(posting_date.isoformat(), f"{amount:.2f}", description)[:12]
    if occurrence:
        return f"{prefix}-{content}-{occurrence:02d}"
    return f"{prefix}-{content}"


def row_content_key(posting_date: date, description: str, amount: Decimal) -> tuple:
    """Grouping key for :func:`row_reference`'s occurrence counter."""
    return (posting_date, " ".join(description.split()).upper(), amount)


def slug(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return cleaned or fallback


def account_suffix(account_number: str) -> str:
    digits = re.sub(r"\D", "", account_number)
    if digits:
        return digits[-4:]
    return "unknown"


def output_path(
    csv_root: Path,
    pdf_path: Path,
    period: date,
    statement_type: str,
    account_type: str,
    account_number: str,
) -> Path:
    safe_stem = canonical_account_slug(statement_type, account_type, account_number)
    if safe_stem:
        return csv_root / year_month(period) / f"{safe_stem}.csv"

    safe_stem = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        decode_name(pdf_path).rsplit(".", 1)[0],
    ).strip("-")
    return csv_root / period.strftime("%Y-%m") / f"{safe_stem}.csv"


def processed_path(
    processed_dir: Path,
    statement_type: str,
    account_type: str,
    account_number: str,
    period: date,
) -> Path:
    from ledger.adapters.registry import get_pdf_format

    fmt = get_pdf_format(statement_type)
    institution, statement_kind = fmt.institution, fmt.archive_kind
    statement_slug = statement_kind.replace("_", "-") if statement_kind else statement_type
    account_slug = canonical_account_slug(statement_type, account_type, account_number)
    if account_slug.startswith(f"{institution}-{statement_slug}-"):
        account_slug = account_slug.removeprefix(f"{institution}-{statement_slug}-")
    elif account_slug.startswith(f"{institution}-"):
        account_slug = account_slug.removeprefix(f"{institution}-")
    account_slug = account_slug or slug(account_type, "account")
    suffix = account_suffix(account_number)
    if account_slug.endswith(f"-{suffix}"):
        return (
            processed_dir
            / f"{period.isoformat()}-{institution}-{statement_slug}-{account_slug}.pdf"
        )
    return (
        processed_dir
        / f"{period.isoformat()}-{institution}-{statement_slug}-{account_slug}-{suffix}.pdf"
    )


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not find available archive filename for {path}")


def write_csv(path: Path, rows: list[dict[str, str]], headers: list[str], dry_run: bool) -> None:
    if dry_run:
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    write_private_text_atomic(path, buffer.getvalue(), encoding="utf-8", newline="")


def move_pdf(pdf_path: Path, target_path: Path, dry_run: bool) -> Path:
    if dry_run:
        return target_path
    if pdf_path.resolve() == target_path.resolve():
        tighten_private_file(target_path)
        return target_path
    final_path = unique_path(target_path)
    copy_private_file(pdf_path, final_path)
    try:
        pdf_path.unlink()
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    return final_path


def canonical_account_slug(statement_type: str, account_type: str, account_number: str) -> str:
    from ledger.adapters.registry import get_pdf_format

    return get_pdf_format(statement_type).account_slug(account_type, account_number)
