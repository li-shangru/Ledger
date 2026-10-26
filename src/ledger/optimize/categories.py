"""Post-repair merchant and category optimization."""

from __future__ import annotations

import contextlib
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.core.results import RunResult
from ledger.finance_policy import spend_sql_filters
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.rules import RuleApplicationService
from ledger.runtime import runtime_path
from ledger.storage.database import DBOperationAuditDB, TransactionDB, session_scope
from ledger.storage.migrations import invalidate_db_caches
from ledger.storage.operations import backup_database, record_db_operation
from ledger.storage.snapshots import restore_database
from ledger.utils.dates import naive_utc_now
from ledger.utils.private_files import (
    ensure_private_dir,
    private_atomic_text,
    write_private_text_atomic,
)
from ledger.utils.text import neutralize_csv_row

MIXED_MERCHANTS = {"AMAZON", "COSTCO", "WALMART"}
ROOT_CATEGORIES = [
    "Home & Fixed Costs",
    "Food & Dining",
    "Transportation",
    "Shopping & Lifestyle",
    "Health & Family",
    "Digital & Subscriptions",
    "Travel & Entertainment",
    "Financial & Excluded",
]

CATEGORY_MAP = {
    "Housing": ("Housing", "Home & Fixed Costs"),
    "Housing & Utilities": ("Home & Fixed Costs", None),
    "Utilities": ("Utilities", "Home & Fixed Costs"),
    "Insurance": ("Insurance", "Home & Fixed Costs"),
    "Food & Dining": ("Food & Dining", None),
    "Alcohol": ("Alcohol", "Food & Dining"),
    "Coffee & Quick Bites": ("Coffee & Quick Bites", "Food & Dining"),
    "Delivery Services": ("Delivery Services", "Food & Dining"),
    "Fast Food & Takeout": ("Fast Food & Takeout", "Food & Dining"),
    "Groceries": ("Groceries", "Food & Dining"),
    "Restaurants & Dining": ("Restaurants & Dining", "Food & Dining"),
    "Transportation & Auto": ("Transportation", None),
    "Auto Maintenance": ("Auto Maintenance", "Transportation"),
    "Gas & Car Wash": ("Gas & Car Wash", "Transportation"),
    "Parking & Tolls": ("Parking & Tolls", "Transportation"),
    "Ride Shares": ("Ride Shares", "Transportation"),
    "Shopping": ("Shopping & Lifestyle", None),
    "Apparel & Beauty": ("Apparel & Beauty", "Shopping & Lifestyle"),
    "Electronics": ("Electronics", "Shopping & Lifestyle"),
    "General Shopping": ("General Shopping", "Shopping & Lifestyle"),
    "Gifts": ("Gifts", "Shopping & Lifestyle"),
    "Home & Garden": ("Home & Garden", "Shopping & Lifestyle"),
    "Personal Care": ("Personal Care", "Shopping & Lifestyle"),
    "Health & Wellness": ("Health & Wellness", "Health & Family"),
    "Pets": ("Pets", "Health & Family"),
    "Software & Digital": ("Digital & Subscriptions", None),
    "Gaming": ("Gaming", "Digital & Subscriptions"),
    "Software Tools": ("Software Tools", "Digital & Subscriptions"),
    "Subscriptions": ("Subscriptions", "Digital & Subscriptions"),
    "Entertainment": ("Travel & Entertainment", None),
    "Entertainment & Events": ("Entertainment & Events", "Travel & Entertainment"),
    "Golf": ("Golf", "Travel & Entertainment"),
    "Travel": ("Travel", "Travel & Entertainment"),
    "Fees": ("Fees", "Financial & Excluded"),
    "Taxes": ("Taxes", "Financial & Excluded"),
    "Cash Withdrawals": ("Cash Withdrawals", "Financial & Excluded"),
    "Investing": ("Investing", "Financial & Excluded"),
    "Income (Excluded)": ("Income", "Financial & Excluded"),
    "Transfers (Excluded)": ("Transfers", "Financial & Excluded"),
    "Adjustments (Excluded)": ("Adjustments", "Financial & Excluded"),
    "Other": ("Other", "Financial & Excluded"),
}

