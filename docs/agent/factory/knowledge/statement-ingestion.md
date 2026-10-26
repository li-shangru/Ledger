# Statement ingestion knowledge

This pack supplies domain interpretation, independent of worker responsibility. Routing, retrieval triggers and required starting sources have one canonical record in the catalog. Load the role contract separately; this file is not a procedure, worker or approval.

Complete row parsing and canonical account/currency identity are separate requirements from balance equality. Dropped offsetting rows can leave a total unchanged while losing evidence. Strict identity collisions fail; non-strict collisions retain distinct content deterministically. Printed reconciled statement intervals establish source coverage; filenames and promotional dates do not.

Investment facts retain reported native currency and nulls. An import alone does not establish confirmed account lifecycle or live valuation. Any adapter-specific quantity/price rounding allowance must be explicit and cannot relax cash reconciliation. Private staging and unresolved review remain publication gates. A parser change touching finalize also has publication/recovery obligations.

Relevant failure modes include dropped malformed rows, offsetting loss hidden by matching totals, account/currency identity drift, duplicate collapse, noncanonical period inference, nulls replaced by zero, and applying an investment reconciliation tolerance to cash. The affected institution parser/extractor, pipeline identity and completeness/collision expectations describe different parts of the contract.

Maintained against the developer sources listed below. Current required-source hashes bind inspected bytes, not perpetual truth of this interpretation. Re-read task-relevant sources. This pack establishes no current owner account, statement, financial, coverage or runtime fact. Real statements are not developer fixtures; any missing format specification needs separately authorized sanitized evidence.

Authoritative starting pointers: [processing pipeline](../../../../src/ledger/processing/pipeline.py), [PDF workflow](../../../../src/ledger/imports/pdf/workflow.py), [adapter contract](../../../../src/ledger/adapters/contracts.py), [collision tests](../../../../tests/test_dedup_collision.py), [generic investment adapter tests](../../../../tests/test_generic_investment_adapter.py), and [printed coverage tests](../../../../tests/test_precise_statement_coverage.py).
