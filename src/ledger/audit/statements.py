"""Read-only statement-to-database audit service."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ledger.config import load_ledger_config
from ledger.config.repository import load_runtime_app_config
from ledger.core import Transaction
from ledger.imports.pdf import statement_pdf_to_csv
from ledger.processing import ProcessingResult, process
from ledger.runtime import runtime_path, seed_config_path
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    ProcessedFileDB,
    TransactionDB,
    TransferDB,
    session_scope,
)
from ledger.storage.mappers import transaction_from_db, transfer_from_db
from ledger.utils.dates import year_month
from ledger.utils.json_io import decimal_json
from ledger.utils.private_files import (
    ensure_private_dir,
    private_atomic_text,
    write_private_text_atomic,
)
from ledger.utils.text import clean_account_name, neutralize_csv_cell

CENT = Decimal("0.01")


@dataclass(frozen=True)
class StatementAuditOptions:
    db_path: Path
    pdf_dirs: list[Path]
    start_date: date
    end_date: date
    audit_runs_dir: Path = field(default_factory=lambda: runtime_path("audit-runs"))
    ledger_config: Path = field(default_factory=lambda: seed_config_path("ledger.toml"))
    merchant_rules: Path = field(default_factory=lambda: seed_config_path("merchants.toml"))
    categories: Path = field(default_factory=lambda: seed_config_path("categories.toml"))


@dataclass(frozen=True)
class StatementAuditResult:
    run_dir: Path
    status: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class AuditRow:
    posting_date: date
    institution: str
    logical_account: str
    account: str
    account_type: str
    amount: Decimal
    description: str
    source_file: str
    source_kind: str
    account_identity: str | None = None
    currency: str = "CAD"

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.posting_date.isoformat(),
            normalize_token(self.institution),
            normalize_token(self.account_identity or self.logical_account),
            (self.currency or "CAD").strip().upper(),
            str(self.amount.quantize(CENT)),
            description_signature(self.description),
        )


def normalize_token(value: object) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def canonical_account_identity(
    account: str,
    account_aliases: dict[str, str] | None = None,
) -> str:
    """Return the exact parser account identity after configured aliasing."""
    return clean_account_name(account, account_aliases)


def normalize_description(value: str) -> str:
    return " ".join(str(value or "").upper().split())


def description_signature(value: str) -> str:
    cleaned = normalize_description(value)
    cleaned = cleaned.replace("PAI EMENT", "PAIEMENT")
    cleaned = re.sub(r"\b(PURCHASE|TRANS)\b", " ", cleaned)
    tokens = re.findall(r"[A-Z0-9*#&]+", cleaned)
    while tokens and tokens[-1] in {"ON", "BC", "CA", "QC", "AB"}:
        tokens.pop()
    if tokens and tokens[-1].endswith("ON") and len(tokens[-1]) > 4:
        tokens[-1] = tokens[-1][:-2]
    return " ".join(tokens[:4])


def logical_account_token(institution: str, account: str, account_type: str) -> str:
    text = normalize_token(f"{institution} {account} {account_type}")
    if "chequ" in text or "checking" in text:
        token = "chequing"
    elif "saving" in text:
        token = "savings"
    else:
        from ledger.adapters.registry import compatibility_policy

        token = next(
            (
                value
                for pattern, value in compatibility_policy()["account_product_tokens"]
                if pattern in text
            ),
            normalize_token(account_type) or normalize_token(account) or "unknown",
        )
    return f"{normalize_token(institution)}:{token}"


def _month_index(ym: str) -> int:
    year, month = ym.split("-")
    return int(year) * 12 + (int(month) - 1)


def _month_from_index(idx: int) -> str:
    year, month = divmod(idx, 12)
    return f"{year:04d}-{month + 1:02d}"


def detect_statement_continuity_gaps(
    inventory: list[dict[str, Any]],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Describe gaps in supplied statement months within each identity's observed span.

    Optional bounds limit missing months to calendar months intersecting the selected
    interval. They never extend the observed inventory span. Missing entries say
    nothing about transaction activity, completeness of evidence, or account status.
    First/last month and present count describe the full supplied inventory span.
    """
    months_by_account: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    labels: dict[tuple[str, str, str], tuple[str, str]] = {}
    for item in inventory:
        ym = str(item.get("statement_month") or "")
        if re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", ym) is None or ym[:4] == "0000":
            continue
        account = str(item.get("account_identity") or item.get("logical_account") or "")
        if not normalize_token(account):
            continue
        logical_account = str(item.get("logical_account") or "")
        institution = str(
            item.get("institution")
            or (logical_account.partition(":")[0] if ":" in logical_account else "")
        )
        # Missing currency remains unknown; inventory evidence must not invent one.
        currency = str(item.get("currency") or "").strip().upper()
        key = (normalize_token(institution), normalize_token(account), currency)
        months_by_account[key].add(ym)
        labels.setdefault(key, (account, institution))
    gaps: list[dict[str, Any]] = []
    for key in sorted(months_by_account):
        months = sorted(months_by_account[key])
        if len(months) < 2:
            continue
        present = {_month_index(m) for m in months}
        lo, hi = min(present), max(present)
        if start_date is not None:
            lo = max(lo, _month_index(year_month(start_date)))
        if end_date is not None:
            hi = min(hi, _month_index(year_month(end_date)))
        missing = [_month_from_index(i) for i in range(lo, hi + 1) if i not in present]
        if missing:
            account, institution = labels[key]
            gaps.append(
                {
                    "institution": institution,
                    "logical_account": account,
                    "currency": key[2],
                    "first_month": months[0],
                    "last_month": months[-1],
                    "present_count": len(months),
                    "missing_months": missing,
                    "note": (
                        f"The supplied statement inventory has no entry for {account} in "
                        f"{', '.join(missing)}. This describes supplied evidence only; "
                        "transaction activity, evidence completeness, and account status "
                        "are not determined."
                    ),
                }
            )
    return gaps


