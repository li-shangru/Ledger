"""Command-line interface for ledger."""

from __future__ import annotations

import typer

from ledger.commands.accounts import accounts_app
from ledger.commands.adapters import adapters_app
from ledger.commands.adjustments import adjustments_app
from ledger.commands.analysis import analysis_app
from ledger.commands.audit import audit_app
from ledger.commands.budget import budget_app
from ledger.commands.build import register_build_commands
from ledger.commands.common import resolve_db_path
from ledger.commands.config import config_app, rules_app
from ledger.commands.csv_profiles import csv_profiles_app
from ledger.commands.data import data_app
from ledger.commands.db import db_app
from ledger.commands.evidence import evidence_app
from ledger.commands.export import export_app
from ledger.commands.import_investment import import_investment_app
from ledger.commands.import_pdf import import_pdf_app
from ledger.commands.optimize import optimize_app
from ledger.commands.proposals import proposals_app
from ledger.commands.quality import quality_app
from ledger.commands.repair import repair_app
from ledger.commands.reports import reports_app

app = typer.Typer(
    name="ledger",
    help="Personal finance ledger with multi-institution support.",
    add_completion=False,
)

app.add_typer(accounts_app, name="accounts")
app.add_typer(adapters_app, name="adapters")
app.add_typer(config_app, name="config")
app.add_typer(rules_app, name="rules")
app.add_typer(adjustments_app, name="adjustments")
app.add_typer(export_app, name="export")
app.add_typer(db_app, name="db")
app.add_typer(evidence_app, name="evidence")
app.add_typer(reports_app, name="reports")
app.add_typer(analysis_app, name="analysis")
app.add_typer(data_app, name="data")
app.add_typer(quality_app, name="quality")
app.add_typer(budget_app, name="budget")
app.add_typer(audit_app, name="audit")
app.add_typer(repair_app, name="repair")
app.add_typer(optimize_app, name="optimize")
app.add_typer(proposals_app, name="proposals")

import_app = typer.Typer(help="Assisted-safe local import workflows.")
import_app.add_typer(import_pdf_app, name="pdf")
import_app.add_typer(csv_profiles_app, name="csv")
import_app.add_typer(import_investment_app, name="investment")
app.add_typer(import_app, name="import")

register_build_commands(app)

__all__ = ["app", "resolve_db_path"]


if __name__ == "__main__":
    app()