EXCLUDED_CATEGORIES = {
    "Income",
    "Transfers",
    "Credit Card Payments",
    "Reimbursements",
    "Adjustments",
    "Investing",
}


@dataclass(frozen=True)
class CategoryRecommendation:
    """A single old->new category remap, carrying its spend-exclusion status.

    ``old_exclude_from_spending`` / ``new_exclude_from_spending`` make every
    include<->exclude transition explicit in ``category_recommendations.csv`` so
    ``--execute-safe`` can never silently change what counts as spending (F10).
    """

    old_name: str
    new_name: str
    parent: str | None
    old_exclude_from_spending: bool
    new_exclude_from_spending: bool


@dataclass(frozen=True)
class CategoryOptimizationOptions:
    db_path: Path
    runs_dir: Path = field(default_factory=lambda: runtime_path("optimization-runs"))
    execute_safe: bool = False
    exported_config_dir: Path = field(default_factory=lambda: runtime_path("exported"))
    require_repaired: bool = True
    backup_dir: Path | None = None


@dataclass(frozen=True)
class CategoryOptimizationResult:
    status: str
    run_dir: Path
    counts: dict[str, int]
    artifacts: dict[str, Path]
    warnings: list[str]
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.status == "success" and not self.errors


class CategoryOptimizer:
    """Optimize runtime merchant/category config from repaired spending history."""

    def run(self, options: CategoryOptimizationOptions) -> CategoryOptimizationResult:
        run_dir = make_run_dir(options.runs_dir)
        artifacts = {
            "summary": run_dir / "summary.json",
            "merchant_recommendations": run_dir / "merchant_recommendations.csv",
            "category_recommendations": run_dir / "category_recommendations.csv",
            "auto_applied_changes": run_dir / "auto_applied_changes.csv",
            "manual_review": run_dir / "manual_review.csv",
        }
        warnings: list[str] = []
        errors: list[str] = []
        started_at = naive_utc_now()

        # Tracks the backup artifact path once a pre-mutation backup succeeds.
        # Set before any mutation so the except handler knows whether to restore.
        backup_path: Path | None = None
        restored_after_failure = False
        boundary = contextlib.ExitStack()
        boundary_entered = False
        repo_read_completed = False
        try:
            if options.execute_safe:
                boundary.enter_context(
                    live_mutation_boundary(
                        options.db_path,
                        operation="category_optimizer",
                    )
                )
                boundary_entered = True
            if options.require_repaired and not latest_repair_passed(options.db_path):
                raise RuntimeError(
                    "Latest statement repair has not passed; refusing to optimize categories."
                )

            repo = SQLiteConfigRepository(options.db_path, prepare_schema=False)
            merchants = repo.load_merchant_rules()
            categories = repo.load_categories()
            category_recommendations, optimized_categories = optimize_taxonomy(categories)
            merchant_recommendations, auto_rules, manual_review = analyze_merchants(
                options.db_path,
                merchants,
            )
            optimized_merchants = merge_merchant_rules(
                merchants,
                auto_rules,
                {rec.old_name: rec.new_name for rec in category_recommendations},
            )
            repo_read_completed = True

            auto_applied = []
            if options.execute_safe:
                with contextlib.nullcontext():
                    backup = backup_database(
                        options.db_path,
                        backup_dir=options.backup_dir or options.db_path.parent / "db-backups",
                        reason="pre-category-optimize",
                    )
                    if not backup.ok:
                        raise RuntimeError(
                            "Refusing to optimize categories: pre-mutation backup failed"
                            + (f": {'; '.join(backup.errors)}" if backup.errors else "")
                        )
                    # Record backup path before first mutation so a failure can restore it
                    # while the target exclusion boundary is still held.
                    backup_path = Path(str(backup.artifacts["backup"]))
                    try:
                        repo.replace_from_configs(optimized_merchants, optimized_categories)
                        apply_result = RuleApplicationService(options.db_path).apply_all(
                            source="category_optimizer",
                            refresh_analytics=True,
                        )
                        # Defense-in-depth (H1): apply_all EARLY-RETURNS a non-success result
                        # without raising when the post-replace taxonomy fails validate().
                        if apply_result.status != "success":
                            raise RuntimeError(
                                "Refusing to report success: rule application did not succeed "
                                f"(status={apply_result.status!r}). Live DB restored from backup. "
                                "Issues: "
                                + (
                                    "; ".join(apply_result.validation_issues)
                                    if apply_result.validation_issues
                                    else "(none reported)"
                                )
                            )
                        repo.export_to_directory(options.exported_config_dir)
                        auto_applied = [
                            *[
                                {
                                    "change_type": "category_map",
                                    "subject": rec.old_name,
                                    "new_value": rec.new_name,
                                    "reason": "taxonomy_redesign",
                                }
                                for rec in category_recommendations
                                if rec.old_name != rec.new_name
                            ],
                            *auto_rules,
                        ]
                        warnings.extend(apply_result.validation_issues)
                    except Exception:
                        restore_database(backup_path, options.db_path)
                        invalidate_db_caches()
                        restored_after_failure = True
                        raise

            write_csv(
                artifacts["category_recommendations"], category_rows(category_recommendations)
            )
            write_csv(artifacts["merchant_recommendations"], merchant_recommendations)
            write_csv(artifacts["auto_applied_changes"], auto_applied)
            write_csv(artifacts["manual_review"], manual_review)
            counts = {
                "category_recommendations": len(category_recommendations),
                "merchant_recommendations": len(merchant_recommendations),
                "auto_applied": len(auto_applied),
                "manual_review": len(manual_review),
            }
            summary = {
                "status": "success",
                "execute_safe": options.execute_safe,
                "started_at": started_at.isoformat(),
                "finished_at": naive_utc_now().isoformat(),
                "counts": counts,
                "warnings": warnings,
                "errors": errors,
            }
            write_json(artifacts["summary"], summary)
            record_db_operation(
                options.db_path,
                operation="category_optimize",
                result=RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=naive_utc_now(),
                    counts=counts,
                    warnings=warnings,
                    artifacts=cast(dict[str, Path | str], artifacts),
                    details={"execute_safe": options.execute_safe},
                ),
                artifact_path=artifacts["summary"],
            )
            return CategoryOptimizationResult(
                "success", run_dir, counts, artifacts, warnings, errors
            )
        except Exception as exc:
            errors.append(str(exc))

            # --- Restore-on-fail (execute_safe only) ---
            # If the backup was taken and a mutation may have been applied, restore the
            # pre-mutation snapshot so the live DB is not left in a partially-mutated state.
            restore_error: str | None = None
            if backup_path is not None and not restored_after_failure:
                try:
                    restore_database(backup_path, options.db_path)
                    invalidate_db_caches()
                    restored_after_failure = True
                except Exception as restore_exc:  # best-effort: surface but do not swallow
                    restore_error = (
                        f"RESTORE FAILED — live DB may be half-mutated. "
                        f"Backup is at: {backup_path}. Error: {restore_exc}"
                    )
                    errors.append(restore_error)

            counts = {
                "category_recommendations": 0,
                "merchant_recommendations": 0,
                "auto_applied": 0,
                "manual_review": 0,
            }
            write_json(
                artifacts["summary"],
                {
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": naive_utc_now().isoformat(),
                    "counts": counts,
                    "warnings": warnings,
                    "errors": errors,
                },
            )

            # --- Failed audit row ---
            # Record the failed destructive attempt so it is auditable even on failure.
            # This also satisfies the audit-on-failure requirement for category_optimize.
            if repo_read_completed or boundary_entered:
                with contextlib.suppress(Exception):
                    record_db_operation(
                        options.db_path,
                        operation="category_optimize",
                        result=RunResult(
                            status="failed",
                            started_at=started_at,
                            finished_at=naive_utc_now(),
                            counts=counts,
                            warnings=warnings,
                            errors=errors,
                            details={
                                "execute_safe": options.execute_safe,
                                "restored": restored_after_failure and restore_error is None,
                            },
                        ),
                        artifact_path=backup_path,
                    )

            return CategoryOptimizationResult(
                "failed", run_dir, counts, artifacts, warnings, errors
            )
        finally:
            boundary.close()


