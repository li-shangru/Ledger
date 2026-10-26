"""Shared query module base."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from sqlalchemy.orm import Session

from ledger.storage.database import session_scope

SessionScopeFactory = Callable[[Path], AbstractContextManager[Session]]


class QueryModule:
    """Small focused query module base."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
    ) -> None:
        self.db_path = db_path
        self._session_scope_factory = session_scope_factory

    def _open_session(self) -> AbstractContextManager[Session]:
        """Open the caller-selected session boundary for one query."""
        return self._session_scope_factory(self.db_path)


__all__ = ["QueryModule", "SessionScopeFactory"]
