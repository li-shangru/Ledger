"""
Analysis module for account detection, balance tracking, trend aggregation, and health scoring.
"""

from ledger.analysis.account_detector import AccountDetector
from ledger.analysis.balance_calculator import BalanceCalculator
from ledger.analysis.health_scorer import FinancialHealthScorer
from ledger.analysis.pattern_detector import RecurringPatternDetector
from ledger.analysis.pattern_matcher import PatternMatcher
from ledger.analysis.runner import AnalysisRunner
from ledger.analysis.trend_aggregator import TrendAggregator

__all__ = [
    "AccountDetector",
    "AnalysisRunner",
    "BalanceCalculator",
    "TrendAggregator",
    "FinancialHealthScorer",
    "RecurringPatternDetector",
    "PatternMatcher",
]
