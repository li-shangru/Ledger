"""Path configuration for assisted PDF import runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    pdf_dir: Path
    processed_dir: Path
    db_path: Path
    base_db_path: Path | None
    backup_dir: Path
    ledger_config: Path
    merchant_rules: Path
    categories: Path
    runs_dir: Path