def audit_row_from_transaction(
    txn: Transaction,
    source_kind: str,
    account_aliases: dict[str, str] | None = None,
) -> AuditRow:
    return AuditRow(
        posting_date=txn.posting_date,
        institution=txn.institution,
        logical_account=logical_account_token(txn.institution, txn.account, txn.account_type),
        account=txn.account,
        account_type=txn.account_type,
        amount=audit_amount(txn),
        description=txn.description,
        source_file=txn.source_file,
        source_kind=source_kind,
        account_identity=canonical_account_identity(txn.account, account_aliases),
        currency=(txn.currency or "CAD").strip().upper(),
    )


def audit_amount(txn: Transaction) -> Decimal:
    signed = Decimal(str(txn.amount_signed)).quantize(CENT)
    raw = Decimal(str(txn.amount_raw)).quantize(CENT)
    if signed == 0 and raw != 0:
        return raw
    return signed


def compare_audit_rows(
    source_rows: list[AuditRow], db_rows: list[AuditRow]
) -> list[dict[str, Any]]:
    source_counts = Counter(row.key for row in source_rows)
    db_counts = Counter(row.key for row in db_rows)
    source_examples = {row.key: row for row in source_rows}
    db_examples = {row.key: row for row in db_rows}

    mismatches = []
    for key in sorted(set(source_counts) | set(db_counts)):
        source_count = source_counts[key]
        db_count = db_counts[key]
        if source_count == db_count:
            continue

        example = source_examples.get(key) or db_examples[key]
        if source_count and db_count:
            mismatch_type = "count_mismatch"
        elif source_count:
            mismatch_type = "source_only"
        else:
            mismatch_type = "db_only"
        mismatches.append(
            {
                "mismatch_type": mismatch_type,
                "posting_date": example.posting_date.isoformat(),
                "institution": example.institution,
                "logical_account": example.logical_account,
                "account_identity": example.account_identity or example.logical_account,
                "currency": (example.currency or "CAD").strip().upper(),
                "source_count": source_count,
                "db_count": db_count,
                "amount": str(example.amount),
                "description_key": description_signature(example.description),
                "description": example.description,
                "source_file": example.source_file,
                "source_kind": example.source_kind,
                "db_account": db_examples.get(key, example).account,
                "source_account": source_examples.get(key, example).account,
            }
        )
    return mismatches


