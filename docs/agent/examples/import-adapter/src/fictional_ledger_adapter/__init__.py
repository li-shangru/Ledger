"""A fictional five-column transaction export; no real institution layout."""

import re
from datetime import date
from decimal import Decimal

from ledger.adapters.contracts import API_VERSION, AdapterDefinition, CsvFormat
from ledger.parsers.base import BaseParser
from ledger.utils.hashing import fingerprint, stable_id


class FictionalParser(BaseParser):
    """Only the invented Day/Memo/Delta/Unit/Receipt layout is supported."""

    FORMAT_NAME = "fictional-csv-v1"
    INSTITUTION = "fictional"
    REQUIRED_HEADERS = {"Day", "Memo", "Delta", "Unit", "Receipt"}

    @classmethod
    def can_parse(cls, headers: set[str]) -> bool:
        return headers == cls.REQUIRED_HEADERS

    def parse_row(self, row: dict, row_num: int):
        # No silent skips, currency conversion, float arithmetic or implicit rounding.
        if set(row) != self.REQUIRED_HEADERS or not all(type(v) is str for v in row.values()):
            raise ValueError("fictional_row_invalid")
        if not re.fullmatch(r"[+-]?[0-9]{1,18}(?:\.[0-9]{1,2})?", row["Delta"]):
            raise ValueError("fictional_amount_invalid")
        if not re.fullmatch(r"[A-Z]{3}", row["Unit"]):
            raise ValueError("fictional_currency_invalid")
        day = date.fromisoformat(row["Day"])
        if day.isoformat() != row["Day"] or not row["Memo"].strip():
            raise ValueError("fictional_row_invalid")
        amount = Decimal(row["Delta"])
        description = row["Memo"].strip()
        reference = row["Receipt"].strip()
        identity = (self.FORMAT_NAME, self.INSTITUTION, self.ctx.account, row["Unit"])
        content = (day.isoformat(), format(amount, ".2f"), description)
        txn_id = (
            stable_id(*identity, "reference", reference)
            if reference
            else stable_id(*identity, *content)
        )
        flow = "income" if amount > 0 else "expense" if amount < 0 else "unknown"
        builder = self._builder(row_num).set(
            txn_id=txn_id,
            txn_fingerprint=fingerprint(*identity, *content, reference),
            posting_date=day,
            transaction_date=day,
            source_period=day.strftime("%Y-%m"),
            # This invented format is a transaction export with no account-type evidence.
            account_type="",
            flow=flow,
            cashflow_type=flow if amount != 0 else "neutral",
            include_in_cashflow=amount != 0,
            amount_raw=amount,
            amount_signed=amount,
            currency=row["Unit"],
            description=description,
        )
        builder = self._enrich(builder, description, "", description)
        return builder.set(**self._compute_temporal_features(day)).build()


def create_adapter() -> AdapterDefinition:
    """Called only by explicit enablement or an enabled, artifact-pinned import."""
    return AdapterDefinition(
        adapter_id="fictional-example",
        api_version=API_VERSION,
        layout_revision="1",
        csv_formats=(CsvFormat(FictionalParser.FORMAT_NAME, "fictional", FictionalParser),),
    )
