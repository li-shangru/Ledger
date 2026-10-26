"""Transactional persistence for parsed investment statement aggregates."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError

from ledger.investments.models import InvestmentStatement
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentCashBalanceDB,
    InvestmentImportRunDB,
    InvestmentPerformanceDB,
    InvestmentPositionDB,
    InvestmentSourceAssociationDB,
    InvestmentSourceFileDB,
    InvestmentStatementDB,
    init_database,
    session_scope,
)

_ACCOUNT_TOTAL_CATEGORY = "Total (All Assets)"
_LEGACY_TOTAL_CATEGORY = "Total"


def _account_performance_total(rows):
    """Select a canonical account total or one unambiguous legacy total.

    Adapters normalize source labels to these semantic roles. Multiple rows for
    the preferred role are ambiguous; neither their order nor a subtotal supplies
    an account total in that case.
    """
    for category in (_ACCOUNT_TOTAL_CATEGORY, _LEGACY_TOTAL_CATEGORY):
        totals = [row for row in rows if row.asset_category == category]
        if totals:
            return totals[0] if len(totals) == 1 else None
    return None


class InvestmentConflictError(ValueError):
    """A same-period statement differs from the stored source-backed aggregate."""


class InvestmentRepository:
    """Own statement aggregates, source provenance, and account lifecycle facts."""

    def __init__(self, db_path: Path, *, initialize: bool = True):
        self.db_path = Path(db_path)
        # Recovery already validated its existing database and must not change
        # schema or the fingerprints that bind a previously reviewed import.
        if initialize:
            init_database(self.db_path)

    def store_statement(
        self,
        statement: InvestmentStatement,
        *,
        source_csv_path: Path | None = None,
        source_csv_sha256: str | None = None,
        source_pdf_path: Path,
        source_pdf_sha256: str,
        source_pdf_role: Literal["pdf", "statement_pdf"] = "pdf",
        source_csv_size: int | None = None,
        source_pdf_size: int | None = None,
        import_run_id: str | None = None,
        replace_conflict: bool = False,
    ) -> Literal["inserted", "unchanged", "replaced"]:
        """Store one complete aggregate, requiring opt-in for changed periods.

        The parsed fingerprint is compared before mutating statement-owned rows.
        Replacement removes rows linked to only the conflicting statement inside
        this transaction, leaving every other account-period untouched.
        A structured statement PDF supplies its own source facts and needs no CSV.
        """
        if (source_csv_path is None) != (source_csv_sha256 is None):
            raise ValueError("Investment CSV source path and hash must be supplied together.")
        if source_pdf_role not in {"pdf", "statement_pdf"}:
            raise ValueError("Investment PDF source role must be pdf or statement_pdf.")
        if source_csv_path is None and source_pdf_role != "statement_pdf":
            raise ValueError("A PDF-only investment source requires the statement_pdf role.")
        now = datetime.now(UTC).replace(tzinfo=None)
        try:
            with session_scope(self.db_path) as session:
                existing = (
                    session.query(InvestmentStatementDB)
                    .filter(
                        InvestmentStatementDB.account_id == statement.account.account_id,
                        InvestmentStatementDB.period_start == statement.period_start,
                        InvestmentStatementDB.period_end == statement.period_end,
                    )
                    .one_or_none()
                )
                self._validate_end_date_snapshot(session, statement, existing)
                if (
                    existing is not None
                    and existing.parsed_fingerprint == statement.parsed_fingerprint
                ):
                    self._validate_source_provenance(
                        session,
                        source_csv_path,
                        source_csv_sha256,
                        source_pdf_path,
                        source_pdf_sha256,
                        existing.statement_id,
                    )
                    if source_csv_path is not None and source_csv_sha256 is not None:
                        self._add_source_file(
                            session,
                            source_csv_path,
                            source_csv_sha256,
                            existing.statement_id,
                            now,
                            source_role="csv",
                            byte_size=source_csv_size,
                            import_run_id=import_run_id,
                            statement_fingerprint=statement.parsed_fingerprint,
                        )
                    self._add_source_file(
                        session,
                        source_pdf_path,
                        source_pdf_sha256,
                        existing.statement_id,
                        now,
                        source_role=source_pdf_role,
                        byte_size=source_pdf_size,
                        import_run_id=import_run_id,
                        statement_fingerprint=statement.parsed_fingerprint,
                    )
                    return "unchanged"
                if existing is not None and not replace_conflict:
                    raise InvestmentConflictError(
                        "Investment statement content conflicts with the stored account-period; "
                        "explicit replacement is required."
                    )

                self._validate_source_provenance(
                    session,
                    source_csv_path,
                    source_csv_sha256,
                    source_pdf_path,
                    source_pdf_sha256,
                    existing.statement_id if existing is not None else None,
                )

                self._upsert_account(session, statement, now)
                result: Literal["inserted", "replaced"] = "inserted"
                reusable_source_hashes: set[str] = set()
                if existing is None:
                    record = InvestmentStatementDB(
                        statement_id=statement.statement_id,
                        account_id=statement.account.account_id,
                        period_start=statement.period_start,
                        period_end=statement.period_end,
                        generated_at=statement.generated_at,
                        base_currency=statement.base_currency,
                        total_nav=statement.total_nav,
                        time_weighted_return=statement.time_weighted_return,
                        parsed_fingerprint=statement.parsed_fingerprint,
                        imported_at=now,
                    )
                    session.add(record)
                else:
                    result = "replaced"
                    reusable_source_hashes = self._supersede_statement_sources(
                        session,
                        existing.statement_id,
                        statement.statement_id,
                        now,
                    )
                    self._delete_statement_children(session, existing.statement_id)
                    existing.statement_id = statement.statement_id
                    existing.account_id = statement.account.account_id
                    existing.period_start = statement.period_start
                    existing.period_end = statement.period_end
                    existing.generated_at = statement.generated_at
                    existing.base_currency = statement.base_currency
                    existing.total_nav = statement.total_nav
                    existing.time_weighted_return = statement.time_weighted_return
                    existing.parsed_fingerprint = statement.parsed_fingerprint
                    existing.imported_at = now

                session.flush()
                self._add_children(session, statement)
                if source_csv_path is not None and source_csv_sha256 is not None:
                    self._add_source_file(
                        session,
                        source_csv_path,
                        source_csv_sha256,
                        statement.statement_id,
                        now,
                        source_role="csv",
                        byte_size=source_csv_size,
                        import_run_id=import_run_id,
                        statement_fingerprint=statement.parsed_fingerprint,
                        reusable_source=source_csv_sha256 in reusable_source_hashes,
                    )
                self._add_source_file(
                    session,
                    source_pdf_path,
                    source_pdf_sha256,
                    statement.statement_id,
                    now,
                    source_role=source_pdf_role,
                    byte_size=source_pdf_size,
                    import_run_id=import_run_id,
                    statement_fingerprint=statement.parsed_fingerprint,
                    reusable_source=source_pdf_sha256 in reusable_source_hashes,
                )
                return result
        except IntegrityError as exc:
            if _is_investment_period_conflict(exc):
                raise InvestmentConflictError(
                    "Investment statement content conflicts with the stored account-period; "
                    "explicit replacement is required."
                ) from None
            raise

    def set_account_lifecycle(
        self,
        alias: str,
        status: Literal["active", "closed"],
        status_confirmed_on: date,
        transfer_destination: str | None,
        closed_on: date | None = None,
    ) -> None:
        """Set explicit lifecycle facts for a known account alias."""
        if status not in {"active", "closed"}:
            raise ValueError("Investment account status must be active or closed.")
        if status == "closed" and (
            transfer_destination is None or not transfer_destination.strip()
        ):
            raise ValueError("A closed investment account requires a transfer destination.")
        normalized_destination = (
            transfer_destination.strip() if transfer_destination is not None else None
        )
        with session_scope(self.db_path) as session:
            account = (
                session.query(InvestmentAccountDB)
                .filter(InvestmentAccountDB.account_alias == alias)
                .one_or_none()
            )
            if account is None:
                raise ValueError("No investment account exists for the supplied alias.")
            account.status = status
            account.status_confirmed_on = status_confirmed_on
            account.transfer_destination = normalized_destination
            account.closed_on = closed_on
            account.updated_at = datetime.now(UTC).replace(tzinfo=None)

    def conflict_delta(self, statement: InvestmentStatement) -> tuple[dict, dict] | None:
        """Return private binding material and a value-free structural review summary."""
        with session_scope(self.db_path) as session:
            existing = (
                session.query(InvestmentStatementDB)
                .filter(
                    InvestmentStatementDB.account_id == statement.account.account_id,
                    InvestmentStatementDB.period_start == statement.period_start,
                    InvestmentStatementDB.period_end == statement.period_end,
                )
                .one_or_none()
            )
            if existing is None or existing.parsed_fingerprint == statement.parsed_fingerprint:
                return None
            existing_counts = {
                "asset_balances": session.query(InvestmentAssetBalanceDB)
                .filter(InvestmentAssetBalanceDB.statement_id == existing.statement_id)
                .count(),
                "positions": session.query(InvestmentPositionDB)
                .filter(InvestmentPositionDB.statement_id == existing.statement_id)
                .count(),
                "cash_balances": (
                    session.query(InvestmentCashBalanceDB)
                    .filter(InvestmentCashBalanceDB.statement_id == existing.statement_id)
                    .count()
                    if _cash_balances_available(session)
                    else 0
                ),
                "performance": session.query(InvestmentPerformanceDB)
                .filter(InvestmentPerformanceDB.statement_id == existing.statement_id)
                .count(),
                "activities": session.query(InvestmentActivityDB)
                .filter(InvestmentActivityDB.statement_id == existing.statement_id)
                .count(),
            }
            incoming_counts = {
                "asset_balances": len(statement.asset_balances),
                "cash_balances": len(statement.cash_balances),
                "positions": len(statement.positions),
                "performance": len(statement.performance),
                "activities": len(statement.activities),
            }
            existing_performance = (
                session.query(InvestmentPerformanceDB)
                .filter(InvestmentPerformanceDB.statement_id == existing.statement_id)
                .all()
            )
            existing_total = _account_performance_total(existing_performance)
            incoming_total = _account_performance_total(statement.performance)
            private = {
                "account_id": statement.account.account_id,
                "period_start": statement.period_start.isoformat(),
                "period_end": statement.period_end.isoformat(),
                "existing_fingerprint": existing.parsed_fingerprint,
                "incoming_fingerprint": statement.parsed_fingerprint,
            }
            structure = {
                name: {
                    "before": existing_counts[name],
                    "after": incoming_counts[name],
                    "delta": incoming_counts[name] - existing_counts[name],
                }
                for name in existing_counts
            }
            summary = {
                "structure": structure,
                "value_changes": {
                    "total_nav": _change_direction(existing.total_nav, statement.total_nav),
                    "statement_return": _change_direction(
                        existing.time_weighted_return, statement.time_weighted_return
                    ),
                    "realized_pl": _change_direction(
                        existing_total.realized_total if existing_total else None,
                        incoming_total.realized_total if incoming_total else None,
                    ),
                    "unrealized_pl": _change_direction(
                        existing_total.unrealized_total if existing_total else None,
                        incoming_total.unrealized_total if incoming_total else None,
                    ),
                },
            }
            return private, summary

    def create_import_run(
        self,
        *,
        run_id: str,
        source_path: Path,
        private_manifest_path: Path,
        baseline_fingerprint: dict[str, Any],
        lifecycle_inputs: dict[str, Any],
        source_count: int,
        structured_source_count: int,
        evidence_source_count: int,
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_scope(self.db_path) as session:
            session.add(
                InvestmentImportRunDB(
                    run_id=run_id,
                    started_at=now,
                    updated_at=now,
                    status="staging",
                    source_path=str(source_path),
                    private_manifest_path=str(private_manifest_path),
                    baseline_fingerprint=baseline_fingerprint,
                    statements_imported=0,
                    source_count=source_count,
                    structured_source_count=structured_source_count,
                    evidence_source_count=evidence_source_count,
                    lifecycle_inputs=lifecycle_inputs,
                    errors=0,
                )
            )

    def update_import_run(self, run_id: str, status: str, **values: Any) -> None:
        """Persist a redacted run transition in the database that owns the run."""
        now = datetime.now(UTC).replace(tzinfo=None)
        timestamp_fields = {
            "staged": "staged_at",
            "finalizing": "finalizing_at",
            "archive_pending": "archive_pending_at",
            "finalized": "finalized_at",
            "cancelled": "cancelled_at",
            "failed": "failed_at",
        }
        with session_scope(self.db_path) as session:
            run = session.get(InvestmentImportRunDB, run_id)
            if run is None:
                raise RuntimeError("Investment import run provenance is missing.")
            run.status = status
            run.updated_at = now
            if field := timestamp_fields.get(status):
                setattr(run, field, now)
            for key, value in values.items():
                if not hasattr(run, key):
                    raise ValueError("Unknown investment import run provenance field.")
                setattr(run, key, value)

    def update_run_sources(self, run_id: str, status: str) -> None:
        """Advance every source already owned by a run to a redacted terminal state."""
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_scope(self.db_path) as session:
            sources = (
                session.query(InvestmentSourceFileDB)
                .filter(InvestmentSourceFileDB.import_run_id == run_id)
                .all()
            )
            for source in sources:
                source.status = status
                source.updated_at = now
            associations = (
                session.query(InvestmentSourceAssociationDB)
                .filter(InvestmentSourceAssociationDB.import_run_id == run_id)
                .all()
            )
            for association in associations:
                association.status = status
                association.updated_at = now

    def record_evidence_source(
        self,
        *,
        source_path: Path,
        file_hash: str,
        byte_size: int,
        import_run_id: str,
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_scope(self.db_path) as session:
            self._add_source_file(
                session,
                source_path,
                file_hash,
                None,
                now,
                source_role="evidence_pdf",
                byte_size=byte_size,
                import_run_id=import_run_id,
                statement_fingerprint=None,
            )

    def update_source_archive(
        self,
        *,
        file_hash: str,
        archived_path: Path,
        finalized: bool,
        import_run_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        with session_scope(self.db_path) as session:
            source = session.get(InvestmentSourceFileDB, file_hash)
            if source is None:
                raise RuntimeError("Investment source provenance is missing.")
            source.archived_path = str(archived_path)
            source.archive_status = "archived"
            source.archived_at = now
            source.updated_at = now
            if finalized:
                source.status = "finalized"
                source.finalized_at = now
            associations_query = session.query(InvestmentSourceAssociationDB).filter(
                InvestmentSourceAssociationDB.file_hash == file_hash
            )
            if import_run_id is not None:
                associations_query = associations_query.filter(
                    InvestmentSourceAssociationDB.import_run_id == import_run_id
                )
            associations = associations_query.all()
            if not associations:
                raise RuntimeError("Investment source association provenance is missing.")
            for association in associations:
                association.archived_path = str(archived_path)
                association.archive_status = "archived"
                association.archived_at = now
                association.updated_at = now
                if finalized:
                    association.status = "finalized"
                    association.finalized_at = now

    @staticmethod
    def _validate_end_date_snapshot(session, statement: InvestmentStatement, existing) -> None:
        """Allow overlapping reports only when their end-date snapshot is identical."""
        overlaps = (
            session.query(InvestmentStatementDB)
            .filter(
                InvestmentStatementDB.account_id == statement.account.account_id,
                InvestmentStatementDB.period_end == statement.period_end,
            )
            .all()
        )
        incoming = _statement_snapshot_signature(statement)
        for overlap in overlaps:
            if existing is not None and overlap.statement_id == existing.statement_id:
                continue
            if _persisted_snapshot_signature(session, overlap) != incoming:
                raise InvestmentConflictError(
                    "Investment end-date snapshot conflicts with another report for the account."
                )

    @staticmethod
    def _upsert_account(session, statement: InvestmentStatement, now: datetime) -> None:
        account = session.get(InvestmentAccountDB, statement.account.account_id)
        if account is None:
            session.add(
                InvestmentAccountDB(
                    account_id=statement.account.account_id,
                    institution=statement.account.institution,
                    source_account_id=statement.account.source_account_id,
                    account_alias=statement.account.account_alias,
                    account_type=statement.account.account_type,
                    base_currency=statement.account.base_currency,
                    status=statement.account.status,
                    transfer_destination=statement.account.transfer_destination,
                    status_confirmed_on=statement.account.status_confirmed_on,
                    closed_on=statement.account.closed_on,
                    first_statement_date=statement.period_start,
                    last_statement_date=statement.period_end,
                    created_at=now,
                    updated_at=now,
                )
            )
            return

        account.institution = statement.account.institution
        account.source_account_id = statement.account.source_account_id
        account.account_alias = statement.account.account_alias
        account.account_type = statement.account.account_type
        account.base_currency = statement.account.base_currency
        account.first_statement_date = min(
            value
            for value in (account.first_statement_date, statement.period_start)
            if value is not None
        )
        account.last_statement_date = max(
            value
            for value in (account.last_statement_date, statement.period_end)
            if value is not None
        )
        account.updated_at = now

    @staticmethod
    def _delete_statement_children(session, statement_id: str) -> None:
        for model in (
            InvestmentActivityDB,
            InvestmentPerformanceDB,
            InvestmentPositionDB,
            InvestmentAssetBalanceDB,
            InvestmentCashBalanceDB,
        ):
            if model is InvestmentCashBalanceDB and not _cash_balances_available(session):
                continue
            session.query(model).filter(model.statement_id == statement_id).delete(
                synchronize_session=False
            )

    @staticmethod
    def _supersede_statement_sources(
        session,
        old_statement_id: str,
        new_statement_id: str,
        now: datetime,
    ) -> set[str]:
        """Detach the current projection while retaining every historical association."""
        associations = (
            session.query(InvestmentSourceAssociationDB)
            .filter(
                InvestmentSourceAssociationDB.statement_id == old_statement_id,
                InvestmentSourceAssociationDB.superseded_at.is_(None),
            )
            .all()
        )
        for association in associations:
            association.superseded_by_statement_id = new_statement_id
            association.superseded_at = now
            association.updated_at = now
        sources = (
            session.query(InvestmentSourceFileDB)
            .filter(InvestmentSourceFileDB.statement_id == old_statement_id)
            .all()
        )
        reusable = {source.file_hash for source in sources}
        for source in sources:
            source.statement_id = None
            source.updated_at = now
        session.flush()
        return reusable

    @staticmethod
    def _validate_source_provenance(
        session,
        csv_path: Path | None,
        csv_hash: str | None,
        pdf_path: Path,
        pdf_hash: str,
        replaceable_statement_id: str | None,
    ) -> None:
        """Reject source reuse outside this aggregate without exposing paths."""
        hashes = [pdf_hash] if csv_hash is None else [csv_hash, pdf_hash]
        paths = [str(pdf_path)] if csv_path is None else [str(csv_path), str(pdf_path)]
        hash_sources = (
            session.query(InvestmentSourceFileDB)
            .filter(InvestmentSourceFileDB.file_hash.in_(hashes))
            .all()
        )
        path_sources = (
            session.query(InvestmentSourceFileDB)
            .filter(InvestmentSourceFileDB.source_path.in_(paths))
            .all()
        )
        hash_conflict = bool(hash_sources) and (
            replaceable_statement_id is None
            or any(source.statement_id != replaceable_statement_id for source in hash_sources)
        )
        path_conflict = any(
            source.statement_id is not None and source.statement_id != replaceable_statement_id
            for source in path_sources
        )
        if hash_conflict or path_conflict:
            raise InvestmentConflictError(
                "Investment source provenance conflicts with an existing statement."
            )

    @staticmethod
    def _add_children(session, statement: InvestmentStatement) -> None:
        session.add_all(
            [
                InvestmentCashBalanceDB(
                    statement_id=statement.statement_id,
                    **row.model_dump(),
                )
                for row in statement.cash_balances
            ]
        )
        session.add_all(
            [
                InvestmentAssetBalanceDB(
                    statement_id=statement.statement_id,
                    asset_class=row.asset_class,
                    prior_total=row.prior_total,
                    current_long=row.current_long,
                    current_short=row.current_short,
                    current_total=row.current_total,
                    change=row.change,
                )
                for row in statement.asset_balances
            ]
        )
        session.add_all(
            [
                InvestmentPositionDB(
                    statement_id=statement.statement_id,
                    source_row=row.source_row,
                    asset_category=row.asset_category,
                    currency=row.currency,
                    symbol=row.symbol,
                    discriminator=row.discriminator,
                    quantity=row.quantity,
                    multiplier=row.multiplier,
                    cost_price=row.cost_price,
                    cost_basis=row.cost_basis,
                    close_price=row.close_price,
                    market_value=row.market_value,
                    unrealized_pl=row.unrealized_pl,
                )
                for row in statement.positions
            ]
        )
        session.add_all(
            [
                InvestmentPerformanceDB(
                    statement_id=statement.statement_id,
                    source_row=row.source_row,
                    asset_category=row.asset_category,
                    symbol=row.symbol,
                    cost_adjustment=row.cost_adjustment,
                    realized_short_profit=row.realized_short_profit,
                    realized_short_loss=row.realized_short_loss,
                    realized_long_profit=row.realized_long_profit,
                    realized_long_loss=row.realized_long_loss,
                    realized_total=row.realized_total,
                    unrealized_short_profit=row.unrealized_short_profit,
                    unrealized_short_loss=row.unrealized_short_loss,
                    unrealized_long_profit=row.unrealized_long_profit,
                    unrealized_long_loss=row.unrealized_long_loss,
                    unrealized_total=row.unrealized_total,
                    total=row.total,
                    code=row.code,
                )
                for row in statement.performance
            ]
        )
        session.add_all(
            [
                InvestmentActivityDB(
                    statement_id=statement.statement_id,
                    activity_id=row.activity_id,
                    activity_type=row.activity_type.value,
                    activity_date=row.activity_date,
                    activity_time=row.activity_time,
                    symbol=row.symbol,
                    currency=row.currency,
                    quantity=row.quantity,
                    trade_price=row.trade_price,
                    close_price=row.close_price,
                    amount=row.amount,
                    commission_fee=row.commission_fee,
                    basis=row.basis,
                    realized_pl=row.realized_pl,
                    mtm_pl=row.mtm_pl,
                    description=row.description,
                    direction=row.direction,
                    source_row=row.source_row,
                )
                for row in statement.activities
            ]
        )

    @staticmethod
    def _add_source_file(
        session,
        source_path: Path,
        file_hash: str,
        statement_id: str | None,
        now: datetime,
        *,
        source_role: Literal["csv", "pdf", "statement_pdf", "evidence_pdf"],
        byte_size: int | None,
        import_run_id: str | None,
        statement_fingerprint: str | None,
        reusable_source: bool = False,
    ) -> None:
        existing = session.get(InvestmentSourceFileDB, file_hash)
        if existing is None:
            session.add(
                InvestmentSourceFileDB(
                    file_hash=file_hash,
                    source_path=str(source_path),
                    source_role=source_role,
                    original_path=str(source_path),
                    archived_path=None,
                    structured_import=source_role != "evidence_pdf",
                    byte_size=byte_size,
                    archive_status="pending",
                    import_run_id=import_run_id,
                    statement_id=statement_id,
                    discovered_at=now,
                    staged_at=now,
                    archived_at=None,
                    finalized_at=None,
                    updated_at=now,
                    status="staged",
                )
            )
        elif existing.statement_id != statement_id and not (
            reusable_source and existing.statement_id is None
        ):
            raise InvestmentConflictError(
                "Investment source provenance conflicts with an existing statement."
            )
        else:
            existing.source_path = str(source_path)
            existing.source_role = source_role
            existing.original_path = str(source_path)
            existing.archived_path = None
            existing.structured_import = source_role != "evidence_pdf"
            existing.byte_size = byte_size
            existing.archive_status = "pending"
            existing.import_run_id = import_run_id
            existing.statement_id = statement_id
            existing.staged_at = now
            existing.archived_at = None
            existing.finalized_at = None
            existing.updated_at = now
            existing.status = "staged"

        association_id = _source_association_id(
            file_hash=file_hash,
            import_run_id=import_run_id,
            statement_id=statement_id,
            source_path=source_path,
            source_role=source_role,
        )
        association = session.get(InvestmentSourceAssociationDB, association_id)
        # An account-period ID remains stable across revisions. When a source is
        # reparsed, follow its supersession chain instead of reviving an old fact
        # or leaving the replacement without a current association. The derived
        # identity remains stable for retries of that revision.
        while association is not None and association.superseded_at is not None:
            association_id = hashlib.sha256(
                json.dumps(
                    [association_id, association.superseded_at.isoformat()],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            association = session.get(InvestmentSourceAssociationDB, association_id)
        if association is None:
            session.add(
                InvestmentSourceAssociationDB(
                    association_id=association_id,
                    file_hash=file_hash,
                    import_run_id=import_run_id,
                    statement_id=statement_id,
                    statement_fingerprint=statement_fingerprint,
                    source_path=str(source_path),
                    source_role=source_role,
                    original_path=str(source_path),
                    archived_path=None,
                    structured_import=source_role != "evidence_pdf",
                    byte_size=byte_size,
                    archive_status="pending",
                    discovered_at=now,
                    staged_at=now,
                    archived_at=None,
                    finalized_at=None,
                    updated_at=now,
                    status="staged",
                    superseded_by_statement_id=None,
                    superseded_at=None,
                )
            )


def _is_investment_period_conflict(error: IntegrityError) -> bool:
    """Recognize only the database invariant without surfacing database details."""
    return "investment_statements.account_id" in str(error.orig)


def _cash_balances_available(session) -> bool:
    """Treat only a complete historical schema as having unavailable cash rows."""
    connection = session.connection()
    if (
        connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'investment_cash_balances'"
        ).fetchone()
        is not None
    ):
        return True
    from ledger.storage.migrations import is_legacy_investment_cash_schema

    if not is_legacy_investment_cash_schema(connection.connection.driver_connection):
        raise ValueError("Investment cash evidence has an unexpected schema.")
    return False


def _change_direction(before: Decimal | None, after: Decimal | None) -> str:
    if before is None and after is None:
        return "unchanged"
    if before is None:
        return "added"
    if after is None:
        return "removed"
    if after > before:
        return "increased"
    if after < before:
        return "decreased"
    return "unchanged"


def _source_association_id(
    *,
    file_hash: str,
    import_run_id: str | None,
    statement_id: str | None,
    source_path: Path,
    source_role: str,
) -> str:
    """Derive an idempotent identity for one source occurrence in one import context."""
    payload = [
        import_run_id or "direct",
        file_hash,
        statement_id or "evidence",
        str(source_path),
        source_role,
    ]
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _statement_snapshot_signature(statement: InvestmentStatement) -> tuple[Any, ...]:
    assets = tuple(
        sorted(
            (
                row.asset_class,
                row.current_long,
                row.current_short,
                row.current_total,
            )
            for row in statement.asset_balances
        )
    )
    positions = tuple(
        sorted(
            [
                (
                    row.asset_category,
                    row.currency,
                    row.symbol,
                    row.discriminator,
                    row.quantity,
                    row.multiplier,
                    row.cost_price,
                    row.cost_basis,
                    row.close_price,
                    row.market_value,
                    row.unrealized_pl,
                )
                for row in statement.positions
            ],
            key=repr,
        )
    )
    cash = tuple(
        sorted(
            (row.currency, row.closing_balance, row.closing_fx_rate)
            for row in statement.cash_balances
        )
    )
    return statement.base_currency, statement.total_nav, assets, positions, cash


def _persisted_snapshot_signature(session, statement: InvestmentStatementDB) -> tuple[Any, ...]:
    assets = tuple(
        sorted(
            (
                row.asset_class,
                row.current_long,
                row.current_short,
                row.current_total,
            )
            for row in session.query(InvestmentAssetBalanceDB)
            .filter(InvestmentAssetBalanceDB.statement_id == statement.statement_id)
            .all()
        )
    )
    positions = tuple(
        sorted(
            [
                (
                    row.asset_category,
                    row.currency,
                    row.symbol,
                    row.discriminator,
                    row.quantity,
                    row.multiplier,
                    row.cost_price,
                    row.cost_basis,
                    row.close_price,
                    row.market_value,
                    row.unrealized_pl,
                )
                for row in session.query(InvestmentPositionDB)
                .filter(InvestmentPositionDB.statement_id == statement.statement_id)
                .all()
            ],
            key=repr,
        )
    )
    cash = tuple(
        sorted(
            (row.currency, row.closing_balance, row.closing_fx_rate)
            for row in session.query(InvestmentCashBalanceDB)
            .filter(InvestmentCashBalanceDB.statement_id == statement.statement_id)
            .all()
        )
    )
    return statement.base_currency, statement.total_nav, assets, positions, cash
