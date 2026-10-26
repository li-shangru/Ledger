"""Detect — and where it is safe, repair — drift in a local ledger database.

Every check here corresponds to a defect that survived a fully green gate suite,
because the problem was in *persisted state* rather than in code. Tests cannot catch
a database that was written by a superseded version of the code; a standing
diagnostic can.

Checks never mutate. Repairs run only under ``execute=True``, and only for the subset
where the correct result is unambiguous: de-duplicating identical config rows,
clearing references to rows that no longer exist, and recomputing derived tables from
their sources. Anything requiring a judgement call — reassembling a regex that was
split in half, or recovering transactions that were never imported — is reported with
the remedy, never guessed at.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ledger.core.results import RunResult
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.runtime import runtime_path
from ledger.storage.database import (
    FinancialHealthScoreDB,
    RecurringPatternDB,
    TransactionDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema, validate_database
from ledger.utils.dates import naive_utc_now
from ledger.utils.hashing import UNKNOWN_VERSION, script_version

# A regex fragment produced by splitting a pattern on a comma inside a quantifier:
# `\d{3,4}` becomes `\d{3` and `4}`. Both halves still compile, which is why
# validate_patterns accepted them and the corruption persisted silently.
_ORPHAN_QUANTIFIER_TAIL = re.compile(r"^\s*\d*\s*\}")
_UNCLOSED_QUANTIFIER = re.compile(r"\{\d*\s*$")


@dataclass(frozen=True)
class DoctorFinding:
    """One detected problem, with the remedy spelled out."""

    check: str
    severity: str  # "high" | "medium" | "low"
    summary: str
    count: int
    remedy: str
    repairable: bool = False
    details: list[str] = field(default_factory=list)


def _severity_rank(finding: DoctorFinding) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(finding.severity, 3)


# Columns proven unwritable by STATIC review -- read the code, find no producer --
# never by data emptiness. Healthy lifecycle fields such as
# investment_accounts.closed_on and insight_findings.reviewed_at can remain
# entirely NULL until the corresponding action occurs. Treating emptiness as
# evidence of a dead column would report false positives on a fresh database.
#
# So the invariant runs the other way. Every entry here has ALREADY been dropped from
# the ORM -- migration 20260810_0023 retired all five -- and this check asserts the
# manifest is still TRUE. A finding means the MANIFEST is stale, not that the
# operator's data is wrong:
#
#   * column absent from the table  -> satisfied by removal; nothing to report. This
#     is the steady state on every migrated database.
#   * column present but all-NULL   -> still dead, manifest accurate. Keeps a legacy
#     database that has not yet run 0023 quiet.
#   * column present AND populated  -> a producer appeared, or the column was
#     re-added and is being written. Report it: the drop would discard real data,
#     and whoever revived the column owes this manifest an edit.
#   * table missing entirely        -> the manifest names something uncheckable and
#     must be recurated.
#
# Adding an entry means asserting a column has no producer anywhere. Prove it by
# reading the write sites, not by running a COUNT.
_KNOWN_DEAD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("transactions", "paired_transfer_id"),
    ("transactions", "transfer_confidence"),
    ("transactions", "counterparty_account"),
    ("transactions", "counterparty_institution"),
    ("transactions", "days_since_payday"),
)


# (table, key column, the accounts column that key must resolve against).
#
# Every relation here is DERIVED from, or must agree with, the accounts dimension, so
# an orphan means the dimension drifted from its own sources and rebuilding it is the
# fix. account_lifecycle is deliberately NOT in this tuple -- see below.
_ACCOUNT_RELATIONS: tuple[tuple[str, str, str], ...] = (
    ("account_balances", "account_id", "account_id"),
    ("recurring_patterns", "account_id", "account_id"),
    ("account_statement_credit_limits", "account_slug", "account_number"),
    ("transactions", "account", "account_number"),
    ("transfers", "account", "account_number"),
)

# account_lifecycle is censused SEPARATELY, at low severity, because an orphan here is
# the state the table was designed to reach rather than drift. It is operator-authored
# and carries no ForeignKey (an FK would make `PRAGMA foreign_key_check` read the
# aged-out case as corruption and the read-only server would refuse the database), and
# `save_accounts(replace_all=True)` deletes an account as soon as its last transaction
# ages out -- which is exactly what happens to an account the operator closed. Routed
# through _ACCOUNT_RELATIONS it produced a permanent HIGH whose remedy could never
# clear it: `analysis refresh` derives accounts FROM transactions, and a closed account
# has none left.
_LIFECYCLE_ACCOUNT_RELATION: tuple[str, str, str] = (
    "account_lifecycle",
    "account_id",
    "account_id",
)


class DatabaseDoctor:
    """Diagnose local database drift; optionally repair the unambiguous subset."""

    def __init__(self, db_path: Path, *, audit_runs_dir: Path | None = None):
        self.db_path = db_path
        self.audit_runs_dir = audit_runs_dir

    # -- checks ---------------------------------------------------------------

    def _check_duplicate_merchant_rules(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT merchant_norm, category, COUNT(*) AS n FROM merchant_rules"
                " GROUP BY UPPER(TRIM(merchant_norm)) HAVING n > 1"
            ).fetchall()
        if not rows:
            return None
        return DoctorFinding(
            check="duplicate_merchant_rules",
            severity="high",
            summary="Duplicate merchant rules block every runtime-config save",
            count=len(rows),
            remedy=(
                "Repairable. Keeps the lowest-id rule per merchant and deletes the rest. "
                "Until then, Rules / Settings saves, 'Remember for this merchant', and "
                "optimize categories --execute-safe all fail with ConfigError."
            ),
            repairable=True,
            details=[f"{row[0]} -> {row[1]} ({row[2]} rules)" for row in rows],
        )

    def _check_shattered_patterns(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT p.rule_id, r.merchant_norm, p.pattern FROM merchant_rule_patterns p"
                " JOIN merchant_rules r ON r.id = p.rule_id ORDER BY p.rule_id, p.sort_order"
            ).fetchall()
        broken = [
            (rule_id, merchant, pattern)
            for rule_id, merchant, pattern in rows
            if _ORPHAN_QUANTIFIER_TAIL.match(pattern) or _UNCLOSED_QUANTIFIER.search(pattern)
        ]
        if not broken:
            return None
        return DoctorFinding(
            check="shattered_patterns",
            severity="high",
            summary="Merchant regex patterns look split mid-quantifier",
            count=len(broken),
            remedy=(
                "Not auto-repairable — rejoining a split pattern is a guess. Open Rules / "
                "Settings and restore each pattern by hand; the comma-splitting save bug "
                "that caused this is fixed, so a corrected pattern will now persist."
            ),
            details=[
                f"rule {rule_id} ({merchant}): {pattern!r}" for rule_id, merchant, pattern in broken
            ],
        )

    def _check_schema_drift(self) -> DoctorFinding | None:
        result = validate_database(self.db_path, record=False)
        if result.status == "success":
            return None
        return DoctorFinding(
            check="schema_drift",
            severity="high",
            summary="Live schema does not match the ORM",
            count=len(result.errors),
            remedy="Run `ledger db migrate` to apply pending migrations.",
            details=list(result.errors),
        )

    def _check_parser_version_drift(self) -> DoctorFinding | None:
        # Keep the historical finding key and row column stable. This hash covers
        # core source only; it cannot compare private adapter or profile inputs.
        current = script_version()
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT parser_version, COUNT(*) FROM transactions GROUP BY parser_version"
            ).fetchall()
        total = sum(count for _version, count in rows)
        stale = [
            (version, count)
            for version, count in rows
            if current == UNKNOWN_VERSION
            or not version
            or version == UNKNOWN_VERSION
            or version != current
        ]
        if not stale or not total:
            return None
        stale_rows = sum(count for _version, count in stale)
        return DoctorFinding(
            check="parser_version_drift",
            severity="medium",
            summary=(
                f"{stale_rows:,} of {total:,} transactions have different or unavailable "
                "core-code provenance"
            ),
            count=stale_rows,
            remedy=(
                "The row hash covers Ledger core code, not private adapters or CSV profiles. "
                "Inspect the original inputs and verified import receipt before deciding "
                "whether reimport is appropriate. A different hash does not establish which "
                "code is older. `rules apply` only rewrites merchant/category."
            ),
            details=[
                f"{version or '(unset)'}: {count:,} row(s)"
                + (
                    " — core-code provenance unavailable"
                    if not version or version == UNKNOWN_VERSION
                    else ""
                )
                for version, count in sorted(stale, key=lambda item: -item[1])
            ]
            + [f"current core-code hash: {current}"],
        )

    def _check_import_interpretation_freshness(self) -> DoctorFinding | None:
        """Do not promote a matching core hash into proof of private input freshness."""
        with closing(_connect(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        if not total:
            return None
        return DoctorFinding(
            check="adapter_profile_freshness_unavailable",
            severity="low",
            summary="Private adapter and CSV profile freshness is unavailable in this diagnostic",
            count=total,
            remedy=(
                "Only a verified import receipt binding the affected rows to the exact current "
                "adapter/profile inputs can establish freshness. This diagnostic does not "
                "verify those receipts; matching core-code hashes are insufficient. "
                "This unavailable result does not imply a reimport is needed."
            ),
        )

    def _check_stale_health_scores(self) -> DoctorFinding | None:
        with session_scope(self.db_path) as session:
            latest_score = (
                session.query(FinancialHealthScoreDB)
                .order_by(FinancialHealthScoreDB.score_date.desc())
                .first()
            )
            score_date: date | None = latest_score.score_date if latest_score else None
            latest_txn = (
                session.query(TransactionDB.posting_date)
                .order_by(TransactionDB.posting_date.desc())
                .first()
            )
        latest_transaction: date | None = latest_txn[0] if latest_txn else None

        if latest_transaction is None:
            return None
        if score_date is not None and score_date == latest_transaction:
            return None
        return DoctorFinding(
            check="stale_health_scores",
            severity="medium",
            summary=(
                "No health score exists for the newest transaction date"
                if score_date is None
                else f"Newest health score is dated {score_date}, data runs to {latest_transaction}"
            ),
            count=1,
            remedy=(
                "Repairable. Recomputes scores anchored to the data. The dashboard shows the "
                "newest stored score, so a stale row is presented as current."
            ),
            repairable=True,
        )

    def _check_dangling_pattern_links(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE recurring_pattern_id IS NOT NULL"
                " AND recurring_pattern_id NOT IN (SELECT pattern_id FROM recurring_patterns)"
            ).fetchone()[0]
        if not count:
            return None
        return DoctorFinding(
            check="dangling_pattern_links",
            severity="low",
            summary=f"{count:,} transactions reference a recurring pattern that no longer exists",
            count=count,
            remedy="Repairable. Clears the stale link so the rows can match a pattern again.",
            repairable=True,
        )

    def _check_orphaned_categories(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) AS n FROM transactions"
                " WHERE category NOT IN (SELECT name FROM category_config)"
                " GROUP BY category ORDER BY n DESC"
            ).fetchall()
        if not rows:
            return None
        return DoctorFinding(
            check="orphaned_categories",
            severity="high",
            summary="Transactions reference categories that are not in the taxonomy",
            count=sum(row[1] for row in rows),
            remedy=(
                "Restore the missing categories on Rules / Settings, or restore the "
                "pre-mutation backup. Spend/exclusion semantics for these rows are undefined."
            ),
            details=[f"{row[0]}: {row[1]:,} transaction(s)" for row in rows],
        )

    def _check_balance_anchors(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM account_balances").fetchone()[0]
            absolute = conn.execute(
                "SELECT COUNT(*) FROM account_balances WHERE balance_basis = 'absolute'"
            ).fetchone()[0]
        if not total or absolute:
            return None
        return DoctorFinding(
            check="no_absolute_balance_anchor",
            severity="medium",
            summary=f"All {total:,} balance rows are relative — no statement-backed balance exists",
            count=total,
            remedy=(
                "Emergency-fund months, debt ratio, and credit utilization cannot be computed "
                "and are correctly reported as unavailable. Import deposit statements with "
                "closing balances, or run `ledger repair statements`, to establish anchors."
            ),
        )

    def _check_unmapped_merchants(self) -> DoctorFinding | None:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT description, COUNT(*) AS n FROM transactions"
                " WHERE merchant IS NULL OR TRIM(merchant) = ''"
                " GROUP BY description ORDER BY n DESC LIMIT 20"
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE merchant IS NULL OR TRIM(merchant) = ''"
            ).fetchone()[0]
        if not total:
            return None
        return DoctorFinding(
            check="unmapped_merchants",
            severity="low",
            summary=f"{total:,} transactions have no normalized merchant",
            count=total,
            remedy=(
                "The Review Queue only covers rows staged by an import run, so rows already "
                "in the database are invisible to it. Add merchant rules on Rules / Settings, "
                "or use 'Fix a category' on the Transactions page."
            ),
            details=[f"{row[1]:,}x {row[0][:70]}" for row in rows],
        )

    def _check_statement_row_mismatches(self) -> DoctorFinding | None:
        summary = _latest_audit_counts(self.audit_runs_dir or runtime_path("audit-runs"))
        if summary is None:
            return None
        by_type = summary.get("mismatches_by_type") or {}
        collapsed = int(by_type.get("count_mismatch", 0))
        if not collapsed:
            return None
        counts = summary.get("counts") or {}
        missing = int(counts.get("source_rows", 0)) - int(counts.get("db_rows", 0))
        return DoctorFinding(
            check="statement_row_mismatches",
            severity="high",
            summary=(
                f"{collapsed} statement row group(s) have fewer rows in the database "
                f"than in the source PDF ({missing:,} row(s) missing)"
            ),
            count=collapsed,
            remedy=(
                "Reimport the affected statements from data/processed. Same-day repeat "
                "purchases imported before the within-file disambiguation fix collapsed "
                "into a single row, so spending is understated."
            ),
        )

    def _count_account_orphans(self, relations: tuple[tuple[str, str, str], ...]) -> list[str]:
        """Count, per relation, the rows whose account key resolves against nothing.

        Details carry table names and counts only. A raw slug or account id is an
        account identifier, and doctor output reaches logs and run artifacts.
        """
        offenders: list[str] = []
        with closing(_connect(self.db_path)) as conn:
            for table, column, parent_column in relations:
                # Table and column names are trusted module constants, never input.
                orphan_rows = conn.execute(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE {column} NOT IN (SELECT {parent_column} FROM accounts)"
                ).fetchone()[0]
                if orphan_rows:
                    offenders.append(f"{table}.{column}: {int(orphan_rows)} row(s)")
        return offenders

    def _check_account_orphans(self) -> DoctorFinding | None:
        """Report derived rows whose account key resolves to no row in `accounts`.

        Two of these relations carry a foreign key and cannot drift; they are
        listed anyway so the census stays complete if a constraint is ever
        dropped. The other three cannot carry one. Statement credit limits land at
        PDF finalize, before analysis has created the account row, so a constraint
        would fail the import. Transactions and transfers are what accounts are
        derived FROM -- constraining them would invert the dependency.

        `account_lifecycle` is checked by `_check_lifecycle_orphans` instead: its
        orphans are expected rather than wrong, and this finding's remedy cannot
        clear them.
        """
        offenders = self._count_account_orphans(_ACCOUNT_RELATIONS)
        if not offenders:
            return None
        return DoctorFinding(
            check="account_orphans",
            severity="high",
            summary="Rows reference an account the accounts dimension does not have",
            count=len(offenders),
            remedy=(
                "Run `ledger analysis refresh` to rebuild the accounts dimension. An orphan "
                "in account_statement_credit_limits means a statement was finalized for an "
                "account with no transactions yet; it resolves once one imports."
            ),
            repairable=False,
            details=offenders,
        )

    def _check_lifecycle_orphans(self) -> DoctorFinding | None:
        """Report lifecycle records naming an account the dimension no longer has.

        Low severity and a different remedy, because this is not drift. The table
        is operator-authored, carries no ForeignKey by design, and
        `save_accounts(replace_all=True)` deletes an account the moment its last
        transaction ages out -- which is the normal end state of an account the
        operator marked closed. Riding the generic high-severity orphan finding
        reported a permanent HIGH that `ledger analysis refresh` can NEVER clear,
        because detection derives `accounts` from transactions and a closed account
        has none. It is reported at all only because no constraint guards the table,
        so a typo'd account id would otherwise be invisible forever.

        STATUS-NEUTRAL, in the summary and in the details. The count covers every
        orphaned row whatever its `status`, and the remedy's own prescription
        produces the contradiction: `ledger accounts reopen` sets the row active
        and leaves it orphaned, because the account still has no transactions --
        so a summary saying "closure records" then asserted a closure the table
        no longer held anywhere. The remedy says outright that reopening does not
        clear this, because an operator who follows an instruction and sees the
        same finding twice has been told the fix did not work.
        """
        offenders = self._count_account_orphans((_LIFECYCLE_ACCOUNT_RELATION,))
        if not offenders:
            return None
        # Statuses and counts only, never an account id: doctor output reaches
        # logs and run artifacts, and an account id is an account identifier.
        with closing(_connect(self.db_path)) as conn:
            by_status = conn.execute(
                "SELECT status, count(*) FROM account_lifecycle "
                "WHERE account_id NOT IN (SELECT account_id FROM accounts) "
                "GROUP BY status ORDER BY status"
            ).fetchall()
        breakdown = ", ".join(f"{status!s} {int(count)}" for status, count in by_status)
        return DoctorFinding(
            check="account_lifecycle_orphans",
            severity="low",
            summary="Account lifecycle records name accounts the accounts dimension no longer has",
            count=len(offenders),
            remedy=(
                "Expected for an account that was closed and whose last transaction has "
                "aged out of the dimension; nothing needs doing. `ledger analysis refresh` "
                "cannot clear it, because accounts are derived from transactions. If the "
                "account is in fact still open, run `ledger accounts reopen <slug>` to "
                "restore its coverage-gap alerts -- reopening does not clear this finding, "
                "and is not meant to: the row stays orphaned until that account's "
                "transactions are imported again. If the record was written against the "
                "wrong account id, delete the stale row directly (nothing reads it once it "
                "is gone). `ledger accounts list` shows every slug this database knows."
            ),
            repairable=False,
            details=[f"{offender} ({breakdown})" for offender in offenders],
        )

    def _check_dead_columns(self) -> DoctorFinding | None:
        """Report a stale ``_KNOWN_DEAD_COLUMNS`` manifest.

        See the constant's docstring for why this is keyed on a curated manifest and
        not on emptiness. The finding is about the manifest, never about the data --
        which is why it is ``low`` and not repairable: the fix is a code edit
        (re-curate the manifest, or drop the column that came back), and neither
        choice is unambiguous enough for the doctor to make.

        ORDERING IS LOAD-BEARING. ``diagnose()`` calls ``ensure_schema()``, which
        runs the migrations, so ``ledger db doctor`` is NOT read-only even without
        ``--execute`` -- a pre-existing limitation recorded in the 1.4.0 design doc
        (§2.12, §17) and deliberately out of scope here. But migration
        ``20260810_0023`` DROPS exactly the five columns this manifest names. Run
        after ``ensure_schema``, this check inspected post-migration state while
        claiming to report pre-migration drift: a database predating 0023 with real
        values in all five came back with the columns gone and no finding raised,
        and the default (backup-less) invocation is what destroyed them. Two of the
        four branches the manifest enumerates were unobservable, and the remedy
        advised about a state the doctor could never reach. So ``diagnose()`` calls
        this FIRST, before ``ensure_schema``, and it must stay there.

        Being first means the database may not exist yet, in which case there is
        nothing to inspect and nothing to lose -- ``ensure_schema`` is about to
        create it at the correct shape. Reporting "table is missing" there would be
        a false positive on every brand-new ledger, and merely connecting would
        create the file ahead of ``init_database``.
        """
        if not self.db_path.exists():
            return None
        offenders: list[str] = []
        # One missing TABLE is one problem, not one per manifest entry. All five
        # real entries name `transactions`, so an un-deduped census emitted the
        # identical line five times and a count of 5 for a single missing table --
        # and commands/db.py prints both the details and the count to the operator.
        missing_tables: set[str] = set()
        with closing(_connect(self.db_path)) as conn:
            for table, column in _KNOWN_DEAD_COLUMNS:
                # Table and column names are trusted module constants, never input.
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()
                if not table_exists:
                    if table not in missing_tables:
                        missing_tables.add(table)
                        offenders.append(f"{table}: table is missing; the manifest names nothing")
                    continue
                columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    # Retired as intended. Nothing to report.
                    continue
                populated = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
                ).fetchone()[0]
                if populated:
                    offenders.append(f"{table}.{column}: {int(populated)} row(s) carry a value")
        if not offenders:
            return None
        return DoctorFinding(
            check="dead_columns",
            severity="low",
            summary="A column recorded as having no producer is populated or unresolvable",
            count=len(offenders),
            remedy=(
                "Not repairable automatically. Either something now writes a column this "
                "code records as dead -- in which case remove it from _KNOWN_DEAD_COLUMNS "
                "in maintenance/doctor.py and re-declare it on the ORM -- or the database "
                "predates the migration that dropped it and `ledger db migrate` discards "
                "those values. Note that THIS run already applied pending migrations "
                "(diagnose calls ensure_schema after this check), so on the second reading "
                "the values are gone; recover them from a backup if they mattered."
            ),
            repairable=False,
            details=offenders,
        )

    # -- orchestration --------------------------------------------------------

    def diagnose(self) -> list[DoctorFinding]:
        # `_check_dead_columns` runs BEFORE `ensure_schema`, and that ordering is
        # the whole reason the check can observe anything. `ensure_schema` runs the
        # migrations -- so the "read-only" doctor is not read-only, a pre-existing
        # limitation recorded in the 1.4.0 design doc (§2.12) and out of scope here
        # -- and migration 20260810_0023 drops precisely the columns the manifest
        # names. See `_check_dead_columns` for what that cost when it ran second.
        findings = [finding for finding in (self._check_dead_columns(),) if finding is not None]
        ensure_schema(self.db_path)
        checks = (
            self._check_schema_drift,
            self._check_duplicate_merchant_rules,
            self._check_shattered_patterns,
            self._check_orphaned_categories,
            self._check_statement_row_mismatches,
            self._check_parser_version_drift,
            self._check_import_interpretation_freshness,
            self._check_stale_health_scores,
            self._check_balance_anchors,
            self._check_dangling_pattern_links,
            self._check_unmapped_merchants,
            self._check_account_orphans,
            self._check_lifecycle_orphans,
        )
        findings += [finding for check in checks if (finding := check()) is not None]
        return sorted(findings, key=_severity_rank)

    def repair(self, findings: list[DoctorFinding]) -> dict[str, int]:
        """Apply the unambiguous repairs. Callers must take a backup first."""
        repaired: dict[str, int] = {}
        for finding in findings:
            if not finding.repairable:
                continue
            if finding.check == "duplicate_merchant_rules":
                repaired[finding.check] = self._repair_duplicate_merchant_rules()
            elif finding.check == "dangling_pattern_links":
                repaired[finding.check] = self._repair_dangling_pattern_links()
            elif finding.check == "stale_health_scores":
                repaired[finding.check] = self._repair_health_scores()
        return repaired

    def _repair_duplicate_merchant_rules(self) -> int:
        """Keep the lowest-id rule per merchant; delete the rest and their patterns.

        Lowest id is the earliest-inserted rule, which is also the one
        _canonicalize_category_memberships keeps on the write path ("first rule wins"),
        so repairing here produces the same config that a successful save would have.
        """
        with closing(_connect(self.db_path)) as conn, conn:
            conn.execute(
                "DELETE FROM merchant_rule_patterns WHERE rule_id IN ("
                "  SELECT id FROM merchant_rules WHERE id NOT IN ("
                "    SELECT MIN(id) FROM merchant_rules GROUP BY UPPER(TRIM(merchant_norm))"
                "  )"
                ")"
            )
            cursor = conn.execute(
                "DELETE FROM merchant_rules WHERE id NOT IN ("
                "  SELECT MIN(id) FROM merchant_rules GROUP BY UPPER(TRIM(merchant_norm))"
                ")"
            )
            return max(cursor.rowcount, 0)

    def _repair_dangling_pattern_links(self) -> int:
        with session_scope(self.db_path) as session:
            valid = {row[0] for row in session.query(RecurringPatternDB.pattern_id).all()}
            rows = (
                session.query(TransactionDB)
                .filter(TransactionDB.recurring_pattern_id.isnot(None))
                .all()
            )
            cleared = 0
            for row in rows:
                if row.recurring_pattern_id not in valid:
                    row.recurring_pattern_id = None
                    cleared += 1
            return cleared

    def _repair_health_scores(self) -> int:
        from ledger.analysis.health_scorer import FinancialHealthScorer

        return FinancialHealthScorer(self.db_path).compute_recent_scores(days=30)

    def run(self, *, execute: bool = False) -> RunResult:
        started_at = naive_utc_now()
        if execute:
            with live_mutation_boundary(self.db_path, operation="database_doctor"):
                findings = self.diagnose()
                repaired = self.repair(findings)
                remaining = self.diagnose()
        else:
            # Diagnose is documented to migrate on read. It therefore participates
            # in the same exclusion boundary as execute mode and must refuse before
            # `ensure_schema` when an import can still publish.
            with live_mutation_boundary(self.db_path, operation="database_doctor"):
                findings = self.diagnose()
                repaired = {}
                remaining = findings

        return RunResult(
            status="success",
            started_at=started_at,
            finished_at=naive_utc_now(),
            counts={
                "findings": len(findings),
                "repaired": sum(repaired.values()),
                "remaining": len(remaining),
            },
            warnings=[f"{finding.check}: {finding.summary}" for finding in remaining],
            details={
                "findings": [
                    {
                        "check": finding.check,
                        "severity": finding.severity,
                        "summary": finding.summary,
                        "count": finding.count,
                        "remedy": finding.remedy,
                        "repairable": finding.repairable,
                        "details": finding.details,
                    }
                    for finding in findings
                ],
                "repaired": repaired,
            },
        )


def _connect(db_path: Path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _latest_audit_counts(audit_runs_dir: Path) -> dict[str, Any] | None:
    if not audit_runs_dir.exists():
        return None
    summaries = sorted(audit_runs_dir.glob("*/summary.json"))
    if not summaries:
        return None
    try:
        return json.loads(summaries[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None
