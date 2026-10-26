"""Core-owned intermediate CSV produced only after an explicit profile preview."""

import re
from datetime import date
from decimal import Decimal

from ledger.parsers.base import BaseParser
from ledger.utils.hashing import fingerprint, stable_id

CANONICAL_HEADERS = (
    "Ledger Format",
    "Date",
    "Description",
    "Amount",
    "Currency",
    "Reference",
    "Institution",
    "Account Type",
)


class CanonicalParser(BaseParser):
    FORMAT_NAME = "ledger-mapped-v1"
    REQUIRED_HEADERS = set(CANONICAL_HEADERS)
    INSTITUTION = "mapped"

    @classmethod
    def can_parse(cls, headers: set[str]) -> bool:
        return headers == cls.REQUIRED_HEADERS

    def parse_row(self, row: dict, row_num: int):
        if row.get("Ledger Format") != self.FORMAT_NAME:
            raise ValueError("canonical_format_invalid")
        amount_text = row.get("Amount", "")
        currency = row.get("Currency", "")
        institution = row.get("Institution", "")
        if not re.fullmatch(r"[+-]?[0-9]{1,18}(?:\.[0-9]{1,2})?", amount_text):
            raise ValueError("canonical_amount_invalid")
        if not re.fullmatch(r"[A-Z]{3}", currency) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", institution
        ):
            raise ValueError("canonical_identity_invalid")
        amount = Decimal(amount_text)
        posting_date = date.fromisoformat(row["Date"])
        description = row["Description"].strip()
        if not description or posting_date.isoformat() != row["Date"]:
            raise ValueError("canonical_row_invalid")
        reference = row.get("Reference", "").strip()
        identity = (self.FORMAT_NAME, institution, self.ctx.account, currency)
        content = (posting_date.isoformat(), format(amount, ".2f"), description)
        occurrences = getattr(self, "_mapped_occurrences", {})
        occurrence_scope = (*identity, *content)
        occurrence = occurrences.get(occurrence_scope, 0) if not reference else 0
        if not reference:
            occurrences[occurrence_scope] = occurrence + 1
        self._mapped_occurrences = occurrences
        # Identical rows without references may represent repeated purchases. Keep
        # their multiplicity while retaining stable IDs across repeat exports.
        occurrence_key = "" if reference or occurrence == 0 else str(occurrence)
        txn_id = (
            stable_id(*identity, "reference", reference)
            if reference
            else stable_id(*identity, *content, occurrence_key)
        )
        builder = self._builder(row_num).set(
            institution=institution,
            account_type=row.get("Account Type", ""),
            txn_id=txn_id,
            txn_fingerprint=fingerprint(*identity, *content, reference, occurrence_key),
            posting_date=posting_date,
            transaction_date=posting_date,
            source_period=posting_date.strftime("%Y-%m"),
            flow="income" if amount > 0 else "expense" if amount < 0 else "unknown",
            cashflow_type="income" if amount > 0 else "expense" if amount < 0 else "neutral",
            include_in_cashflow=amount != 0,
            amount_raw=amount,
            amount_signed=amount,
            currency=currency,
            description=description,
        )
        builder = self._enrich(builder, description, "", description)
        return builder.set(**self._compute_temporal_features(posting_date)).build()
