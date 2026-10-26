# Transfer matching

Use [architecture](architecture.md) for the surrounding pipeline and
[configuration](configuration.md) for runtime rules.

Transfer matching links internal money movements across accounts while keeping transfers out of cashflow totals.

## Runtime Path

`ledger build` runs transfer matching after parsing, validation, deduplication, enrichment, and transfer partitioning.

Relevant files:

- `src/ledger/processing/pipeline.py`
- `src/ledger/analysis/transfer_matcher.py`
- `src/ledger/storage/writer.py`
- `src/ledger/analysis/runner.py`
- `src/ledger/query/service.py`
- `streamlit_app.py`

## Configuration

`configs/ledger.toml` supports:

```toml
[general]
enable_transfer_matching = true
transfer_match_window = 5
transfer_amount_tolerance = 0.0
transfer_min_confidence = 0.5
```

These values are loaded by `load_ledger_config()`. If omitted, the schema defaults above apply.

## Matching Signals

Two legs in **different currencies** are never paired: a USD leg and a CAD leg of
equal magnitude cannot be reconciled without an FX rate, so the matcher rejects the
pair outright (0.0) before scoring. Cross-currency transfers are intentionally left
unmatched rather than paired on magnitude alone.

For each candidate pair the matcher computes a confidence score (0.0-1.0) from
these factors:

- exact absolute `amount_raw` match within the configured tolerance (+0.50). A
  pair where both legs are zero-amount, or whose amounts differ beyond the
  tolerance, is rejected outright.
- posting date proximity within the configured window (+0.20 for same day, with
  a linear decay for nearer-but-not-same dates). Dates beyond the window are
  rejected.
- opposite `amount_raw` signs (+0.10). Opposite signs are required unless exactly
  one leg is a credit card (a cross-issuer card payment can be same-signed).
- valid account-type pattern such as chequing to credit card or savings to
  chequing, or an internal transfer between two different accounts at the same
  institution (+0.10).
- cross-institution payment hints from the selected private adapter's validated
  declarative policy (+0.10). See `compatibility_policy()` in
  `src/ledger/adapters/registry.py`; provider-specific tokens stay outside the core.
- a shared trailing reference number — a 3-4 digit token some institutions append
  to both legs of an online transfer, e.g. `Online Banking transfer - 9816`
  (+0.15). This is a near-certain disambiguator. A trailing 4-digit run that is a
  plausible calendar **year** (~1900-2099) is excluded: two unrelated legs whose
  descriptions merely both end in `2024` do not share a genuine reference and must
  not pair on that alone.

The final score is clamped to 1.0.

### Confirming link signal (gate)

Amount + date + opposite-sign alone are **not** treated as proof that two legs are
the same transfer — any two coincidental equal-amount, opposite-signed, same-day
legs would satisfy them. A pair is rejected (score 0.0) unless at least one
positive **linking** signal ties those two specific legs together:

- a shared trailing reference number on both legs (year-like values excluded), or
- a recognised cross-institution credit-card payment relationship, or
- an account-type pattern of chequing ↔ credit card or savings ↔ chequing, or
- a shared **distinctive** description token present in both legs — a
  counterparty/reference-like word that survives stripping the generic
  transfer/payment vocabulary (`transfer`, `payment`, `online`, `interac`,
  `sent`, `received`, etc.). It must be at least three characters and non-numeric.

The weak case of "same institution, two different accounts, amount + sign only"
earns the +0.10 account-type bonus but is **not** a standalone linking signal, so
such a pair still needs one of the signals above to clear the confidence
threshold. Two legs on the same account at the same institution are never paired.

`amount_raw` is used intentionally. Transfers have `amount_signed = 0` because they do not affect cashflow, so matching on `amount_signed` would pair unrelated transfers.

## Stored Fields

Matched transfers are stored on the **`transfers`** table (and on the in-memory
`Transaction` the matcher builds per leg) with:

- `paired_transfer_id`
- `transfer_confidence`
- `counterparty_account`
- `counterparty_institution`

`transactions` carried identically-named columns until migration
`20260810_0023_drop_dead_transaction_columns`. The matcher never ran over that table,
so those unused columns were dropped. Transfer pairing is a `transfers` concept only.

The query service exposes:

- `get_transfer_matches()`
- `get_unmatched_transfers()`
- `get_transfer_stats()`

The dashboard shows these in the Transactions workflow page.

## Operational Notes

- Unmatched transfers are expected for external accounts, investments, or statement gaps.
- Manual overrides and multi-leg transfer support are not implemented.

Implementation: [transfer matcher](../../src/ledger/analysis/transfer_matcher.py);
regressions: [transfer matching tests](../../tests/test_transfer_matcher.py).
