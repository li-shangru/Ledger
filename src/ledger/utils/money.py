"""
Money parsing utilities.

Handles various formats:
- -2000
- $118.51
- -$102.68
- ($102.68)  (parentheses = negative)
- 1,234.56
- CAD 100.00
"""

import re
from decimal import Decimal, InvalidOperation

# Patterns for cleaning
CURRENCY_SYMBOLS = re.compile(r"[$€£¥]")
CURRENCY_CODES = re.compile(r"\b(CAD|USD|EUR|GBP)\b", re.IGNORECASE)
# Valid US-grouped number: 1-3 leading digits, then one or more groups of exactly 3
# digits, with an optional decimal fraction.  Examples: 1,000  1,234.56  1,234,567.89
VALID_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
# Trailing credit/debit notation: "100.00 CR" (credit), "100.00 DR"/"DB" (debit)
TRAILING_CR_DR = re.compile(r"\s*(CR|DR|DB)$", re.IGNORECASE)


def parse_money(value: str | None) -> Decimal | None:
    """
    Parse a monetary string to Decimal.

    Returns None if parsing fails (prefer explicit None over exceptions).

    Examples:
        >>> parse_money("$1,234.56")
        Decimal('1234.56')
        >>> parse_money("($102.68)")
        Decimal('-102.68')
        >>> parse_money("-$50.00")
        Decimal('-50.00')
    """
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    # Normalize typographic minus glyphs to ASCII "-" so the leading/trailing-minus
    # logic below sees them. Some statement layouts
    # print negatives with an en/em dash or the Unicode MINUS SIGN; without this a
    # single such row returns None and aborts the whole staging run.
    raw = raw.translate({0x2013: "-", 0x2014: "-", 0x2212: "-"})

    # Track if negative
    is_negative = False

    # Handle parentheses notation: ($100) = -100
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1].strip()

    # Remove currency symbols and codes
    raw = CURRENCY_SYMBOLS.sub("", raw)
    raw = CURRENCY_CODES.sub("", raw).strip()

    # Handle trailing CR/DR notation: CR = credit (positive), DR/DB = debit (negative)
    cr_dr = TRAILING_CR_DR.search(raw)
    if cr_dr:
        raw = raw[: cr_dr.start()].strip()
        if cr_dr.group(1).upper() in ("DR", "DB"):
            is_negative = True

    # Handle leading minus
    if raw.startswith("-"):
        is_negative = True
        raw = raw[1:].strip()

    # Handle trailing minus (some formats use this)
    if raw.endswith("-"):
        is_negative = True
        raw = raw[:-1].strip()

    # Remove thousands separators -- but ONLY when the numeric portion is a valid
    # US-grouped number (1-3 leading digits, then groups of exactly 3).  If the
    # comma doesn't fit that pattern, leave it so Decimal() will reject it and
    # we return None instead of silently mangling the value (e.g. "1,2345" must
    # not become Decimal('12345')).
    if "," in raw:
        if VALID_GROUPED.match(raw):
            raw = raw.replace(",", "")
        # else: leave the comma in — Decimal(raw) will raise → return None

    if not raw:
        return None

    # Reject scientific notation (e.g. "1e5", "1.5E2", "2.5e-3").  A real
    # statement amount never uses it, but Decimal() would happily parse it and
    # silently mis-scale the value, so treat it as unparseable → None.
    if "e" in raw or "E" in raw:
        return None

    try:
        amount = Decimal(raw)
        if not amount.is_finite():
            return None
        return -amount if is_negative else amount
    except InvalidOperation, ValueError:
        return None


def quantize_cents(amount: Decimal) -> Decimal:
    """Round to 2 decimal places."""
    return amount.quantize(Decimal("0.01"))


def to_cents(value) -> Decimal:
    """Coerce an arbitrary numeric/None value to a cent-quantized Decimal.

    Single canonical contract for the "quantize money to cents" helpers that were
    re-implemented across the aggregate, reporting, adjustments, and readiness
    layers with subtly different None/empty/falsy-zero handling. ``None`` and the
    empty string become ``Decimal("0.00")``; everything else is coerced via
    ``str()`` (so floats from SQLite ``SUM`` round-trip exactly) and quantized.
    """
    if value is None or value == "":
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def is_cad(currency: str | None) -> bool:
    """Compatibility wrapper for the public CAD finance policy.

    Rows imported before currency tracking existed carry ``None``/``""`` and are
    treated as CAD for backward compatibility; only a real foreign currency such
    as ``"USD"`` is excluded. New code should use
    :func:`ledger.finance_policy.is_cad_row` directly.
    """
    from ledger.finance_policy import is_cad_row

    return is_cad_row(currency=currency)
