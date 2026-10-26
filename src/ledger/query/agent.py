"""Bounded transaction facts for read-only agent integrations."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import partial
from sqlite3 import Connection as SQLiteConnection
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import and_, false, func, or_
from sqlalchemy.orm import Session

from ledger.core.account_identity import lifecycle_account_ids
from ledger.finance_policy import (
    income_sql_filters,
    income_treatment_sql_filter,
    spend_sql_filters,
)
from ledger.query.account_refs import (
    account_id_ref_map,
    account_ref_map,
    investment_account_ref_map,
    resolve_account_ref,
)
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    AccountLifecycleDB,
    AccountStatementCreditLimitDB,
    DataQualityCheckDB,
    InsightFindingDB,
    InvestmentAccountDB,
    InvestmentStatementDB,
    RecurringPatternDB,
    StatementCoverageDB,
    TransactionDB,
    TransactionTreatmentDB,
    TransferDB,
)
from ledger.utils.money import to_cents

if TYPE_CHECKING:  # pragma: no cover - typing only
    # `ledger.analysis.__init__` imports `health_scorer`, which imports
    # `ledger.query`, so every analysis import in this module is deferred into
    # the function body that needs it. This one is a type annotation, and
    # `from __future__ import annotations` keeps it a string at runtime.
    from ledger.analysis.projection import PatternDisposition, RecurringInput

Flow = Literal["income", "spending"]
GroupBy = Literal["source", "merchant", "category", "month", "day", "none", "account_ref"]
Order = Literal["date_asc", "date_desc", "amount_desc", "amount_asc"]
# Names every step _normalize_match performs. The previous
# "unicode_nfkc_casefold_exact" omitted the whitespace collapse, so a client
# reimplementing the advertised rule mismatched every multi-word label.
MATCH_SEMANTICS = "unicode_nfkc_whitespace_collapse_casefold_exact"
# `contains` runs the SAME normalization the exact filters do, then tests for a
# substring, so a value discovered here is spelled the way a filter must send it.
CONTAINS_SEMANTICS = "unicode_nfkc_whitespace_collapse_casefold_substring"
_FLOW_VALUES = frozenset({"income", "spending"})
# `search_transaction_evidence` only. `any` is the UNION of the two predicate
# sets, not a relaxation of either, so the tools whose answer is a single total
# (aggregate) or a single-flow value list (dimensions) keep `_FLOW_VALUES`.
# Deliberately a frozenset and not a `Literal` alias: every caller reaches this
# module through a plain `str` off the wire, so an alias would advertise a
# narrowing mypy is never given the chance to check.
_SEARCH_FLOW_VALUES = frozenset({"income", "spending", "any"})
_GROUP_BY_VALUES = frozenset(
    {"source", "merchant", "category", "month", "day", "none", "account_ref"}
)
_ORDER_VALUES = frozenset({"date_asc", "date_desc", "amount_desc", "amount_asc"})
_DIMENSION_VALUES = frozenset({"categories", "institutions", "sources"})
_RECURRING_STATUS_VALUES = frozenset({"active", "inactive", "any"})
_EVIDENCE_ID_PATTERN = re.compile(r"[0-9a-f]{40}")
_MONTH_PATTERN = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_MAX_FILTER_VALUES = 50
_MAX_FILTER_LENGTH = 200
_MAX_PAGE_SIZE = 100
# Both surfaces aggregate in Python -- transfers because per-leg direction comes
# from the shared credit-leg predicate, dimensions because `contains` matches
# after Unicode normalization that SQL LIKE cannot reproduce. Neither may fetch
# an unbounded set: past these caps the caller narrows the request instead.
_MAX_TRANSFER_SCAN = 20_000
_MAX_DIMENSION_VALUES = 5_000
# `group_by: "account_ref"` pages IN SQL -- `group_by(...).order_by(...).offset()
# .limit()` -- so only one page of groups is ever materialized, and this is NOT a
# memory bound. It bounds the ACCOUNT-REF TRANSLATION instead: the ref is an HMAC
# and not a SQL expression, so `account_ref_map` loads every account before the
# page is cut, and a caller whose database really holds this many accounts wants
# to hear so rather than be served one page of five hundred identities. One row
# per account, so this bounds the same dimension as `_MAX_BALANCE_ACCOUNTS`.
_MAX_ACCOUNT_GROUPS = 500
# Net worth materializes one bounded cross-domain account set so it can produce
# whole-answer per-currency subtotals before cutting the component page. The cap
# refuses an oversized account set before allocating its component records.
_MAX_NET_WORTH_COMPONENTS = 1_000
# Historical statements are scanned only to select one latest row per account.
# Bound that scan before materializing it; ordinary statement history is tiny.
_MAX_NET_WORTH_STATEMENTS = 20_000
# `ledger.changes` merges two grouped result sets in Python, so both are
# materialized. Each entry represents an account-period that changed, so a
# whole-database rebuild can produce one for every imported account-period.
#
# Counted in SQL and compared BEFORE either set is fetched, like
# `_MAX_ACCOUNT_GROUPS` above and unlike the first version of this check, which
# ran after `.all()` on both -- a tripwire that fires only once the memory it
# exists to bound has been allocated is not one.
_MAX_CHANGE_ENTRIES = 5_000
# Where a supplied boundary sits against ONE set of import stamps. Used at both
# levels: over both tables together, and inside each per-table verdict. These
# four names ARE the vocabulary: `changes` builds its answer from these
# constants, and `test_changes_surface` pins the set against the published enum,
# so a fifth value cannot reach one and not the other.
#
# `before_earliest_import` is the one that was missing, and its absence was not a
# missing signal but a wrong one: a boundary older than every stamp was reported
# as `within_imported_range`, which is the opposite of true, alongside a count
# that was the whole table.
RELATION_BEFORE_EARLIEST = "before_earliest_import"
RELATION_WITHIN_RANGE = "within_imported_range"
RELATION_AFTER_LATEST = "after_latest_import"
RELATION_NO_IMPORTS = "no_imports_recorded"
_CHANGES_RELATIONS = frozenset(
    {
        RELATION_BEFORE_EARLIEST,
        RELATION_WITHIN_RANGE,
        RELATION_AFTER_LATEST,
        RELATION_NO_IMPORTS,
    }
)
# What the counts in a `ChangesPage` MEAN. Read before any number on it.
#
# `whole_database` exists because the count and the label were separable: a count
# that is the entire table it counts is exact, and calling it a delta invites a
# caller to report a rebuild as newly arrived activity. A rebuild can restamp
# the entire table within one import window without adding new activity.
#
# It is the WEAKER of the two per-table verdicts below, never an aggregate of
# them: `delta` only when neither count is the whole of its own table.
#
# There is deliberately no third value for "the database was rebuilt". An earlier
# design had one, gated on `client_generation == self._snapshot_generation`, and
# it was unimplementable as specified: `_snapshot_generation()` hashes
# `(st_dev, st_ino, st_size, st_mtime_ns, transaction_count)`, so EVERY write
# rotates it and an ordinary incremental import moves it exactly as much as a
# repair does. A caller pairing a boundary with the generation it observed
# therefore mismatched precisely when something had arrived and matched only when
# nothing had -- the documented loop answered "rebuilt" after every routine
# import and could never report one. The two verdicts below are derived from the
# stamps themselves, which DO distinguish the two cases: an incremental import
# preserves the stamps already stored, and a rebuild rewrites all of them.
BASIS_DELTA = "delta"
BASIS_WHOLE_DATABASE = "whole_database"
_CHANGES_BASES = frozenset({BASIS_DELTA, BASIS_WHOLE_DATABASE})
# The verdict on ONE counted table, which is the level the decision actually
# belongs at. `basis` above was decided from the earliest stamp over BOTH tables
# while `total_added` counts `transactions` alone; the two agree only while
# coverage is the newer table. Make the first thing ever imported a statement
# that produced no rows and they part company, and the whole transactions table
# is served as a delta -- the exact answer `whole_database` was introduced to
# prevent.
SOURCE_BASIS_DELTA = "delta"
SOURCE_BASIS_WHOLE_TABLE = "whole_table"
SOURCE_BASIS_NO_IMPORTS = "no_imports"
_CHANGES_SOURCE_BASES = frozenset(
    {SOURCE_BASIS_DELTA, SOURCE_BASIS_WHOLE_TABLE, SOURCE_BASIS_NO_IMPORTS}
)
# Whether an account identity reached the caller, and if not, why not. Two
# values, because there are now exactly two cases: the salt was minted and the
# entries carry refs, or it was not and they are withheld. A third value
# (`not_computed`) belonged to the withdrawn rebuild branch, which returned
# before an entry existed; with that branch gone it names a state this surface
# can no longer reach, and a published enum value nothing can produce is a claim
# about behaviour that does not exist.
ACCOUNT_REFS_DISCLOSED = "disclosed"
ACCOUNT_REFS_WITHHELD = "withheld"
# The balance surface answers per ACCOUNT and computes its level counts over
# every account, not just the page, so the account table itself is the scan.
# This is a tripwire, not a page size. Unlike the
# two caps above there is no filter to narrow -- past this the database is
# outside the shape this contract serves, and refusing beats issuing thousands of
# per-account lookups behind one call.
_MAX_BALANCE_ACCOUNTS = 500
# `ledger.projection` reads the whole of `recurring_patterns` and groups it by
# account in one pass, rather than issuing a query per account on the page.
# Counted in SQL before anything is fetched, like every other scan cap here,
# with one row per detected pattern.
_MAX_PROJECTION_PATTERNS = 20_000
# One insight refresh caps itself at 60 findings and one check run records nine
# rows, so both caps are tripwires against a table nothing prunes rather than
# bounds a real answer approaches. Both surfaces materialize their whole scoped
# set before paging, because a row this build cannot serve must not take a page
# slot -- so the cap is what keeps that materialization bounded.
_MAX_INSIGHT_FINDINGS = 2_000
_MAX_QUALITY_CHECKS = 2_000


class _DecimalCentSum:
    """SQLite aggregate that sums arbitrary-precision integer cents."""

    def __init__(self) -> None:
        self.total_cents = 0

    def step(self, value: object | None, negate: object) -> None:
        if value is None:
            return
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError
        # Unary Decimal negation and multiplication use the caller's context.
        # Convert the represented value with integers so rounding cannot precede
        # the fractional-cent guard, even with low precision or enabled traps.
        numerator, denominator = amount.as_integer_ratio()
        cents, remainder = divmod(numerator * 100, denominator)
        if remainder:
            raise ValueError
        self.total_cents += -cents if bool(negate) else cents

    def finalize(self) -> str:
        sign = "-" if self.total_cents < 0 else ""
        whole, cents = divmod(abs(self.total_cents), 100)
        return f"{sign}{whole}.{cents:02d}"


def _register_decimal_cent_sum(session: Session) -> None:
    dbapi_connection = session.connection().connection.driver_connection
    if not isinstance(dbapi_connection, SQLiteConnection):
        raise RuntimeError("Agent transaction aggregates require SQLite.")
    dbapi_connection.create_aggregate(
        "decimal_sum_cents",
        2,
        _DecimalCentSum,  # type: ignore[arg-type]  # typeshed fixes aggregate I/O to int
    )


class AgentQueryError(ValueError):
    """A content-free invalid query or cursor failure."""

    def __init__(self, code: str = "invalid_argument") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AgentFilters:
    sources: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    institutions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AggregateGroup:
    key: str
    amount: Decimal
    transaction_count: int
    # Set ONLY by `group_by: "account_ref"`, where the key is an opaque ref in one
    # of two namespaces and the caller has to be told which it holds (D3).
    account_ref_status: str | None = None


@dataclass(frozen=True)
class ExcludedCounts:
    """How many rows the policy dropped, and why.

    Counts only, never amounts -- an amount would reconstruct the very figures
    the exclusions exist to keep out of the total.

    Reasons OVERLAP: one row can be both non-CAD and excluded from spending, so
    the per-reason counts do not sum to `any_reason`. Bucketing by precedence
    would make `non_cad` under-report, and being an FX tripwire is its entire
    job. `any_reason` is supplied for reconciliation:

        candidate_rows - any_reason == transaction_count
    """

    candidate_rows: int = 0
    any_reason: int = 0
    non_cad: int = 0
    not_in_cashflow: int = 0
    exclude_from_spending: int = 0
    income_treatment: int = 0
    blank_merchant: int = 0


@dataclass(frozen=True)
class AggregateResult:
    groups: tuple[AggregateGroup, ...]
    total: Decimal
    transaction_count: int
    group_count: int
    excluded: ExcludedCounts
    matched_sources: tuple[str, ...]
    matched_categories: tuple[str, ...]
    matched_institutions: tuple[str, ...]
    unmatched_sources: tuple[str, ...]
    unmatched_categories: tuple[str, ...]
    unmatched_institutions: tuple[str, ...]
    match_semantics: str
    has_more: bool
    next_cursor: str | None
    # False only when `group_by: "account_ref"` was asked for and the account-ref
    # salt has never been minted. Minting it is a write, so the groups are
    # withheld exactly as `ledger.coverage` withholds its entries -- never
    # degraded to raw account slugs. Every other grouping leaves it True.
    account_refs_disclosed: bool = True


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    posting_date: date
    amount: Decimal
    currency: str
    flow: str
    cashflow_type: str
    source: str
    category: str
    institution: str


@dataclass(frozen=True)
class EvidencePage:
    items: tuple[EvidenceRow, ...]
    total_count: int
    matched_sources: tuple[str, ...]
    matched_categories: tuple[str, ...]
    matched_institutions: tuple[str, ...]
    unmatched_sources: tuple[str, ...]
    unmatched_categories: tuple[str, ...]
    unmatched_institutions: tuple[str, ...]
    match_semantics: str
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class CoverageStatusCounts:
    """How the matched account-months divide. Absence is NOT one of these.

    A period with no row at all was never imported, and no count can express
    that -- only the absence of an entry can.
    """

    imported_with_activity: int = 0
    imported_no_activity: int = 0
    imported_no_rows_unexplained: int = 0


@dataclass(frozen=True)
class CoverageEntry:
    """One imported account-month, identified only by its opaque salted ref.

    `statement_coverage` carries no foreign key -- rows land at PDF finalize,
    before account detection has created the row for a newly-seen account -- so
    a slug that resolves to no `accounts` record is a state this table reaches.

    That case reports the `unresolved-` sentinel plus `account_ref_status`, never
    a ref derived from whatever identity the coverage row happens to carry: a
    locally derived ref in the `acct-` namespace would be a second namespace
    wearing the first one's name. It is not NULL either, which was this surface's
    own version of the same defect -- two rows for two DIFFERENT unjoinable
    accounts were byte-identical, so a caller grouping by ref merged two real
    accounts' statements into one (D3, `query.account_refs`).
    """

    account_ref: str
    account_ref_status: str
    period: str
    institution: str
    statement_type: str
    row_count: int
    statement_count: int
    status: str
    imported_at: datetime


@dataclass(frozen=True)
class CoverageGap:
    """One account's statement-gap verdict, as the wire carries it.

    Per ACCOUNT, not per month, and only the TRAILING silence. Interior gaps
    inform the cadence estimate; they do not independently establish missing
    statements. The next expected period follows the estimated account cadence.

    `accounts.is_active` contributes nothing here and never will (D8). Closure
    comes from `account_lifecycle`, which only the operator writes.
    """

    account_ref: str
    account_ref_status: str
    last_covered_period: str
    classification: str
    deciding_signal: str
    elapsed_months: int
    cadence_months: int | None
    # None exactly when `cadence_months` is; a fraction of nothing is not zero.
    cadence_confidence: float | None
    cadence_observations: int
    missing_periods: tuple[str, ...]
    missing_period_count: int


@dataclass(frozen=True)
class CoverageGapCounts:
    """How the gap entries divide, so `len(gaps)` is never the only number.

    `naming_a_missing_statement` is the one an agent answering "which statements
    am I missing?" actually wants. It can be smaller than the entry count:
    a closed account and an account whose history is too short to establish a
    cadence both get an entry that asserts no missing statement.
    """

    overdue: int
    dormant: int
    unknown: int
    naming_a_missing_statement: int


@dataclass(frozen=True)
class CoverageGapCensus:
    """The gaps block, with every count that makes it readable.

    Unpaginated by construction: a per-account verdict over each account's
    complete covered history, so it neither moves with the requested window nor
    with the page offset, and it is identical on every page of one cursor walk.
    """

    gaps: tuple[CoverageGap, ...]
    counts: CoverageGapCounts
    assessed_accounts: int
    # Accounts holding coverage rows whose periods are ALL unparseable, so no
    # cadence and no last-covered period exist to classify. Counted rather than
    # dropped: silently omitting them from both the entries and the census is
    # exactly the "absent means fine" reading this whole tool exists to prevent.
    unscoreable_accounts: int
    # True when `gaps` was cut at `_MAX_COVERAGE_GAPS`. A flag and not an
    # inference: `len(gaps) < assessed_accounts` also occurs when some accounts
    # have no gaps, so the census cannot distinguish a truncated answer from a
    # complete one.
    truncated: bool


@dataclass(frozen=True)
class CoveragePage:
    entries: tuple[CoverageEntry, ...]
    entry_count: int
    status_counts: CoverageStatusCounts
    observed_start: str | None
    observed_end: str | None
    # False when the account-ref salt was never minted. Minting it is a write, so
    # a read-only server fails closed here rather than emitting raw slugs.
    account_refs_disclosed: bool
    # Per-ACCOUNT and unpaginated, so this does not move with `entries`: it is
    # derived from every covered period the account has, not from the requested
    # window, or a caller asking about three months would be told every account
    # has an unknown cadence.
    gap_census: CoverageGapCensus
    gaps_evaluated_on: date
    has_more: bool
    next_cursor: str | None


# Matched to the published `maxItems`, because the envelope is validated against
# its own schema before it is returned and an overflow would surface as
# `corrupt_data` rather than as a long list. `gaps_truncated` reports the cut;
# `gaps_assessed_accounts` cannot, because an account with no gap is not in the
# list either.
_MAX_COVERAGE_GAPS = 500
# Matched to the published `maxItems` on `missing_periods`. Five years of monthly
# silence, past which the untruncated `missing_period_count` carries the answer.
_MAX_MISSING_PERIODS = 60
# The scan cap the add-a-tool checklist requires. `_coverage_gaps` reads the
# whole of `statement_coverage` unfiltered -- it has to, since cadence is a
# property of an account's COMPLETE history -- so it is the one uncapped read on
# this surface. Counted in SQL before anything is fetched, like
# `_MAX_ACCOUNT_GROUPS`, so the tripwire fires before the memory it bounds is
# allocated. The table holds one row per account-month, so its size grows with
# both the number of accounts and the length of imported history.
_MAX_GAP_COVERAGE_ROWS = 200_000


@dataclass(frozen=True)
class ChangeEntry:
    """One account-period that changed, as counts only.

    `rows_added` counts stored transaction rows, NOT rows that survive the
    CAD-only cashflow policy: this surface answers "what arrived", not "what
    counts". `statement_row_count` is the extractor's own figure from
    `statement_coverage`, which includes rows finalize routes into `transfers`,
    so it is not a transaction count either and the two legitimately differ.
    """

    account_ref: str
    account_ref_status: str
    period: str
    institution: str
    rows_added: int
    statement_status: str
    statement_count: int
    statement_row_count: int


@dataclass
class _ChangeBucket:
    """One account-period being accumulated from both counted tables.

    Internal to `changes`: rows arrive grouped, statements arrive per row, and the
    status is derived at the END from what the bucket holds rather than written by
    whichever statement was read last.
    """

    rows_added: int = 0
    statement_count: int = 0
    statement_row_count: int = 0
    # A statement that produced no rows AND recorded no reason. Worth looking at,
    # so it is reported apart from the explained empties rather than folded in.
    unexplained: bool = False
    # Every spelling either table carried for this account-period. Used only when
    # the account has no stored record to read the institution from.
    observed_institutions: set[str] = field(default_factory=set)

    def statement_status(self) -> str:
        if self.statement_count == 0:
            return "no_statement_in_window"
        if self.statement_row_count > 0:
            return "imported_with_activity"
        if self.unexplained:
            return "imported_no_rows_unexplained"
        return "imported_no_activity"


@dataclass(frozen=True)
class ChangeCountSource:
    """One COUNTED TABLE's stamp bounds, and the verdict on counts drawn from it.

    The verdict has to be per-table because the two earliest stamps are
    independent. `statement_coverage` is stamped once at finalize and
    `transactions` once per parsed file, and a statement that produced no rows --
    the case this surface joins coverage to make visible at all -- puts a coverage
    stamp before every transaction stamp. Deciding one verdict over both bounds
    and then counting one table read the wrong bound for the count it labelled.
    """

    basis: str
    since_relation: str
    earliest_imported_at: datetime | None
    latest_imported_at: datetime | None


@dataclass(frozen=True)
class ChangeCountSources:
    """The verdict for each count this surface reports.

    `rows` governs `total_added` and every entry's `rows_added`; `statements`
    governs `statement_count` and `statement_row_count`.
    """

    rows: ChangeCountSource
    statements: ChangeCountSource


@dataclass(frozen=True)
class ChangesPage:
    entries: tuple[ChangeEntry, ...]
    # Never None. Both counts are always produced: no input this surface accepts
    # can suppress one, because the only thing that ever did was a generation
    # comparison that could not tell an import from a rebuild.
    entry_count: int
    total_added: int
    # One of `_CHANGES_BASES`, and the WEAKER of the two per-source verdicts:
    # `delta` only when neither count is the whole of the table it counts.
    # `delta_available` is True for `delta` alone.
    basis: str
    delta_available: bool
    since_relation: str
    count_sources: ChangeCountSources
    # Both bounds of the snapshot's import stamps, over transactions AND
    # statement coverage. `latest_imported_at` is the SAME value ledger.status
    # emits; the pair is what lets a caller see that an entire "imported history"
    # is one rebuild window wide. Per-table bounds live on `count_sources`.
    latest_imported_at: datetime | None
    earliest_imported_at: datetime | None
    # `disclosed` or `withheld`. A string rather than a boolean because the reason
    # travels with it on the wire.
    account_refs: str
    # None when no `client_generation` was supplied: a comparison with nothing to
    # compare against is not False, it is absent. Informational either way -- it
    # never reaches a count.
    generation_changed: bool | None
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class RecurringEntry:
    merchant: str
    category: str
    institution: str
    pattern_type: str
    frequency: str
    # A magnitude, never signed: `pattern_type` carries the income/expense sense.
    typical_amount: Decimal
    next_expected_date: date
    missed_count: int
    is_active: bool
    # When the detector last rewrote this pattern. Detector output is write-once
    # until the next analysis run, and the sibling insights/quality tools already
    # timestamp what they serve; without this, stale patterns read as current.
    updated_at: datetime | None


@dataclass(frozen=True)
class RecurringPage:
    entries: tuple[RecurringEntry, ...]
    entry_count: int
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class TransferGroup:
    institution: str
    counterparty_institution: str | None
    direction: str
    amount: Decimal
    transfer_count: int


@dataclass(frozen=True)
class TransferAggregate:
    groups: tuple[TransferGroup, ...]
    group_count: int
    total: Decimal
    transfer_count: int
    candidate_rows: int
    non_cad: int
    paired_legs: int
    with_counterparty_institution: int
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class BalanceLevel:
    """One account's balance LEVEL, or the reason there is none.

    ``balance_basis`` is only ever ``"absolute"``: a level is served from the
    statement-anchored series or not at all. The relative series is seeded at 0
    on the account's first imported transaction, so its level is a change since
    an arbitrary point and reporting it as a balance is a different quantity,
    not a rougher one.
    """

    status: str
    balance: Decimal | None
    balance_basis: str | None
    balance_date: date | None
    as_of_lag_days: int | None
    credit_utilization_pct: Decimal | None
    credit_utilization_status: str


@dataclass(frozen=True)
class BalanceChange:
    """Net movement over a window, taken from the RELATIVE series.

    The one honest use of that series: its zero is arbitrary, so a difference
    between two of its readings is exact even though neither reading is a level.
    """

    status: str
    amount: Decimal | None
    from_date: date
    to_date: date
    # The closing observation's own date. When it is earlier than to_date the
    # series ended inside the window and the delta measures only through this
    # day — the staleness fact travels on the wire like the level's lag does.
    observed_through: date | None = None
    balance_basis: str = "relative"


@dataclass(frozen=True)
class BalanceEntry:
    account_ref: str
    institution: str
    account_type: str
    is_credit_card: bool
    level: BalanceLevel
    change: BalanceChange | None


@dataclass(frozen=True)
class BalanceLevelCounts:
    """How many accounts each level outcome covers, over the WHOLE account set."""

    reported: int = 0
    no_absolute_anchor: int = 0
    no_anchor_on_or_before_as_of: int = 0


@dataclass(frozen=True)
class BalancePage:
    entries: tuple[BalanceEntry, ...]
    entry_count: int
    level_counts: BalanceLevelCounts
    # False when the account-ref salt was never minted, exactly as in CoveragePage.
    account_refs_disclosed: bool
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class NetWorthComponent:
    """One signed statement-backed contribution to a currency subtotal.

    ``amount`` remains a Decimal at its source precision here.  Investment NAV
    comes from ``ExactFixedPoint`` TEXT and is not rounded or converted while
    components are selected or subtotals are accumulated; the MCP mapper owns
    the single signed two-decimal wire quantization.
    """

    account_ref: str
    component_type: str
    institution: str
    account_type: str
    amount: Decimal
    currency: str
    basis: str
    as_of: date
    as_of_lag_days: int
    status: str
    closed_on: date | None
    # Investment-only. Banking lifecycle records do not assert whether a final
    # statement has arrived, so null is more truthful than a fabricated false.
    final_statement_outstanding: bool | None


@dataclass(frozen=True)
class NetWorthSubtotal:
    currency: str
    amount: Decimal
    component_count: int


@dataclass(frozen=True)
class NetWorthComponentCounts:
    """Which accounts entered the composition, and which were left out and why.

    Every other account-bearing surface (balances, projection) publishes a
    census of its absences; net worth silently dropping an unanchored account
    read as "that account holds nothing" — the exact absence-as-zero confusion
    the coverage statement exists to prevent."""

    banking_reported: int
    banking_no_anchor_on_or_before_as_of: int
    banking_no_absolute_anchor: int
    investment_reported: int
    investment_no_statement_on_or_before_as_of: int


@dataclass(frozen=True)
class NetWorthPage:
    components: tuple[NetWorthComponent, ...]
    component_count: int
    component_counts: NetWorthComponentCounts
    subtotals: tuple[NetWorthSubtotal, ...]
    account_refs_disclosed: bool
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class ProjectionAnchor:
    """The balance a projection starts from, or the reason there is none.

    The same three outcomes ``BalanceLevel`` reports, under the same names and
    for the same reason: a level comes from the statement-anchored ``absolute``
    series or it is refused. The ``relative`` series is seeded at zero on the
    account's first imported transaction, so projecting from it would produce a
    number that looks like a balance and is a change since an arbitrary point.
    """

    status: str
    balance: Decimal | None
    balance_basis: str | None
    balance_date: date | None
    as_of_lag_days: int | None


@dataclass(frozen=True)
class ProjectionLineItem:
    """One expected flow, with everything that was derived to place it.

    Deliberately WITHOUT the pattern id. It is reminted on every analysis refresh
    -- it hashes the pattern's full-precision running average, and `save_patterns`
    deletes and re-inserts -- so it is stable only within one answer, and it is
    used server-side to close an ordering rather than emitted as an identity.

    `amount` is SIGNED and cent-quantized: positive is money in. The sign comes
    from `pattern_type` alone, because `expected_amount` is stored as an unsigned
    magnitude.
    """

    merchant: str
    category: str
    pattern_type: str
    frequency: str
    amount: Decimal
    projected_date: date
    # The STORED value, which is one period after the last occurrence the
    # detector accepted and therefore routinely in the past, beside the SIGNED
    # number of periods between the two. Both, so the caller can reproduce the
    # derivation.
    next_expected_date: date
    periods_from_next_expected: int
    # Whether that stored date has itself passed. A property of the PATTERN, so
    # it is identical on each of its lines -- and it is the field to sort by when
    # hunting stale patterns, because the offset above counts occurrences, not
    # lateness.
    next_expected_is_retrospective: bool
    is_overdue: bool
    missed_count: int
    confidence: float


@dataclass(frozen=True)
class ProjectionEntry:
    """One account's projection, or its refusal, with every pattern accounted for.

    `disposition` is None exactly when no projection was built -- the anchor was
    refused, or the window implied more flows than one answer serves. Without a
    window there is nothing to classify occurrences against, and reporting zeros
    would say the account's patterns were considered and produced nothing.
    `pattern_count` is populated either way, so an empty line-item list can be
    told apart from an account with nothing recurring on it.

    `status` is what the caller reads FIRST. It repeats the anchor's three
    outcomes and adds the fourth this surface can produce on its own, so a caller
    never has to infer "no projection" from a null.
    """

    account_ref: str
    institution: str
    account_type: str
    is_credit_card: bool
    status: str
    anchor: ProjectionAnchor
    projected_balance: Decimal | None
    net_projected_flow: Decimal | None
    line_items: tuple[ProjectionLineItem, ...]
    pattern_count: int
    # Projectable patterns on this account whose stored next_expected_date has
    # passed, counted whether or not any occurrence of theirs landed in the
    # window. None exactly when no projection was built.
    patterns_past_due: int | None
    disposition: PatternDisposition | None


# The three anchor outcomes, plus the one this surface produces itself. A window
# between a very old anchor and the target can imply more flows than the served
# bound carries; truncating them would leave `anchor + sum(lines) == projected`
# false while every block still looked well formed, so that account is NAMED and
# the rest of the answer survives. Refusing the whole call instead would abort a
# cursor walk mid-way over one account, on a condition no input can narrow -- the
# window is (anchor, target] and the caller does not choose the anchor.
PROJECTION_ENTRY_STATUSES = (
    "projected",
    "no_absolute_anchor",
    "no_anchor_on_or_before_as_of",
    "window_exceeds_line_item_cap",
)


@dataclass(frozen=True)
class ProjectionAnchorCounts:
    """How many accounts each anchor outcome covers, over the WHOLE account set."""

    reported: int = 0
    no_absolute_anchor: int = 0
    no_anchor_on_or_before_as_of: int = 0


@dataclass(frozen=True)
class ProjectionPage:
    entries: tuple[ProjectionEntry, ...]
    entry_count: int
    anchor_counts: ProjectionAnchorCounts
    # False when the account-ref salt was never minted, exactly as in BalancePage.
    account_refs_disclosed: bool
    has_more: bool
    next_cursor: str | None


# The two vocabularies `insight_findings` stores and nothing constrains: both
# columns are plain VARCHAR with no CHECK. These sets are therefore the ONLY
# thing that makes the published enums true, and both are taken from what the
# PRODUCERS can write rather than from what a writer accepts.
#
# `resolved` is the reason that distinction matters. `refresh_period` writes it
# when a finding stops regenerating, and `InsightEngine.VALID_STATUSES` -- the
# write guard -- omits it, so a status set copied from the guard would be missing
# a value the engine mints on its own. It is readable here and not writable
# anywhere.
#
# `critical` is the mirror image: the Streamlit page ranks five severities
# including it, and no producer emits one. A set copied from the renderer would
# have published a severity that cannot occur.
INSIGHT_SEVERITIES = frozenset({"high", "low", "medium"})
INSIGHT_STATUSES = frozenset({"ignored", "open", "resolved", "reviewed", "watch"})
# `data_quality_checks` is a different table with two column names in common and
# NO values in common. One shared enum across the two would be wrong on both.
QUALITY_SEVERITIES = frozenset({"error", "info", "warning"})
QUALITY_STATUSES = frozenset({"fail", "pass", "warn"})

# Per finding type: the wire field, the `evidence` key it is read from, and how
# to read it. `evidence` is a schema-less JSON column -- whatever the
# report-snapshot payload element was -- so nothing here is a stored contract;
# it is this surface deciding, per type, which scalars that type GUARANTEES.
#
# Three keys are absent by decision rather than by omission. `account_type` can
# contain a raw account slug from `accounts.account_number`;
# `pattern_id` is an unsalted account-derived SHA-256,
# the construction `ledger.recurring` already refuses by name; and every second
# amount (`volume`, `current`, `previous`, `budget`, `actual`) is a magnitude
# whose meaning the finding type does not fix.
#
# `txn_id` IS emitted, as `evidence_id`: an anomaly finding is about one
# transaction, the id is the same value `ledger.transactions.search` returns and
# `ledger.transaction.get` accepts, and withholding it would leave an agent
# re-identifying the row by fuzzy-matching a merchant, a date and an amount.
_INSIGHT_FIELD_SOURCES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "anomalies": (
        ("amount", "amount", "amount"),
        ("category", "category", "text:category"),
        ("date", "date", "date"),
        ("evidence_id", "txn_id", "evidence_id"),
        ("merchant", "merchant", "text:merchant"),
    ),
    # The stored `variance` is budget - actual, so it is NEGATIVE exactly when
    # this finding exists; the magnitude is the overrun.
    "budget": (("amount", "variance", "amount"), ("category", "category", "text:category")),
    "category_drift": (("amount", "change", "amount"), ("category", "category", "text:category")),
    "cost_cutting": (("amount", "amount", "amount"), ("merchant", "merchant", "text:merchant")),
    "credit_cards": (
        ("account_ref", "account_id", "account_ref"),
        ("amount", "net", "amount"),
        ("institution", "institution", "text:institution"),
    ),
    "possible_duplicates": (
        ("amount", "amount", "amount"),
        ("category", "category", "text:category"),
        ("date", "date", "date"),
        ("evidence_id", "txn_id", "evidence_id"),
        ("merchant", "merchant", "text:merchant"),
        ("related_evidence_id", "related_txn_id", "evidence_id"),
    ),
    # Transfer match statistics over the finding's PERIOD. `ledger.transfers`
    # queried over that same period reports the pairing coverage, so this
    # finding carries the triage state and nothing that duplicates it.
    # `ledger.quality`'s `unmatched_transfers` check is NOT the same number:
    # `DataQualityService._transfer_check` calls `get_transfer_stats()` with no
    # date arguments, so it counts the whole database.
    "quality": (),
    "recurring": (
        ("amount", "expected_amount", "amount"),
        ("category", "category", "text:category"),
        ("date", "first_occurrence", "date"),
        ("institution", "institution", "text:institution"),
        ("merchant", "merchant", "text:merchant"),
    ),
    "recurring_price_increase": (
        ("amount", "amount", "amount"),
        ("category", "category", "text:category"),
        ("date", "date", "date"),
        ("evidence_id", "txn_id", "evidence_id"),
        ("merchant", "merchant", "text:merchant"),
        ("related_evidence_id", "related_txn_id", "evidence_id"),
    ),
    "unresolved_reimbursements": (
        ("amount", "amount", "amount"),
        ("category", "category", "text:category"),
        ("date", "date", "date"),
        ("evidence_id", "txn_id", "evidence_id"),
        ("merchant", "merchant", "text:merchant"),
    ),
}
# Derived, so the schema and the mapper cannot disagree about which fields one
# finding type serves.
INSIGHT_TYPE_FIELDS: Mapping[str, tuple[str, ...]] = {
    finding_type: tuple(sorted(field for field, _key, _kind in sources))
    for finding_type, sources in _INSIGHT_FIELD_SOURCES.items()
}
_EVIDENCE_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# The width each text scalar is bounded at, taken from the storage column the
# evidence value was interpolated from: `transactions.category` is String(100),
# `transactions.institution` is String(50), `transactions.merchant` is
# String(200). One bound for the mapper and the published schema both, because a
# schema looser than the mapper serves a value every other tool would refuse and
# a schema tighter than the mapper answers `corrupt_data` for the whole period.
INSIGHT_TEXT_MAX_LENGTHS: Mapping[str, int] = {
    "category": 100,
    "institution": 50,
    "merchant": 200,
}


@dataclass(frozen=True)
class InsightFinding:
    """One triage row, with the typed scalars its finding type guarantees.

    `scalars` is already in wire form -- an amount as an exact two-decimal
    unsigned magnitude, a date as ISO-8601 -- and keyed by the wire field name,
    because the key SET is per finding type. A mapper reading a fixed attribute
    list would have to decide what an absent one means, and that decision belongs
    here: a row whose evidence cannot supply one of its type's scalars is not
    served at all, and is counted instead.
    """

    finding_type: str
    severity: str
    status: str
    period_start: date
    period_end: date
    scalars: Mapping[str, str]
    action_priority: str
    preference_scope: str | None
    preference_disposition: str | None
    preference_effect: str | None


@dataclass(frozen=True)
class InsightsPage:
    entries: tuple[InsightFinding, ...]
    # Servable findings in the scoped period -- the whole answer, not the page.
    entry_count: int
    # Rows in the scoped period this build cannot serve. Never silently dropped:
    # a stored value outside a published vocabulary degrades ONE row rather than
    # failing the envelope and answering `corrupt_data` for every row.
    unservable_count: int
    period_start: date | None
    period_end: date | None
    # The newest `created_at` among the SERVED rows: when the analysis behind
    # this answer ran. Null exactly when nothing is stored.
    computed_at: datetime | None
    severity_counts: Mapping[str, int]
    status_counts: Mapping[str, int]
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class QualityCheck:
    check_name: str
    severity: str
    status: str
    issue_count: int


@dataclass(frozen=True)
class QualityPage:
    entries: tuple[QualityCheck, ...]
    entry_count: int
    unservable_count: int
    run_at: datetime | None
    severity_counts: Mapping[str, int]
    status_counts: Mapping[str, int]
    has_more: bool
    next_cursor: str | None


def _insight_text(value: object, *, max_length: int) -> str | None:
    """A stored label, bounded by the width its OWN wire field admits.

    Per field rather than one bound for all three: a single 200 served a
    150-character category that `ledger.recurring` and
    `ledger.transactions.search` would both refuse, from a column that cannot
    hold one.
    """
    if not isinstance(value, str) or len(value) > max_length:
        return None
    return value


def _insight_amount(value: object) -> str | None:
    """A stored evidence amount as an exact unsigned two-decimal magnitude.

    `json_safe` writes these as JSON floats, and two of the six are negative by
    construction (`variance` is budget - actual, `net` is a card's spend), so the
    magnitude is taken here rather than left for a caller to read a sign this
    surface does not otherwise carry.

    Every failure returns None, which makes the ROW unservable and counted. The
    column is schema-less JSON, so `1e50` is a storable value, and quantizing it
    to cents needs more digits than the decimal context carries: raising there
    would fail the whole envelope and answer `corrupt_data` for every other
    finding in the period.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return None
        return format(abs(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
    except ArithmeticError, ValueError:
        return None


def _insight_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _insight_evidence_id(value: object) -> str | None:
    if not isinstance(value, str) or not _EVIDENCE_ID_PATTERN.fullmatch(value):
        return None
    return value


# Keyed by `kind`, and the text kinds are one reader per FIELD, bound to that
# field's own width from `INSIGHT_TEXT_MAX_LENGTHS`. Built from the mapping so a
# text field without a declared width raises here rather than defaulting to
# whichever bound happened to be widest.
_INSIGHT_SCALAR_READERS: Mapping[str, Callable[[object], str | None]] = {
    "amount": _insight_amount,
    "date": _insight_date,
    "evidence_id": _insight_evidence_id,
    **{
        f"text:{field}": partial(_insight_text, max_length=width)
        for field, width in INSIGHT_TEXT_MAX_LENGTHS.items()
    },
}


def _insight_finding(
    row: InsightFindingDB,
    *,
    account_refs_by_id: Mapping[str, str] | None = None,
    preferences: list[dict[str, Any]] | None = None,
    merchant_categories: Mapping[str, str] | None = None,
) -> InsightFinding | None:
    """One stored finding as it crosses, or None when this build cannot serve it.

    Four ways a row is unservable, all of them stored-data states rather than
    caller errors: a finding type with no schema, a severity or a status outside
    the published vocabulary, and an evidence blob that cannot supply a scalar
    the type guarantees. Each degrades exactly one row.
    """
    finding_type = str(row.finding_type)
    sources = _INSIGHT_FIELD_SOURCES.get(finding_type)
    if sources is None:
        return None
    severity = str(row.severity)
    status = str(row.status)
    if severity not in INSIGHT_SEVERITIES or status not in INSIGHT_STATUSES:
        return None
    period_start = row.period_start
    period_end = row.period_end
    if not isinstance(period_start, date) or not isinstance(period_end, date):
        return None
    evidence: Mapping[str, object] = row.evidence if isinstance(row.evidence, Mapping) else {}
    scalars: dict[str, str] = {}
    for field_name, key, kind in sources:
        if kind == "account_ref":
            raw_account_id = evidence.get(key)
            served = (
                account_refs_by_id.get(str(raw_account_id))
                if account_refs_by_id is not None and raw_account_id is not None
                else None
            )
        else:
            served = _INSIGHT_SCALAR_READERS[kind](evidence.get(key))
        if served is None:
            return None
        scalars[field_name] = served
    preference_scope: str | None = None
    preference_disposition: str | None = None
    preference_effect: str | None = None
    action_priority = "normal"
    from ledger.insights.preferences import SpendingPreferenceService

    match_evidence = dict(evidence)
    merchant_key = _preference_key(evidence.get("merchant"), scope="merchant")
    if not evidence.get("category") and merchant_key:
        fallback = (merchant_categories or {}).get(merchant_key)
        if fallback:
            match_evidence["category"] = fallback
    preference = SpendingPreferenceService.match(match_evidence, preferences or [])
    if preference is not None:
        preference_scope = str(preference["scope"])
        preference_disposition = str(preference["disposition"])
        material = _material_insight_evidence(finding_type, str(row.title), evidence)
        if material:
            preference_effect = "material_evidence_retained"
        elif finding_type == "cost_cutting":
            action_priority = "suppressed"
            preference_effect = "size_only_reduction_suppressed"
        else:
            action_priority = "low"
            preference_effect = "ordinary_recommendation_deprioritized"
    return InsightFinding(
        finding_type=finding_type,
        severity=severity,
        status=status,
        period_start=period_start,
        period_end=period_end,
        scalars=scalars,
        action_priority=action_priority,
        preference_scope=preference_scope,
        preference_disposition=preference_disposition,
        preference_effect=preference_effect,
    )


def _preference_key(value: object, *, scope: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        from ledger.insights.preferences import normalize_preference_name

        return normalize_preference_name(scope, value).casefold()
    except ValueError:
        return ""


def _positive_insight_change(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"true", "yes", "increase", "increased"}:
        return True
    try:
        amount = Decimal(text)
    except InvalidOperation, ValueError:
        return False
    return amount.is_finite() and amount > 0


def _material_insight_evidence(
    finding_type: str,
    title: str,
    evidence: Mapping[str, object],
) -> bool:
    if finding_type in {
        "anomalies",
        "budget",
        "category_drift",
        "possible_duplicates",
        "recurring_price_increase",
        "unresolved_reimbursements",
    }:
        return True
    issue = (
        str(
            evidence.get("issue")
            or evidence.get("evidence_type")
            or evidence.get("change_type")
            or ""
        )
        .strip()
        .casefold()
    )
    if issue in {
        "possible_duplicate",
        "duplicate_possibility",
        "unresolved_reimbursement",
        "price_increase",
        "recurring_price_increase",
        "material_change",
    }:
        return True
    if any(
        _positive_insight_change(evidence.get(key))
        for key in ("price_increase", "price_change", "material_change")
    ):
        return True
    return finding_type == "recurring" and (
        title.casefold().startswith("new recurring cost:") or evidence.get("is_new") is True
    )


def _quality_check(row: DataQualityCheckDB) -> QualityCheck | None:
    """One recorded check, or None when a stored value is outside the schema."""
    check_name = row.check_name
    severity = str(row.severity)
    status = str(row.status)
    issue_count = row.issue_count
    if not isinstance(check_name, str) or not 1 <= len(check_name) <= 120:
        return None
    if severity not in QUALITY_SEVERITIES or status not in QUALITY_STATUSES:
        return None
    if isinstance(issue_count, bool) or not isinstance(issue_count, int) or issue_count < 0:
        return None
    return QualityCheck(
        check_name=check_name,
        severity=severity,
        status=status,
        issue_count=issue_count,
    )


@dataclass(frozen=True)
class DimensionValue:
    value: str
    transaction_count: int


@dataclass(frozen=True)
class DimensionPage:
    values: tuple[DimensionValue, ...]
    value_count: int
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class _ResolvedFilters:
    sources: tuple[str, ...]
    categories: tuple[str, ...]
    institutions: tuple[str, ...]
    sources_requested: bool
    categories_requested: bool
    institutions_requested: bool
    # Requested verbatim, so a caller can see that "Restaurants" resolved to
    # nothing rather than reading the resulting zero as an answer.
    unmatched_sources: tuple[str, ...] = ()
    unmatched_categories: tuple[str, ...] = ()
    unmatched_institutions: tuple[str, ...] = ()


def _normalize_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _validate_filters(filters: AgentFilters) -> None:
    for values in (filters.sources, filters.categories, filters.institutions):
        if len(values) > _MAX_FILTER_VALUES:
            raise AgentQueryError
        for value in values:
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _MAX_FILTER_LENGTH
                or "\x00" in value
            ):
                raise AgentQueryError
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise AgentQueryError from None


def _validate_amount(value: Decimal | None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise AgentQueryError


def _validate_query(
    *,
    start_date: date,
    end_date: date,
    flow: str,
    group_by: str | None,
    filters: AgentFilters,
    limit: int,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    order: str | None = None,
    allowed_flows: frozenset[str] = _FLOW_VALUES,
) -> None:
    """Validate one bounded query. `allowed_flows` defaults to the TWO flows.

    Only `search_transaction_evidence` widens it. An aggregate over a union of
    income and spending is a single number with no meaning, and a dimension list
    counted over both offers values no single-flow aggregate can reproduce, so
    the widening is opt-in per call site rather than a change to the default.
    """
    if (
        not isinstance(start_date, date)
        or not isinstance(end_date, date)
        or start_date > end_date
        or flow not in allowed_flows
        or (group_by is not None and group_by not in _GROUP_BY_VALUES)
        or (order is not None and order not in _ORDER_VALUES)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_PAGE_SIZE
    ):
        raise AgentQueryError
    _validate_amount(min_amount)
    _validate_amount(max_amount)
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise AgentQueryError
    _validate_filters(filters)


def _cursor_fingerprint(
    *,
    kind: str,
    start_date: date | None = None,
    end_date: date | None = None,
    flow: str | None = None,
    group_by: str | None = None,
    filters: AgentFilters | None = None,
    generation: str,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    order: str | None = None,
    scope: Mapping[str, str | None] | None = None,
) -> str:
    """Bind a cursor to both the query shape AND the database state it was minted against.

    Without the generation, a cursor minted before an import and replayed after
    the documented restart validates cleanly and returns page two of a DIFFERENT
    dataset -- a silently wrong answer through the sanctioned workflow. With it,
    that cursor fails `invalid_cursor` instead.

    EVERY input that changes which row an offset addresses belongs here. An
    amount bound and an ordering both renumber the result set, so a cursor
    replayed with either one changed would skip rows and repeat others while
    returning a perfectly ordinary success envelope.

    `scope` carries the inputs that are not period/flow/filter shaped -- a period
    bound in months, a pattern status, a dimension, a normalized substring, a
    balance as-of date and its change window. It exists so a tool cannot quietly
    add an input without binding it: an unbound input is that same silent
    skip-and-duplicate bug. Values are normalized exactly as the query normalizes
    them, so two spellings the query cannot tell apart do not mint two cursor
    shapes.
    """
    document = {
        "end_date": None if end_date is None else end_date.isoformat(),
        "generation": generation,
        "filters": {
            "categories": sorted({_normalize_match(value) for value in filters.categories})
            if filters
            else [],
            "institutions": sorted({_normalize_match(value) for value in filters.institutions})
            if filters
            else [],
            "sources": sorted({_normalize_match(value) for value in filters.sources})
            if filters
            else [],
        },
        "flow": flow,
        "group_by": group_by,
        "kind": kind,
        "max_amount": None if max_amount is None else format(max_amount, ".2f"),
        "min_amount": None if min_amount is None else format(min_amount, ".2f"),
        "order": order,
        "scope": None if scope is None else dict(sorted(scope.items())),
        "start_date": None if start_date is None else start_date.isoformat(),
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(*, offset: int, fingerprint: str) -> str:
    payload = json.dumps(
        {"offset": offset, "query": fingerprint, "version": 1},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None, *, fingerprint: str) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
        raise AgentQueryError("invalid_cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except ValueError, UnicodeError, json.JSONDecodeError:
        raise AgentQueryError("invalid_cursor") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"offset", "query", "version"}
        or payload.get("version") != 1
        or payload.get("query") != fingerprint
        or isinstance(payload.get("offset"), bool)
        or not isinstance(payload.get("offset"), int)
        or not 0 <= payload["offset"] <= 2_147_483_647
    ):
        raise AgentQueryError("invalid_cursor")
    return int(payload["offset"])


def _resolve_values(
    session,
    column,
    requested: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the stored values that match, and the requested values that did not."""
    if not requested:
        return (), ()
    requested_keys = {_normalize_match(value) for value in requested}
    candidates = [str(row[0]) for row in session.query(column).distinct().all() if row[0]]
    matches = sorted({value for value in candidates if _normalize_match(value) in requested_keys})
    if len(matches) > _MAX_FILTER_VALUES:
        raise AgentQueryError("filter_too_broad")
    if any(len(value) > _MAX_FILTER_LENGTH for value in matches):
        raise AgentQueryError
    matched_keys = {_normalize_match(value) for value in matches}
    unmatched: list[str] = []
    for value in requested:
        if _normalize_match(value) not in matched_keys and value not in unmatched:
            unmatched.append(value)
    return tuple(matches), tuple(unmatched)


class AgentQueries:
    """Exact, bounded transaction facts intended for read-only agents."""

    def __init__(
        self,
        open_session: Callable[[], AbstractContextManager[Session]],
        snapshot_generation: str = "",
    ) -> None:
        self._open_session = open_session
        # Empty for CLI callers, which never hand a cursor across a database
        # change. The MCP always supplies one.
        self._snapshot_generation = snapshot_generation

    def _resolve_filters(self, session, filters: AgentFilters) -> _ResolvedFilters:
        sources, unmatched_sources = _resolve_values(
            session, TransactionDB.merchant, filters.sources
        )
        categories, unmatched_categories = _resolve_values(
            session, TransactionDB.category, filters.categories
        )
        institutions, unmatched_institutions = _resolve_values(
            session, TransactionDB.institution, filters.institutions
        )
        return _ResolvedFilters(
            sources=sources,
            categories=categories,
            institutions=institutions,
            sources_requested=bool(filters.sources),
            categories_requested=bool(filters.categories),
            institutions_requested=bool(filters.institutions),
            unmatched_sources=unmatched_sources,
            unmatched_categories=unmatched_categories,
            unmatched_institutions=unmatched_institutions,
        )

    @staticmethod
    def _magnitude_expression(flow: str):
        """The amount the tool REPORTS, which is what a caller filters and sorts on.

        Spending is reported as its absolute value and income as its positive
        signed value, so bounding or ordering on the raw signed column would
        invert every spending comparison. The `any` union reports the absolute
        value on BOTH sides -- an unsigned magnitude whose direction is carried
        by `cashflow_type` -- so it takes the same expression spending does, and
        a bound sent with `any` means the same thing for either kind of row.
        """
        if flow == "income":
            return TransactionDB.amount_signed
        return func.abs(TransactionDB.amount_signed)

    @classmethod
    def _amount_filters(
        cls,
        flow: str,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
    ) -> list:
        magnitude = cls._magnitude_expression(flow)
        bounds = []
        if min_amount is not None:
            bounds.append(magnitude >= min_amount)
        if max_amount is not None:
            bounds.append(magnitude <= max_amount)
        return bounds

    @classmethod
    def _base_query(
        cls,
        session,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        filters: _ResolvedFilters,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
    ):
        query = session.query(TransactionDB).filter(
            TransactionDB.posting_date >= start_date,
            TransactionDB.posting_date <= end_date,
        )
        if flow == "income":
            query = query.outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
            ).filter(*income_sql_filters(), income_treatment_sql_filter())
        elif flow == "any":
            # The SAME union `get_transaction_evidence` already queries, so a row
            # a `flow: "any"` search returns is exactly a row the by-id tool can
            # re-read. The two predicate sets are disjoint (one requires a
            # positive amount with cashflow_type income, the other a negative
            # amount with cashflow_type expense), so the OR cannot double-count,
            # and `transaction_treatments.txn_id` is that table's primary key, so
            # the outer join cannot duplicate a row either.
            query = query.outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
            ).filter(
                or_(
                    and_(*income_sql_filters(), income_treatment_sql_filter()),
                    and_(*spend_sql_filters()),
                )
            )
        else:
            query = query.filter(*spend_sql_filters())
        query = query.filter(*cls._amount_filters(flow, min_amount, max_amount))

        for requested, values, column in (
            (filters.sources_requested, filters.sources, TransactionDB.merchant),
            (filters.categories_requested, filters.categories, TransactionDB.category),
            (filters.institutions_requested, filters.institutions, TransactionDB.institution),
        ):
            if requested:
                query = query.filter(column.in_(values) if values else false())
        return query

    def _excluded_counts(
        self,
        session,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        filters: _ResolvedFilters,
        kept: int,
        blank_merchant: int,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
    ) -> ExcludedCounts:
        """Count the rows the policy dropped, over the same period and filters.

        The policy block declares the rules; without these counts an agent
        totalling a period has no way to know that any row was left out. Each
        exclusion bucket is counted independently so an empty bucket does not
        conceal rows excluded by another policy.

        `not_in_cashflow` is reported even though it is structurally zero today
        (every parser emits that flag only with amount_signed == 0, and the
        pipeline routes those rows to `transfers`). It costs one term, and a
        silent zero is indistinguishable from a bucket nobody checked.

        Deliberately NOT built from finance_policy's predicates: that module is
        in _SCRIPT_VERSION_FILES, so editing it would put every stored
        parser_version into `ledger db doctor` drift.

        The amount bounds narrow the candidate set too. Counting rows the caller
        excluded by amount as rows the POLICY dropped would misattribute the
        caller's own filter to the CAD-only cashflow rules.
        """
        candidates = session.query(TransactionDB).filter(
            TransactionDB.posting_date >= start_date,
            TransactionDB.posting_date <= end_date,
            *self._amount_filters(flow, min_amount, max_amount),
        )
        if flow == "income":
            candidates = candidates.filter(
                TransactionDB.amount_signed > 0,
                TransactionDB.cashflow_type == "income",
            )
        else:
            candidates = candidates.filter(
                TransactionDB.amount_signed < 0,
                TransactionDB.cashflow_type == "expense",
            )
        for requested, values, column in (
            (filters.sources_requested, filters.sources, TransactionDB.merchant),
            (filters.categories_requested, filters.categories, TransactionDB.category),
            (filters.institutions_requested, filters.institutions, TransactionDB.institution),
        ):
            if requested:
                candidates = candidates.filter(column.in_(values) if values else false())

        non_cad_row = ~or_(
            TransactionDB.currency == "CAD",
            TransactionDB.currency.is_(None),
            TransactionDB.currency == "",
        )
        not_in_cashflow_row = TransactionDB.include_in_cashflow == False  # noqa: E712

        def _count(*predicates) -> int:
            return int(
                candidates.filter(*predicates)
                .with_entities(func.count(func.distinct(TransactionDB.txn_id)))
                .scalar()
                or 0
            )

        candidate_rows = _count()
        counts = {
            "non_cad": _count(non_cad_row),
            "not_in_cashflow": _count(not_in_cashflow_row),
            "blank_merchant": blank_merchant,
        }
        if flow == "spending":
            counts["exclude_from_spending"] = _count(
                TransactionDB.exclude_from_spending == True  # noqa: E712
            )
        else:
            # Count treatment exclusions that canonical income reporting nets
            # away, including automatically applied treatments.
            counts["income_treatment"] = int(
                candidates.outerjoin(
                    TransactionTreatmentDB,
                    TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
                )
                .filter(~income_treatment_sql_filter())
                .with_entities(func.count(func.distinct(TransactionDB.txn_id)))
                .scalar()
                or 0
            )
        return ExcludedCounts(
            candidate_rows=candidate_rows,
            any_reason=max(candidate_rows - kept, 0),
            **counts,
        )

    def aggregate_transaction_facts(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        group_by: str,
        filters: AgentFilters,
        limit: int = 50,
        cursor: str | None = None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        account_ref_salt: str | None = None,
    ) -> AggregateResult:
        """Aggregate exact CAD transaction facts with deterministic matching.

        `group_by="account_ref"` is the one grouping whose key is not a stored
        value: it groups in SQL on `transactions.account` and translates each key
        through the ONE resolver afterwards, so the ref is the same one
        `ledger.coverage` and `ledger.balances` emit for that account. Without a
        minted salt the groups are withheld rather than degraded to raw slugs.
        """
        _validate_query(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=group_by,
            filters=filters,
            limit=limit,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        fingerprint = _cursor_fingerprint(
            kind="aggregate",
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=group_by,
            filters=filters,
            generation=self._snapshot_generation,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        # Only the account grouping can withhold anything; every other grouping
        # keys on a stored value that is already on this surface.
        account_refs_disclosed = True

        with self._open_session() as session:
            resolved = self._resolve_filters(session, filters)
            # SCOPE layer: period, flow, filters and amount bounds. This is what
            # the reported total and transaction_count must be computed from.
            base = self._base_query(
                session,
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                filters=resolved,
                min_amount=min_amount,
                max_amount=max_amount,
            )
            # GROUP layer: an unresolved blank merchant is real spending but is
            # not a named source, so it is dropped from merchant/source GROUPS
            # only. Narrowing the scope here instead made the reported total
            # depend on group_by -- the same period and filters returned a
            # different number depending on how it was grouped, with nothing in
            # the envelope to explain it.
            grouped_base = base
            blank_merchant = 0
            if group_by in {"source", "merchant"}:
                grouped_base = base.filter(TransactionDB.merchant != "")
                blank_merchant = int(
                    base.filter(TransactionDB.merchant == "")
                    .with_entities(func.count(func.distinct(TransactionDB.txn_id)))
                    .scalar()
                    or 0
                )
            _register_decimal_cent_sum(session)
            amount_expression = func.decimal_sum_cents(
                TransactionDB.amount_signed,
                int(flow == "spending"),
            )
            total_row = base.with_entities(
                amount_expression,
                func.count(func.distinct(TransactionDB.txn_id)),
            ).one()
            total = Decimal(str(total_row[0] or "0.00"))
            transaction_count = int(total_row[1] or 0)
            excluded = self._excluded_counts(
                session,
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                filters=resolved,
                kept=transaction_count,
                blank_merchant=blank_merchant,
                min_amount=min_amount,
                max_amount=max_amount,
            )

            if group_by == "none":
                all_groups = (
                    (AggregateGroup("all", total, transaction_count),) if transaction_count else ()
                )
                groups = all_groups[offset : offset + limit]
                group_count = len(all_groups)
            elif group_by == "account_ref":
                # Grouped in SQL on the stored slug and translated afterwards:
                # the ref is an HMAC, not an expression SQLite can group by.
                #
                # The count is taken over the slugs even when the refs are
                # withheld, because a count is not an identity and the shape of
                # the answer stays legible without one.
                group_count = int(
                    base.with_entities(func.count(func.distinct(TransactionDB.account))).scalar()
                    or 0
                )
                if group_count > _MAX_ACCOUNT_GROUPS:
                    raise AgentQueryError("filter_too_broad")
                account_refs_disclosed = bool(account_ref_salt)
                if account_ref_salt:
                    refs = account_ref_map(session, account_ref_salt)
                    # Ordered and PAGED on the stored slug, never on the derived
                    # ref. Two rules meet here:
                    #
                    # Not by magnitude -- the `length(amount) DESC` trick below is
                    # a magnitude ranking that inverts on a negative total, and an
                    # account total is not a ranking at all.
                    #
                    # Not by ref either. The ref is an HMAC of the account-ref
                    # salt, and the salt is NOT bound into the cursor fingerprint,
                    # so a ref sort key made a cursor minted under one salt and
                    # replayed under another address a DIFFERENT account: one
                    # account silently never served, behind an ordinary success
                    # envelope. The slug is stored, unique per account and
                    # salt-independent, so the order is total, an offset addresses
                    # one fixed group under any salt, and the coupling is gone
                    # rather than bound. It is a sort key only: the caller sees
                    # the ref. This is exactly why `ledger.coverage` and
                    # `ledger.balances` were already safe.
                    rows = (
                        base.with_entities(
                            TransactionDB.account.label("group_key"),
                            amount_expression.label("amount"),
                            func.count(func.distinct(TransactionDB.txn_id)).label(
                                "transaction_count"
                            ),
                        )
                        .group_by(TransactionDB.account)
                        .order_by(TransactionDB.account.asc())
                        .offset(offset)
                        .limit(limit)
                        .all()
                    )
                    resolved_groups = []
                    for row in rows:
                        ref, ref_status = resolve_account_ref(
                            refs, str(row.group_key), account_ref_salt
                        )
                        resolved_groups.append(
                            AggregateGroup(
                                key=ref,
                                amount=Decimal(str(row.amount)),
                                transaction_count=int(row.transaction_count),
                                account_ref_status=ref_status,
                            )
                        )
                    groups = tuple(resolved_groups)
                else:
                    groups = ()
            else:
                key_expression: Any
                if group_by in {"source", "merchant"}:
                    key_expression = TransactionDB.merchant
                elif group_by == "category":
                    key_expression = TransactionDB.category
                elif group_by == "day":
                    key_expression = func.strftime("%Y-%m-%d", TransactionDB.posting_date)
                else:
                    key_expression = func.strftime("%Y-%m", TransactionDB.posting_date)
                if group_by in {"month", "day"}:
                    # A time series is read as a series. The amount ranking below
                    # would return periods largest-first, which reads as a trend
                    # that runs in whatever order the spending happened to fall.
                    ordering: tuple[Any, ...] = (key_expression.asc(),)
                else:
                    ordering = (
                        func.length(amount_expression).desc(),
                        amount_expression.desc(),
                        key_expression.asc(),
                    )
                grouped = (
                    grouped_base.with_entities(
                        key_expression.label("group_key"),
                        amount_expression.label("amount"),
                        func.count(func.distinct(TransactionDB.txn_id)).label("transaction_count"),
                    )
                    .group_by(key_expression)
                    .order_by(*ordering)
                )
                group_count = grouped.order_by(None).count()
                rows = grouped.offset(offset).limit(limit).all()
                groups = tuple(
                    AggregateGroup(
                        key=str(row.group_key),
                        amount=Decimal(str(row.amount)),
                        transaction_count=int(row.transaction_count),
                    )
                    for row in rows
                )

        # Withheld groups are never served, so there is no next page to offer:
        # advertising one would page a caller through nothing forever.
        has_more = account_refs_disclosed and offset + len(groups) < group_count
        return AggregateResult(
            groups=tuple(groups),
            account_refs_disclosed=account_refs_disclosed,
            total=total,
            transaction_count=transaction_count,
            group_count=group_count,
            excluded=excluded,
            matched_sources=resolved.sources,
            matched_categories=resolved.categories,
            matched_institutions=resolved.institutions,
            unmatched_sources=resolved.unmatched_sources,
            unmatched_categories=resolved.unmatched_categories,
            unmatched_institutions=resolved.unmatched_institutions,
            match_semantics=MATCH_SEMANTICS,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(groups), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def search_transaction_evidence(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        filters: AgentFilters,
        limit: int = 50,
        cursor: str | None = None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        order: str = "date_asc",
    ) -> EvidencePage:
        """Return normalized evidence rows without descriptions, accounts, or paths.

        The ONE surface that accepts `flow="any"`. It is a union of the two
        predicate sets rather than a relaxation of either: the rows are exactly
        those the income search and the spending search return between them,
        every amount is an unsigned magnitude, and direction stays on each row's
        `cashflow_type`.
        """
        _validate_query(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=None,
            filters=filters,
            limit=limit,
            min_amount=min_amount,
            max_amount=max_amount,
            order=order,
            allowed_flows=_SEARCH_FLOW_VALUES,
        )
        fingerprint = _cursor_fingerprint(
            kind="evidence",
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=None,
            filters=filters,
            generation=self._snapshot_generation,
            min_amount=min_amount,
            max_amount=max_amount,
            order=order,
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            resolved = self._resolve_filters(session, filters)
            base = self._base_query(
                session,
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                filters=resolved,
                min_amount=min_amount,
                max_amount=max_amount,
            )
            total_count = int(
                base.with_entities(func.count(func.distinct(TransactionDB.txn_id))).scalar() or 0
            )
            rows = (
                base.order_by(*self._evidence_ordering(flow, order))
                .offset(offset)
                .limit(limit)
                .all()
            )
            items = tuple(self._evidence_row(row, flow=flow) for row in rows)

        has_more = offset + len(items) < total_count
        return EvidencePage(
            items=items,
            total_count=total_count,
            matched_sources=resolved.sources,
            matched_categories=resolved.categories,
            matched_institutions=resolved.institutions,
            unmatched_sources=resolved.unmatched_sources,
            unmatched_categories=resolved.unmatched_categories,
            unmatched_institutions=resolved.unmatched_institutions,
            match_semantics=MATCH_SEMANTICS,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(items), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    @classmethod
    def _evidence_ordering(cls, flow: str, order: str) -> tuple[Any, ...]:
        """Order the evidence rows totally, on the magnitude the tool reports.

        `txn_id` closes EVERY branch. Without a unique final key, rows sharing a
        date or an amount are free to come back in a different order per page,
        and an offset then skips some rows and repeats others.
        """
        magnitude = cls._magnitude_expression(flow)
        leading: Any
        if order == "date_desc":
            leading = TransactionDB.posting_date.desc()
        elif order == "amount_desc":
            leading = magnitude.desc()
        elif order == "amount_asc":
            leading = magnitude.asc()
        else:
            leading = TransactionDB.posting_date.asc()
        return (leading, TransactionDB.txn_id.asc())

    @staticmethod
    def _evidence_row(row, *, flow: str) -> EvidenceRow:
        """Map one stored row to the wire shape.

        `flow` here is the REQUESTED flow and selects the amount basis only.
        `EvidenceRow.flow` is the row's own stored parser label, which is a
        different vocabulary and is never the requested value: a `flow="any"`
        search reports each row's real label, never "any". Under `any` the basis
        is the magnitude for both kinds of row, which is what the income branch's
        positive signed value already equals.
        """
        return EvidenceRow(
            evidence_id=row.txn_id,
            posting_date=row.posting_date,
            amount=to_cents(row.amount_signed if flow == "income" else abs(row.amount_signed)),
            currency=row.currency or "CAD",
            flow=row.flow,
            cashflow_type=row.cashflow_type,
            source=row.merchant,
            category=row.category,
            institution=row.institution,
        )

    def get_transaction_evidence(self, *, evidence_id: str) -> EvidenceRow:
        """Re-read one evidence row by id, from the rows a search could already return.

        Scoped to the income OR spending predicates rather than to the raw table,
        so it cannot answer a question `search_transaction_evidence` refuses: a
        row the CAD-only cashflow policy hides is reported exactly as a row that
        does not exist. Both are `invalid_argument`, which the contract already
        defines -- an id the caller did not get from Ledger is not a valid one.
        """
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID_PATTERN.fullmatch(evidence_id):
            raise AgentQueryError
        with self._open_session() as session:
            row = (
                session.query(TransactionDB)
                .outerjoin(
                    TransactionTreatmentDB,
                    TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
                )
                .filter(
                    TransactionDB.txn_id == evidence_id,
                    or_(
                        and_(*income_sql_filters(), income_treatment_sql_filter()),
                        and_(*spend_sql_filters()),
                    ),
                )
                .first()
            )
            if row is None:
                raise AgentQueryError
            # Those predicates admit a positive amount only on the income side
            # and a negative one only on the spending side, so the sign names
            # which reporting rule the row came back under.
            return self._evidence_row(
                row,
                flow="income" if row.amount_signed > 0 else "spending",
            )

    @staticmethod
    def _validated_limit(limit: int) -> int:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_PAGE_SIZE
        ):
            raise AgentQueryError
        return limit

    @staticmethod
    def _validated_month(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _MONTH_PATTERN.fullmatch(value):
            raise AgentQueryError
        return value

    @staticmethod
    def _coverage_gaps(
        session: Session,
        refs: Mapping[str, str],
        salt: str,
        today: date,
    ) -> CoverageGapCensus:
        """Assess every covered account's trailing statement silence.

        Read over the WHOLE table rather than the requested window. Cadence is an
        account-level property estimated from that account's complete covered
        history, and a caller who asked about three months would otherwise be
        told every account's cadence is unclaimable.

        ONE POPULATION, SHARED WITH THE DASHBOARD. The gather, the institution
        tie-break, the lifecycle fallback and the malformed-period drop all live
        in `analysis/statement_population.py`, because `statement_frontier`
        publishes a universal over this same set and cites THIS block's rule as
        its authority. Two independent implementations of one published rule is
        how a dashboard and an MCP tool come to classify the same account
        differently after an edit to one of them.

        The population import is deferred because `ledger.analysis.__init__`
        imports `health_scorer`, which imports `ledger.query` -- a module-level
        import here is a cycle. Python caches it after the first call.
        """
        from ledger.analysis.statement_cadence import classify_gap, estimate_cadence
        from ledger.analysis.statement_population import gather_coverage_population

        # Counted in SQL, before anything is fetched. This read is deliberately
        # unfiltered and there is no narrower filter a caller could pass, so the
        # cap exists to bound the scan rather than to steer the request.
        coverage_rows = int(
            session.query(func.count()).select_from(StatementCoverageDB).scalar() or 0
        )
        if coverage_rows > _MAX_GAP_COVERAGE_ROWS:
            raise AgentQueryError("filter_too_broad")

        population = gather_coverage_population(session)

        gaps: list[CoverageGap] = []
        assessed = 0
        for slug in population.slugs():
            assessed += 1
            covered = population.periods[slug]
            cadence = estimate_cadence(covered)
            assessment = classify_gap(
                last_covered=covered[-1],
                cadence=cadence,
                today=today,
                lifecycle_status=population.status(slug),
            )
            if assessment.classification is None:
                continue
            ref, ref_status = resolve_account_ref(refs, slug, salt)
            gaps.append(
                CoverageGap(
                    account_ref=ref,
                    account_ref_status=ref_status,
                    last_covered_period=covered[-1],
                    classification=assessment.classification,
                    deciding_signal=assessment.deciding_signal,
                    elapsed_months=assessment.elapsed_months,
                    cadence_months=cadence.months,
                    cadence_confidence=None if cadence.months is None else cadence.confidence,
                    cadence_observations=cadence.observations,
                    missing_periods=assessment.missing_periods[:_MAX_MISSING_PERIODS],
                    missing_period_count=len(assessment.missing_periods),
                )
            )
        served = tuple(gaps[:_MAX_COVERAGE_GAPS])
        # Counted over what is SERVED, not over what was computed, so every
        # number in the block reconciles against the entries beside it even when
        # the list was cut.
        return CoverageGapCensus(
            gaps=served,
            counts=CoverageGapCounts(
                overdue=sum(1 for gap in served if gap.classification == "overdue"),
                dormant=sum(1 for gap in served if gap.classification == "dormant"),
                unknown=sum(1 for gap in served if gap.classification == "unknown"),
                naming_a_missing_statement=sum(1 for gap in served if gap.missing_period_count),
            ),
            assessed_accounts=assessed,
            unscoreable_accounts=len(population.all_slugs - set(population.periods)),
            truncated=len(gaps) > len(served),
        )

    def statement_coverage(
        self,
        *,
        start_period: str | None = None,
        end_period: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> CoveragePage:
        """Report which account-months have an imported statement.

        This is the one question the transaction table cannot answer: a month
        that produced no rows and a month that was never imported are the same
        emptiness there, and only one of them means a statement is outstanding.

        The account identity is emitted ONLY as the salted opaque ref, and only
        when the salt already exists. Minting it is a write (`ledger config
        ensure-account-salt`) and this reader cannot write, so a missing salt
        withholds the entries entirely rather than degrading to raw slugs.
        """
        # Deferred for the same import cycle `_coverage_gaps` documents, and
        # imported INSIDE the body so that patching the module attribute reaches
        # this call: a `from ... import` at module scope would freeze the seam.
        from ledger.analysis.statement_cadence import gap_evaluation_date

        start = self._validated_month(start_period)
        end = self._validated_month(end_period)
        limit = self._validated_limit(limit)
        if start is not None and end is not None and start > end:
            raise AgentQueryError
        fingerprint = _cursor_fingerprint(
            kind="statement_coverage",
            generation=self._snapshot_generation,
            scope={"end_period": end, "start_period": start},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            matched = session.query(StatementCoverageDB)
            if start is not None:
                matched = matched.filter(StatementCoverageDB.period >= start)
            if end is not None:
                matched = matched.filter(StatementCoverageDB.period <= end)
            entry_count = int(matched.count())
            empty = StatementCoverageDB.row_count <= 0
            counts = CoverageStatusCounts(
                imported_with_activity=int(
                    matched.filter(StatementCoverageDB.row_count > 0).count()
                ),
                imported_no_activity=int(
                    matched.filter(empty, StatementCoverageDB.skipped_reason != "").count()
                ),
                imported_no_rows_unexplained=int(
                    matched.filter(empty, StatementCoverageDB.skipped_reason == "").count()
                ),
            )
            observed = session.query(
                func.min(StatementCoverageDB.period),
                func.max(StatementCoverageDB.period),
            ).one()

            entries: tuple[CoverageEntry, ...] = ()
            # Every gap entry names an account, so the block fails closed on the
            # same terms the entries do: minting the salt is a write and this
            # reader cannot write.
            census = CoverageGapCensus(
                gaps=(),
                counts=CoverageGapCounts(
                    overdue=0, dormant=0, unknown=0, naming_a_missing_statement=0
                ),
                assessed_accounts=0,
                unscoreable_accounts=0,
                truncated=False,
            )
            # THE seam, and the only one: `ledger accounts list` imports the same
            # attribute, so the CLI and this tool classify the same account
            # against the same clock and a test patching it reaches both.
            evaluated_on = gap_evaluation_date()
            if account_ref_salt:
                # The ONE resolver, keyed on `account_slug` because that column
                # holds the account's `accounts.account_number`. The ref is NOT
                # derived from this row: `statement_type` is the parser's
                # statement label, and hashing it minted a coverage-only
                # namespace that matched no other tool on any account.
                refs = account_ref_map(session, account_ref_salt)
                # `account_slug` closes the ordering because it is half the
                # primary key, so an offset addresses one fixed row. It is a sort
                # key only -- it never reaches the caller.
                rows = (
                    matched.order_by(
                        StatementCoverageDB.period.asc(),
                        StatementCoverageDB.institution.asc(),
                        StatementCoverageDB.statement_type.asc(),
                        StatementCoverageDB.account_slug.asc(),
                    )
                    .offset(offset)
                    .limit(limit)
                    .all()
                )
                # Both branches through the shared minter, so an unjoinable slug
                # carries the same sentinel here as in the export file rather
                # than a blank key two different accounts would share.
                labelled = [
                    (row, *resolve_account_ref(refs, str(row.account_slug), account_ref_salt))
                    for row in rows
                ]
                entries = tuple(
                    CoverageEntry(
                        account_ref=ref,
                        account_ref_status=ref_status,
                        period=row.period,
                        institution=row.institution,
                        statement_type=row.statement_type,
                        row_count=int(row.row_count),
                        statement_count=int(row.statement_count),
                        status=(
                            "imported_with_activity"
                            if row.row_count > 0
                            else (
                                "imported_no_activity"
                                if row.skipped_reason
                                else "imported_no_rows_unexplained"
                            )
                        ),
                        imported_at=row.imported_at,
                    )
                    for row, ref, ref_status in labelled
                )
                # Deliberately NOT `matched`: gaps are a per-account verdict over
                # the account's complete covered history, so they do not move
                # with the requested window or with the page offset -- which also
                # means the block repeats in full on every page of a cursor walk,
                # a property the tool description states so a client following the
                # pagination protocol does not accumulate it.
                census = self._coverage_gaps(session, refs, account_ref_salt, evaluated_on)

        # Withheld entries are never served, so there is no next page to offer.
        # Advertising one would page a caller through nothing forever.
        has_more = bool(account_ref_salt) and offset + len(entries) < entry_count
        return CoveragePage(
            entries=entries,
            entry_count=entry_count,
            status_counts=counts,
            observed_start=str(observed[0]) if observed[0] is not None else None,
            observed_end=str(observed[1]) if observed[1] is not None else None,
            account_refs_disclosed=bool(account_ref_salt),
            gap_census=census,
            gaps_evaluated_on=evaluated_on,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    @staticmethod
    def _as_datetime(value: object) -> datetime | None:
        """A stored DATETIME, whichever form the driver handed back."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            raise AgentQueryError("corrupt_data") from None

    @classmethod
    def _table_stamp_bounds(cls, session, column) -> tuple[datetime | None, datetime | None]:
        """One stamped table's own (earliest, latest) import stamps.

        Per table, because the verdict on a count belongs to the table the count
        came from. Both ends are read in ONE aggregate query, so the two bounds
        cannot be taken from different reads of a moving table.
        """
        row = session.query(func.min(column), func.max(column)).one()
        return cls._as_datetime(row[0]), cls._as_datetime(row[1])

    @staticmethod
    def _combined_bound(
        stamps: tuple[datetime | None, ...],
        *,
        newest: bool,
    ) -> datetime | None:
        """One end of the snapshot's import-stamp range, over BOTH stamped tables.

        `ledger.status` computes the same pair in `ReadOnlyDatabase.preflight`, and
        the two must agree exactly: `latest_imported_at` is the boundary a caller
        is told to echo back, and a status value that lags this one re-reports its
        own import on every call.
        """
        present = [stamp for stamp in stamps if stamp is not None]
        if not present:
            return None
        return max(present) if newest else min(present)

    @staticmethod
    def _stamp_relation(
        since: datetime,
        earliest: datetime | None,
        latest: datetime | None,
    ) -> str:
        """Where a boundary sits against ONE set of import stamps."""
        if earliest is None or latest is None:
            return RELATION_NO_IMPORTS
        if since >= latest:
            return RELATION_AFTER_LATEST
        if since < earliest:
            return RELATION_BEFORE_EARLIEST
        return RELATION_WITHIN_RANGE

    @classmethod
    def _count_source(
        cls,
        since: datetime,
        bounds: tuple[datetime | None, datetime | None],
    ) -> ChangeCountSource:
        """The verdict on the counts drawn from one table, from that table's bounds.

        A count is that table's ENTIRE contents exactly when the boundary precedes
        the table's own earliest stamp -- never when it merely precedes the other
        table's. A table with no stamps at all counts zero because there is nothing
        to count, which is neither a delta nor a whole table worth calling one.
        """
        earliest, latest = bounds
        relation = cls._stamp_relation(since, earliest, latest)
        if relation == RELATION_NO_IMPORTS:
            basis = SOURCE_BASIS_NO_IMPORTS
        elif relation == RELATION_BEFORE_EARLIEST:
            basis = SOURCE_BASIS_WHOLE_TABLE
        else:
            basis = SOURCE_BASIS_DELTA
        return ChangeCountSource(
            basis=basis,
            since_relation=relation,
            earliest_imported_at=earliest,
            latest_imported_at=latest,
        )

    def changes(
        self,
        *,
        since: datetime,
        client_generation: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> ChangesPage:
        """Count what arrived after a boundary -- or refuse to call it a delta.

        The boundary is compared against ``imported_at``, which is stamped once
        per parsed FILE (``ParseContext.__init__``) and is PRESERVED by a normal
        incremental import, because staging clones the live database. Every
        ``repair statements`` and ``rebuild-db`` promote, by contrast, re-parses
        and re-stamps every row.

        That difference IS the discriminator (D12/D13), and it is read per counted
        table from the table's own earliest surviving stamp. A boundary older than
        it selects everything the table holds, which is what a restamp leaves
        behind and what an incremental import never produces -- so the count is
        labelled the whole table rather than a delta. The verdict is derived from
        the data it describes.

        `client_generation` is NOT that discriminator and cannot be. The snapshot
        generation hashes the pinned file's size and mtime along with the
        transaction count, so every write rotates it and a routine import moves it
        exactly as much as a repair does; gating on it mismatched precisely when
        something had arrived. It is accepted as an OPTIONAL input, compared
        against the pin serving this call, and reported through
        ``generation_changed`` so a caller can see the data moved. It never
        suppresses a count.

        `since` is naive UTC, matching storage, and is parsed to a datetime
        before it reaches SQL: SQLite stores these as ``YYYY-MM-DD HH:MM:SS`` and
        a raw string comparison against the ISO ``T`` form is wrong for every row
        in the same direction, which reads as "nothing was imported".

        `statement_coverage` is joined in so the answer can name which STATEMENTS
        arrived. That is the only way a statement which produced no rows is
        visible at all -- in `transactions` it is indistinguishable from a month
        nobody imported.
        """
        if not isinstance(since, datetime) or since.tzinfo is not None:
            raise AgentQueryError
        if client_generation is not None and (
            not isinstance(client_generation, str)
            or not client_generation
            or len(client_generation) > 64
        ):
            raise AgentQueryError
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="changes",
            generation=self._snapshot_generation,
            scope={
                # Bound even though it selects no row: the cursor law binds every
                # INPUT, and a page walked under one stated observation is not the
                # same request as the same page walked under another. Null when
                # omitted, so supplying it mid-walk is a changed input too.
                "client_generation": client_generation,
                # Normalized exactly as the query normalizes it, so two spellings
                # of one instant do not mint two cursor shapes.
                "since": since.isoformat(),
            },
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        # Informational, and computed nowhere near the counts: the whole defect
        # this replaced was letting this comparison decide what could be reported.
        generation_changed = (
            None if client_generation is None else client_generation != self._snapshot_generation
        )

        with self._open_session() as session:
            # PER TABLE, because each table's counts are judged against that
            # table's own earliest stamp. The combined pair below is a different
            # quantity with a different job: `latest_imported_at` is the value
            # ledger.status emits and the caller echoes back, and the two bounds
            # together disclose the span of the stored import stamps, including
            # a narrow span left by a rebuild that restamped every row.
            row_bounds = self._table_stamp_bounds(session, TransactionDB.imported_at)
            statement_bounds = self._table_stamp_bounds(session, StatementCoverageDB.imported_at)
            earliest = self._combined_bound(
                (row_bounds[0], statement_bounds[0]),
                newest=False,
            )
            latest = self._combined_bound((row_bounds[1], statement_bounds[1]), newest=True)
            relation = self._stamp_relation(since, earliest, latest)

            # No branch returns early here. There used to be one, taken whenever
            # the supplied generation differed from the pin, and it returned null
            # counts under `basis: "rebuilt"` -- on exactly the calls that had an
            # import to report, because the import itself rotated the pin.
            count_sources = ChangeCountSources(
                rows=self._count_source(since, row_bounds),
                statements=self._count_source(since, statement_bounds),
            )

            total_added = int(
                session.query(func.count(func.distinct(TransactionDB.txn_id)))
                .filter(TransactionDB.imported_at > since)
                .scalar()
                or 0
            )
            # THE CAP, IN SQL AND BEFORE ANY FETCH. Both result sets are merged in
            # Python, so the merge is the scan -- and the check ran after `.all()`
            # on the grouped rows and on full `StatementCoverageDB` ORM objects,
            # which fired the tripwire only once the memory it exists to bound had
            # been allocated. `_MAX_ACCOUNT_GROUPS` counts in SQL and raises first;
            # this counts the same unit the cap is written in, one entry per
            # account-period, over the union of the two key sets.
            row_keys = (
                session.query(
                    TransactionDB.account.label("account"),
                    TransactionDB.source_period.label("period"),
                )
                .filter(TransactionDB.imported_at > since)
                .distinct()
            )
            statement_keys = (
                session.query(
                    StatementCoverageDB.account_slug.label("account"),
                    StatementCoverageDB.period.label("period"),
                )
                .filter(StatementCoverageDB.imported_at > since)
                .distinct()
            )
            entry_count = int(
                session.query(func.count())
                .select_from(row_keys.union(statement_keys).subquery())
                .scalar()
                or 0
            )
            if entry_count > _MAX_CHANGE_ENTRIES:
                raise AgentQueryError("filter_too_broad")

            entries: tuple[ChangeEntry, ...] = ()
            if account_ref_salt:
                entries = self._change_entries(
                    session,
                    since=since,
                    offset=offset,
                    limit=limit,
                    account_ref_salt=account_ref_salt,
                )

        # Withheld entries are never served, so there is no next page to offer.
        has_more = bool(account_ref_salt) and offset + len(entries) < entry_count
        # Every count is exact either way; only its MEANING differs, and the
        # meaning is per table. The collapse is deliberately the WEAKER verdict:
        # `whole_database` as soon as either count is the whole of the table it
        # counts, because a caller that reads `delta` reports newly arrived
        # activity, and one whole table presented that way is exactly the answer
        # that reads a rebuild as an import.
        whole_table = {count_sources.rows.basis, count_sources.statements.basis}
        basis = BASIS_WHOLE_DATABASE if SOURCE_BASIS_WHOLE_TABLE in whole_table else BASIS_DELTA
        return ChangesPage(
            entries=entries,
            entry_count=entry_count,
            total_added=total_added,
            basis=basis,
            delta_available=basis == BASIS_DELTA,
            since_relation=relation,
            count_sources=count_sources,
            latest_imported_at=latest,
            earliest_imported_at=earliest,
            account_refs=(ACCOUNT_REFS_DISCLOSED if account_ref_salt else ACCOUNT_REFS_WITHHELD),
            generation_changed=generation_changed,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    @staticmethod
    def _change_entries(
        session,
        *,
        since: datetime,
        offset: int,
        limit: int,
        account_ref_salt: str,
    ) -> tuple[ChangeEntry, ...]:
        """One entry per account-period that changed, for one page.

        Bucketed on (account slug, statement period) -- which is `statement_coverage`'s
        OWN primary key -- and never on the institution. The institution was a key
        and is parser-supplied on `transactions` while coverage derives it from the
        statement type, so a drift between the two spellings put the rows and the
        very statement that produced them in DIFFERENT buckets, and the row bucket
        reported `no_statement_in_window` about a statement that was right there.
        That is the mirror of the error this surface exists to prevent: it makes a
        statement that DID produce rows read as absent.

        The institution is now DERIVED, from the same authority `account_ref`
        resolves through: the stored `accounts` record. A slug with no such record
        has no authority to read, so it reports the spelling its own rows carried,
        chosen in sorted order rather than by whichever row was read last.

        Reached only when a salt was minted. Without one every entry is withheld,
        and materializing a set that is never served is work done to be discarded.
        """
        added_rows = (
            session.query(
                TransactionDB.account.label("account"),
                TransactionDB.source_period.label("period"),
                TransactionDB.institution.label("institution"),
                func.count(func.distinct(TransactionDB.txn_id)).label("rows_added"),
            )
            .filter(TransactionDB.imported_at > since)
            .group_by(
                TransactionDB.account,
                TransactionDB.source_period,
                TransactionDB.institution,
            )
            .all()
        )
        statements = (
            session.query(
                StatementCoverageDB.account_slug.label("account"),
                StatementCoverageDB.period.label("period"),
                StatementCoverageDB.institution.label("institution"),
                StatementCoverageDB.row_count.label("row_count"),
                StatementCoverageDB.statement_count.label("statement_count"),
                StatementCoverageDB.skipped_reason.label("skipped_reason"),
            )
            .filter(StatementCoverageDB.imported_at > since)
            .all()
        )

        buckets: dict[tuple[str, str], _ChangeBucket] = {}
        for row in added_rows:
            bucket = buckets.setdefault((str(row.account), str(row.period or "")), _ChangeBucket())
            bucket.rows_added += int(row.rows_added)
            bucket.observed_institutions.add(str(row.institution or ""))
        for statement in statements:
            bucket = buckets.setdefault(
                (str(statement.account), str(statement.period or "")), _ChangeBucket()
            )
            bucket.statement_count += int(statement.statement_count)
            bucket.statement_row_count += int(statement.row_count)
            if not statement.skipped_reason:
                bucket.unexplained = True
            bucket.observed_institutions.add(str(statement.institution or ""))

        # Ordered by the STORED SLUG, never by the derived ref. The ref is an HMAC
        # of the account-ref salt, and the salt is NOT bound into the cursor
        # fingerprint, so a ref sort key made a replayed cursor address a different
        # entry under a different salt: an ordinary success envelope with an
        # account silently never served. Ordering on the slug removes the coupling
        # instead of papering over it -- the same reason `ledger.coverage` sorts on
        # `account_slug`. The slug is a sort key only and never reaches the caller.
        #
        # The key is unique per entry, so the order is total and an offset
        # addresses one fixed entry. Never ordered by count: that is a magnitude
        # ranking, and this is a log.
        ordered_keys = sorted(buckets, key=lambda key: (key[1], key[0]))
        refs = account_ref_map(session, account_ref_salt)
        institutions = {
            str(number): str(institution)
            for number, institution in session.query(
                AccountDB.account_number, AccountDB.institution
            ).all()
        }
        page: list[ChangeEntry] = []
        for account, period in ordered_keys[offset : offset + limit]:
            bucket = buckets[(account, period)]
            ref, ref_status = resolve_account_ref(refs, account, account_ref_salt)
            observed = sorted(bucket.observed_institutions)
            page.append(
                ChangeEntry(
                    account_ref=ref,
                    account_ref_status=ref_status,
                    period=period,
                    institution=institutions.get(account, observed[0] if observed else ""),
                    rows_added=bucket.rows_added,
                    statement_status=bucket.statement_status(),
                    statement_count=bucket.statement_count,
                    statement_row_count=bucket.statement_row_count,
                )
            )
        return tuple(page)

    @staticmethod
    def _latest_balance_row(
        session,
        account_id: str,
        *,
        balance_basis: str,
        on_or_before: date | None = None,
        before: date | None = None,
    ):
        """The account's most recent row of one basis within a date bound.

        Carry-forward is the right reading for both bases: a balance series
        records observations, and the level between two observations is the
        earlier one, not a gap.
        """
        query = session.query(AccountBalanceDB).filter(
            AccountBalanceDB.account_id == account_id,
            AccountBalanceDB.balance_basis == balance_basis,
        )
        if on_or_before is not None:
            query = query.filter(AccountBalanceDB.balance_date <= on_or_before)
        if before is not None:
            query = query.filter(AccountBalanceDB.balance_date < before)
        return query.order_by(AccountBalanceDB.balance_date.desc()).first()

    @staticmethod
    def _printed_credit_limit_slugs(session) -> frozenset[str]:
        """Account slugs whose credit limit was PRINTED on a statement.

        Existence only -- this surface never divides, it reports the percentage
        the analysis layer already stored. What it needs from this table is the
        one thing ``AccountDB.detected_credit_limit`` cannot tell it: whether
        that limit was printed or estimated from the deepest balance the card
        ever owed. The estimate is a lower bound, so a percentage over it reads
        near 100% at the card's worst moment by construction.
        """
        rows = (
            session.query(AccountStatementCreditLimitDB.account_slug)
            .filter(AccountStatementCreditLimitDB.credit_limit > 0)
            .distinct()
            .all()
        )
        return frozenset(str(row[0]) for row in rows)

    @staticmethod
    def _credit_utilization(
        account,
        row,
        printed_limit_slugs: frozenset[str],
    ) -> tuple[Decimal | None, str]:
        """The stored percentage, or the reason there is not one.

        Every refusal is named. A null percentage rendered as "0% utilized" is
        the most reassuring possible reading of "we do not know", and the four
        reasons call for four different actions.

        The printed-limit check comes BEFORE the stored value, deliberately: a
        percentage computed against an estimated limit survives on the row until
        the next reconciliation clears it, and serving it because it happens to
        be there would defeat the provenance rule.
        """
        if not bool(account.is_credit_card):
            return None, "not_a_credit_card"
        if str(account.account_number) not in printed_limit_slugs:
            return None, "no_printed_credit_limit"
        if row.credit_utilization is None:
            return None, "not_computed"
        return to_cents(row.credit_utilization), "reported"

    def _balance_level(
        self,
        session,
        account,
        *,
        as_of: date,
        printed_limit_slugs: frozenset[str],
    ) -> BalanceLevel:
        row = self._latest_balance_row(
            session,
            str(account.account_id),
            balance_basis="absolute",
            on_or_before=as_of,
        )
        if row is None:
            # Two different absences. "Never anchored" needs a statement import;
            # "not yet on this date" needs a later date. One null for both would
            # hide which.
            anchored = self._latest_balance_row(
                session, str(account.account_id), balance_basis="absolute"
            )
            return BalanceLevel(
                status=(
                    "no_anchor_on_or_before_as_of" if anchored is not None else "no_absolute_anchor"
                ),
                balance=None,
                balance_basis=None,
                balance_date=None,
                as_of_lag_days=None,
                credit_utilization_pct=None,
                credit_utilization_status="no_level",
            )
        utilization, utilization_status = self._credit_utilization(
            account, row, printed_limit_slugs
        )
        return BalanceLevel(
            status="reported",
            balance=to_cents(row.balance),
            balance_basis="absolute",
            balance_date=row.balance_date,
            # The whole staleness disclosure. An anchor exists only inside a
            # statement window, so a card paid off after its last statement still
            # reads as that statement's debt unless the age travels with it.
            as_of_lag_days=(as_of - row.balance_date).days,
            credit_utilization_pct=utilization,
            credit_utilization_status=utilization_status,
        )

    def _balance_change(
        self,
        session,
        account_id: str,
        *,
        change_since: date,
        as_of: date,
    ) -> BalanceChange:
        closing = self._latest_balance_row(
            session, account_id, balance_basis="relative", on_or_before=as_of
        )
        if closing is None:
            return BalanceChange(
                status="no_closing_observation",
                amount=None,
                from_date=change_since,
                to_date=as_of,
            )
        if closing.balance_date < change_since:
            # The series ended before the window opened: closing and opening
            # would resolve to the SAME stale row and the subtraction would
            # report 0.00 with status "reported" — the most reassuring possible
            # reading of a window holding zero observations. Say what is true
            # instead: nothing was observed inside the window.
            return BalanceChange(
                status="no_observation_in_window",
                amount=None,
                from_date=change_since,
                to_date=as_of,
                observed_through=closing.balance_date,
            )
        # STRICTLY before the window opens, so the window is inclusive at both
        # ends like every other date bound on this surface: the reading ON
        # `change_since` already contains that day's movement.
        opening = self._latest_balance_row(
            session, account_id, balance_basis="relative", before=change_since
        )
        if opening is None:
            # The series' seeded zero is not an opening level, and subtracting it
            # would silently turn a change into the relative level this tool
            # refuses to report.
            return BalanceChange(
                status="no_opening_observation",
                amount=None,
                from_date=change_since,
                to_date=as_of,
                observed_through=closing.balance_date,
            )
        return BalanceChange(
            status="reported",
            amount=to_cents(closing.balance) - to_cents(opening.balance),
            from_date=change_since,
            to_date=as_of,
            observed_through=closing.balance_date,
        )

    def account_balances(
        self,
        *,
        as_of: date,
        change_since: date | None = None,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> BalancePage:
        """Report each account's signed balance LEVEL as of a date.

        The refusal is the feature. ``account_balances`` holds two series per
        account and only one of them is a level: the ``absolute`` series is
        anchored to a printed statement balance, while the ``relative`` series is
        seeded at 0 on the account's first imported transaction. Serving the
        relative one as "your balance" would not be an approximation, it would be
        a different quantity, so an account with no anchor reports no level and
        names which absence applies. The relative series reaches the caller only
        as an explicitly labelled change between two dates.

        Signs are the stored convention (see ``AccountBalanceDB``): POSITIVE is
        money held, NEGATIVE is money owed. These levels, projection, budgets,
        net worth, financial briefs, metric explanations, cash plans, and plan
        reconciliation share the signed money contract (only variance is signed
        on budgets). The published signed tool set is checked by
        ``test_the_tools_named_as_serving_signed_amounts_are_the_ones_that_do``,
        so a new signed surface must update the contract documentation too.

        Accounts are identified ONLY by the salted opaque ref, and only when the
        salt already exists -- minting it is a write, so a missing salt withholds
        the entries entirely rather than degrading to raw slugs.
        """
        if not isinstance(as_of, date):
            raise AgentQueryError
        if change_since is not None and (
            not isinstance(change_since, date) or change_since > as_of
        ):
            raise AgentQueryError
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="balances",
            generation=self._snapshot_generation,
            scope={
                "as_of": as_of.isoformat(),
                "change_since": None if change_since is None else change_since.isoformat(),
            },
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            # `account_id` closes the ordering because it is the primary key, so
            # an offset addresses one fixed row. It is a sort key only -- it never
            # reaches the caller. Ordering is by ACCOUNT and never by amount: the
            # aggregate's magnitude sort inverts on negatives, and these are the
            # only signed amounts on the surface.
            accounts = (
                session.query(AccountDB)
                .order_by(
                    AccountDB.institution.asc(),
                    AccountDB.account_type.asc(),
                    AccountDB.account_id.asc(),
                )
                .all()
            )
            if len(accounts) > _MAX_BALANCE_ACCOUNTS:
                raise AgentQueryError("filter_too_broad")
            entry_count = len(accounts)
            printed_limit_slugs = self._printed_credit_limit_slugs(session)
            # Every account, not just the page: the counts describe the whole
            # answer and stay legible when the entries are withheld.
            levels = [
                self._balance_level(
                    session,
                    account,
                    as_of=as_of,
                    printed_limit_slugs=printed_limit_slugs,
                )
                for account in accounts
            ]
            counts = BalanceLevelCounts(
                reported=sum(1 for level in levels if level.status == "reported"),
                no_absolute_anchor=sum(
                    1 for level in levels if level.status == "no_absolute_anchor"
                ),
                no_anchor_on_or_before_as_of=sum(
                    1 for level in levels if level.status == "no_anchor_on_or_before_as_of"
                ),
            )

            entries: tuple[BalanceEntry, ...] = ()
            if account_ref_salt:
                # Through the shared resolver, which derives exactly what this
                # method derived inline: the values do not move.
                #
                # Subscripted, not `resolve_account_ref`, because this surface
                # CANNOT reach the unjoinable case coverage reaches: `entries`
                # are `AccountDB` rows and `account_ref_map` is built by
                # iterating that same table on that same key, so every lookup
                # resolves by construction. Coverage differs because
                # `statement_coverage` carries no foreign key and is written at
                # finalize before account detection exists. Keeping the subscript
                # makes any future page that sourced rows from somewhere else
                # fail loudly here instead of quietly minting a sentinel for an
                # account that has a record.
                refs = account_ref_map(session, account_ref_salt)
                entries = tuple(
                    BalanceEntry(
                        account_ref=refs[str(account.account_number)],
                        institution=str(account.institution),
                        account_type=str(account.account_type),
                        is_credit_card=bool(account.is_credit_card),
                        level=level,
                        change=(
                            None
                            if change_since is None
                            else self._balance_change(
                                session,
                                str(account.account_id),
                                change_since=change_since,
                                as_of=as_of,
                            )
                        ),
                    )
                    for account, level in list(zip(accounts, levels, strict=True))[
                        offset : offset + limit
                    ]
                )

        # Withheld entries are never served, so there is no next page to offer.
        has_more = bool(account_ref_salt) and offset + len(entries) < entry_count
        return BalancePage(
            entries=entries,
            entry_count=entry_count,
            level_counts=counts,
            account_refs_disclosed=bool(account_ref_salt),
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def net_worth(
        self,
        *,
        as_of: date,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> NetWorthPage:
        """Compose statement-backed banking and investment values by currency.

        Banking contributes only its latest ``absolute`` anchor on or before
        ``as_of``. Investment contributes only its latest statement NAV on or
        before that date, including a closed account's last-known NAV. Values
        stay in native currency and exact Decimal form through subtotaling; no
        exchange-rate source exists and no cross-currency total is computed.

        The page contains identities, so it follows every other account-bearing
        surface: absent salt withholds the component rows rather than exposing a
        banking slug, investment alias, source id, or transfer destination.
        """
        if not isinstance(as_of, date):
            raise AgentQueryError
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="net_worth",
            generation=self._snapshot_generation,
            scope={"as_of": as_of.isoformat()},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            banking_count = int(session.query(func.count(AccountDB.account_id)).scalar() or 0)
            investment_count = int(
                session.query(func.count(InvestmentAccountDB.account_id)).scalar() or 0
            )
            if banking_count + investment_count > _MAX_NET_WORTH_COMPONENTS:
                raise AgentQueryError("filter_too_broad")

            banking_accounts = (
                session.query(AccountDB)
                .order_by(
                    AccountDB.institution.asc(),
                    AccountDB.account_type.asc(),
                    AccountDB.account_id.asc(),
                )
                .all()
            )
            investment_accounts = (
                session.query(InvestmentAccountDB)
                .order_by(
                    InvestmentAccountDB.institution.asc(),
                    InvestmentAccountDB.account_type.asc(),
                    InvestmentAccountDB.account_id.asc(),
                )
                .all()
            )

            statement_query = session.query(InvestmentStatementDB).filter(
                InvestmentStatementDB.period_end <= as_of
            )
            if statement_query.count() > _MAX_NET_WORTH_STATEMENTS:
                raise AgentQueryError("filter_too_broad")
            statements = statement_query.order_by(
                InvestmentStatementDB.account_id.asc(),
                InvestmentStatementDB.period_end.desc(),
                InvestmentStatementDB.period_start.desc(),
                InvestmentStatementDB.statement_id.asc(),
            ).all()
            latest_statements: dict[str, InvestmentStatementDB] = {}
            for statement in statements:
                latest_statements.setdefault(str(statement.account_id), statement)

            stored_banking_ids = {
                str(account.account_number): str(account.account_id) for account in banking_accounts
            }
            lifecycle_candidates = {
                str(account.account_id): lifecycle_account_ids(
                    stored_banking_ids,
                    str(account.account_number),
                    str(account.institution),
                )
                for account in banking_accounts
            }
            candidate_ids = tuple(
                dict.fromkeys(
                    candidate
                    for candidates in lifecycle_candidates.values()
                    for candidate in candidates
                )
            )
            lifecycle_by_id = {
                str(row.account_id): row
                for row in (
                    session.query(AccountLifecycleDB)
                    .filter(AccountLifecycleDB.account_id.in_(candidate_ids))
                    .all()
                    if candidate_ids
                    else []
                )
            }
            banking_refs = account_ref_map(session, account_ref_salt)
            investment_refs = investment_account_ref_map(session, account_ref_salt)

            # The private database id closes the order but is discarded with the
            # tuple wrapper. It can never reach ``NetWorthComponent`` or a mapper.
            ranked: list[tuple[tuple[str, str, str, str], NetWorthComponent]] = []
            anchored_ever, _anchored_by_as_of = self._absolute_anchor_account_ids(
                session, as_of=as_of
            )
            banking_reported = 0
            banking_no_anchor_by_as_of = 0
            banking_never_anchored = 0
            investment_reported = 0
            investment_no_statement = 0
            for banking_account in banking_accounts:
                anchor = self._latest_balance_row(
                    session,
                    str(banking_account.account_id),
                    balance_basis="absolute",
                    on_or_before=as_of,
                )
                if anchor is None:
                    if str(banking_account.account_id) in anchored_ever:
                        banking_no_anchor_by_as_of += 1
                    else:
                        banking_never_anchored += 1
                    continue
                banking_reported += 1
                lifecycle = next(
                    (
                        lifecycle_by_id[candidate]
                        for candidate in lifecycle_candidates[str(banking_account.account_id)]
                        if candidate in lifecycle_by_id
                    ),
                    None,
                )
                ranked.append(
                    (
                        (
                            "banking",
                            str(banking_account.institution),
                            str(banking_account.account_type),
                            str(banking_account.account_id),
                        ),
                        NetWorthComponent(
                            account_ref=(
                                banking_refs[str(banking_account.account_number)]
                                if account_ref_salt
                                else ""
                            ),
                            component_type="banking",
                            institution=str(banking_account.institution),
                            account_type=str(banking_account.account_type),
                            amount=Decimal(anchor.balance),
                            currency="CAD",
                            basis="absolute",
                            as_of=anchor.balance_date,
                            as_of_lag_days=(as_of - anchor.balance_date).days,
                            status=("not_recorded" if lifecycle is None else str(lifecycle.status)),
                            closed_on=None if lifecycle is None else lifecycle.closed_on,
                            final_statement_outstanding=None,
                        ),
                    )
                )

            for investment_account in investment_accounts:
                latest_statement = latest_statements.get(str(investment_account.account_id))
                if latest_statement is None:
                    investment_no_statement += 1
                    continue
                investment_reported += 1
                identity = (
                    str(investment_account.institution),
                    str(investment_account.account_alias),
                    str(investment_account.account_type),
                )
                ranked.append(
                    (
                        (
                            "investment",
                            str(investment_account.institution),
                            str(investment_account.account_type),
                            str(investment_account.account_id),
                        ),
                        NetWorthComponent(
                            account_ref=(investment_refs[identity] if account_ref_salt else ""),
                            component_type="investment",
                            institution=str(investment_account.institution),
                            account_type=str(investment_account.account_type),
                            # ExactFixedPoint returns Decimal from canonical TEXT.
                            # Do not route it through float or quantize before the
                            # per-currency subtotal is computed.
                            amount=Decimal(latest_statement.total_nav),
                            currency=str(latest_statement.base_currency),
                            basis="last_known",
                            as_of=latest_statement.period_end,
                            as_of_lag_days=(as_of - latest_statement.period_end).days,
                            status=str(investment_account.status),
                            closed_on=investment_account.closed_on,
                            final_statement_outstanding=(
                                None
                                if str(investment_account.status) == "unknown"
                                else (
                                    str(investment_account.status) == "closed"
                                    and investment_account.status_confirmed_on is not None
                                    and investment_account.status_confirmed_on
                                    > latest_statement.period_end
                                )
                            ),
                        ),
                    )
                )

        ranked.sort(key=lambda item: item[0])
        all_components = tuple(component for _key, component in ranked)
        subtotal_amounts: dict[str, Decimal] = {}
        subtotal_counts: dict[str, int] = {}
        for component in all_components:
            subtotal_amounts[component.currency] = (
                subtotal_amounts.get(component.currency, Decimal("0")) + component.amount
            )
            subtotal_counts[component.currency] = subtotal_counts.get(component.currency, 0) + 1
        subtotals = tuple(
            NetWorthSubtotal(
                currency=currency,
                amount=subtotal_amounts[currency],
                component_count=subtotal_counts[currency],
            )
            for currency in sorted(subtotal_amounts)
        )

        components: tuple[NetWorthComponent, ...] = ()
        if account_ref_salt:
            components = all_components[offset : offset + limit]
        has_more = bool(account_ref_salt) and offset + len(components) < len(all_components)
        return NetWorthPage(
            components=components,
            component_count=len(all_components),
            component_counts=NetWorthComponentCounts(
                banking_reported=banking_reported,
                banking_no_anchor_on_or_before_as_of=banking_no_anchor_by_as_of,
                banking_no_absolute_anchor=banking_never_anchored,
                investment_reported=investment_reported,
                investment_no_statement_on_or_before_as_of=investment_no_statement,
            ),
            subtotals=subtotals,
            account_refs_disclosed=bool(account_ref_salt),
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(components), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    @staticmethod
    def _absolute_anchor_account_ids(
        session, *, as_of: date
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Which accounts hold an absolute anchor at all, and which hold one by
        ``as_of``: the two existence facts an anchor OUTCOME is decided from.

        Two distinct-value queries rather than two per account. The census
        describes the whole answer rather than the page, so it must be computed
        over every account -- but computing it by building each account's anchor
        made a cursor walk quadratic: at the published 500-account ceiling a
        conforming ``limit=1`` walk issued up to 1000 balance queries per page,
        on 500 pages, to serve one row each time.
        """
        anchored = frozenset(
            str(account_id)
            for (account_id,) in session.query(AccountBalanceDB.account_id)
            .filter(AccountBalanceDB.balance_basis == "absolute")
            .distinct()
        )
        by_as_of = frozenset(
            str(account_id)
            for (account_id,) in session.query(AccountBalanceDB.account_id)
            .filter(
                AccountBalanceDB.balance_basis == "absolute",
                AccountBalanceDB.balance_date <= as_of,
            )
            .distinct()
        )
        return anchored, by_as_of

    def _projection_anchor(
        self,
        session,
        account_id: str,
        *,
        as_of: date,
    ) -> ProjectionAnchor:
        """The absolute anchor a projection starts from, or the refusal.

        Identical selection to `_balance_level`, deliberately: the two surfaces
        must not disagree about what an account's balance was on a date. What
        this one drops is the credit utilization, which is a property of a level
        and not of a projection.
        """
        row = self._latest_balance_row(
            session, account_id, balance_basis="absolute", on_or_before=as_of
        )
        if row is None:
            anchored = self._latest_balance_row(session, account_id, balance_basis="absolute")
            return ProjectionAnchor(
                status=(
                    "no_anchor_on_or_before_as_of" if anchored is not None else "no_absolute_anchor"
                ),
                balance=None,
                balance_basis=None,
                balance_date=None,
                as_of_lag_days=None,
            )
        return ProjectionAnchor(
            status="reported",
            balance=to_cents(row.balance),
            balance_basis="absolute",
            balance_date=row.balance_date,
            # Disclose the anchor's age so a projection based on an older
            # statement balance cannot be mistaken for a current balance.
            as_of_lag_days=(as_of - row.balance_date).days,
        )

    @staticmethod
    def _projection_patterns(session) -> dict[str, list[RecurringInput]]:
        """Every recurring pattern, grouped by the account it belongs to.

        `recurring_patterns.account_id` is a foreign key into `accounts`.
        It is an unsalted truncated hash of institution|slug, so it is used
        internally and never emitted. `institution` cannot stand in for it:
        several accounts can share an institution while holding distinct patterns.
        """
        from ledger.analysis.projection import RecurringInput

        grouped: dict[str, list[RecurringInput]] = {}
        for row in session.query(RecurringPatternDB).all():
            grouped.setdefault(str(row.account_id), []).append(
                RecurringInput(
                    pattern_id=str(row.pattern_id),
                    merchant=str(row.merchant),
                    category=str(row.category),
                    pattern_type=str(row.pattern_type),
                    frequency=str(row.frequency),
                    expected_day_of_period=int(row.expected_day_of_period),
                    expected_amount=Decimal(str(row.expected_amount)),
                    next_expected_date=row.next_expected_date,
                    first_occurrence=row.first_occurrence,
                    missed_count=int(row.missed_count),
                    # Clamped exactly as the detector clamps it on write
                    # (`max(0.0, min(1.0, consistency))`). The column is an
                    # unconstrained Float, and a value outside the unit interval
                    # is not a confidence -- serving one would fail the published
                    # two-decimal schema and take the whole answer down as
                    # corrupt data over one malformed row.
                    confidence=max(0.0, min(1.0, float(row.confidence))),
                    is_active=bool(row.is_active),
                )
            )
        return grouped

    def projection(
        self,
        *,
        as_of: date,
        target_date: date,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> ProjectionPage:
        """Project each account's balance to a date from its own recurring flows.

        The answer is exact arithmetic over disclosed inputs, not a forecast:
        ``anchor + sum(line items) == projected balance`` per account, to the
        cent, because the projection IS that sum. Everything that could make it
        wrong is on the wire beside it -- the anchor's age, how many periods each
        expected date sits from the stored one, what was omitted and why.

        What it does NOT contain is stated as loudly as what it does (D7).
        Transfers between accounts -- card payments and savings sweeps -- live in
        a separate table with zero `txn_id` overlap with `transactions`, which is
        what the recurring detector reads, so they are structurally invisible
        here. Omitting transfers can materially change the projected balance,
        so the response must disclose that limitation.

        A pattern is attributed to the account EVERY ONE of its occurrences
        posted to: the detector groups by account and merchant together, so a
        card-hosted charge draws down the card. What that does not do is follow a
        merchant that MOVES: the old account keeps its pattern until the missed
        occurrences retire it, and the new account has none until the detector
        has seen enough there to claim one. That limit is published as
        `attribution_semantics` rather than left for a caller to discover.
        """
        from ledger.analysis.projection import (
            ProjectionTooBroadError,
            build_projection,
            validate_window,
        )

        if not isinstance(as_of, date) or not isinstance(target_date, date):
            raise AgentQueryError
        try:
            # The same bound the pure module enforces, applied BEFORE the
            # database is opened: an out-of-horizon request is refused rather
            # than read for.
            validate_window(as_of=as_of, target_date=target_date)
        except ValueError:
            raise AgentQueryError from None
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="projection",
            generation=self._snapshot_generation,
            scope={"as_of": as_of.isoformat(), "target_date": target_date.isoformat()},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            # Ordered by the account, never by a projected amount: these are
            # signed, so a magnitude sort inverts on the accounts that owe money,
            # and `account_id` closes the ordering because it is the primary key.
            accounts = (
                session.query(AccountDB)
                .order_by(
                    AccountDB.institution.asc(),
                    AccountDB.account_type.asc(),
                    AccountDB.account_id.asc(),
                )
                .all()
            )
            if len(accounts) > _MAX_BALANCE_ACCOUNTS:
                raise AgentQueryError("filter_too_broad")
            pattern_rows = int(
                session.query(func.count()).select_from(RecurringPatternDB).scalar() or 0
            )
            if pattern_rows > _MAX_PROJECTION_PATTERNS:
                raise AgentQueryError("filter_too_broad")
            entry_count = len(accounts)
            # Every account, not just the page, so the census describes the whole
            # answer and stays legible when the entries are withheld -- and from
            # two set queries rather than two per account, so a cursor walk stays
            # linear in the account count instead of quadratic.
            anchored, anchored_by_as_of = self._absolute_anchor_account_ids(session, as_of=as_of)
            account_ids = [str(account.account_id) for account in accounts]
            counts = ProjectionAnchorCounts(
                reported=sum(1 for key in account_ids if key in anchored_by_as_of),
                no_absolute_anchor=sum(1 for key in account_ids if key not in anchored),
                no_anchor_on_or_before_as_of=sum(
                    1 for key in account_ids if key in anchored and key not in anchored_by_as_of
                ),
            )

            entries: tuple[ProjectionEntry, ...] = ()
            if account_ref_salt:
                refs = account_ref_map(session, account_ref_salt)
                patterns = self._projection_patterns(session)
                built: list[ProjectionEntry] = []
                for account in accounts[offset : offset + limit]:
                    # Built for the PAGE only. The census above already covers
                    # every account, so the anchor row itself is read only for the
                    # entries actually served.
                    anchor = self._projection_anchor(session, str(account.account_id), as_of=as_of)
                    account_patterns = patterns.get(str(account.account_id), [])
                    plan = None
                    status = anchor.status if anchor.status != "reported" else "projected"
                    if anchor.status == "reported" and anchor.balance_date is not None:
                        try:
                            plan = build_projection(
                                account_patterns,
                                anchor_date=anchor.balance_date,
                                target_date=target_date,
                                as_of=as_of,
                            )
                        except ProjectionTooBroadError:
                            # NAMED, never truncated and never a refusal of the
                            # whole call. A cut list leaves `anchor + sum(lines)
                            # == projected` false while every block still looks
                            # well formed; aborting the call would take the other
                            # accounts down with this one, mid-cursor-walk, over a
                            # window the caller cannot narrow -- it is
                            # (anchor, target], and the anchor is not an input.
                            status = "window_exceeds_line_item_cap"
                        except ValueError:  # pragma: no cover - validated above
                            raise AgentQueryError from None
                    built.append(
                        ProjectionEntry(
                            # Subscripted, not `resolve_account_ref`: these rows
                            # ARE `accounts` rows and `account_ref_map` iterates
                            # that same table on that same key, so a lookup that
                            # missed would be a bug rather than the unjoinable
                            # case coverage reaches.
                            account_ref=refs[str(account.account_number)],
                            institution=str(account.institution),
                            account_type=str(account.account_type),
                            is_credit_card=bool(account.is_credit_card),
                            status=status,
                            anchor=anchor,
                            projected_balance=(
                                None
                                if plan is None or anchor.balance is None
                                else anchor.balance + plan.net
                            ),
                            net_projected_flow=None if plan is None else plan.net,
                            line_items=(
                                ()
                                if plan is None
                                else tuple(
                                    ProjectionLineItem(
                                        merchant=line.merchant,
                                        category=line.category,
                                        pattern_type=line.pattern_type,
                                        frequency=line.frequency,
                                        amount=line.amount,
                                        projected_date=line.projected_date,
                                        next_expected_date=line.next_expected_date,
                                        periods_from_next_expected=(
                                            line.periods_from_next_expected
                                        ),
                                        next_expected_is_retrospective=(
                                            line.next_expected_is_retrospective
                                        ),
                                        is_overdue=line.is_overdue,
                                        missed_count=line.missed_count,
                                        confidence=line.confidence,
                                    )
                                    for line in plan.lines
                                )
                            ),
                            pattern_count=len(account_patterns),
                            patterns_past_due=None if plan is None else plan.patterns_past_due,
                            disposition=None if plan is None else plan.disposition,
                        )
                    )
                entries = tuple(built)

        # Withheld entries are never served, so there is no next page to offer.
        has_more = bool(account_ref_salt) and offset + len(entries) < entry_count
        return ProjectionPage(
            entries=entries,
            entry_count=entry_count,
            anchor_counts=counts,
            account_refs_disclosed=bool(account_ref_salt),
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def insight_findings(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        account_ref_salt: str | None = None,
    ) -> InsightsPage:
        """One stored period's triage rows, without the text they are stored in.

        Scoped to ONE `(period_start, period_end)` pair: the newest
        `period_end`, and among the pairs tying it the EARLIEST `period_start`,
        so the widest window ending on that date wins. Reconciliation is keyed
        on the exact pair with no pruning, so every analysed window mints its
        own row set and leaves the previous one `open` forever; an unscoped
        surface would serve them all with two dates as the only difference.

        `refresh_period` analyses the supplied window, including the sidebar
        period selected in Streamlit. Refreshing a narrower window can create a
        second pair with the same `period_end`. Updating an existing row does
        not bump `created_at`, so timestamp ordering cannot reliably restore
        the wider window after another refresh. Preferring the earliest
        `period_start` keeps an ad-hoc slice from displacing the widest analysis
        ending on that date.

        The scope is derived, not an input, so nothing beyond the generation
        needs binding into the cursor: the pair can only move when the pinned
        database moves, and that rotates the generation.

        `computed_at` is the newest `created_at` among the served rows. Without
        it the response carries no way to tell derived state that predates the
        imports beside it from state computed over them -- `scoped_period` is a
        window, not a timestamp, and `ledger.quality`'s `insight_freshness`
        check is served from a run that goes stale the same way.

        What is withheld is withheld for a stated reason, never for tidiness:
        `title`, `explanation` and `suggested_action` are interpolated stored
        strings that can contain merchant names and raw account identifiers.
        Their bounded length does not make them safe to emit on a wire whose
        posture is `instruction_trust: "inert"`. `evidence` is a schema-less
        JSON column, so only the explicitly selected typed fields are served.
        """
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="insight_findings",
            generation=self._snapshot_generation,
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        severity_counts = dict.fromkeys(sorted(INSIGHT_SEVERITIES), 0)
        status_counts = dict.fromkeys(sorted(INSIGHT_STATUSES), 0)

        with self._open_session() as session:
            from ledger.insights.preferences import SpendingPreferenceService

            account_refs_by_id = account_id_ref_map(session, account_ref_salt)
            # Grouped rather than taken off one row: `computed_at` must be the
            # newest stamp in the SERVED pair, and a row-ordered SELECT would
            # hand back whichever row won the period ordering instead.
            newest = (
                session.query(
                    InsightFindingDB.period_start,
                    InsightFindingDB.period_end,
                    func.max(InsightFindingDB.created_at),
                )
                .group_by(InsightFindingDB.period_start, InsightFindingDB.period_end)
                .order_by(
                    InsightFindingDB.period_end.desc(),
                    InsightFindingDB.period_start.asc(),
                )
                .first()
            )
            if newest is None:
                return InsightsPage(
                    entries=(),
                    entry_count=0,
                    unservable_count=0,
                    period_start=None,
                    period_end=None,
                    computed_at=None,
                    severity_counts=severity_counts,
                    status_counts=status_counts,
                    has_more=False,
                    next_cursor=None,
                )
            period_start, period_end = newest[0], newest[1]
            computed_at = self._as_datetime(newest[2])
            preferences = SpendingPreferenceService.list_from_session(session)
            scoped = session.query(InsightFindingDB).filter(
                InsightFindingDB.period_start == period_start,
                InsightFindingDB.period_end == period_end,
            )
            if int(scoped.count()) > _MAX_INSIGHT_FINDINGS:
                raise AgentQueryError("filter_too_broad")
            # Ordered on two STORED columns ending in the primary key, so the
            # order is total and an offset addresses one fixed row. Deliberately
            # not on severity: the column is text, so a SQL sort ranks it
            # `high, low, medium`, and ranking it would order a paginated
            # surface by a value derived outside the stored data.
            rows = scoped.order_by(
                InsightFindingDB.finding_type.asc(),
                InsightFindingDB.id.asc(),
            ).all()
            wanted_merchants = {
                _preference_key(row.evidence.get("merchant"), scope="merchant")
                for row in rows
                if isinstance(row.evidence, Mapping)
                and _preference_key(row.evidence.get("merchant"), scope="merchant")
            }
            categories_by_merchant: dict[str, set[str]] = {}
            if wanted_merchants:
                category_rows = (
                    session.query(TransactionDB.merchant, TransactionDB.category)
                    .filter(TransactionDB.posting_date >= period_start)
                    .filter(TransactionDB.posting_date <= period_end)
                    .filter(*spend_sql_filters())
                    .distinct()
                    .all()
                )
                for merchant, category in category_rows:
                    key = _preference_key(merchant, scope="merchant")
                    category_key = _preference_key(category, scope="category")
                    if key in wanted_merchants and category_key:
                        categories_by_merchant.setdefault(key, set()).add(category_key)
            merchant_categories = {
                merchant: next(iter(categories))
                for merchant, categories in categories_by_merchant.items()
                if len(categories) == 1
            }
            # Materialized before paging because a row this build cannot serve
            # must not occupy a page slot: the whole scoped set is bounded by the
            # scan cap above, and the engine caps one refresh at 60 findings.
            served: list[InsightFinding] = []
            unservable = 0
            for row in rows:
                entry = _insight_finding(
                    row,
                    account_refs_by_id=account_refs_by_id,
                    preferences=preferences,
                    merchant_categories=merchant_categories,
                )
                if entry is None:
                    unservable += 1
                else:
                    served.append(entry)

        priority_rank = {"normal": 0, "low": 1, "suppressed": 2}
        served.sort(key=lambda entry: priority_rank[entry.action_priority])

        for entry in served:
            severity_counts[entry.severity] += 1
            status_counts[entry.status] += 1
        entries = tuple(served[offset : offset + limit])
        has_more = offset + len(entries) < len(served)
        return InsightsPage(
            entries=entries,
            entry_count=len(served),
            unservable_count=unservable,
            period_start=period_start,
            period_end=period_end,
            computed_at=computed_at,
            severity_counts=severity_counts,
            status_counts=status_counts,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def quality_checks(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> QualityPage:
        """The newest recorded check run: four columns per check, and no details.

        `details` never crosses (D15). It is an unbounded JSON blob, and
        `runtime_config.details.issues` is built with f-strings that `repr()`
        merchant and category names verbatim. An empty result in one database
        would not make that field safe to expose in another.

        Scoped to the newest `run_at` because the table only ever INSERTs: the
        same nine check names are recorded again on every run, forever.
        """
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="quality_checks",
            generation=self._snapshot_generation,
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        severity_counts = dict.fromkeys(sorted(QUALITY_SEVERITIES), 0)
        status_counts = dict.fromkeys(sorted(QUALITY_STATUSES), 0)

        with self._open_session() as session:
            newest = (
                session.query(DataQualityCheckDB.run_at)
                .order_by(DataQualityCheckDB.run_at.desc())
                .first()
            )
            if newest is None:
                return QualityPage(
                    entries=(),
                    entry_count=0,
                    unservable_count=0,
                    run_at=None,
                    severity_counts=severity_counts,
                    status_counts=status_counts,
                    has_more=False,
                    next_cursor=None,
                )
            run_at = self._as_datetime(newest[0])
            scoped = session.query(DataQualityCheckDB).filter(
                DataQualityCheckDB.run_at == newest[0]
            )
            if int(scoped.count()) > _MAX_QUALITY_CHECKS:
                raise AgentQueryError("filter_too_broad")
            rows = scoped.order_by(
                DataQualityCheckDB.check_name.asc(),
                DataQualityCheckDB.id.asc(),
            ).all()
            served: list[QualityCheck] = []
            unservable = 0
            for row in rows:
                check = _quality_check(row)
                if check is None:
                    unservable += 1
                else:
                    served.append(check)

        for check in served:
            severity_counts[check.severity] += 1
            status_counts[check.status] += 1
        entries = tuple(served[offset : offset + limit])
        has_more = offset + len(entries) < len(served)
        return QualityPage(
            entries=entries,
            entry_count=len(served),
            unservable_count=unservable,
            run_at=run_at,
            severity_counts=severity_counts,
            status_counts=status_counts,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def recurring_patterns(
        self,
        *,
        status: str = "active",
        limit: int = 50,
        cursor: str | None = None,
    ) -> RecurringPage:
        """Return detected recurring patterns without their account identity.

        `account_id` is an UNSALTED truncated hash of institution|slug -- the same
        low-entropy construction the export layer deprecated as brute-forceable
        over institution x account -- so it is not on this wire at any width.
        """
        if not isinstance(status, str) or status not in _RECURRING_STATUS_VALUES:
            raise AgentQueryError
        limit = self._validated_limit(limit)
        fingerprint = _cursor_fingerprint(
            kind="recurring",
            generation=self._snapshot_generation,
            scope={"status": status},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        with self._open_session() as session:
            query = session.query(RecurringPatternDB)
            if status == "active":
                query = query.filter(RecurringPatternDB.is_active == True)  # noqa: E712
            elif status == "inactive":
                query = query.filter(RecurringPatternDB.is_active == False)  # noqa: E712
            entry_count = int(query.count())
            rows = (
                query.order_by(
                    RecurringPatternDB.next_expected_date.asc(),
                    RecurringPatternDB.merchant.asc(),
                    RecurringPatternDB.pattern_id.asc(),
                )
                .offset(offset)
                .limit(limit)
                .all()
            )
            entries = tuple(
                RecurringEntry(
                    merchant=row.merchant,
                    category=row.category,
                    institution=row.institution,
                    pattern_type=row.pattern_type,
                    frequency=row.frequency,
                    typical_amount=to_cents(abs(row.expected_amount)),
                    next_expected_date=row.next_expected_date,
                    missed_count=int(row.missed_count),
                    is_active=bool(row.is_active),
                    updated_at=row.updated_at,
                )
                for row in rows
            )

        has_more = offset + len(entries) < entry_count
        return RecurringPage(
            entries=entries,
            entry_count=entry_count,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(entries), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def transfer_aggregates(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int = 50,
        cursor: str | None = None,
    ) -> TransferAggregate:
        """Aggregate transfer legs by counterparty INSTITUTION, never by account.

        Reported from the `transfers` table, whose pairing columns are the only
        populated ones, and with its own coverage: a leg that was never paired is
        aggregated with a null counterparty rather than silently attributed.

        Amounts are unsigned magnitudes carrying a separate direction. A signed
        value would fail the money schema as corrupt data, and the group ordering
        is by magnitude, which a negative would invert.
        """
        if (
            not isinstance(start_date, date)
            or not isinstance(end_date, date)
            or start_date > end_date
        ):
            raise AgentQueryError
        limit = self._validated_limit(limit)
        from ledger.adapters.registry import compatibility_policy_snapshot

        policy = compatibility_policy_snapshot()
        fingerprint = _cursor_fingerprint(
            kind="transfers",
            start_date=start_date,
            end_date=end_date,
            generation=self._snapshot_generation,
            scope={"compatibility_policy": policy.digest},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        # Imported lazily: `ledger.analysis.__init__` pulls in health_scorer, which
        # imports ledger.query, so a module-level import here would be circular.
        from ledger.analysis.transfer_matcher import is_credit_leg
        from ledger.storage.mappers import transfer_from_db

        totals: dict[tuple[str, str | None, str], tuple[Decimal, int]] = {}
        total = Decimal("0.00")
        paired_legs = 0
        with_counterparty = 0
        with self._open_session() as session:
            period = session.query(TransferDB).filter(
                TransferDB.posting_date >= start_date,
                TransferDB.posting_date <= end_date,
            )
            candidate_rows = int(period.count())
            # Same CAD-only rule the transaction tools apply: a foreign-currency
            # leg summed into a CAD magnitude would be a silent FX error.
            cad = period.filter(
                or_(
                    TransferDB.currency == "CAD",
                    TransferDB.currency.is_(None),
                    TransferDB.currency == "",
                )
            )
            legs = int(cad.count())
            if legs > _MAX_TRANSFER_SCAN:
                raise AgentQueryError("filter_too_broad")
            for row in cad.order_by(TransferDB.txn_id.asc()).all():
                magnitude = to_cents(abs(row.amount_raw))
                total += magnitude
                if row.paired_transfer_id:
                    paired_legs += 1
                counterparty = row.counterparty_institution or None
                if counterparty:
                    with_counterparty += 1
                # The canonical predicate, shared with the transfer matcher: a
                # credit-card leg is the DESTINATION of a payment, which is the
                # only direction signal that survives two same-signed legs.
                if is_credit_leg(transfer_from_db(row), policy=policy):
                    direction = "inbound"
                elif row.amount_raw < 0:
                    direction = "outbound"
                elif row.amount_raw > 0:
                    direction = "inbound"
                else:
                    direction = "undetermined"
                key = (row.institution, counterparty, direction)
                amount, count = totals.get(key, (Decimal("0.00"), 0))
                totals[key] = (amount + magnitude, count + 1)

        ordered = sorted(
            (
                TransferGroup(
                    institution=institution,
                    counterparty_institution=counterparty,
                    direction=direction,
                    amount=amount,
                    transfer_count=count,
                )
                for (institution, counterparty, direction), (amount, count) in totals.items()
            ),
            # Magnitude first, then a total tiebreak, so an offset addresses one
            # fixed group. `counterparty is None` sorts the unmatched legs last.
            key=lambda group: (
                -group.amount,
                group.institution,
                group.counterparty_institution is None,
                group.counterparty_institution or "",
                group.direction,
            ),
        )
        groups = tuple(ordered[offset : offset + limit])
        has_more = offset + len(groups) < len(ordered)
        return TransferAggregate(
            groups=groups,
            group_count=len(ordered),
            total=total,
            transfer_count=legs,
            candidate_rows=candidate_rows,
            non_cad=candidate_rows - legs,
            paired_legs=paired_legs,
            with_counterparty_institution=with_counterparty,
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(groups), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )

    def dimension_values(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        dimension: str,
        contains: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> DimensionPage:
        """List the stored spelling of every value a filter can match, with counts.

        Counted through the SAME flow predicates an aggregate applies, so every
        value returned is one a filtered aggregate can actually reproduce. A list
        taken from the raw table would offer values that total zero under policy.

        `contains` matches after the documented normalization, which is what
        removes the guess-then-page loop exact matching otherwise forces.
        """
        _validate_query(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=None,
            filters=AgentFilters(),
            limit=limit,
        )
        if dimension not in _DIMENSION_VALUES:
            raise AgentQueryError
        if contains is not None and (
            not isinstance(contains, str)
            or not contains.strip()
            or len(contains) > _MAX_FILTER_LENGTH
            or "\x00" in contains
        ):
            raise AgentQueryError
        needle = None if contains is None else _normalize_match(contains)
        fingerprint = _cursor_fingerprint(
            kind="dimensions",
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            generation=self._snapshot_generation,
            # The NORMALIZED needle, because that is what the query matched on:
            # binding the raw text would refuse a cursor over a difference the
            # result set cannot show.
            scope={"contains": needle, "dimension": dimension},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)

        column = {
            "categories": TransactionDB.category,
            "institutions": TransactionDB.institution,
            "sources": TransactionDB.merchant,
        }[dimension]
        with self._open_session() as session:
            base = self._base_query(
                session,
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                filters=_ResolvedFilters((), (), (), False, False, False),
            )
            rows = (
                base.with_entities(
                    column.label("value"),
                    func.count(func.distinct(TransactionDB.txn_id)).label("transaction_count"),
                )
                # A blank value is not a name a filter can send, and the aggregate
                # already drops blank merchants from its groups for that reason.
                .filter(column != "")
                .group_by(column)
                .all()
            )
            if len(rows) > _MAX_DIMENSION_VALUES:
                raise AgentQueryError("filter_too_broad")
            values = [
                DimensionValue(value=str(row.value), transaction_count=int(row.transaction_count))
                for row in rows
                if needle is None or needle in _normalize_match(str(row.value))
            ]

        values.sort(key=lambda value: (-value.transaction_count, value.value))
        page = tuple(values[offset : offset + limit])
        has_more = offset + len(page) < len(values)
        return DimensionPage(
            values=page,
            value_count=len(values),
            has_more=has_more,
            next_cursor=(
                _encode_cursor(offset=offset + len(page), fingerprint=fingerprint)
                if has_more
                else None
            ),
        )


__all__ = [
    "ACCOUNT_REFS_DISCLOSED",
    "ACCOUNT_REFS_WITHHELD",
    "BASIS_DELTA",
    "BASIS_WHOLE_DATABASE",
    "SOURCE_BASIS_DELTA",
    "SOURCE_BASIS_NO_IMPORTS",
    "SOURCE_BASIS_WHOLE_TABLE",
    "AgentFilters",
    "AgentQueries",
    "AgentQueryError",
    "AggregateGroup",
    "AggregateResult",
    "BalanceChange",
    "BalanceEntry",
    "BalanceLevel",
    "BalanceLevelCounts",
    "BalancePage",
    "CONTAINS_SEMANTICS",
    "ChangeCountSource",
    "ChangeCountSources",
    "ChangeEntry",
    "ChangesPage",
    "CoverageEntry",
    "CoverageGap",
    "CoverageGapCensus",
    "CoverageGapCounts",
    "CoveragePage",
    "CoverageStatusCounts",
    "DimensionPage",
    "DimensionValue",
    "EvidencePage",
    "EvidenceRow",
    "Flow",
    "GroupBy",
    "INSIGHT_SEVERITIES",
    "INSIGHT_STATUSES",
    "INSIGHT_TYPE_FIELDS",
    "InsightFinding",
    "InsightsPage",
    "MATCH_SEMANTICS",
    "NetWorthComponent",
    "NetWorthPage",
    "NetWorthSubtotal",
    "Order",
    "PROJECTION_ENTRY_STATUSES",
    "ProjectionAnchor",
    "ProjectionAnchorCounts",
    "ProjectionEntry",
    "ProjectionLineItem",
    "ProjectionPage",
    "QUALITY_SEVERITIES",
    "QUALITY_STATUSES",
    "QualityCheck",
    "QualityPage",
    "RecurringEntry",
    "RecurringPage",
    "TransferAggregate",
    "TransferGroup",
]
