"""Compatibility facade for focused ledger query modules."""

from __future__ import annotations

from pathlib import Path

from ledger.query.accounts import AccountQueries
from ledger.query.agent import AgentQueries
from ledger.query.aggregates import AggregateQueries
from ledger.query.base import SessionScopeFactory
from ledger.query.exports import ExportQueries
from ledger.query.insights import InsightQueries
from ledger.query.investments import InvestmentQueries
from ledger.query.quality import QualityQueries
from ledger.query.reports import ReportQueries
from ledger.query.transactions import TransactionQueries
from ledger.query.transfers import TransferQueries


class LedgerQueryService(
    TransactionQueries,
    AggregateQueries,
    AccountQueries,
    InsightQueries,
    QualityQueries,
    TransferQueries,
    ExportQueries,
    ReportQueries,
    InvestmentQueries,
):
    """High-level query interface for ledger data.

    Public methods live in focused query modules, while this typed facade keeps
    both direct calls and the legacy query namespaces available.
    """

    transactions: TransactionQueries
    aggregates: AggregateQueries
    accounts: AccountQueries
    insights: InsightQueries
    quality: QualityQueries
    transfers: TransferQueries
    exports: ExportQueries
    reports: ReportQueries
    investments: InvestmentQueries
    agent: AgentQueries

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory | None = None,
        snapshot_generation: str = "",
    ) -> None:
        if session_scope_factory is None and not db_path.exists():
            raise FileNotFoundError(f"Database not found: {db_path}")

        if session_scope_factory is None:
            super().__init__(db_path)
        else:
            super().__init__(db_path, session_scope_factory=session_scope_factory)
        self.agent = AgentQueries(self._open_session, snapshot_generation)
        self.transactions = self
        self.aggregates = self
        self.accounts = self
        self.insights = self
        self.quality = self
        self.transfers = self
        self.exports = self
        self.reports = self
        self.investments = self
