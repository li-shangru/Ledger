"""
Unit tests for utility functions.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger.utils import (
    format_iso,
    parse_date,
    parse_money,
    quantize_cents,
    quarter_of,
    stable_id,
    year_month,
    year_quarter,
)


class TestParseMoney:
    """Tests for parse_money function."""

    def test_simple_positive(self):
        assert parse_money("100.00") == Decimal("100.00")
        assert parse_money("1234") == Decimal("1234")

    def test_simple_negative(self):
        assert parse_money("-100.00") == Decimal("-100.00")
        assert parse_money("-1234") == Decimal("-1234")

    def test_with_dollar_sign(self):
        assert parse_money("$100.00") == Decimal("100.00")
        assert parse_money("-$100.00") == Decimal("-100.00")
        assert parse_money("$1,234.56") == Decimal("1234.56")

    def test_parentheses_negative(self):
        assert parse_money("($102.68)") == Decimal("-102.68")
        assert parse_money("(1,234.56)") == Decimal("-1234.56")

    def test_with_currency_code(self):
        assert parse_money("CAD 100.00") == Decimal("100.00")
        assert parse_money("USD -50.00") == Decimal("-50.00")

    def test_with_thousands_separator(self):
        assert parse_money("1,234.56") == Decimal("1234.56")
        assert parse_money("$1,234,567.89") == Decimal("1234567.89")

    def test_empty_and_none(self):
        assert parse_money(None) is None
        assert parse_money("") is None
        assert parse_money("   ") is None

    def test_invalid(self):
        assert parse_money("abc") is None
        assert parse_money("$abc") is None

    def test_cr_dr_suffix(self):
        # CR = credit (money in / positive); DR/DB = debit (money out / negative)
        assert parse_money("100.00 CR") == Decimal("100.00")
        assert parse_money("100.00 DR") == Decimal("-100.00")
        assert parse_money("1,234.56 CR") == Decimal("1234.56")
        assert parse_money("$50.00CR") == Decimal("50.00")
        assert parse_money("50.00DR") == Decimal("-50.00")
        assert parse_money("75.00 cr") == Decimal("75.00")

    def test_normalizes_unicode_dashes(self):
        # M5: statements from some institutions (e.g. Lumen) print negatives
        # with an en/em dash or the Unicode MINUS SIGN. Without normalization a single
        # such row returns None and aborts the whole staging run. (Use \u escapes so
        # the literals stay unambiguous to lint - RUF001 flags bare dash glyphs.)
        assert parse_money("\u2013$50.00") == Decimal("-50.00")  # en dash
        assert parse_money("\u2014$50.00") == Decimal("-50.00")  # em dash
        assert parse_money("\u2212$50.00") == Decimal("-50.00")  # minus sign
        assert parse_money("$50.00") == Decimal("50.00")  # unchanged
        assert parse_money("-$50.00") == Decimal("-50.00")  # ASCII unchanged
        assert parse_money("\u2013$1,234.56") == Decimal("-1234.56")  # grouping intact

    def test_malformed_grouping_rejected(self):
        # Commas in wrong positions must return None, not silently-mangled Decimals.
        assert parse_money("1,2345") is None  # 4 digits after comma — invalid grouping
        assert parse_money("1,23,456") is None  # first group only 2 digits — invalid
        assert parse_money("12,34") is None  # only 2 digits after comma — invalid
        # Regression: old lookahead `,(?=\d{3})` matched the comma in "1234,567" and
        # stripped it, silently producing Decimal('1234567').  The VALID_GROUPED guard
        # rejects a 4-digit leading group so the comma survives and Decimal raises → None.
        assert parse_money("1234,567") is None  # 4-digit leading group — invalid
        assert parse_money(",123") is None  # no leading digits at all — invalid

    def test_valid_grouping_still_parses(self):
        # Regression: well-formed US-grouped numbers continue to parse.
        assert parse_money("1,000.00") == Decimal("1000.00")
        assert parse_money("1,234,567.89") == Decimal("1234567.89")
        assert parse_money("1,000") == Decimal("1000")

    def test_currency_sign_composition_with_grouping(self):
        # Currency symbols, leading minus, and parens must still compose correctly.
        assert parse_money("$1,234.56") == Decimal("1234.56")
        assert parse_money("-1,000.00") == Decimal("-1000.00")
        assert parse_money("(1,000.00)") == Decimal("-1000.00")

    def test_european_decimal_comma_stays_none(self):
        # We do not guess European format — these must remain None.
        assert parse_money("1.234,56") is None

    # --- non-finite guard (Fix 1) ---

    def test_nan_returns_none(self):
        """parse_money('NaN') must return None, not a non-finite Decimal."""
        assert parse_money("NaN") is None

    def test_infinity_returns_none(self):
        """parse_money('Infinity') must return None."""
        assert parse_money("Infinity") is None

    def test_inf_returns_none(self):
        """parse_money('inf') must return None (Decimal accepts 'inf')."""
        assert parse_money("inf") is None

    def test_nan_in_parens_returns_none(self):
        """parse_money('(NaN)') — parens-negative notation with NaN — must return None."""
        assert parse_money("(NaN)") is None

    def test_dollar_infinity_returns_none(self):
        """parse_money('$Infinity') — currency prefix with Infinity — must return None."""
        assert parse_money("$Infinity") is None

    def test_valid_amounts_unaffected_by_non_finite_guard(self):
        """Non-finite guard must not interfere with normal finite amounts."""
        assert parse_money("100.00") == Decimal("100.00")
        assert parse_money("-50.00") == Decimal("-50.00")
        assert parse_money("$1,234.56") == Decimal("1234.56")


class TestParseDate:
    """Tests for parse_date function."""

    def test_iso_format(self):
        assert parse_date("2025-01-15") == date(2025, 1, 15)
        assert parse_date("2025-12-31") == date(2025, 12, 31)

    def test_mdy_format(self):
        assert parse_date("1/15/2025") == date(2025, 1, 15)
        assert parse_date("01/15/2025") == date(2025, 1, 15)
        assert parse_date("12/31/2025") == date(2025, 12, 31)

    def test_empty_and_none(self):
        assert parse_date(None) is None
        assert parse_date("") is None
        assert parse_date("   ") is None

    def test_invalid(self):
        assert parse_date("abc") is None
        assert parse_date("2025-13-01") is None  # Invalid month
        assert parse_date("2025-01-32") is None  # Invalid day


class TestDateHelpers:
    """Tests for date helper functions."""

    def test_format_iso(self):
        assert format_iso(date(2025, 1, 15)) == "2025-01-15"

    def test_quarter_of(self):
        assert quarter_of(date(2025, 1, 15)) == "Q1"
        assert quarter_of(date(2025, 4, 15)) == "Q2"
        assert quarter_of(date(2025, 7, 15)) == "Q3"
        assert quarter_of(date(2025, 10, 15)) == "Q4"

    def test_year_month(self):
        assert year_month(date(2025, 1, 15)) == "2025-01"
        assert year_month(date(2025, 12, 1)) == "2025-12"

    def test_year_quarter(self):
        assert year_quarter(date(2025, 1, 15)) == "2025-Q1"
        assert year_quarter(date(2025, 12, 15)) == "2025-Q4"


class TestQuantizeCents:
    """Tests for quantize_cents function."""

    def test_rounds_correctly(self):
        assert quantize_cents(Decimal("1.234")) == Decimal("1.23")
        assert quantize_cents(Decimal("1.235")) == Decimal("1.24")
        assert quantize_cents(Decimal("1.999")) == Decimal("2.00")


class TestStableId:
    """Tests for stable_id function."""

    def test_deterministic(self):
        id1 = stable_id("a", "b", "c")
        id2 = stable_id("a", "b", "c")
        assert id1 == id2

    def test_different_inputs_different_ids(self):
        id1 = stable_id("a", "b", "c")
        id2 = stable_id("a", "b", "d")
        assert id1 != id2

    def test_case_insensitive(self):
        id1 = stable_id("ABC")
        id2 = stable_id("abc")
        assert id1 == id2

    def test_whitespace_normalized(self):
        id1 = stable_id("hello  world")
        id2 = stable_id("hello world")
        assert id1 == id2

    def test_handles_none(self):
        id1 = stable_id("a", None, "c")
        id2 = stable_id("a", "", "c")
        assert id1 == id2

    def test_length(self):
        id1 = stable_id("test")
        assert len(id1) == 40  # Truncated SHA-256


class TestCurrencyIdentitySuffix:
    def test_cad_and_blank_currency_preserve_legacy_hash_parts(self):
        from ledger.utils import hashing

        assert hashing.currency_identity_suffix(None) == ()
        assert hashing.currency_identity_suffix("") == ()
        assert hashing.currency_identity_suffix(" cad ") == ()

    def test_non_cad_currency_adds_one_normalized_identity_part(self):
        from ledger.utils import hashing

        assert hashing.currency_identity_suffix(" usd ") == ("currency:USD",)


class TestScriptVersion:
    def test_production_manifest_includes_finance_policy(self):
        from ledger.utils import hashing

        assert "finance_policy.py" in hashing._SCRIPT_VERSION_FILES

    def test_hashes_sorted_relative_paths_and_raw_bytes(self, tmp_path: Path):
        from ledger.utils import hashing

        (tmp_path / "a.py").write_bytes(b"same bytes")
        (tmp_path / "b.py").write_bytes(b"other bytes")

        forward = hashing._hash_version_files(tmp_path, ("a.py", "b.py"))
        reverse = hashing._hash_version_files(tmp_path, ("b.py", "a.py"))

        assert forward == reverse
        assert len(forward) == 40
        assert all(character in "0123456789abcdef" for character in forward)

        (tmp_path / "a.py").write_bytes(b"changed bytes")
        assert hashing._hash_version_files(tmp_path, ("a.py", "b.py")) != forward

    def test_relative_path_is_part_of_version_hash(self, tmp_path: Path):
        from ledger.utils import hashing

        (tmp_path / "first.py").write_bytes(b"identical")
        first = hashing._hash_version_files(tmp_path, ("first.py",))
        (tmp_path / "second.py").write_bytes(b"identical")
        second = hashing._hash_version_files(tmp_path, ("second.py",))

        assert first != second

    def test_incomplete_required_inputs_return_unknown(self, tmp_path: Path, monkeypatch):
        from ledger.utils import hashing

        readable = tmp_path / "readable.py"
        unreadable = tmp_path / "unreadable.py"
        empty = tmp_path / "empty.py"
        readable.write_bytes(b"ok")
        unreadable.write_bytes(b"blocked")
        empty.write_bytes(b"")

        assert hashing._hash_version_files(tmp_path, ()) == "unknown"
        assert hashing._hash_version_files(tmp_path, ("missing.py",)) == "unknown"
        assert hashing._hash_version_files(tmp_path, ("empty.py",)) == "unknown"

        original_read_bytes = Path.read_bytes

        def fail_one(path: Path) -> bytes:
            if path == unreadable:
                raise OSError("forced unreadable provenance input")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", fail_one)
        assert hashing._hash_version_files(tmp_path, ("readable.py", "unreadable.py")) == "unknown"

    def test_live_script_version_is_complete_40_character_hash(self):
        from ledger.utils import hashing

        version = hashing.script_version()

        assert version != "unknown"
        assert len(version) == 40
        assert all(character in "0123456789abcdef" for character in version)

    def test_production_manifest_covers_parser_extractor_and_normalization_modules(self):
        import ledger
        from ledger.utils import hashing

        package_root = Path(ledger.__file__).resolve().parent
        required = {
            "core/models.py",
            "processing/pipeline.py",
            "utils/dates.py",
            "utils/fuzzy.py",
            "utils/hashing.py",
            "utils/money.py",
            "utils/text.py",
        }
        required.update(
            path.relative_to(package_root).as_posix()
            for path in (package_root / "parsers").glob("*.py")
        )
        required.update(
            path.relative_to(package_root).as_posix()
            for path in (package_root / "processing").glob("*.py")
        )
        required.update(
            path.relative_to(package_root).as_posix()
            for path in (package_root / "imports" / "pdf" / "statement_pdf_to_csv").glob("*.py")
        )

        assert required <= set(hashing._SCRIPT_VERSION_FILES)
