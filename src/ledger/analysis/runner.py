"""Orchestrate post-import analysis jobs."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ledger.core.results import RunResult
from ledger.finance_policy import is_cad_row
from ledger.storage.database import TransactionDB, TransferDB, session_scope
from ledger.storage.mappers import transaction_from_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisRefreshResult:
    """Outcome of a full derived-analytics recompute."""

    accounts: dict[str, int]
    patterns: dict[str, int]
    trends: dict[str, int]
    health: dict[str, int]
    adjustments: dict[str, int]
    snapshot: dict[str, Any]
    insights: RunResult
    quality: list[Any]


class AnalysisRunner:
    """Run all derived analysis tables for a ledger database."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def run_account_analysis(self) -> dict[str, int]:
        from ledger.analysis import AccountDetector, BalanceCalculator

        logger.info("Running account detection and balance calculation...")

        # Load all transactions AND all transfer legs in a single session so that
        # account detection, stats update, and balance computation all share the
        # same in-memory data rather than each issuing their own DB queries.
        with session_scope(self.db_path) as session:
            db_txns = session.query(TransactionDB).all()
            transactions = [transaction_from_db(db_txn) for db_txn in db_txns]

            # Group transactions by (institution, account) for per-account fast paths.
            txns_by_acct: dict[tuple[str, str], list] = defaultdict(list)
            for txn in transactions:
                txns_by_acct[(txn.institution, txn.account)].append(txn)

            # Group transfer legs by (institution, account).
            legs_by_acct: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for row in session.query(TransferDB).all():
                legs_by_acct[(str(row.institution), str(row.account))].append(
                    {
                        "posting_date": row.posting_date,
                        "amount_raw": row.amount_raw,
                        "currency": row.currency,
                    }
                )

        detector = AccountDetector(self.db_path)
        accounts = detector.detect_accounts(transactions)
        accounts_detected = detector.save_accounts(accounts, replace_all=True)

        # detect_accounts builds the credit limit from PURCHASES ONLY, which massively
        # overstates it (the running balance becomes a monotonic cumulative-purchase
        # total). Re-run the leg-aware stats updater per account so the persisted
        # detected_credit_limit folds in the card's payment legs (transfers table) and
        # reflects the real deepest balance owed. This must precede balance computation,
        # which reads detected_credit_limit as the credit_utilization denominator.
        #
        # Use the fast-path that reuses already-loaded data instead of re-querying.
        for account in accounts:
            key = (account.institution, account.account_number)
            detector.update_account_stats_with_data(
                account,
                txns_by_acct.get(key, []),
                legs_by_acct.get(key, []),
            )

        calculator = BalanceCalculator(self.db_path)
        balance_results = calculator.compute_all_balances()
        total_balances = sum(balance_results.values())

        # Credit utilization needs an ABSOLUTE balance, and those rows are written by
        # statement repair rather than by this calculator -- so it cannot be derived
        # during the relative recompute above. Reconcile it here, against whatever
        # anchors the database currently holds, so an analysis refresh is enough to
        # populate (or retire) the column instead of it waiting on the next repair.
        utilization = calculator.apply_credit_utilization()

        return {
            "accounts_detected": accounts_detected,
            "balances_computed": total_balances,
            "credit_utilization_populated": utilization["populated"],
            "credit_utilization_cleared": utilization["cleared"],
        }

    def apply_credit_utilization(self) -> dict[str, int]:
        """Reconcile credit_utilization against the anchors currently stored.

        Exposed separately because statement repair writes absolute anchors AFTER
        it runs account analysis; without a second pass in that order the column
        is left NULL on every card until the next refresh.
        """
        from ledger.analysis import BalanceCalculator

        return BalanceCalculator(self.db_path).apply_credit_utilization()

    def run_pattern_detection(self) -> dict[str, int]:
        from ledger.analysis import PatternMatcher, RecurringPatternDetector

        logger.info("Running recurring pattern detection...")
        with session_scope(self.db_path) as session:
            db_txns = session.query(TransactionDB).all()
            transactions = [transaction_from_db(db_txn) for db_txn in db_txns]

        # Patterns average amounts across occurrences into expected_amount /
        # amount_std_dev, which PatternMatcher then matches future txns against.
        # Mixing un-FX-converted currencies would average dollars-as-CAD, so detect
        # patterns over CAD-equivalent rows only (consistent with the rest of the
        # analysis layer's CAD-only money invariant).
        transactions = [txn for txn in transactions if is_cad_row(currency=txn.currency)]

        detector = RecurringPatternDetector(self.db_path, min_occurrences=3)
        patterns = detector.detect_patterns(transactions)
        patterns_detected = detector.save_patterns(patterns)

        matcher = PatternMatcher(self.db_path)
        match_results = matcher.match_all_transactions()

        return {
            "patterns_detected": patterns_detected,
            "transactions_matched": match_results["transactions_matched"],
            "patterns_updated": match_results["patterns_updated"],
        }

    def run_trend_aggregation(self, days: int = 30, months: int = 12) -> dict[str, int]:
        from ledger.analysis import TrendAggregator

        logger.info("Running trend aggregation (%s days, %s months)...", days, months)
        aggregator = TrendAggregator(self.db_path)
        daily_trends = aggregator.aggregate_recent_days(days)
        monthly_trends = aggregator.aggregate_recent_months(months)

        return {
            "daily_trends_computed": daily_trends,
            "monthly_trends_computed": monthly_trends,
        }

    def run_health_scoring(self, days: int = 30, *, today: date | None = None) -> dict[str, int]:
        from ledger.analysis import FinancialHealthScorer

        logger.info("Running health scoring (%s days)...", days)
        scorer = FinancialHealthScorer(self.db_path)
        scores_computed = scorer.compute_recent_scores(days, today=today)
        return {"scores_computed": scores_computed}

    def run_all(
        self,
        *,
        source: str,
        days: int = 30,
        months: int = 12,
        today: date | None = None,
        detect_adjustments: bool = True,
    ) -> AnalysisRefreshResult:
        """Recompute every derived table, in dependency order.

        The single definition of "refresh the derived analytics". Callers previously
        hand-rolled this sequence and had quietly diverged -- `ledger build` skipped
        adjustments, insights, and quality checks entirely, so a CSV-built database
        carried no findings at all. Ordering matters and is preserved here:

        1. accounts/balances first, because pattern detection and health scoring read them;
        2. adjustments BEFORE the snapshot, so True Cost reflects offsets immediately
           (and so the snapshot freshness gate sees the adjustments_detect audit row);
        3. insights after the snapshot, since findings are derived from its payload.

        Transfer matching is deliberately excluded: it needs the parsed `config` and
        belongs to the import path, not to a standalone recompute.
        """
        from ledger.adjustments import AdjustmentDetectionService
        from ledger.insights import InsightEngine
        from ledger.quality import DataQualityService
        from ledger.reporting import ReportSnapshotService

        accounts = self.run_account_analysis()
        patterns = self.run_pattern_detection()
        trends = self.run_trend_aggregation(days=days, months=months)
        health = self.run_health_scoring(days=days, today=today)

        adjustments: dict[str, int] = {"offsets": 0, "treatments": 0}
        if detect_adjustments:
            detected = AdjustmentDetectionService(self.db_path).detect(execute=True)
            adjustments = {
                "offsets": len(detected.offsets),
                "treatments": len(detected.treatments),
            }

        snapshot = ReportSnapshotService(self.db_path).create_snapshot(source=source)
        insights = InsightEngine(self.db_path).refresh_period(
            snapshot["period_start"], snapshot["period_end"]
        )
        quality = DataQualityService(self.db_path).run_checks()

        return AnalysisRefreshResult(
            accounts=accounts,
            patterns=patterns,
            trends=trends,
            health=health,
            adjustments=adjustments,
            snapshot=snapshot,
            insights=insights,
            quality=quality,
        )
