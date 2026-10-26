"""Type aliases for common types used throughout the ledger system."""

from datetime import date
from decimal import Decimal

# Transaction aggregations
type CategorySpending = dict[str, Decimal]
type MerchantSpending = dict[str, Decimal]
type InstitutionCounts = dict[str, int]
type FlowCounts = dict[str, int]

# Configuration
type FilterDict = dict[str, str | None]
type ConfigVersions = dict[str, str]

# Cashflow data
type QuarterCashflow = dict[str, dict[str, Decimal]]
type MonthlyCashflow = dict[str, Decimal]

# Database stats
type DatabaseStats = dict[str, int | date | None]
