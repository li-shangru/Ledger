"""Generate privacy-safe local exports for external analysis."""

from __future__ import annotations

import json
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from ledger.adjustments import AdjustmentDetectionService
from ledger.core.account_identity import account_identity
from ledger.finance_policy import is_income_row, is_spend_row
from ledger.query.account_refs import account_ref_map, resolve_account_ref
from ledger.reporting import ReportSnapshotService
from ledger.runtime import resolve_runtime_path
from ledger.storage.database import (
    AccountDB,
    ExportRunDB,
    RecurringPatternDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import ensure_account_ref_salt
from ledger.utils.dates import naive_utc_now, year_month
from ledger.utils.json_io import decimal_json
from ledger.utils.private_files import (
    ensure_private_dir,
    private_atomic_binary,
    private_atomic_text,
)
from ledger.utils.text import neutralize_csv_cell

EXPORT_PRESETS = {
    "chatgpt-safe",
    "credit-card-optimization",
    "merchant-cleanup",
    "tax-category-review",
    "full-local-audit",
}

# Presets whose intent is to share the export with an external LLM/third party.
# These get aggregates-only redaction (no transaction-level merchants/amounts/ids).
SHARE_SAFE_PRESETS = frozenset({"chatgpt-safe"})

# Bucket size (dollars) for redacted recurring expected_amount.
_RECURRING_BUCKET = Decimal("5")

# Coarse bucket size (dollars) for share-safe AGGREGATE amounts (summary,
# category/monthly/card/movement). A category/month/card/movement group with a
# single transaction would otherwise emit that transaction's EXACT amount, so in
# share-safe mode every emitted money value is rounded to this bucket. Counts
# (transaction_count/occurrence_count) and percentages are never bucketed.
_AGG_BUCKET = Decimal("10")


@dataclass(frozen=True)
class ExportPackageResult:
    """Generated export artifact metadata."""

    path: Path
    rows_exported: int
    preset: str
    export_format: str
    files: list[str]


class ExportPackageService:
    """Create privacy-safe datasets for local or external analysis."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._salt: str | None = None
        self._account_refs: dict[str, str] | None = None
        self._account_refs_by_id: dict[str, str] | None = None
        init_database(self.db_path)

    def create_package(
        self,
        *,
        dest_dir: Path | None = None,
        preset: str = "chatgpt-safe",
        export_format: str = "zip",
        start_date: date | None = None,
        end_date: date | None = None,
        institutions: list[str] | None = None,
        categories: list[str] | None = None,
        merchants: list[str] | None = None,
        include_transfers: bool = False,
    ) -> ExportPackageResult:
        dest_dir = resolve_runtime_path(dest_dir, "exports")
        if preset not in EXPORT_PRESETS:
            raise ValueError(f"preset must be one of: {', '.join(sorted(EXPORT_PRESETS))}")
        if export_format not in {"zip", "csv", "jsonl", "parquet"}:
            raise ValueError("format must be zip, csv, jsonl, or parquet")

        ensure_private_dir(dest_dir)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        stem = f"ledger-{preset}-{timestamp}"

        transactions = self._transaction_rows(
            start_date=start_date,
            end_date=end_date,
            institutions=institutions,
            categories=categories,
            merchants=merchants,
            include_transfers=include_transfers,
        )
        df = pd.DataFrame(transactions)
        files: list[Path] = []
        share_safe = preset in SHARE_SAFE_PRESETS

        if export_format in {"csv", "jsonl", "parquet"} and share_safe:
            # These formats emit ONLY the raw per-transaction table (merchant,
            # exact amount, txn_id). A share-with-an-LLM preset must never produce
            # transaction-level PII, so refuse rather than silently leak. Use the
            # zip preset for aggregates-only output.
            raise ValueError(
                f"preset '{preset}' is aggregates-only and cannot be exported as a "
                "per-transaction csv/jsonl/parquet; use format=zip"
            )

        if export_format == "csv":
            output = dest_dir / f"{stem}.csv"
            self._safe_to_csv(df, output)
            files = [output]
        elif export_format == "jsonl":
            output = dest_dir / f"{stem}.jsonl"
            self._write_jsonl(output, transactions)
            files = [output]
        elif export_format == "parquet":
            output = dest_dir / f"{stem}.parquet"
            with private_atomic_binary(output) as output_handle:
                df.to_parquet(output_handle, index=False)
            files = [output]
        else:
            # Build the per-file outputs inside a throwaway temp directory and write
            # ONLY the final .zip into dest_dir. The intermediate transactions.csv /
            # merchant_summary.csv carry raw PII (merchant + exact amount); for any
            # non-data/ --dest they are NOT gitignored, so a leftover package dir
            # under dest_dir would leak. TemporaryDirectory guarantees the unzipped
            # files never survive a successful OR failed export.
            output = dest_dir / f"{stem}.zip"
            with tempfile.TemporaryDirectory(prefix=f"{stem}-") as tmp:
                package_dir = Path(tmp).resolve()
                summary_json = package_dir / "summary.json"
                category_csv = package_dir / "category_summary.csv"
                monthly_csv = package_dir / "monthly_trend.csv"
                recurring_csv = package_dir / "recurring_spend.csv"
                card_csv = package_dir / "card_account_summary.csv"
                category_movement_csv = package_dir / "category_movement.csv"

                # Aggregate/derived files emitted by EVERY preset (category/period/
                # account-level; inherently less identifying).
                zip_paths: list[Path] = []
                with private_atomic_text(summary_json) as summary_handle:
                    self._summary(df, preset, include_transfers, share_safe=share_safe).to_json(
                        summary_handle, orient="index", indent=2
                    )
                self._safe_to_csv(
                    self._group(df, ["category"], "category_summary", share_safe=share_safe),
                    category_csv,
                )
                self._safe_to_csv(
                    # `account_ref_status` is carried into the group key so the
                    # reason a ref is unresolved reaches the file rather than
                    # living in prose. It adds no splits -- it is a function of
                    # the ref -- and `_account_ref` already guarantees distinct
                    # accounts get distinct keys, which a blank ref did not.
                    self._group(
                        df,
                        ["month", "account_ref", "account_ref_status"],
                        "monthly_trend",
                        share_safe=share_safe,
                    ),
                    monthly_csv,
                )
                # These three files come from period-scoped services and a whole-period
                # snapshot, NOT from the filtered transaction frame, so an
                # institution/category/merchant filter never reached them: a package
                # narrowed to one category still shipped whole-portfolio recurring
                # spend, per-card totals, and category drift. Omit them entirely when a
                # scope filter is set rather than silently shipping unscoped aggregates.
                scoped = bool(institutions or categories or merchants)
                if not scoped:
                    self._safe_to_csv(
                        self._recurring_rows(start_date, end_date, redact=share_safe),
                        recurring_csv,
                    )
                report_service = ReportSnapshotService(self.db_path)
                snapshot = (
                    report_service.snapshot_for_period(
                        start_date,
                        end_date,
                        source="export_package",
                        persist=True,  # export build is an explicit apply action
                    )
                    if start_date and end_date
                    else report_service.create_snapshot(source="export_package")
                )
                payload = snapshot["payload"] if snapshot else {}
                card_rows = payload.get("account_card_summaries", [])
                # Card identity was added to the existing report schema version, so
                # an explicit-period lookup can find a pre-change cached payload that
                # has no canonical account_id. Refresh that cache entry instead of
                # reviving the legacy (institution, account_type) reverse lookup or
                # refusing an otherwise valid export. The default-period branch above
                # already calls create_snapshot and is always fresh.
                if (
                    start_date
                    and end_date
                    and card_rows
                    and any(
                        not isinstance(row, dict) or not row.get("account_id") for row in card_rows
                    )
                ):
                    snapshot = report_service.create_snapshot(
                        source="export_package",
                        start_date=start_date,
                        end_date=end_date,
                    )
                    payload = snapshot["payload"] if snapshot else {}
                card_df = pd.DataFrame(payload.get("account_card_summaries", []))
                movement_df = pd.DataFrame(payload.get("category_drift", []))
                if share_safe:
                    # card_account_summary money columns: net, volume (transaction_count
                    # is a count). category_movement money columns: current, previous,
                    # change. change_pct = change / abs(previous) * 100 is emitted at
                    # FULL float precision; combined with the bucketed previous/change
                    # band and the 2-decimal cents constraint it makes the exact
                    # (previous, change) cent-pair UNIQUELY recoverable for a single-row
                    # drift group, leaking the exact transaction amounts. The bucketed
                    # current/previous/change already convey direction and magnitude, so
                    # drop change_pct entirely in share-safe mode. (Full-detail presets
                    # keep it.)
                    card_df = self._share_safe_card_summary(card_df)
                    movement_df = self._bucket_columns(
                        movement_df, ["current", "previous", "change"]
                    )
                    movement_df = movement_df.drop(columns=["change_pct"], errors="ignore")
                if not scoped:
                    self._safe_to_csv(card_df, card_csv)
                    self._safe_to_csv(movement_df, category_movement_csv)
                zip_paths = [
                    summary_json,
                    category_csv,
                    monthly_csv,
                ]
                if not scoped:
                    zip_paths += [recurring_csv, card_csv, category_movement_csv]
                else:
                    # Say so in the package rather than leaving a silent gap.
                    (package_dir / "OMITTED.txt").write_text(
                        "recurring_spend.csv, card_account_summary.csv and "
                        "category_movement.csv are omitted from this package.\n\n"
                        "They are derived from whole-period aggregates that do not honour "
                        "the institution/category/merchant filters applied here, so "
                        "including them would ship unscoped portfolio data in a package "
                        "you deliberately narrowed.\n",
                        encoding="utf-8",
                    )
                    zip_paths.append(package_dir / "OMITTED.txt")

                if not share_safe:
                    # Full-detail presets retain transaction-level data and the
                    # merchant-by-merchant breakdown for local analysis. The
                    # share-safe preset drops all three to emit aggregates only.
                    transaction_csv = package_dir / "transactions.csv"
                    transaction_jsonl = package_dir / "transactions.jsonl"
                    merchant_csv = package_dir / "merchant_summary.csv"
                    self._safe_to_csv(df, transaction_csv)
                    self._write_jsonl(transaction_jsonl, transactions)
                    self._safe_to_csv(
                        self._group(df, ["merchant"], "merchant_summary"),
                        merchant_csv,
                    )
                    zip_paths = [
                        transaction_csv,
                        transaction_jsonl,
                        merchant_csv,
                        *zip_paths,
                    ]

                with (
                    private_atomic_binary(output) as output_handle,
                    zipfile.ZipFile(output_handle, "w", compression=zipfile.ZIP_DEFLATED) as zf,
                ):
                    for path in zip_paths:
                        zf.write(path, arcname=path.name)
            files = [output]

        result = ExportPackageResult(
            path=files[0],
            rows_exported=len(transactions),
            preset=preset,
            export_format=export_format,
            files=[path.name for path in files],
        )
        self._record_run(
            result,
            start_date=start_date,
            end_date=end_date,
            include_transfers=include_transfers,
        )
        return result

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with session_scope(self.db_path) as session:
            rows = (
                session.query(ExportRunDB)
                .order_by(ExportRunDB.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "Created At": row.created_at,
                    "Preset": row.preset,
                    "Format": row.export_format,
                    "Rows": row.rows_exported,
                    "Status": row.status,
                    "Path": row.output_path,
                }
                for row in rows
            ]

    def _transaction_rows(
        self,
        *,
        start_date: date | None,
        end_date: date | None,
        institutions: list[str] | None,
        categories: list[str] | None,
        merchants: list[str] | None,
        include_transfers: bool,
    ) -> list[dict[str, Any]]:
        with session_scope(self.db_path) as session:
            query = session.query(TransactionDB)
            if start_date:
                query = query.filter(TransactionDB.posting_date >= start_date)
            if end_date:
                query = query.filter(TransactionDB.posting_date <= end_date)
            if institutions:
                query = query.filter(TransactionDB.institution.in_(institutions))
            if categories:
                query = query.filter(TransactionDB.category.in_(categories))
            if merchants:
                query = query.filter(TransactionDB.merchant.in_(merchants))
            if not include_transfers:
                query = query.filter(TransactionDB.cashflow_type != "transfer")

            rows = query.order_by(TransactionDB.posting_date, TransactionDB.txn_id).all()
            transactions = [self._safe_transaction(row) for row in rows]

        annotations = AdjustmentDetectionService(self.db_path).annotation_map(
            [row["txn_id"] for row in transactions]
        )
        for row in transactions:
            row.update(
                annotations.get(
                    row["txn_id"],
                    {
                        "treatment_type": "",
                        "treatment_status": "",
                        "netting_policy": "",
                        "target_category": "",
                        "true_cost_offset": 0.0,
                    },
                )
            )
        return transactions

    def _safe_transaction(self, row: TransactionDB) -> dict[str, Any]:
        institution = str(row.institution or "")
        account = str(row.account or "")
        account_type = str(row.account_type or "")
        account_ref, account_ref_status = self._account_ref(account)
        return {
            "txn_id": row.txn_id,
            "date": row.posting_date.isoformat(),
            "month": year_month(row.posting_date),
            "institution": institution,
            "account_ref": account_ref,
            "account_ref_status": account_ref_status,
            "account_type": account_type,
            "cashflow_type": row.cashflow_type,
            "flow": row.flow,
            "merchant": row.merchant,
            "category": row.category,
            # Decimal preserves exact cents in both CSV (writes as "73.41") and
            # parquet (pyarrow stores as decimal128 — exact, numeric, no float drift).
            "amount": row.amount_signed,
            "currency": row.currency,
            "include_in_cashflow": bool(row.include_in_cashflow),
            "exclude_from_spending": bool(row.exclude_from_spending),
            "treatment_type": "",
            "treatment_status": "",
            "netting_policy": "",
            "target_category": "",
            "true_cost_offset": 0.0,
            "day_of_week": row.day_of_week,
            "is_weekend": bool(row.is_weekend),
        }

    def _account_salt(self) -> str:
        """Return the per-DB account-ref salt, minting it once via the shared helper.

        The salt lives in config_metadata and is shared with every other consumer
        (see ``storage.operations``), so a salt seeded by ``ledger config
        ensure-account-salt`` is reused here rather than replaced. Cached on the
        instance so one package build reads it once. It is never written into any
        export artifact.
        """
        if self._salt is not None:
            return self._salt
        self._salt = ensure_account_ref_salt(self.db_path)
        return self._salt

    def _account_refs_by_slug(self) -> dict[str, str]:
        """Slug → ref for every stored account, read once per package build.

        Cached on the instance for the same reason the salt is: one build reads
        the accounts dimension once, not once per exported row.
        """
        if self._account_refs is None:
            with session_scope(self.db_path) as session:
                self._account_refs = account_ref_map(session, self._account_salt())
        return self._account_refs

    def _account_ref(self, account: str) -> tuple[str, str]:
        """Opaque account reference and the status that explains it.

        Resolved through the ``accounts`` dimension, NOT hashed from the row's own
        ``account_type``. That column is uncontrolled text harvested from PDFs.
        Variants or extraction noise must not split one physical account into
        several references. Resolution through the dimension keeps exports and
        read-only surfaces in the same namespace (see ``query.account_refs``).

        Behavioral note: ALL presets (not just share-safe) use this salted HMAC ref
        — even the full-detail transactions.csv in full-local-audit carries an
        account_ref instead of the raw account number. The ref is stable within a
        DB but differs across DBs, so it cannot be correlated by third parties. It
        is not, on its own, a promise that no account identifier reaches the file:
        a parser can write the account slug into ``transactions.account_type``,
        and `_safe_transaction` emits that column verbatim. The ref replaces the
        ACCOUNT NUMBER column; it does not sanitise account_type.

        A slug the dimension has no row for gets a sentinel in a visibly different
        namespace (``unresolved-…``, never ``acct-…``) plus the status
        ``no_account_record``. D3 forbids a fallback ref wearing the resolved
        namespace's name; it does not ask for a blank key, and a blank key is its
        own silent defect: ``monthly_trend.csv`` groups on ["month", "account_ref"],
        so two transactions on two DIFFERENT unjoinable accounts collapsed into one
        row with their money summed. The sentinel keeps distinct accounts distinct,
        is minted under the same salt so it discloses nothing about the slug, can
        never be joined against a real `acct-` ref by mistake, and travels with a
        stated reason in ``account_ref_status`` — the same disclosure channel
        ``ledger.coverage`` uses.

        BOTH branches come from ``query.account_refs``, which is also what
        ``ledger.coverage`` calls, so the file and the wire cannot drift: one
        unjoinable account carries one sentinel everywhere. Coverage shipped a null
        here while this method already minted the sentinel, which is exactly the
        drift a second local derivation produces.
        """
        return resolve_account_ref(self._account_refs_by_slug(), account, self._account_salt())

    def _account_refs_by_canonical_id(self) -> dict[str, str]:
        """Canonical account id → the existing public account reference.

        Reporting card rows now carry the same ``accounts.account_id`` identity used
        throughout the application. Resolve that id through the accounts dimension
        while reusing ``account_ref_map`` for the actual salted reference, so card
        exports join to ``monthly_trend.csv`` and other account-bearing surfaces.
        """
        if self._account_refs_by_id is None:
            refs_by_slug = self._account_refs_by_slug()
            with session_scope(self.db_path) as session:
                rows = session.query(
                    AccountDB.institution,
                    AccountDB.account_number,
                ).all()
            self._account_refs_by_id = {
                account_identity(str(institution), str(account_slug)): refs_by_slug[
                    str(account_slug)
                ]
                for institution, account_slug in rows
                if str(account_slug) in refs_by_slug
            }
        return self._account_refs_by_id

    def _card_account_ref(self, account_id: str) -> tuple[str, str]:
        """Resolve one canonical card id without exposing it in the export."""
        return resolve_account_ref(
            self._account_refs_by_canonical_id(),
            account_id,
            self._account_salt(),
        )

    def _share_safe_card_summary(
        self,
        df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Replace canonical card ids with opaque refs and bucket their money.

        ``ReportSnapshotService`` already emits one row per canonical account id.
        Preserve those rows rather than regrouping on display metadata: naming
        changes remain one card, and two cards with the same type remain separate.
        The raw id and descriptive identity columns never cross the share-safe ZIP
        boundary. A canonical id absent from ``accounts`` receives the shared
        unresolved namespace and an explicit status.
        """
        private_columns = ["account_id", "institution", "account_type"]
        if df.empty:
            return df.drop(columns=private_columns, errors="ignore")
        if "account_id" not in df.columns:
            raise ValueError("card summary is missing canonical account_id")
        work = df.copy()
        labelled = [
            self._card_account_ref(str(account_id or "")) for account_id in work["account_id"]
        ]
        work["account_ref"] = [ref for ref, _status in labelled]
        work["account_ref_status"] = [status for _ref, status in labelled]
        result = work.drop(columns=private_columns, errors="ignore")
        result = result[["account_ref", "account_ref_status", "transaction_count", "net", "volume"]]
        return self._bucket_columns(result, ["net", "volume"])

    @staticmethod
    def _safe_to_csv(df: pd.DataFrame, path: Path) -> None:
        """Write a DataFrame to CSV with formula-injection defused.

        PDF-derived merchant/description text can begin with = + - @ (or a control
        char) and execute as a formula when the CSV is opened in Excel/Sheets.
        df.map(neutralize_csv_cell) prefixes a single quote on such string cells;
        numbers/Decimals/non-strings pass through unchanged (amount columns intact).
        """
        with private_atomic_text(path) as handle:
            df.map(neutralize_csv_cell).to_csv(handle, index=False)

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        class _DecimalEncoder(json.JSONEncoder):
            def default(self, o: Any) -> Any:
                # Canonical hook: Decimal -> exact str ("0.10" not 0.1), dates/Path too.
                converted = decimal_json(o)
                return converted if converted is not o else super().default(o)

        with (
            private_atomic_text(path) as handle,
        ):
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True, cls=_DecimalEncoder) + "\n")

    # Money-valued columns emitted by ``_group`` (everything but the group keys
    # and ``transaction_count``). Bucketed in share-safe mode.
    # Note: ``spending`` is an alias of ``gross_spend`` (set on line below the
    # groupby agg); both are listed here so the bucketing loop is exhaustive and
    # neither copy can slip through un-bucketed.
    _GROUP_MONEY_COLUMNS = [
        "income",
        "spending",
        "gross_spend",
        "offsets",
        "true_cost",
        "net",
    ]

    @staticmethod
    def _dec_sum(values) -> Decimal:
        """Sum an iterable of float/Decimal values exactly using Decimal arithmetic."""
        return sum((Decimal(str(v)) for v in values), Decimal("0"))

    @staticmethod
    def _norm_currency(currency: Any) -> str | None:
        """Normalize NULL/NaN currency to match the public CAD finance policy.

        The SQL predicate treats ``currency IS NULL`` (and ``""``) as CAD. But when a
        NULL currency is loaded into a pandas string column mixed with real currencies,
        pandas coerces ``None`` to a float ``nan``, which is not a CAD policy value --
        so an in-memory mask would DROP the row that SQL keeps. Coerce that NaN/None back
        to ``None`` (which the in-memory policy treats as legacy CAD) so the
        in-memory mask stays in lockstep with ``cad_sql_filters``.
        """
        if currency is None or (isinstance(currency, float) and pd.isna(currency)):
            return None
        return str(currency)

    @classmethod
    def _spend_mask(cls, df: pd.DataFrame) -> pd.Series:
        """Boolean row mask for the spend universe, from columns already present.

        Uses ``is_spend_row`` (the in-memory mirror of ``spend_sql_filters``) so
        the export's aggregate spend counts the
        SAME rows the dashboard aggregates do: expense, include_in_cashflow,
        not exclude_from_spending, CAD. Only the AGGREGATE spend columns use this
        -- the detail transaction rows stay complete.
        """
        return df.apply(
            lambda row: is_spend_row(
                amount_signed=Decimal(str(row["amount"])),
                cashflow_type=str(row["cashflow_type"]),
                include_in_cashflow=bool(row["include_in_cashflow"]),
                exclude_from_spending=bool(row["exclude_from_spending"]),
                currency=cls._norm_currency(row["currency"]),
            ),
            axis=1,
        )

    @classmethod
    def _income_mask(cls, df: pd.DataFrame) -> pd.Series:
        """Boolean row mask matching the canonical CAD income policy."""
        return df.apply(
            lambda row: is_income_row(
                amount_signed=Decimal(str(row["amount"])),
                cashflow_type=str(row["cashflow_type"]),
                include_in_cashflow=bool(row["include_in_cashflow"]),
                currency=cls._norm_currency(row["currency"]),
            ),
            axis=1,
        )

    @classmethod
    def _group(
        cls,
        df: pd.DataFrame,
        columns: list[str],
        name: str,
        *,
        share_safe: bool = False,
    ) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(
                columns=[
                    *columns,
                    "transaction_count",
                    "income",
                    "spending",
                    "gross_spend",
                    "offsets",
                    "true_cost",
                    "net",
                ]
            )
        # Aggregate money uses the strict canonical income/spend policies. Detail
        # rows remain complete, but excluded, non-cashflow, foreign-currency,
        # zero, and invalid-sign rows cannot alter income, spend, or net.
        work = df.copy()
        income_mask = cls._income_mask(work)
        spend_mask = cls._spend_mask(work)
        work["_income_amount"] = [
            Decimal(str(amount)) if in_universe else Decimal("0")
            for amount, in_universe in zip(work["amount"], income_mask, strict=True)
        ]
        work["_spend_amount"] = [
            Decimal(str(amount)) if in_universe else Decimal("0")
            for amount, in_universe in zip(work["amount"], spend_mask, strict=True)
        ]
        work["_spend_offset"] = [
            Decimal(str(offset)) if in_universe else Decimal("0")
            for offset, in_universe in zip(work["true_cost_offset"], spend_mask, strict=True)
        ]
        work["_finance_amount"] = [
            income_amount + spend_amount
            for income_amount, spend_amount in zip(
                work["_income_amount"], work["_spend_amount"], strict=True
            )
        ]
        grouped = work.groupby(columns, dropna=False).agg(
            transaction_count=("txn_id", "count"),
            income=("_income_amount", lambda values: cls._dec_sum(values)),
            gross_spend=(
                "_spend_amount",
                lambda values: abs(cls._dec_sum(values)),
            ),
            offsets=("_spend_offset", lambda values: cls._dec_sum(values)),
            net=("_finance_amount", lambda values: cls._dec_sum(values)),
        )
        grouped["spending"] = grouped["gross_spend"]
        grouped["true_cost"] = (grouped["gross_spend"] - grouped["offsets"]).clip(
            lower=Decimal("0")
        )
        result = grouped.reset_index().sort_values("true_cost", ascending=False)
        result.insert(0, "summary", name)
        if share_safe:
            # Bucket every money column so a single-transaction group cannot
            # leak that transaction's exact amount. transaction_count stays raw.
            result = cls._bucket_columns(result, cls._GROUP_MONEY_COLUMNS)
        return result

    # Money-valued keys emitted by ``_summary`` (bucketed in share-safe mode).
    _SUMMARY_MONEY_KEYS = (
        "income",
        "spending",
        "gross_spend",
        "offsets",
        "true_cost",
        "net",
    )

    @classmethod
    def _summary(
        cls,
        df: pd.DataFrame,
        preset: str,
        include_transfers: bool,
        *,
        share_safe: bool = False,
    ) -> pd.DataFrame:
        # The three basis rows travel with every package: the income/net figures
        # are GROSS (no offset or income-treatment netting), while offsets and
        # true_cost adopt the treatment-netted vocabulary on the spend side only.
        # A recipient pairing income with true_cost would otherwise mix bases
        # with nothing in the zip saying so; Overview's adjusted income differs.
        basis = {
            "income_basis": (
                "gross cashflow income; no offset or income-treatment netting "
                "(the dashboard's adjusted income nets both and will differ)"
            ),
            "spend_basis": "gross_spend is policy spending; true_cost = gross_spend - offsets",
            "net_basis": "income - gross_spend, both gross",
        }
        if df.empty:
            data = {
                "preset": preset,
                "include_transfers": include_transfers,
                **basis,
                "transaction_count": 0,
                "income": Decimal("0"),
                "spending": Decimal("0"),
                "gross_spend": Decimal("0"),
                "offsets": Decimal("0"),
                "true_cost": Decimal("0"),
                "net": Decimal("0"),
            }
        else:
            amounts = df["amount"]
            # Aggregate money follows the same strict policies as SQL reporting.
            income_mask = cls._income_mask(df)
            spend_mask = cls._spend_mask(df)
            offsets = cls._dec_sum(
                Decimal(str(value))
                for value, in_universe in zip(df["true_cost_offset"], spend_mask, strict=True)
                if in_universe
            )
            income = cls._dec_sum(
                Decimal(str(value))
                for value, in_universe in zip(amounts, income_mask, strict=True)
                if in_universe
            )
            gross_spend = abs(
                cls._dec_sum(
                    Decimal(str(value))
                    for value, in_universe in zip(amounts, spend_mask, strict=True)
                    if in_universe
                )
            )
            net = income - gross_spend
            data = {
                "preset": preset,
                "include_transfers": include_transfers,
                **basis,
                "transaction_count": len(df),
                "income": income,
                "spending": gross_spend,
                "gross_spend": gross_spend,
                "offsets": offsets,
                "true_cost": max(gross_spend - offsets, Decimal("0")),
                "net": net,
            }
        if share_safe:
            # Bucket every money value; a single-transaction export otherwise
            # emits that transaction's exact amount here. Counts/flags stay raw.
            for key in cls._SUMMARY_MONEY_KEYS:
                data[key] = cls._bucket_amount(data[key], _AGG_BUCKET)
        return pd.DataFrame.from_dict(data, orient="index", columns=["value"])

    def _recurring_rows(
        self,
        start_date: date | None,
        end_date: date | None,
        *,
        redact: bool = False,
    ) -> pd.DataFrame:
        with session_scope(self.db_path) as session:
            query = session.query(RecurringPatternDB)
            if start_date:
                # An ACTIVE pattern is a live commitment whatever its last charge
                # date — requiring last_occurrence inside the window dropped every
                # active bill that had not recharged yet in a young export window
                # (the readiness figures had the same predicate defect). Retired
                # patterns still need an in-window occurrence to be relevant.
                query = query.filter(
                    (RecurringPatternDB.is_active == True)  # noqa: E712
                    | (RecurringPatternDB.last_occurrence.is_(None))
                    | (RecurringPatternDB.last_occurrence >= start_date)
                )
            if end_date:
                query = query.filter(
                    (RecurringPatternDB.first_occurrence.is_(None))
                    | (RecurringPatternDB.first_occurrence <= end_date)
                )
            rows = query.order_by(RecurringPatternDB.expected_amount.desc()).all()
            records: list[dict[str, Any]] = []
            for row in rows:
                if redact:
                    # Share-safe: drop the merchant/payee string entirely. A
                    # recurring "merchant" is frequently a personal payee (Zelle/
                    # PayPal transfer to a person) and the alphabetic name survives
                    # any digit/punctuation stripping, so no canonicalized form is
                    # safe to emit. Category + bucketed amount + cadence stay
                    # useful and leak no payee. Institution dropped; expected_amount
                    # bucketed to nearest $5.
                    record = {
                        "pattern_type": row.pattern_type,
                        "category": row.category,
                        "frequency": row.frequency,
                        "expected_amount_bucket": self._bucket_amount(row.expected_amount),
                        "confidence": float(row.confidence),
                        "occurrence_count": row.occurrence_count,
                        "is_active": bool(row.is_active),
                    }
                else:
                    record = {
                        "pattern_type": row.pattern_type,
                        "merchant": row.merchant,
                        "category": row.category,
                        "institution": row.institution,
                        "frequency": row.frequency,
                        "expected_amount": float(row.expected_amount),
                        "confidence": float(row.confidence),
                        "occurrence_count": row.occurrence_count,
                        "is_active": bool(row.is_active),
                    }
                records.append(record)
            return pd.DataFrame(records)

    @staticmethod
    def _bucket_amount(amount: Any, bucket: Decimal = _RECURRING_BUCKET) -> float:
        """Round an amount to the nearest bucket so the exact value never leaks.

        Defaults to the recurring expected-amount bucket ($5); pass ``_AGG_BUCKET``
        for the coarser share-safe aggregate bucket.
        """
        value = Decimal(str(amount))
        bucketed = (value / bucket).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * bucket
        return float(bucketed)

    @classmethod
    def _bucket_columns(
        cls, df: pd.DataFrame, columns: list[str], bucket: Decimal = _AGG_BUCKET
    ) -> pd.DataFrame:
        """Return a copy of ``df`` with the given money columns bucketed.

        Only the named money columns are rounded; counts, percentages, and
        non-numeric columns are left untouched. Used to redact share-safe
        aggregate outputs so a single-row aggregate cannot leak an exact
        per-transaction amount.
        """
        if df.empty:
            return df
        result = df.copy()
        for column in columns:
            if column in result.columns:
                result[column] = result[column].map(lambda value: cls._bucket_amount(value, bucket))
        return result

    def _record_run(
        self,
        result: ExportPackageResult,
        *,
        start_date: date | None,
        end_date: date | None,
        include_transfers: bool,
    ) -> None:
        with session_scope(self.db_path) as session:
            session.add(
                ExportRunDB(
                    created_at=naive_utc_now(),
                    preset=result.preset,
                    export_format=result.export_format,
                    output_path=str(result.path),
                    rows_exported=result.rows_exported,
                    status="success",
                    details={
                        "files": result.files,
                        "period_start": start_date.isoformat() if start_date else None,
                        "period_end": end_date.isoformat() if end_date else None,
                        "include_transfers": include_transfers,
                    },
                )
            )