def latest_repair_passed(db_path: Path) -> bool:
    with session_scope(db_path) as session:
        row = (
            session.query(DBOperationAuditDB)
            .filter(DBOperationAuditDB.operation == "statement_repair_promote")
            .order_by(DBOperationAuditDB.started_at.desc())
            .first()
        )
        if not row or row.status != "success":
            return False
        details = row.details or {}
        return details.get("final_audit_status") == "pass"


def optimize_taxonomy(
    categories: CategoriesConfig,
) -> tuple[list[CategoryRecommendation], CategoriesConfig]:
    recommendations: list[CategoryRecommendation] = []
    seen: set[str] = set()
    optimized: list[CategoryRule] = []
    by_name: dict[str, CategoryRule] = {}
    existing_by_name = {category.name: category for category in categories.categories}
    for root in ROOT_CATEGORIES:
        rule = CategoryRule(
            name=root,
            parent=None,
            merchants=[],
            exclude_from_spending=root == "Financial & Excluded",
        )
        optimized.append(rule)
        by_name[root] = rule
        seen.add(root)

    for category in categories.categories:
        # An unmapped category keeps its name; it must also keep its PARENT. The old
        # fallback hardcoded None, so re-running the optimizer stripped the parent from
        # any category the map does not name -- making the operation non-idempotent.
        new_name, parent = CATEGORY_MAP.get(category.name, (category.name, category.parent))
        # RESOLVED (fifth-pass F10, owner decision "preserve, never flip"): optimization
        # is taxonomy-only (rename/reparent), so an EXISTING category keeps its own
        # exclude_from_spending flag verbatim. We deliberately do NOT force-exclude a leaf
        # just because it appears in EXCLUDED_CATEGORIES (e.g. Investing), which would
        # silently change persisted spend totals. Exclusion is leaf-driven -- aggregates.py
        # and rules/service.py gate on each row's own flag, never the parent -- so placing
        # a still-counted leaf (Fees / Taxes / Cash Withdrawals / Other) under the
        # "Financial & Excluded" parent is a grouping label only and does not exclude it.
        # EXCLUDED_CATEGORIES now only DEFAULTS brand-new categories (the loop below), and
        # every include<->exclude status is surfaced in category_recommendations.csv so
        # --execute-safe can never change what counts as spending without it being visible.
        new_exclude = category.exclude_from_spending
        if new_name in seen:
            # MERGE, do not skip. A collision happens when two source categories map to
            # the same new name, or when a mapped name equals a seeded root. Skipping
            # left the ALREADY-PRESENT node in place -- typically the root, seeded with
            # exclude_from_spending=False -- while the recommendation CSV still reported
            # "no change" for this category. A category the user had marked excluded
            # therefore came back included, silently changing what counts as spending:
            # exactly what the F10 decision ("preserve, never flip") forbids.
            #
            # Preserving the flag means a merge keeps exclusion if EITHER side excludes.
            existing_rule = by_name[new_name]
            merged_exclude = existing_rule.exclude_from_spending or new_exclude
            incoming_merchants = (
                existing_by_name[category.name].merchants
                if category.name in existing_by_name
                else []
            )
            merged_merchants = list(existing_rule.merchants)
            seen_merchants = {name.upper().strip() for name in merged_merchants}
            for merchant in incoming_merchants:
                if merchant.upper().strip() not in seen_merchants:
                    merged_merchants.append(merchant)
                    seen_merchants.add(merchant.upper().strip())
            if merged_exclude != existing_rule.exclude_from_spending or merged_merchants != list(
                existing_rule.merchants
            ):
                merged = existing_rule.model_copy(
                    update={
                        "exclude_from_spending": merged_exclude,
                        "merchants": merged_merchants,
                    }
                )
                optimized[optimized.index(existing_rule)] = merged
                by_name[new_name] = merged
            new_exclude = merged_exclude
        else:
            rule = CategoryRule(
                name=new_name,
                parent=parent,
                # Seed-only (unruled) memberships live here. Rebuilding every node with
                # merchants=[] discarded them permanently, because
                # _canonicalize_category_memberships only re-materializes RULE-owned
                # merchants -- so --execute-safe silently deleted every association the
                # user had not backed with a merchant rule.
                merchants=list(
                    existing_by_name[category.name].merchants
                    if category.name in existing_by_name
                    else []
                ),
                exclude_from_spending=new_exclude,
            )
            optimized.append(rule)
            by_name[new_name] = rule
            seen.add(new_name)

        recommendations.append(
            CategoryRecommendation(
                old_name=category.name,
                new_name=new_name,
                parent=parent,
                old_exclude_from_spending=category.exclude_from_spending,
                # Report what will ACTUALLY persist, including a merge outcome.
                new_exclude_from_spending=new_exclude,
            )
        )

    for excluded in sorted(EXCLUDED_CATEGORIES):
        if excluded not in seen:
            rule = CategoryRule(
                name=excluded,
                parent="Financial & Excluded",
                merchants=[],
                exclude_from_spending=True,
            )
            optimized.append(rule)
            by_name[excluded] = rule
            seen.add(excluded)
    return recommendations, CategoriesConfig(
        version="optimized",
        # default_category was dropped here, silently resetting it to the schema
        # default ("Other") on every optimize run.
        default_category=categories.default_category,
        categories=optimized,
    )


