# Exact arithmetic knowledge

This pack supplies domain interpretation, independent of worker responsibility. Routing, retrieval triggers and required starting sources have one canonical record in the catalog. Load the role contract separately; this file is not a procedure, worker or approval.

Transaction agent aggregates use arbitrary-precision integer cents. Their conversion uses a finite Decimal's integer ratio, rejects fractional cents and avoids ambient-context multiplication/negation. Other aggregate helpers still use cent-quantizing adapters; not every numeric column uses exact TEXT. `to_cents` converts None/empty input to 0.00 and uses the caller's Decimal context, so it must not represent unavailable printed evidence.

Investment fixed-point values use canonical exponent-free SQLite TEXT, scale at most 12 and absolute magnitude below 10^16. Invalid, nonfinite, coerced or out-of-contract values fail without rounding; null remains null. Investment quantities and FX rates do not share transaction-cent quantization. Values that happen to have equal numerical value can still violate a declared scale contract.

Currency/sign/account scope is explicit. The canonical transaction CAD policy includes legacy null/empty currency and excludes actual foreign currency. Income, expense and spending have different predicates; some treatment policies net income rows away. Totals and groups must describe the same selected amount universe. No native-currency combination or inferred conversion follows from a helper name.

Relevant failure modes are float aggregation/coercion, ambient Decimal context/traps, persistence affinity loss, sign changes before rejection, scale/magnitude boundary drift, differing total/group filters and unavailable-as-zero output. Independent literal or integer/rational expected results, currency exclusions and null cases distinguish these failures.

Maintained against the developer sources listed below. Current required-source hashes bind inspected bytes, not perpetual truth of this interpretation. Re-read the task-relevant sources. This pack establishes no current owner account, statement, financial, coverage or runtime fact.

Authoritative starting pointers: [exact persistence](../../../../src/ledger/storage/exact_decimal.py), [money helpers](../../../../src/ledger/utils/money.py), [finance predicates](../../../../src/ledger/finance_policy.py), [agent queries](../../../../src/ledger/query/agent.py), [investment queries](../../../../src/ledger/query/investments.py), [investment storage tests](../../../../tests/test_investment_storage.py), [aggregate precision tests](../../../../tests/test_aggregate_precision.py), [money invariant tests](../../../../tests/test_money_decimal_invariant.py), and [public Decimal context tests](../../../../tests/test_decimal_context_public.py).
