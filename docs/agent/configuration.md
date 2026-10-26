# Configuration and category hierarchy

Read this reference for configuration, taxonomy, budget-import, or hierarchy work. Runtime exports remain private in external owner-only paths. Changes to tracked seeds use independently authored synthetic placeholders.

Examples use `/absolute/private-ledger` as a placeholder for an operator-selected
external runtime. Replace it with the intended private path; development uses
only disposable synthetic runtimes.

## Configuration Files

### configs/ledger.toml
Main settings and institution-specific classification rules:
- `general.strict`: Fail on errors vs warn
- `general.etransfer_is_cashflow`: Include e-transfers in cashflow totals
- `general.cc_payment_is_transfer`: Treat credit card payments as transfers
- `rules.<institution>.*_patterns`: Regex patterns for flow classification

### Runtime Merchant/Category Configuration
Merchant and category rules are stored in SQLite runtime tables during normal operation.
The checked-in TOML files are independently authored synthetic seeds for reproducibility; runtime TOML exports
are private snapshots and remain in external owner-only destinations:

- `ledger config import --db /absolute/private-ledger/ledger.db --source configs` (dry run; add `--execute` to
  apply — this REPLACES the runtime config with the independently authored synthetic seed)
- `ledger config validate --db /absolute/private-ledger/ledger.db`
- `ledger config export --db /absolute/private-ledger/ledger.db --dest /absolute/private-ledger/exported`
- `ledger rules validate --db /absolute/private-ledger/ledger.db`
- `ledger rules apply --db /absolute/private-ledger/ledger.db --execute`  (default is a no-op dry run; `--execute` takes an automatic pre-mutation backup before rewriting every transaction's merchant/category)
- `ledger budget suggest --db /absolute/private-ledger/ledger.db --start-date 2025-01-01 --end-date 2025-12-31 --dest /absolute/private-ledger/budget-suggestions.csv` — write exact `month,category,amount,notes` rows using each category's median of represented nonzero monthly gross-spending totals; calendar months with no matching spend are omitted, not zero-padded
- For `budget suggest` and `budget export`, the CSV destination must not exist; they never replace an existing entry
- `ledger budget import --db /absolute/private-ledger/ledger.db --source <budgets.csv>` — dry-run a budget replacement by default; add `--execute` to take and report a pre-write backup before replacing rows (also `budget export`, `budget validate`)

`ledger optimize categories --db <ledger>` previews taxonomy changes. The authorized
`--execute-safe` form takes a pre-mutation backup, applies the taxonomy rewrite and
rules, and restores the backup if application fails. For artifact exports and their
privacy scopes, read [local operations](local-operations.md#local-exports-and-report-evidence).

The Streamlit Rules / Settings page can edit merchant profiles and category taxonomy. Merchant profile category is the source of truth for merchant-to-category mapping, so category membership is derived instead of edited in two places.

### configs/merchants.toml
Merchant normalization seed/export format:
- `[[rule]]` array of merchant rules
- `merchant_norm`: Canonical merchant name
- `patterns`: Regex patterns to match raw descriptions
- `category`: Default category for this merchant

### configs/categories.toml
Hierarchical category seed/export format:
- `[[category]]` array of categories
- `category_norm`: Category name
- `parent`: Optional parent category name (for child categories)
- `merchants`: List of normalized merchant names (empty for parent categories)
- `exclude_from_spend_metrics`: Exclude from spending metrics (e.g., Income, Reimbursement)

**Hierarchy Structure**:
- Parent categories have `merchants = []` and no `parent` field
- Child categories specify `parent = "Parent Name"`
- System validates that all parent references exist
- Automatic rollup: child spending aggregates to parent totals

**Example**:
```toml
# Parent category
[[category]]
category_norm = "Food & Dining"
merchants = []

# Child category
[[category]]
category_norm = "Restaurants & Dining"
parent = "Food & Dining"
merchants = ["SYNTHETIC_CAFE", "SYNTHETIC_BISTRO"]
```

**The tracked seed is not the live taxonomy.** `configs/categories.toml` and
`configs/merchants.toml` are **independently authored synthetic seeds** (`MERCHANT_001`, …),
enforced by `tests/test_config_seed_scrubbed.py`, because `configs/` is tracked in git
and real merchant names are a spend fingerprint. The live taxonomy lives only in the
SQLite runtime tables and will differ in both size and naming.

- Runtime taxonomy: whatever the user's SQLite tables hold — inspect it with
  `ledger config validate --db /absolute/private-ledger/ledger.db` or the Rules / Settings page.

Because of this, `ledger config import` against a database that already has a runtime
config REPLACES real rules with placeholders. It is dry-run by default for that reason;
see [imports and recovery](imports-and-recovery.md) for the recovery boundaries.

## Teaching merchant rules

Teaching is offered in the Streamlit Transactions page and through
`ledger import pdf review resolve --teach`. The read-only MCP server (`ledger.mcp.v1`)
stays read-only. During an open import, teaching belongs to staging; follow
[the import review boundary](imports-and-recovery.md#pdf-review-and-finalize).

`upsert_merchant_rule` preserves an existing rule's stored `exclude_from_spending`.
A new rule inherits the target category's exclusion. An explicit conflicting flag
raises `SpendSemanticsChangeRefused` unless the caller deliberately supplies
`allow_spend_semantics_change=True`. Merchant and category exclusions are independent
stored decisions; the effective exclusion ORs them. The UI preview discloses that result.

Derive the pattern from raw statement evidence, never the resolved display label.
Missing raw text or an ambiguous e-transfer counterparty requires an explicit
raw-derived pattern. Preview evaluates the candidate within the updated full ruleset,
uses first-match precedence, and skips MANUAL rows, just like application. Its count
belongs to the target rule; unrelated repairs from the full pass are reported separately.

`TeachingDuringOpenImportRefused` and `OPEN_IMPORT_RUN_REMEDY` expose the shared
open-import refusal. A taught rule survives finalize and `repair statements` through
runtime-config copying. It does not survive `rebuild-db`, which reseeds configuration.
The full [recovery contract](imports-and-recovery.md#recovery-and-statement-repair)
distinguishes runtime-config copying from merge-forward tables.

Implementation: [categorization](../../src/ledger/rules/categorization.py) and
[runtime configuration mutations](../../src/ledger/rules/configuration.py).
Regressions: `tests/test_categorization_f10.py`,
`tests/test_teaching_reaches_future_imports.py`, and
`tests/test_review_teach.py::test_teach_preserves_and_discloses_an_existing_rules_stored_exclusion`.

## Hierarchy and classification constraints

[CategoriesConfig](../../src/ledger/config/schemas.py) validates unique names,
existing parent references, and absence of cycles. Use its hierarchy methods and
[shared UI rollups](../../src/ledger/ui/components.py), rather than maintaining a
second merchant membership map. `is_parent` means an empty merchant list; use
actual parent references when determining which categories have children.

Runtime hierarchy exports belong in external owner-only destinations such as
`/absolute/private-ledger/exported`. Never copy them into tracked seeds. Statement ingestion
uses Ledger's runtime categorization; a source application's categories are not
authoritative Ledger categories. Keep ambiguous multi-purpose merchants broad unless raw evidence
supports a narrower category.

Account alias values currently have no cross-institution uniqueness validation,
while `accounts.account_number` is globally unique. Choose distinct aliases across
institutions; see [alias normalization](../../src/ledger/utils/text.py) and
[the storage schema](../../src/ledger/storage/database.py).