def analyze_merchants(
    db_path: Path,
    merchants: MerchantRulesConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    configured = {rule.merchant_norm.upper().strip() for rule in merchants.rules}
    grouped: dict[str, dict[str, Any]] = {}
    with session_scope(db_path) as session:
        rows = session.query(TransactionDB).filter(*spend_sql_filters()).all()
        for row in rows:
            merchant = str(row.merchant or "").strip().upper()
            if not merchant:
                merchant = merchant_from_description(str(row.description or ""))
            if not merchant:
                continue
            group = grouped.setdefault(
                merchant,
                {
                    "merchant": merchant,
                    "rows": 0,
                    "spend": Decimal("0"),
                    "categories": Counter(),
                    "examples": [],
                },
            )
            group["rows"] += 1
            group["spend"] += abs(Decimal(str(row.amount_signed or 0)))
            group["categories"][str(row.category or "Other")] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(str(row.description or ""))

    recommendations = []
    auto_rules = []
    manual_review = []
    for merchant, group in sorted(grouped.items(), key=lambda item: (-item[1]["spend"], item[0])):
        target_category, count = group["categories"].most_common(1)[0]
        row_share = Decimal(count) / Decimal(group["rows"])
        is_configured = merchant in configured
        spend = Decimal(group["spend"]).quantize(Decimal("0.01"))
        recommendation = {
            "merchant": merchant,
            "current_state": "configured" if is_configured else "missing_profile",
            "target_category": target_category,
            "rows": group["rows"],
            "spend": str(spend),
            "category_row_share": str(row_share.quantize(Decimal("0.01"))),
            "examples": " | ".join(group["examples"]),
        }
        if merchant in MIXED_MERCHANTS:
            recommendation["action"] = "manual_review_mixed_merchant"
            manual_review.append(recommendation)
        elif (
            not is_configured
            and (group["rows"] >= 3 or spend >= Decimal("100"))
            and row_share >= Decimal("0.90")
        ):
            recommendation["action"] = "auto_add_profile"
            auto_rules.append(
                {
                    "change_type": "merchant_profile",
                    "subject": merchant,
                    "new_value": target_category,
                    "reason": "high_confidence_spending_pattern",
                    "pattern": simple_merchant_pattern(merchant),
                }
            )
        elif not is_configured:
            recommendation["action"] = "manual_review_low_confidence"
            manual_review.append(recommendation)
        else:
            recommendation["action"] = "keep"
        recommendations.append(recommendation)
    return recommendations, auto_rules, manual_review


def merge_merchant_rules(
    merchants: MerchantRulesConfig,
    auto_rules: list[dict[str, Any]],
    category_map: dict[str, str],
) -> MerchantRulesConfig:
    existing = {rule.merchant_norm.upper().strip() for rule in merchants.rules}
    # Taxonomy-only remap: preserve each rule's own exclude_from_spending verbatim (F10,
    # "preserve, never flip"). We must NOT force-exclude a rule just because its (remapped)
    # category name is in EXCLUDED_CATEGORIES -- that would silently flip a counted merchant
    # like an Investing rule via the merchant path (rules/service.py ORs merchant_exclude
    # with the category flag at apply-time). Genuinely-excluded categories (Transfers,
    # Income, ...) still exclude their transactions through the preserved category flag,
    # so dropping the override here does not under-exclude anything.
    rules = [
        MerchantRule(
            merchant_norm=rule.merchant_norm,
            category=category_map.get(rule.category, rule.category),
            patterns=rule.patterns,
            exclude_from_spending=rule.exclude_from_spending,
        )
        for rule in merchants.rules
    ]
    for item in auto_rules:
        merchant = str(item["subject"]).upper().strip()
        if merchant in existing:
            continue
        rules.append(
            MerchantRule(
                merchant_norm=merchant,
                category=category_map.get(str(item["new_value"]), str(item["new_value"])),
                patterns=[str(item["pattern"])],
            )
        )
        existing.add(merchant)
    return MerchantRulesConfig(version="optimized", rules=rules)


def merchant_from_description(description: str) -> str:
    tokens = re.findall(r"[A-Z0-9&'.*#-]+", description.upper())
    while tokens and tokens[-1] in {"ON", "BC", "CA", "QC", "AB"}:
        tokens.pop()
    return " ".join(tokens[:3]).strip()


def simple_merchant_pattern(merchant: str) -> str:
    escaped = re.escape(merchant).replace(r"\ ", r"\s+")
    return rf"\b{escaped}\b"


def category_rows(recommendations: list[CategoryRecommendation]) -> list[dict[str, Any]]:
    rows = []
    for rec in recommendations:
        if not rec.old_exclude_from_spending and rec.new_exclude_from_spending:
            change = "include->exclude"
        elif rec.old_exclude_from_spending and not rec.new_exclude_from_spending:
            change = "exclude->include"
        else:
            change = ""
        rows.append(
            {
                "old_category": rec.old_name,
                "new_category": rec.new_name,
                "parent": rec.parent or "",
                "old_exclude_from_spending": str(rec.old_exclude_from_spending).lower(),
                "new_exclude_from_spending": str(rec.new_exclude_from_spending).lower(),
                "spend_semantics_change": change,
                "action": (
                    "rename_or_reparent" if rec.old_name != rec.new_name or rec.parent else "keep"
                ),
            }
        )
    return rows


def make_run_dir(runs_dir: Path) -> Path:
    ensure_private_dir(runs_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / stamp
    suffix = 2
    while run_dir.exists():
        run_dir = runs_dir / f"{stamp}-{suffix}"
        suffix += 1
    return ensure_private_dir(run_dir)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        write_private_text_atomic(path, "")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with private_atomic_text(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(neutralize_csv_row(row) for row in rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_private_text_atomic(path, json_dumps(payload))


def json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)
