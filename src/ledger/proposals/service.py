"""Store deferred merchant-rule intent and apply it ledger-first exactly once."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.config.repository import (
    SQLiteConfigRepository,
    _canonicalize_category_memberships,
    _validate_configs_in_memory,
)
from ledger.config.schemas import (
    CategoriesConfig,
    MerchantRule,
    MerchantRulesConfig,
    merchant_match_texts,
)
from ledger.core.enums import Confidence
from ledger.core.results import RunResult
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.proposals.database import proposal_session
from ledger.proposals.planning import PlanningProposalMixin
from ledger.rules.categorization import upsert_merchant_rule
from ledger.storage.migrations import invalidate_db_caches
from ledger.storage.operations import backup_database, record_db_operation
from ledger.storage.snapshots import (
    DatabaseWriteCommittedError,
    audited_database_write_connection,
    database_write_outcome,
    restore_database,
)
from ledger.utils.dates import naive_utc_now
from ledger.utils.hashing import stable_id
from ledger.utils.json_io import dumps_json_column
from ledger.utils.private_files import PinnedPrivateFile, pin_existing_directory

PROPOSAL_TYPE = "merchant_rule"
PROPOSAL_STATUSES = frozenset({"pending", "applied", "rejected"})
_EVIDENCE_ID_PATTERN = re.compile(r"[0-9a-f]{40}")
_PROPOSAL_COLUMNS = (
    "proposal_id, proposal_type, status, merchant_norm, pattern, category, evidence_id, "
    "created_at, applied_at, rejected_at"
)


class ProposalRestoreError(RuntimeError):
    """A failed proposal write could not be restored from its retained backup."""

    def __init__(self, mutation_error: BaseException):
        super().__init__("Proposal apply failed and its pre-write backup could not be restored")
        self.mutation_error = mutation_error


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    proposal_type: str
    status: str
    merchant_norm: str
    pattern: str
    category: str
    evidence_id: str
    created_at: datetime
    applied_at: datetime | None
    rejected_at: datetime | None


def _stored_datetime(value: object, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise sqlite3.DatabaseError("Proposal store contains an invalid timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise sqlite3.DatabaseError("Proposal store contains an invalid timestamp") from None


def _proposal(row: sqlite3.Row) -> Proposal:
    created_at = _stored_datetime(row["created_at"])
    assert created_at is not None
    return Proposal(
        proposal_id=str(row["proposal_id"]),
        proposal_type=str(row["proposal_type"]),
        status=str(row["status"]),
        merchant_norm=str(row["merchant_norm"]),
        pattern=str(row["pattern"]),
        category=str(row["category"]),
        evidence_id=str(row["evidence_id"]),
        created_at=created_at,
        applied_at=_stored_datetime(row["applied_at"], nullable=True),
        rejected_at=_stored_datetime(row["rejected_at"], nullable=True),
    )


def _proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT {_PROPOSAL_COLUMNS} FROM proposals WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise KeyError("Proposal not found")
    return row


def _strict_text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{field} is invalid") from None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field} is invalid")
    return normalized


def _absolute(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _failed_apply(
    *,
    status: str,
    started_at: datetime,
    errors: list[str],
    details: dict[str, Any] | None = None,
    artifacts: dict[str, Path | str] | None = None,
) -> RunResult:
    return RunResult(
        status=status,
        started_at=started_at,
        finished_at=naive_utc_now(),
        errors=errors,
        details=details or {},
        artifacts=artifacts or {},
    )


def _proposal_apply_audit_exists(ledger_db: Path, idempotency_key: str) -> bool:
    """Check one stable audit key in SQL, without materializing the audit trail."""
    uri = f"{ledger_db.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            """
            select 1
            from db_operation_audit
            where operation = 'proposal_apply'
              and status = 'success'
              and json_extract(details, '$.idempotency_key') = ?
            limit 1
            """,
            (idempotency_key,),
        ).fetchone()
    return row is not None


def _replace_runtime_config_in_connection(
    connection: sqlite3.Connection,
    merchants: MerchantRulesConfig,
    categories: CategoriesConfig,
) -> None:
    """Connection-aware equivalent of ``replace_from_configs``.

    The ordinary repository owns its transaction. Proposal apply needs config,
    historical rows, and the idempotency audit to commit together, so this narrow
    helper deliberately accepts the already-open canonical writer transaction.
    """
    issues = _validate_configs_in_memory(merchants, categories)
    if issues:
        raise ValueError("Proposal runtime configuration is invalid")
    canonical_categories, _ = _canonicalize_category_memberships(merchants, categories)
    now = datetime.now(UTC).replace(microsecond=0).isoformat()

    connection.execute("delete from merchant_rule_patterns")
    connection.execute("delete from merchant_rules")
    connection.execute("delete from category_merchants")
    connection.execute("delete from category_config")
    connection.executemany(
        """
        insert into config_metadata (key, value) values (?, ?)
        on conflict(key) do update set value = excluded.value
        """,
        [
            ("merchants_version", merchants.version),
            ("categories_version", canonical_categories.version),
            ("categories_default", canonical_categories.default_category),
            ("config_imported_at", now),
        ],
    )

    for rule_order, rule in enumerate(merchants.rules):
        cursor = connection.execute(
            """
            insert into merchant_rules (
                merchant_norm, category, exclude_from_spending, sort_order
            ) values (?, ?, ?, ?)
            """,
            (
                rule.merchant_norm,
                rule.category,
                int(rule.exclude_from_spending),
                rule_order,
            ),
        )
        if cursor.lastrowid is None:  # pragma: no cover - SQLite INSERT invariant
            raise sqlite3.DatabaseError("Merchant rule insert returned no identifier")
        rule_id = int(cursor.lastrowid)
        connection.executemany(
            """
            insert into merchant_rule_patterns (rule_id, pattern, sort_order)
            values (?, ?, ?)
            """,
            [
                (rule_id, pattern, pattern_order)
                for pattern_order, pattern in enumerate(rule.patterns)
            ],
        )

    connection.executemany(
        """
        insert into category_config (name, parent, exclude_from_spending, sort_order)
        values (?, ?, ?, ?)
        """,
        [
            (
                category.name,
                category.parent,
                int(category.exclude_from_spending),
                category_order,
            )
            for category_order, category in enumerate(canonical_categories.categories)
        ],
    )
    memberships: list[tuple[str, str, int]] = []
    for category in canonical_categories.categories:
        memberships.extend(
            (category.name, merchant, merchant_order)
            for merchant_order, merchant in enumerate(category.merchants)
        )
    connection.executemany(
        """
        insert into category_merchants (category_name, merchant, sort_order)
        values (?, ?, ?)
        """,
        memberships,
    )


def _apply_rules_in_connection(
    connection: sqlite3.Connection,
    merchants: MerchantRulesConfig,
    categories: CategoriesConfig,
) -> dict[str, int]:
    """Apply the established P6 rule semantics inside the proposal transaction."""
    category_exclusions = {
        category.name: bool(category.exclude_from_spending) for category in categories.categories
    }
    rows = connection.execute(
        """
        select txn_id, merchant_raw, description, merchant, category,
               exclude_from_spending, merchant_confidence, category_confidence
        from transactions
        """
    ).fetchall()
    updated = 0
    changed_merchants: set[str] = set()
    changed_categories: set[str] = set()
    for row in rows:
        merchant, category, merchant_exclusion = merchants.find_merchant(
            *merchant_match_texts(
                str(row["merchant_raw"] or ""),
                str(row["description"] or ""),
            ),
            display_name=str(row["merchant"] or ""),
        )
        if not merchant:
            continue
        if (
            row["category_confidence"] == Confidence.MANUAL
            or row["merchant_confidence"] == Confidence.MANUAL
        ):
            continue
        exclude = bool(merchant_exclusion or category_exclusions.get(category, False))
        if (
            row["merchant"] == merchant
            and row["category"] == category
            and bool(row["exclude_from_spending"]) == exclude
            and row["merchant_confidence"] == Confidence.RULE
            and row["category_confidence"] == Confidence.RULE
        ):
            continue
        connection.execute(
            """
            update transactions
            set merchant = ?, category = ?, exclude_from_spending = ?,
                merchant_confidence = ?, category_confidence = ?
            where txn_id = ?
            """,
            (
                merchant,
                category,
                int(exclude),
                Confidence.RULE,
                Confidence.RULE,
                row["txn_id"],
            ),
        )
        updated += 1
        changed_merchants.add(merchant)
        changed_categories.add(category)

    now = naive_utc_now()
    connection.execute(
        """
        insert into rule_application_runs (
            applied_at, source, status, merchant_profiles_changed,
            categories_changed, transactions_scanned, transactions_updated,
            analytics_refreshed, validation_issues, details
        ) values (?, 'proposal_apply', 'success', ?, ?, ?, ?, 0, 0, ?)
        """,
        (
            now.isoformat(sep=" "),
            len(changed_merchants),
            len(changed_categories),
            len(rows),
            updated,
            dumps_json_column(
                {
                    "changed_merchants": sorted(changed_merchants),
                    "changed_categories": sorted(changed_categories),
                }
            ),
        ),
    )
    return {
        "transactions_scanned": len(rows),
        "transactions_updated": updated,
        "changed_merchants": len(changed_merchants),
        "changed_categories": len(changed_categories),
    }


class ProposalService(PlanningProposalMixin):
    """Proposal-only operations plus the sole ledger-opening apply boundary."""

    def __init__(self, proposals_db: Path):
        self.proposals_db = Path(proposals_db)

    def file_merchant_rule(
        self,
        *,
        merchant_norm: str,
        pattern: str,
        category: str,
        evidence_id: str,
    ) -> Proposal:
        merchant_norm = _strict_text(merchant_norm, field="merchant_norm", max_length=200)
        pattern = _strict_text(pattern, field="pattern", max_length=2000)
        category = _strict_text(category, field="category", max_length=100)
        evidence_id = _strict_text(evidence_id, field="evidence_id", max_length=40)
        if not _EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            raise ValueError("evidence_id is invalid")
        # Reuse the runtime rule validator, including its invalid-regex and
        # nested-unbounded-quantifier refusal, without consulting the ledger.
        validated = MerchantRule(
            merchant_norm=merchant_norm,
            category=category,
            patterns=[pattern],
        )
        now = naive_utc_now()
        proposal_id = str(uuid4())
        proposal = Proposal(
            proposal_id=proposal_id,
            proposal_type=PROPOSAL_TYPE,
            status="pending",
            merchant_norm=validated.merchant_norm,
            pattern=validated.patterns[0],
            category=validated.category,
            evidence_id=evidence_id,
            created_at=now,
            applied_at=None,
            rejected_at=None,
        )
        with proposal_session(self.proposals_db, create=True, writable=True) as connection:
            connection.execute(
                "INSERT INTO proposals "
                "(proposal_id, proposal_type, status, merchant_norm, pattern, category, "
                "evidence_id, created_at, applied_at, rejected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id,
                    proposal.proposal_type,
                    proposal.status,
                    proposal.merchant_norm,
                    proposal.pattern,
                    proposal.category,
                    proposal.evidence_id,
                    proposal.created_at.isoformat(sep=" "),
                    None,
                    None,
                ),
            )
        return proposal

    def list_proposals(self, *, status: str = "pending") -> list[Proposal]:
        if status not in {*PROPOSAL_STATUSES, "all"}:
            raise ValueError("Proposal status is invalid")
        with proposal_session(self.proposals_db, create=False, writable=False) as connection:
            if status == "all":
                rows = connection.execute(
                    f"SELECT {_PROPOSAL_COLUMNS} FROM proposals ORDER BY created_at, proposal_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_PROPOSAL_COLUMNS} FROM proposals WHERE status = ? "
                    "ORDER BY created_at, proposal_id",
                    (status,),
                ).fetchall()
            return [_proposal(row) for row in rows]

    def show_proposal(self, proposal_id: str) -> Proposal:
        proposal_id = _strict_text(proposal_id, field="proposal_id", max_length=36)
        with proposal_session(self.proposals_db, create=False, writable=False) as connection:
            return _proposal(_proposal_row(connection, proposal_id))

    def reject_proposal(self, proposal_id: str) -> Proposal:
        proposal_id = _strict_text(proposal_id, field="proposal_id", max_length=36)
        with proposal_session(self.proposals_db, create=False, writable=True) as connection:
            row = _proposal(_proposal_row(connection, proposal_id))
            if row.status != "pending":
                raise ValueError("Only pending proposals may be rejected")
            rejected_at = naive_utc_now()
            connection.execute(
                "UPDATE proposals SET status = 'rejected', rejected_at = ? WHERE proposal_id = ?",
                (rejected_at.isoformat(sep=" "), proposal_id),
            )
            return replace(row, status="rejected", rejected_at=rejected_at)

    def apply_proposal(
        self,
        proposal_id: str,
        *,
        ledger_db: Path,
        backup_dir: Path | None = None,
    ) -> RunResult:
        """Commit one guarded proposal to the ledger, then mark it applied.

        The success audit and the ledger mutation share one ``BEGIN IMMEDIATE``
        transaction.  Consequently a pending row plus an existing audit key means
        the ledger commit won and only the proposal-store tail needs recovery.
        """
        started_at = naive_utc_now()
        proposal = self.show_proposal(proposal_id)
        if proposal.status == "applied":
            return RunResult(
                status="success",
                started_at=started_at,
                finished_at=naive_utc_now(),
                details={"already_applied": True, "recovered_from_audit": False},
            )
        if proposal.status != "pending":
            return _failed_apply(
                status="invalid_status",
                started_at=started_at,
                errors=["Only a pending proposal may be applied."],
            )

        ledger_db = _absolute(Path(ledger_db))
        idempotency_key = stable_id("proposal_apply", proposal.proposal_id)
        result: RunResult | None = None
        with live_mutation_boundary(ledger_db, operation="proposal_apply"):
            if _proposal_apply_audit_exists(ledger_db, idempotency_key):
                result = RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=naive_utc_now(),
                    details={
                        "idempotency_key": idempotency_key,
                        "proposal_id": proposal.proposal_id,
                        "recovered_from_audit": True,
                    },
                )
            else:
                validation_error, merchants, categories = self._validated_apply_config(
                    proposal,
                    ledger_db,
                )
                if validation_error is not None:
                    return _failed_apply(
                        status="validation_failed",
                        started_at=started_at,
                        errors=[validation_error],
                    )
                assert merchants is not None
                assert categories is not None

                backup: RunResult | None = None
                backup_path: Path | None = None
                try:
                    with (
                        database_write_outcome() as outcome,
                        pin_existing_directory(ledger_db.parent) as parent,
                    ):
                        parent.require_owner_only()
                        with parent.pin_private_file(
                            ledger_db.name,
                            mutable=True,
                        ) as pinned:
                            backup = backup_database(
                                ledger_db,
                                backup_dir=backup_dir,
                                reason="pre-proposal-apply",
                                pinned_source=pinned,
                                record=False,
                            )
                            artifact = backup.artifacts.get("backup")
                            if not backup.ok or artifact is None:
                                return _failed_apply(
                                    status="backup_failed",
                                    started_at=started_at,
                                    errors=[
                                        "Pre-write proposal backup failed; no changes were made."
                                    ],
                                )
                            backup_path = Path(artifact)
                            result = self._commit_ledger_apply(
                                proposal,
                                ledger_db=ledger_db,
                                pinned=pinned,
                                merchants=merchants,
                                categories=categories,
                                backup=backup,
                                backup_path=backup_path,
                                idempotency_key=idempotency_key,
                                started_at=started_at,
                            )
                            # Mark the irreversible boundary immediately after
                            # the canonical writer returns, before either pin
                            # performs its own exit validation.
                            outcome.mark_committed()
                            invalidate_db_caches()
                            pinned.validate()
                except DatabaseWriteCommittedError:
                    return _failed_apply(
                        status="committed_unconfirmed",
                        started_at=started_at,
                        errors=[
                            "The ledger commit may have succeeded; the proposal remains pending "
                            "and its retained audit key makes retry safe."
                        ],
                        details={"committed": True, "idempotency_key": idempotency_key},
                        artifacts=dict(backup.artifacts) if backup is not None else {},
                    )
                except BaseException as mutation_error:
                    if backup_path is not None:
                        try:
                            restore_database(backup_path, ledger_db)
                            invalidate_db_caches()
                        except BaseException as restore_error:
                            raise ProposalRestoreError(mutation_error) from restore_error
                    raise

        assert result is not None
        self._mark_applied(proposal.proposal_id)
        return result

    def _validated_apply_config(
        self,
        proposal: Proposal,
        ledger_db: Path,
    ) -> tuple[str | None, MerchantRulesConfig | None, CategoriesConfig | None]:
        """Reload and validate every stored value against the locked live config."""
        try:
            if proposal.proposal_type != PROPOSAL_TYPE:
                raise ValueError("Proposal type is invalid")
            merchant_norm = _strict_text(
                proposal.merchant_norm,
                field="merchant_norm",
                max_length=200,
            )
            pattern = _strict_text(proposal.pattern, field="pattern", max_length=2000)
            category = _strict_text(proposal.category, field="category", max_length=100)
            evidence_id = _strict_text(
                proposal.evidence_id,
                field="evidence_id",
                max_length=40,
            )
            if not _EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
                raise ValueError("evidence_id is invalid")
            # Revalidate regex/ReDoS posture in case the isolated DB was edited
            # outside the supported filing surface.
            MerchantRule(
                merchant_norm=merchant_norm,
                category=category,
                patterns=[pattern],
            )

            repo = SQLiteConfigRepository(ledger_db, prepare_schema=False)
            merchants = repo.load_merchant_rules()
            categories = repo.load_taxonomy()
            category_names = {item.name for item in categories.categories}
            if category not in category_names:
                raise ValueError("Proposal category is not defined by live runtime config")
            if category in categories.referenced_parent_names():
                raise ValueError("Proposal category is a parent; choose a leaf category")
            updated = upsert_merchant_rule(
                merchants,
                merchant_norm,
                pattern,
                category,
                category_exclusions={
                    item.name: bool(item.exclude_from_spending) for item in categories.categories
                },
            )
            issues = _validate_configs_in_memory(updated, categories)
            if issues:
                raise ValueError("Proposal would make runtime config invalid")
            return None, updated, categories
        except (OSError, sqlite3.Error, ValueError) as exc:
            return str(exc), None, None

    def _commit_ledger_apply(
        self,
        proposal: Proposal,
        *,
        ledger_db: Path,
        pinned: PinnedPrivateFile,
        merchants: MerchantRulesConfig,
        categories: CategoriesConfig,
        backup: RunResult,
        backup_path: Path,
        idempotency_key: str,
        started_at: datetime,
    ) -> RunResult:
        result: RunResult | None = None
        with audited_database_write_connection(
            pinned,
            label="Proposal application target",
        ) as connection:
            existing = connection.execute(
                """
                select 1 from db_operation_audit
                where operation = 'proposal_apply' and status = 'success'
                  and json_extract(details, '$.idempotency_key') = ?
                limit 1
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                result = RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=naive_utc_now(),
                    details={
                        "idempotency_key": idempotency_key,
                        "proposal_id": proposal.proposal_id,
                        "recovered_from_audit": True,
                    },
                )
            else:
                _replace_runtime_config_in_connection(connection, merchants, categories)
                counts = _apply_rules_in_connection(connection, merchants, categories)
                result = RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=naive_utc_now(),
                    counts=counts,
                    artifacts={"backup": backup_path},
                    details={
                        "idempotency_key": idempotency_key,
                        "proposal_id": proposal.proposal_id,
                        "proposal_type": proposal.proposal_type,
                        "evidence_id": proposal.evidence_id,
                        "recovered_from_audit": False,
                    },
                )
                record_db_operation(
                    ledger_db,
                    operation="backup",
                    result=backup,
                    artifact_path=backup_path,
                    connection=connection,
                )
                record_db_operation(
                    ledger_db,
                    operation="proposal_apply",
                    result=result,
                    artifact_path=backup_path,
                    connection=connection,
                )
        assert result is not None
        return result

    def _mark_applied(self, proposal_id: str) -> Proposal:
        """Commit the proposal-store tail only after the ledger commit is durable."""
        with proposal_session(self.proposals_db, create=False, writable=True) as connection:
            row = _proposal(_proposal_row(connection, proposal_id))
            if row.status == "applied":
                return row
            if row.status != "pending":
                raise ValueError("Only a pending proposal may be marked applied")
            applied_at = naive_utc_now()
            connection.execute(
                "UPDATE proposals SET status = 'applied', applied_at = ? WHERE proposal_id = ?",
                (applied_at.isoformat(sep=" "), proposal_id),
            )
            return replace(row, status="applied", applied_at=applied_at)


__all__ = [
    "PROPOSAL_STATUSES",
    "PROPOSAL_TYPE",
    "Proposal",
    "ProposalRestoreError",
    "ProposalService",
]
