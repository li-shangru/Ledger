"""PDF import staging helpers for building and summarising merchant-review candidates.

Operates purely on in-memory ``ProcessingResult`` / ``Transaction`` objects from a
single import run — no DB writes.  Produces the candidate list that
``import_review`` later persists, and formats human-readable summaries.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.core.models import Transaction
from ledger.processing import ProcessingResult
from ledger.utils.private_files import write_private_text_atomic


def review_key(txn: Transaction) -> str:
    return (txn.merchant_raw or txn.merchant or txn.description).strip().upper()


def needs_merchant_review(txn: Transaction) -> bool:
    return (
        txn.merchant_confidence != "high"
        or txn.category_confidence != "high"
        or txn.category == "Other"
    )


def build_merchant_review(result: ProcessingResult) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}

    for txn in result.transactions:
        if not needs_merchant_review(txn):
            continue

        key = review_key(txn)
        if not key:
            continue

        group = groups.setdefault(
            key,
            {
                "key": key,
                "merchant": txn.merchant,
                "merchant_raw": txn.merchant_raw,
                "current_category": txn.category,
                "merchant_confidence": txn.merchant_confidence,
                "category_confidence": txn.category_confidence,
                "count": 0,
                "total_abs_amount": Decimal("0"),
                "txn_ids": [],
                "examples": [],
            },
        )
        group["count"] += 1
        group["total_abs_amount"] += abs(txn.amount_signed)
        group["txn_ids"].append(txn.txn_id)

        example = {
            "description": txn.description,
            "posting_date": txn.posting_date.isoformat(),
            "amount_signed": txn.amount_signed,
            "source_file": txn.source_file,
        }
        if example not in group["examples"] and len(group["examples"]) < 5:
            group["examples"].append(example)

    return sorted(groups.values(), key=lambda item: (-item["count"], item["key"]))


def write_merchant_review_markdown(path: Path, candidates: list[dict[str, Any]]) -> None:
    lines = [
        "# Merchant Review",
        "",
        "Codex should research each candidate online, then use staging-only `review resolve --teach` for confident recurring matches. Do not edit the live Rules / Settings config while this run is open; export runtime config back to TOML after finalize when a seed snapshot is needed.",
        "",
    ]

    if not candidates:
        lines.append("No merchant review candidates found.")
    else:
        for candidate in candidates:
            lines.extend(
                [
                    f"## {candidate['key']}",
                    "",
                    f"- Current merchant: {candidate['merchant'] or '(blank)'}",
                    f"- Current category: {candidate['current_category']}",
                    f"- Confidence: merchant={candidate['merchant_confidence']}, category={candidate['category_confidence']}",
                    f"- Imported transactions: {candidate['count']}",
                    "- Examples:",
                ]
            )
            for example in candidate["examples"]:
                lines.append(
                    f"  - {example['posting_date']} {example['amount_signed']}: {example['description']}"
                )
            lines.append("")

    write_private_text_atomic(
        path,
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


def print_merchant_summary(merchant_summary: dict[str, list[dict[str, Any]]]) -> None:
    resolved = merchant_summary["resolved_merchants"]
    unresolved = merchant_summary["manual_review_merchants"]

    if resolved:
        print("Resolved merchant categories:")
        for item in resolved:
            print(f"  - {item['merchant']}: {item['category']} ({item['count']} transactions)")
    else:
        print("Resolved merchant categories: none")

    if unresolved:
        print("Manual-review merchants:")
        for item in unresolved:
            print(f"  - {item['key']}: {item['count']} transactions")
    else:
        print("Manual-review merchants: none")


def summarize_final_merchants(
    stage_candidates: list[dict[str, Any]],
    result: ProcessingResult,
) -> dict[str, list[dict[str, Any]]]:
    txns_by_id: dict[str, Transaction] = {}
    for txn in result.transactions + result.transfers:
        txns_by_id[txn.txn_id] = txn

    resolved: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []

    for candidate in stage_candidates:
        candidate_txns = [
            txns_by_id[txn_id] for txn_id in candidate["txn_ids"] if txn_id in txns_by_id
        ]
        still_unresolved = [txn for txn in candidate_txns if needs_merchant_review(txn)]

        if candidate_txns and not still_unresolved:
            merchant = candidate_txns[0].merchant
            category = candidate_txns[0].category
            key = (merchant, category)
            item = resolved.setdefault(
                key,
                {
                    "merchant": merchant,
                    "category": category,
                    "count": 0,
                    "from_candidate": candidate["key"],
                },
            )
            item["count"] += len(candidate_txns)
        else:
            unresolved.append(
                {
                    "key": candidate["key"],
                    "current_category": candidate["current_category"],
                    "count": candidate["count"],
                    "examples": candidate["examples"],
                }
            )

    return {
        "resolved_merchants": sorted(resolved.values(), key=lambda item: item["merchant"]),
        "manual_review_merchants": unresolved,
    }