class StatementAuditService:
    """Compare local source statement PDFs against the current SQLite ledger."""

    def run(self, options: StatementAuditOptions) -> StatementAuditResult:
        run_dir = self._make_run_dir(options.audit_runs_dir)
        work_dir = run_dir / "work"
        csv_root = work_dir / "csv"
        processed_placeholder = work_dir / "processed"
        ensure_private_dir(work_dir)

        try:
            ledger = load_ledger_config(options.ledger_config.expanduser().resolve())
            account_aliases = ledger.general.account_aliases
            pdfs = self._discover_pdfs(options.pdf_dirs)
            processed_hashes = self._processed_hashes(options.db_path)
            extraction_results, inventory = self._extract_pdfs(
                pdfs,
                csv_root,
                processed_placeholder,
                processed_hashes,
                account_aliases,
            )
            self._write_csv(run_dir / "statement_inventory.csv", inventory)

            processing = self._process_extracted_csvs(options, csv_root, extraction_results)
            source_rows = [
                audit_row_from_transaction(txn, "source_pdf", account_aliases)
                for txn in (processing.transactions + processing.transfers)
                if options.start_date <= txn.posting_date <= options.end_date
            ]
            db_rows = self._load_db_rows(
                options.db_path,
                options.start_date,
                options.end_date,
                account_aliases,
            )
            row_mismatches = compare_audit_rows(source_rows, db_rows)
            self._write_csv(run_dir / "row_mismatches.csv", row_mismatches)
            mismatch_patterns = analyze_mismatch_causes(
                row_mismatches, start_date=options.start_date, end_date=options.end_date
            )

            balance_mismatches = [
                item
                for item in self._compare_balances(options.db_path, inventory, account_aliases)
                if options.start_date.isoformat()
                <= item["statement_date"]
                <= options.end_date.isoformat()
            ]
            self._write_csv(run_dir / "balance_mismatches.csv", balance_mismatches)

            continuity_gaps = detect_statement_continuity_gaps(
                inventory, start_date=options.start_date, end_date=options.end_date
            )
            self._write_csv(
                run_dir / "statement_continuity_gaps.csv",
                [{**g, "missing_months": ";".join(g["missing_months"])} for g in continuity_gaps],
            )

            processing_errors = [exc.model_dump(mode="json") for exc in processing.exceptions]
            if processing_errors:
                self._write_json(run_dir / "processing_exceptions.json", processing_errors)

            alias_groups = self._account_alias_groups(options.db_path)
            summary = self._summary(
                options,
                pdfs,
                inventory,
                source_rows,
                db_rows,
                row_mismatches,
                balance_mismatches,
                processing_errors,
                mismatch_patterns,
                continuity_gaps,
            )
            self._write_json(run_dir / "summary.json", summary)
            self._write_findings(
                run_dir / "findings.md",
                row_mismatches,
                balance_mismatches,
                alias_groups,
                start_date=options.start_date,
                end_date=options.end_date,
            )
            return StatementAuditResult(
                run_dir=run_dir,
                status=str(summary["status"]),
                summary=summary,
            )
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir)

    def _make_run_dir(self, audit_runs_dir: Path) -> Path:
        ensure_private_dir(audit_runs_dir)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = audit_runs_dir / stamp
        suffix = 2
        while run_dir.exists():
            run_dir = audit_runs_dir / f"{stamp}-{suffix}"
            suffix += 1
        return ensure_private_dir(run_dir)

    def _discover_pdfs(self, pdf_dirs: list[Path]) -> list[Path]:
        pdfs: list[Path] = []
        seen: set[Path] = set()
        for pdf_dir in pdf_dirs:
            for path in sorted(pdf_dir.expanduser().resolve().glob("*")):
                if path.suffix.lower() != ".pdf" or not path.is_file():
                    continue
                if path not in seen:
                    pdfs.append(path)
                    seen.add(path)
        return pdfs

    def _processed_hashes(self, db_path: Path) -> set[str]:
        with session_scope(db_path) as session:
            return {row.file_hash for row in session.query(ProcessedFileDB.file_hash).all()}

    def _extract_pdfs(
        self,
        pdfs: list[Path],
        csv_root: Path,
        processed_placeholder: Path,
        processed_hashes: set[str],
        account_aliases: dict[str, str] | None = None,
    ) -> tuple[list[statement_pdf_to_csv.ExtractedCsv], list[dict[str, Any]]]:
        results = []
        inventory = []
        for pdf in pdfs:
            file_hash = sha256_file(pdf)
            try:
                result = statement_pdf_to_csv.process_pdf(
                    pdf,
                    csv_root,
                    processed_placeholder,
                    dry_run=False,
                    archive_pdf=False,
                )
            except Exception as exc:
                result = statement_pdf_to_csv.ExtractedCsv(
                    pdf=pdf,
                    csv_path=None,
                    processed_pdf=None,
                    statement_type="parser_exception",
                    transaction_count=0,
                    skipped_reason="parser_exception",
                    validation_errors=(str(exc),),
                )
            results.append(result)
            inventory.append(
                inventory_row(
                    pdf,
                    file_hash,
                    result,
                    file_hash in processed_hashes,
                    account_aliases,
                )
            )
        return results, inventory

    def _process_extracted_csvs(
        self,
        options: StatementAuditOptions,
        csv_root: Path,
        extraction_results: list[statement_pdf_to_csv.ExtractedCsv],
    ) -> ProcessingResult:
        ledger = load_ledger_config(options.ledger_config.expanduser().resolve())
        config = load_runtime_app_config(
            options.db_path,
            ledger,
            options.merchant_rules.expanduser().resolve(),
            options.categories.expanduser().resolve(),
        )
        csv_root_resolved = csv_root.resolve()
        account_slugs = {
            str(result.csv_path.resolve().relative_to(csv_root_resolved)): result.account_slug
            for result in extraction_results
            if result.csv_path is not None and result.account_slug
        }
        return process(
            config=config,
            source_root=csv_root,
            include_raw=False,
            account_slugs=account_slugs,
        )

    def _load_db_rows(
        self,
        db_path: Path,
        start_date: date,
        end_date: date,
        account_aliases: dict[str, str] | None = None,
    ) -> list[AuditRow]:
        rows: list[AuditRow] = []
        with session_scope(db_path) as session:
            txns = (
                session.query(TransactionDB)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .all()
            )
            transfers = (
                session.query(TransferDB)
                .filter(TransferDB.posting_date >= start_date)
                .filter(TransferDB.posting_date <= end_date)
                .all()
            )
            rows.extend(
                audit_row_from_transaction(
                    transaction_from_db(row),
                    "db_transaction",
                    account_aliases,
                )
                for row in txns
            )
            rows.extend(
                audit_row_from_transaction(
                    transfer_from_db(row),
                    "db_transfer",
                    account_aliases,
                )
                for row in transfers
            )
        return rows

    def _compare_balances(
        self,
        db_path: Path,
        inventory: list[dict[str, Any]],
        account_aliases: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        mismatches = []
        # Reuse a single DB session across all inventory items. Previously this opened
        # one session per item (N+1 connections); the per-item balance lookup now runs
        # against this shared session. Behavior is identical.
        with session_scope(db_path) as session:
            for item in inventory:
                if item.get("balance_kind") != "deposit":
                    continue
                closing = parse_decimal(item.get("statement_closing_balance"))
                statement_date = parse_iso_date(item.get("statement_end_date"))
                if closing is None or statement_date is None:
                    continue
                candidates = self._balance_candidates(
                    session,
                    item["logical_account"],
                    statement_date,
                    account_identity=str(item.get("account_identity") or ""),
                    account_aliases=account_aliases,
                )
                if not candidates:
                    mismatches.append(
                        {
                            "status": "missing_db_balance",
                            "pdf_path": item["pdf_path"],
                            "logical_account": item["logical_account"],
                            "statement_date": statement_date.isoformat(),
                            "statement_closing_balance": str(closing),
                            "db_balances": "",
                            "nearest_difference": "",
                        }
                    )
                    continue

                if any(candidate["balance"] == closing for candidate in candidates):
                    continue

                nearest = min(abs(candidate["balance"] - closing) for candidate in candidates)
                status = "alias_overlap_mismatch" if len(candidates) > 1 else "mismatch"
                mismatches.append(
                    {
                        "status": status,
                        "pdf_path": item["pdf_path"],
                        "logical_account": item["logical_account"],
                        "statement_date": statement_date.isoformat(),
                        "statement_closing_balance": str(closing),
                        "db_balances": "; ".join(
                            f"{candidate['account_number']}={candidate['balance']}"
                            for candidate in candidates
                        ),
                        "nearest_difference": str(nearest.quantize(CENT)),
                    }
                )
        return mismatches

    def _balance_candidates(
        self,
        session: Any,
        logical_account: str,
        statement_date: date,
        account_identity: str = "",
        account_aliases: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        candidates = []
        canonical_identity = canonical_account_identity(account_identity, account_aliases)
        rows = (
            session.query(AccountDB, AccountBalanceDB)
            .join(AccountBalanceDB, AccountDB.account_id == AccountBalanceDB.account_id)
            .filter(AccountBalanceDB.balance_date == statement_date)
            .filter(AccountBalanceDB.balance_basis == "absolute")
            .all()
        )
        for account, balance in rows:
            if canonical_identity:
                account_key = canonical_account_identity(
                    str(account.account_number),
                    account_aliases,
                )
                if account_key != canonical_identity:
                    continue
            else:
                account_key = logical_account_token(
                    str(account.institution),
                    str(account.account_number),
                    str(account.account_subtype or account.account_type),
                )
                if account_key != logical_account:
                    continue
            candidates.append(
                {
                    "account_id": account.account_id,
                    "account_number": account.account_number,
                    "balance": Decimal(str(balance.balance)).quantize(CENT),
                }
            )
        return candidates

    def _account_alias_groups(self, db_path: Path) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        with session_scope(db_path) as session:
            for account in session.query(AccountDB).order_by(
                AccountDB.institution, AccountDB.account_number
            ):
                key = logical_account_token(
                    str(account.institution),
                    str(account.account_number),
                    str(account.account_subtype or account.account_type),
                )
                groups[key].append(
                    {
                        "account_number": account.account_number,
                        "first": account.first_transaction_date.isoformat(),
                        "last": account.last_transaction_date.isoformat(),
                        "transactions": int(account.transaction_count),
                        "is_active": bool(account.is_active),
                    }
                )
        return dict(groups)

    def _summary(
        self,
        options: StatementAuditOptions,
        pdfs: list[Path],
        inventory: list[dict[str, Any]],
        source_rows: list[AuditRow],
        db_rows: list[AuditRow],
        row_mismatches: list[dict[str, Any]],
        balance_mismatches: list[dict[str, Any]],
        processing_errors: list[dict[str, Any]],
        mismatch_patterns: dict[str, Any],
        continuity_gaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        continuity_gaps = continuity_gaps or []
        validation_error_count = sum(1 for item in inventory if item.get("validation_errors"))
        status = "pass"
        # NOTE: continuity gaps are informational only and deliberately DO NOT affect
        # status: gaps describe supplied inventory, not account activity or completeness.
        if row_mismatches or balance_mismatches or processing_errors or validation_error_count:
            status = "issues_found"
        return {
            "status": status,
            "ok": status == "pass",
            "period": {
                "start": options.start_date.isoformat(),
                "end": options.end_date.isoformat(),
            },
            "counts": {
                "pdfs": len(pdfs),
                "inventory_rows": len(inventory),
                "source_rows": len(source_rows),
                "db_rows": len(db_rows),
                "row_mismatches": len(row_mismatches),
                "balance_mismatches": len(balance_mismatches),
                "processing_errors": len(processing_errors),
                "statement_validation_errors": validation_error_count,
                "processed_file_hash_matches": sum(
                    1 for item in inventory if item["in_processed_files"]
                ),
            },
            "pdfs_by_folder_year": count_by_folder_year(inventory),
            "mismatches_by_type": dict(Counter(item["mismatch_type"] for item in row_mismatches)),
            "balance_mismatches_by_status": dict(
                Counter(item["status"] for item in balance_mismatches)
            ),
            "mismatch_patterns": mismatch_patterns,
            "statement_continuity_gaps": {
                "accounts_with_gaps": len(continuity_gaps),
                "gaps": continuity_gaps,
            },
        }

    def _write_findings(
        self,
        path: Path,
        row_mismatches: list[dict[str, Any]],
        balance_mismatches: list[dict[str, Any]],
        alias_groups: dict[str, list[dict[str, Any]]],
        *,
        start_date: date,
        end_date: date,
    ) -> None:
        start, end = start_date.isoformat(), end_date.isoformat()
        selected_rows = [item for item in row_mismatches if start <= item["posting_date"] <= end]
        selected_balances = [
            item for item in balance_mismatches if start <= item["statement_date"] <= end
        ]
        overlapping_identities = {
            key: [row for row in rows if row["first"] <= end and row["last"] >= start]
            for key, rows in alias_groups.items()
        }
        identity_groups = {
            key: rows for key, rows in overlapping_identities.items() if len(rows) > 1
        }
        patterns = analyze_mismatch_causes(selected_rows, start_date=start_date, end_date=end_date)
        lines = [
            "# Statement Audit Findings",
            "",
            f"Selected interval: {start} to {end} (inclusive).",
            "",
            f"- Row mismatch groups: {len(selected_rows)}",
            f"- Balance mismatches: {len(selected_balances)}",
            f"- Logical account groups with multiple recorded identities: {len(identity_groups)}",
            "- Candidate near-date row pairs within five days: "
            f"{patterns['near_date_matches_within_5_days']}",
            "- Candidate same-date description variants: "
            f"{patterns['same_date_description_variants']}",
            "",
            "## Interpretation Limits",
            "",
            "This report compares the supplied statement evidence with recorded database "
            "evidence for the selected interval. It does not establish completeness beyond "
            "those inputs. Row mismatch counts describe comparison groups, not transaction counts.",
            "",
            "Near-date and description similarities are review candidates, not confirmed "
            "matches or explanations. Recorded identity groups do not establish that records "
            "belong to the same real account. Identity date spans and row counts below describe "
            "historical records, not activity limited to the selected interval.",
            "",
        ]
        if not selected_rows and not selected_balances:
            lines.extend(
                ["No row or balance mismatches were detected in the selected interval.", ""]
            )
        detail_limit = 25
        if identity_groups:
            lines.extend(
                [
                    "## Recorded Account Identities",
                    "",
                    f"Showing {min(detail_limit, len(identity_groups))} of "
                    f"{len(identity_groups)} account identity groups.",
                ]
            )
            for key, rows in sorted(identity_groups.items())[:detail_limit]:
                lines.append(f"- {key}")
                lines.append(
                    f"  - Showing {min(detail_limit, len(rows))} of {len(rows)} recorded identities."
                )
                for row in rows[:detail_limit]:
                    lines.append(
                        f"  - {row['account_number']}: {row['first']} to {row['last']}, "
                        f"{row['transactions']} historical rows, active={row['is_active']}"
                    )
            lines.append("")
        if selected_rows:
            lines.extend(
                [
                    "## Row Mismatches",
                    "",
                    f"Showing {min(detail_limit, len(selected_rows))} of "
                    f"{len(selected_rows)} row mismatch groups.",
                ]
            )
            for item in selected_rows[:detail_limit]:
                lines.append(
                    f"- {item['posting_date']} {item['logical_account']} {item['amount']} "
                    f"{item['mismatch_type']}: {item['description']}"
                )
            lines.append("")
        if selected_balances:
            lines.extend(
                [
                    "## Balance Mismatches",
                    "",
                    f"Showing {min(detail_limit, len(selected_balances))} of "
                    f"{len(selected_balances)} balance mismatches.",
                ]
            )
            for item in selected_balances[:detail_limit]:
                lines.append(
                    f"- {item['statement_date']} {item['logical_account']} {item['status']}: "
                    f"statement={item['statement_closing_balance']} db={item['db_balances']}"
                )
            lines.append("")
        lines.append(
            "Review the complete row_mismatches.csv and balance_mismatches.csv files "
            "before proposing any repair."
        )
        write_private_text_atomic(path, "\n".join(lines).rstrip() + "\n")

    def _write_json(self, path: Path, payload: Any) -> None:
        write_private_text_atomic(path, json.dumps(payload, indent=2, default=json_default))

    def _write_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            write_private_text_atomic(path, "")
            return
        with private_atomic_text(path, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                # Defuse CSV formula injection: statement-derived text (e.g. mismatch
                # descriptions) can begin with = + - @ and execute as a formula when
                # this audit-run CSV is opened in Excel/Sheets/LibreOffice.
                writer.writerow({key: neutralize_csv_cell(value) for key, value in row.items()})


def inventory_row(
    pdf: Path,
    file_hash: str,
    result: statement_pdf_to_csv.ExtractedCsv,
    in_processed_files: bool,
    account_aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    csv_info = read_first_csv_row(result.csv_path)
    statement_end = statement_end_from_processed_path(result.processed_pdf)
    institution = result.institution
    account_type = csv_info.get("Account Type", "")
    account_number = csv_info.get("Account Number", "")
    summary = result.validation_summary or {}
    logical_account = logical_account_token(institution, account_number, account_type)
    account_identity = canonical_account_identity(
        result.account_slug or account_number,
        account_aliases,
    )
    return {
        "pdf_path": str(pdf),
        "folder": pdf.parent.name,
        "file_hash": file_hash,
        "in_processed_files": in_processed_files,
        "statement_type": result.statement_type,
        "institution": institution,
        "balance_kind": "deposit"
        if result.balance_spec and result.balance_spec.get("printed_sign") == 1
        else "other",
        "statement_end_date": statement_end.isoformat() if statement_end else "",
        "statement_month": year_month(statement_end) if statement_end else "",
        "account_type": account_type,
        "account_number": account_number,
        "logical_account": logical_account,
        "account_identity": account_identity,
        "transaction_count": result.transaction_count,
        "skipped_reason": result.skipped_reason,
        "validation_errors": " | ".join(result.validation_errors),
        "validation_warnings": " | ".join(result.validation_warnings),
        "statement_opening_balance": summary.get("opening_balance", ""),
        "statement_total_deposits": summary.get("total_deposits", ""),
        "statement_total_withdrawals": summary.get("total_withdrawals", ""),
        "statement_closing_balance": summary.get("closing_balance", ""),
    }


def read_first_csv_row(csv_path: Path | None) -> dict[str, str]:
    if not csv_path or not csv_path.exists():
        return {}
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return next(reader, {}) or {}


def statement_end_from_processed_path(path: Path | None) -> date | None:
    if not path:
        return None
    match = re.match(r"(20\d{2}-\d{2}-\d{2})", path.name)
    if not match:
        return None
    return date.fromisoformat(match.group(1))


def parse_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(CENT)
    except InvalidOperation, ValueError:
        return None


def parse_iso_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_by_folder_year(inventory: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in inventory:
        year = str(item.get("statement_end_date") or item["pdf_path"])[:4]
        if not year.isdigit():
            match = re.search(r"(20\d{2})", item["pdf_path"])
            year = match.group(1) if match else "unknown"
        counts[f"{item['folder']}:{year}"] += 1
    return dict(sorted(counts.items()))


def analyze_mismatch_causes(
    row_mismatches: list[dict[str, Any]],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Describe candidate mismatch patterns without inferring their cause."""
    near_date_pairs = likely_near_date_pairs(
        row_mismatches, date_start=start_date, date_end=end_date
    )
    return {
        "near_date_matches_within_5_days": len(near_date_pairs),
        "same_date_description_variants": len(
            likely_same_date_description_pairs(
                row_mismatches, date_start=start_date, date_end=end_date
            )
        ),
        "near_date_matches_by_day_delta": dict(
            Counter(str(item["day_delta"]) for item in near_date_pairs)
        ),
    }


def likely_near_date_pairs(
    row_mismatches: list[dict[str, Any]],
    *,
    date_start: date | None,
    date_end: date | None,
) -> list[dict[str, Any]]:
    source_rows, db_rows = source_and_db_only(
        row_mismatches, date_start=date_start, date_end=date_end
    )
    db_by_key: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in db_rows:
        institution, account_identity, currency = mismatch_identity(row)
        db_by_key[
            (institution, account_identity, currency, row["amount"], row["description_key"])
        ].append(row)

    pairs = []
    used_db_ids: set[int] = set()
    for source in source_rows:
        source_date = date.fromisoformat(source["posting_date"])
        institution, account_identity, currency = mismatch_identity(source)
        candidates = []
        for db_row in db_by_key.get(
            (institution, account_identity, currency, source["amount"], source["description_key"]),
            [],
        ):
            if id(db_row) in used_db_ids:
                continue
            db_date = date.fromisoformat(db_row["posting_date"])
            day_delta = (db_date - source_date).days
            if day_delta == 0 or abs(day_delta) > 5:
                continue
            candidates.append((abs(day_delta), day_delta, db_row))
        if not candidates:
            continue
        _distance, day_delta, db_row = min(candidates, key=lambda item: item[0])
        used_db_ids.add(id(db_row))
        pairs.append(
            {
                "source_date": source["posting_date"],
                "db_date": db_row["posting_date"],
                "day_delta": day_delta,
                "institution": source.get("institution", ""),
                "logical_account": source["logical_account"],
                "account_identity": source.get("account_identity") or source["logical_account"],
                "currency": currency,
                "amount": source["amount"],
                "description_key": source["description_key"],
            }
        )
    return pairs


def likely_same_date_description_pairs(
    row_mismatches: list[dict[str, Any]],
    *,
    date_start: date | None,
    date_end: date | None,
) -> list[dict[str, Any]]:
    source_rows, db_rows = source_and_db_only(
        row_mismatches, date_start=date_start, date_end=date_end
    )
    db_by_key: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in db_rows:
        institution, account_identity, currency = mismatch_identity(row)
        db_by_key[
            (row["posting_date"], institution, account_identity, currency, row["amount"])
        ].append(row)

    pairs = []
    used_db_ids: set[int] = set()
    for source in source_rows:
        institution, account_identity, currency = mismatch_identity(source)
        candidates = [
            row
            for row in db_by_key.get(
                (source["posting_date"], institution, account_identity, currency, source["amount"]),
                [],
            )
            if id(row) not in used_db_ids
        ]
        if not candidates:
            continue
        db_row = candidates[0]
        used_db_ids.add(id(db_row))
        pairs.append(
            {
                "posting_date": source["posting_date"],
                "institution": source.get("institution", ""),
                "logical_account": source["logical_account"],
                "account_identity": source.get("account_identity") or source["logical_account"],
                "currency": currency,
                "amount": source["amount"],
                "source_description_key": source["description_key"],
                "db_description_key": db_row["description_key"],
            }
        )
    return pairs


def mismatch_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """Use the same institution/account/currency boundary as strict row comparison."""
    institution = normalize_token(row.get("institution"))
    account = normalize_token(row.get("account_identity") or row.get("logical_account"))
    currency = str(row.get("currency") or "CAD").strip().upper()
    return institution, account, currency


def source_and_db_only(
    row_mismatches: list[dict[str, Any]],
    *,
    date_start: date | None,
    date_end: date | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filtered = []
    for row in row_mismatches:
        row_date = date.fromisoformat(row["posting_date"])
        if date_start and row_date < date_start:
            continue
        if date_end and row_date > date_end:
            continue
        filtered.append(row)
    return (
        [row for row in filtered if row["mismatch_type"] == "source_only"],
        [row for row in filtered if row["mismatch_type"] == "db_only"],
    )


def json_default(value: Any) -> Any:
    # Share the canonical Decimal/Path/date serialization; keep this site's lenient
    # str() fallback for any other unexpected type in an audit payload.
    converted = decimal_json(value)
    return converted if converted is not value else str(value)
