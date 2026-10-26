"""
Pytest fixtures and configuration for ledger tests.
"""

from pathlib import Path

import pytest

from ledger.config import (
    AppConfig,
    CategoriesConfig,
    CategoryRule,
    ClassificationPatterns,
    GeneralConfig,
    LedgerConfig,
    MerchantRule,
    MerchantRulesConfig,
)


@pytest.fixture(autouse=True)
def isolated_adapter_configuration(tmp_path, monkeypatch):
    """Source tests never discover private configuration from an owner runtime."""
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(tmp_path / "unconfigured-adapters.json"))


@pytest.fixture
def sample_merchant_rules() -> MerchantRulesConfig:
    """Sample merchant rules for testing."""
    return MerchantRulesConfig(
        version="test",
        rules=[
            MerchantRule(
                merchant_norm="MCDONALDS",
                category="Dining",
                patterns=[r"(?i)\bMCDONALD'?S\b"],
            ),
            MerchantRule(
                merchant_norm="COSTCO",
                category="Groceries",
                patterns=[r"(?i)\bCOSTCO\b"],
            ),
            MerchantRule(
                merchant_norm="AMAZON",
                category="Shopping",
                patterns=[r"(?i)\bAMAZON\b", r"(?i)\bAMZN\b"],
            ),
        ],
    )


@pytest.fixture
def sample_categories() -> CategoriesConfig:
    """Sample categories for testing."""
    return CategoriesConfig(
        version="test",
        default_category="Other",
        categories=[
            CategoryRule(name="Dining", merchants=["MCDONALDS", "STARBUCKS"]),
            CategoryRule(name="Groceries", merchants=["COSTCO", "WALMART"]),
            CategoryRule(name="Shopping", merchants=["AMAZON"]),
            CategoryRule(
                name="Income",
                merchants=["EXAMPLE FORMER EMPLOYER"],
                exclude_from_spending=True,
            ),
        ],
    )


@pytest.fixture
def sample_ledger_config() -> LedgerConfig:
    """Sample ledger config for testing."""
    return LedgerConfig(
        general=GeneralConfig(
            strict=False,
            etransfer_is_cashflow=True,
            cc_payment_is_transfer=True,
        ),
        rules={
            "harbor": ClassificationPatterns(
                transfer_patterns=[r"(?i)\btransfer\b"],
                etransfer_patterns=[r"(?i)\be-?transfer\b"],
                fee_patterns=[r"(?i)\bfee\b"],
                interest_patterns=[r"(?i)\binterest\b"],
                income_patterns=[r"(?i)\bpayroll\b"],
            ),
        },
    )


@pytest.fixture
def sample_app_config(
    sample_ledger_config: LedgerConfig,
    sample_merchant_rules: MerchantRulesConfig,
    sample_categories: CategoriesConfig,
) -> AppConfig:
    """Complete app config for testing."""
    return AppConfig(
        ledger=sample_ledger_config,
        merchants=sample_merchant_rules,
        categories=sample_categories,
    )


@pytest.fixture
def temp_csv_dir(tmp_path: Path) -> Path:
    """Create temp directory with sample CSV files."""
    csv_dir = tmp_path / "exports" / "2025-01"
    csv_dir.mkdir(parents=True)

    # Core-owned canonical CSV with independently fictional account labels
    harbor_csv = csv_dir / "harbor-chequing.csv"
    harbor_csv.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,McDonalds 90001 EXAMPLE TOWN,-24.50,CAD,,harbor,Chequing\nledger-mapped-v1,2025-01-15,PAYROLL DEPOSIT EXAMPLE WORKSHOP,2400.00,CAD,,harbor,Chequing\n"
    )

    return tmp_path / "exports"


@pytest.fixture(autouse=True)
def clear_db_cache():
    """Clear database engine + schema-init cache between tests."""
    from ledger.storage.database import clear_engine_cache
    from ledger.storage.migrations import clear_schema_cache

    yield
    clear_engine_cache()
    clear_schema_cache()


@pytest.fixture(autouse=True)
def isolated_default_runtime(tmp_path, monkeypatch):
    """Tests never inherit an operator database, defaults file or audit root."""
    monkeypatch.setenv("LEDGER_HOME", str(tmp_path.resolve() / "runtime"))
    monkeypatch.delenv("LEDGER_DB", raising=False)
    monkeypatch.delenv("LEDGER_ADAPTER_CONFIG", raising=False)
    monkeypatch.delenv("LEDGER_AUDIT_RUNS_DIR", raising=False)


@pytest.fixture
def fictional_adapter_metadata(monkeypatch):
    """Inert fictional metadata for generic identity and policy regression tests.

    This fixture loads no provider code and recognizes no bank layout. Parser
    integration tests install their own explicit adapter definitions separately.
    """
    from ledger.adapters import registry

    formats = [
        ("harbor_deposit", "harbor"),
        ("harbor_visa", "harbor"),
        ("cedar_mastercard", "cedar"),
        ("meadow_mastercard", "meadow"),
        ("lumen_deposit", "lumen"),
        ("lumen_credit", "lumen"),
    ]
    metadata = {
        "adapter_id": "fictional-metadata",
        "api_version": 1,
        "layout_revision": "fictional-v1",
        "pdf_formats": [
            {
                "format_id": format_id,
                "institution": institution,
                "headers": [],
                "balance_spec": None,
            }
            for format_id, institution in formats
        ],
        "policy": {
            "credit_institutions": ["cedar", "meadow"],
            "payment_tokens": {"lumen": ["payment"], "harbor": ["cedar", "meadow"]},
            "account_product_tokens": [
                ["spark", "spark"],
                ["orbit", "orbit"],
                ["cedar", "cedar"],
                ["meadow", "meadow"],
            ],
        },
    }
    monkeypatch.setattr(registry, "configured_metadata", lambda: (metadata,))
    return metadata
