from datetime import date
from decimal import Decimal

from ledger.ui.components import (
    _status_strip_markup,
    bar_chart,
    confirmation_ready,
    format_currency,
    month_bounds,
    status_strip_items,
    truncate,
)
from ledger.ui.context import (
    _query_service_supports_period_quality,
    choose_default_page,
    clamp_period,
    format_period_label,
    get_period_dates,
    resolve_preset_period,
)
from ledger.ui.pages.accounts import _account_label, _logical_account_groups
from ledger.ui.pages.admin_data import _evidence_rows
from ledger.ui.pages.budgets_goals import (
    GOAL_COLUMNS,
    _apply_goal_editor_state,
    _goal_dataframe,
    _planning_empty_messages,
    _planning_save_preview,
    _planning_save_ready,
    _with_goal_account_balances,
)
from ledger.ui.pages.import_center import _readiness_gate_rows
from ledger.ui.pages.insights import PRIORITY_REVIEW_FINDING_TYPES, _findings_for_types
from ledger.ui.pages.overview import _evidence_status_items
from ledger.ui.pages.review_queue import (
    REVIEW_SAVE_CONFIRM,
    _review_action_ready,
    _review_decision_preview,
)
from ledger.ui.pages.rules_settings import _split_lines


def test_choose_default_page_prefers_review_when_pending():
    assert choose_default_page(0) == "overview"
    assert choose_default_page(3) == "review"


def test_priority_review_collects_every_material_finding_type() -> None:
    class FakeInsightEngine:
        def list_findings(
            self,
            _start: date,
            _end: date,
            *,
            finding_type: str,
            include_closed: bool,
            include_action_targets: bool = False,
        ) -> list[dict]:
            assert include_closed is False
            return [
                {
                    "id": PRIORITY_REVIEW_FINDING_TYPES.index(finding_type) + 1,
                    "type": finding_type,
                    "severity": "medium",
                    "action_priority": "normal",
                }
            ]

    findings = _findings_for_types(
        FakeInsightEngine(),  # type: ignore[arg-type]
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
        finding_types=PRIORITY_REVIEW_FINDING_TYPES,
        include_closed=False,
    )

    assert tuple(row["type"] for row in findings) == PRIORITY_REVIEW_FINDING_TYPES


def test_period_helpers_are_deterministic_for_reference_date():
    reference = date(2026, 5, 10)

    assert get_period_dates("this_month", reference) == (date(2026, 5, 1), reference)
    assert get_period_dates("last_month", reference) == (date(2026, 4, 1), date(2026, 4, 30))
    assert get_period_dates("last_30d", reference) == (date(2026, 4, 10), reference)


def test_current_period_anchors_to_latest_data_when_today_is_past_it():
    """Regression: when 'today' is past the newest transaction, This Month / This
    Quarter used to expand to the FULL data range (the clamp start>end fallback).
    They must instead resolve to the latest real month/quarter."""
    earliest, latest = date(2024, 12, 11), date(2026, 6, 22)
    today = date(2026, 7, 3)  # July: an empty gap after the data (Q3)

    assert resolve_preset_period("this_quarter", earliest, latest, today) == (
        date(2026, 4, 1),
        date(2026, 6, 22),
    )
    assert resolve_preset_period("this_month", earliest, latest, today) == (
        date(2026, 6, 1),
        date(2026, 6, 22),
    )
    # A preset that already lands inside the data is unchanged.
    assert resolve_preset_period("this_year", earliest, latest, today) == (
        date(2026, 1, 1),
        date(2026, 6, 22),
    )


def test_current_period_is_normal_when_today_within_data():
    earliest, latest = date(2024, 12, 11), date(2026, 6, 22)
    today = date(2026, 6, 15)  # within the data
    assert resolve_preset_period("this_month", earliest, latest, today) == (
        date(2026, 6, 1),
        date(2026, 6, 15),
    )


def test_clamp_period_uses_available_range_when_outside_bounds():
    assert clamp_period(
        date(2020, 1, 1),
        date(2030, 1, 1),
        date(2026, 1, 1),
        date(2026, 12, 31),
    ) == (date(2026, 1, 1), date(2026, 12, 31))


def test_format_helpers():
    assert format_period_label(date(2026, 1, 1), date(2026, 1, 31)) == "January 2026"
    assert format_period_label(date(2026, 1, 1), date(2026, 3, 31)) == "Jan - Mar 2026"
    assert format_currency(-1234.56) == "-$1,235"
    assert truncate("short", 10) == "short"
    assert truncate("0123456789", 6) == "012..."


def test_month_bounds_returns_start_and_next_month_start():
    assert month_bounds("2026-05") == (date(2026, 5, 1), date(2026, 6, 1))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))


def test_rules_settings_split_lines_splits_on_newlines_only():
    """Commas are regex syntax, not separators.

    This previously asserted that "TWO, THREE" split into two patterns, which is what
    shattered any pattern containing a bounded quantifier (\\d{3,4} -> "\\d{3" + "4}").
    Both fragments still compile, so nothing rejected them and two live merchant rules
    were silently corrupted on disk.
    """
    assert _split_lines("ONE\nTWO, THREE") == ["ONE", "TWO, THREE"]
    assert _split_lines(r"(?i)^CEDAR\s+\*{2,}") == [r"(?i)^CEDAR\s+\*{2,}"]
    assert _split_lines(None) == []


def test_bar_chart_omits_plotly_title_when_none():
    fig = bar_chart(["Food & Dining"], [100.0], orientation="h")
    serialized = fig.to_json()
    assert "undefined" not in serialized
    assert fig.layout.title.text is None
    assert fig.layout.margin.l >= 150


def test_status_strip_items_preserve_long_values():
    snapshot = {
        "period_start": date(2026, 5, 1),
        "period_end": date(2026, 5, 7),
        "created_at": date(2026, 5, 14),
    }

    items = status_strip_items(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 7),
        row_count=11,
        snapshot=snapshot,
        source="Snapshot + live queries",
    )

    assert items == [
        {"label": "Period", "value": "2026-05-01 to 2026-05-07", "detail": None},
        {"label": "Rows", "value": "11", "detail": None},
        {"label": "Snapshot", "value": "2026-05-01 to 2026-05-07", "detail": None},
        {"label": "Source", "value": "Snapshot + live queries", "detail": "2026-05-14"},
    ]


def test_status_strip_markup_emits_distinct_unindented_cells():
    items = status_strip_items(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 7),
        row_count=11,
        snapshot=None,
        source="Live queries",
    )

    markup = _status_strip_markup(items)

    assert markup.count('class="ledger-status-cell"') == 4
    assert "\n    <div" not in markup
    assert "<pre" not in markup


def test_forecast_bands_widen_with_volatility_not_avg_net():
    """Optimistic is always the better-off line for both signs of avg_net, the cone widens
    as sqrt(month) from the real net volatility, and it reflects volatility (not the mean),
    so a near-zero avg_net still has a non-zero band."""
    from ledger.ui.pages.insights import _forecast_series

    burning = _forecast_series(1000.0, -200.0, 500.0, date(2026, 1, 1), 4)
    assert [r["Month"] for r in burning] == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]
    for row in burning:
        assert row["Optimistic"] >= row["Base"] >= row["Pessimistic"]
    assert burning[0]["Base"] == 1000.0
    assert burning[-1]["Base"] < burning[0]["Base"]  # burning declines

    # band widens as sqrt(month): width at month 4 is 2x width at month 1
    w1 = burning[1]["Optimistic"] - burning[1]["Base"]
    w4 = burning[4]["Optimistic"] - burning[4]["Base"]
    assert abs(w4 - 2 * w1) < 1e-6

    # band is driven by volatility, not the mean: avg_net == 0 still has a non-zero band
    flat = _forecast_series(1000.0, 0.0, 500.0, date(2026, 1, 1), 2)
    assert flat[1]["Optimistic"] - flat[1]["Base"] == 500.0  # k=1 * std=500 * sqrt(1)


def test_forecast_current_balance_uses_only_explicit_absolute_rows():
    from types import SimpleNamespace

    from ledger.ui.pages.insights import _current_positive_balance

    calls = []

    class FakeService:
        def get_accounts(self, **kwargs):
            # No activity filter: is_active decays against wall-clock at every
            # import finalize, and money must not vanish from the forecast
            # because a different account's statement arrived first.
            assert kwargs == {}
            return [{"account_id": "cash"}, {"account_id": "relative-only"}]

        def get_latest_account_balance(self, account_id, *, end_date, balance_basis):
            calls.append((account_id, end_date, balance_basis))
            assert balance_basis == "absolute"
            if account_id == "cash":
                return {"balance": Decimal("750.00"), "balance_basis": "absolute"}
            return {"balance": Decimal("-100.00"), "balance_basis": "absolute"}

    ctx = SimpleNamespace(service=FakeService(), end_date=date(2026, 7, 1))

    assert _current_positive_balance(ctx) == Decimal("750.00")
    assert calls == [
        ("cash", date(2026, 7, 1), "absolute"),
        ("relative-only", date(2026, 7, 1), "absolute"),
    ]


def test_forecast_balance_is_unassessable_without_absolute_rows():
    from types import SimpleNamespace

    from ledger.ui.pages.insights import _current_positive_balance

    class FakeService:
        def get_accounts(self, **kwargs):
            return [{"account_id": "relative-only"}]

        def get_latest_account_balance(self, account_id, *, end_date, balance_basis):
            assert balance_basis == "absolute"
            return None

    ctx = SimpleNamespace(service=FakeService(), end_date=date(2026, 7, 1))

    assert _current_positive_balance(ctx) is None


def test_forecast_balance_skips_unanchored_accounts_without_vetoing_the_rest():
    """An account with no statement anchor contributes no level — it is unknown,
    not zero — and it must not veto the accounts that do anchor. The old
    behavior returned None whenever ANY account lacked an absolute row, so one
    relative-only account blanked the whole forecast."""
    from types import SimpleNamespace

    from ledger.ui.pages.insights import _current_positive_balance

    class FakeService:
        def get_accounts(self, **kwargs):
            return [{"account_id": "cash"}, {"account_id": "relative-only"}]

        def get_latest_account_balance(self, account_id, *, end_date, balance_basis):
            if account_id == "cash":
                return {"balance": Decimal("750.00"), "balance_basis": "absolute"}
            return None

    ctx = SimpleNamespace(service=FakeService(), end_date=date(2026, 7, 1))

    assert _current_positive_balance(ctx) == Decimal("750.00")


def test_accounts_relative_history_is_never_labeled_as_balance():
    from ledger.ui.pages import accounts

    helper = getattr(accounts, "_account_balance_history", None)
    assert callable(helper)

    calls = []

    class FakeService:
        def get_account_balances(
            self,
            *,
            account_id,
            start_date=None,
            end_date=None,
            balance_basis,
        ):
            calls.append((account_id, balance_basis))
            if balance_basis == "absolute":
                return []
            return [
                {
                    "account_id": account_id,
                    "balance_date": date(2026, 7, 1),
                    "balance": Decimal("25.00"),
                    "balance_7d_avg": Decimal("20.00"),
                    "balance_30d_avg": Decimal("15.00"),
                    "balance_basis": "relative",
                }
            ]

    basis, rows = helper(
        FakeService(),
        ["acct-relative"],
        end_date=date(2026, 7, 1),
    )
    chart = accounts._balance_chart(rows, balance_basis=basis)
    presentation = accounts._balance_presentation(basis)

    assert basis == "relative"
    assert calls == [("acct-relative", "absolute"), ("acct-relative", "relative")]
    assert all("balance" not in str(trace.name).lower() for trace in chart.data)
    assert all("balance" not in str(value).lower() for value in presentation.values())


def test_accounts_mixed_history_prefers_absolute_basis_even_when_relative_is_newer():
    from ledger.ui.pages import accounts

    helper = getattr(accounts, "_account_balance_history", None)
    assert callable(helper)

    class FakeService:
        def get_account_balances(
            self,
            *,
            account_id,
            start_date=None,
            end_date=None,
            balance_basis,
        ):
            if balance_basis == "absolute":
                return [
                    {
                        "account_id": account_id,
                        "balance_date": date(2026, 6, 30),
                        "balance": Decimal("500.00"),
                        "balance_basis": "absolute",
                    }
                ]
            return [
                {
                    "account_id": account_id,
                    "balance_date": date(2026, 7, 1),
                    "balance": Decimal("25.00"),
                    "balance_basis": "relative",
                }
            ]

    basis, rows = helper(
        FakeService(),
        ["acct-mixed"],
        end_date=date(2026, 7, 1),
    )

    assert basis == "absolute"
    assert [row["balance"] for row in rows] == [Decimal("500.00")]


def test_accounts_headline_relative_change_is_explicitly_labeled():
    from ledger.ui.pages import accounts

    metric = accounts._period_end_balance_kpi(
        "relative",
        {
            "balance": Decimal("25.00"),
            "balance_date": date(2026, 7, 1),
            "balance_basis": "relative",
        },
        date(2026, 7, 31),
    )

    assert metric == ("Period-End Relative Change", "$25", "as of 2026-07-01")


def test_accounts_headline_balance_uses_the_absolute_statement_anchor():
    from ledger.ui.pages import accounts

    metric = accounts._period_end_balance_kpi(
        "absolute",
        {
            "balance": Decimal("500.00"),
            "balance_date": date(2026, 7, 1),
            "balance_basis": "absolute",
        },
        date(2026, 7, 31),
    )

    assert metric == ("Period-End Balance", "$500", "as of 2026-07-01")


def test_confirmation_ready_requires_exact_phrase():
    state = {"confirm_cleanup": "delete local cleanup targets"}

    assert confirmation_ready(state, "confirm_cleanup", "delete local cleanup targets")
    assert not confirmation_ready(state, "confirm_cleanup", "DELETE LOCAL CLEANUP TARGETS")
    assert not confirmation_ready({}, "confirm_cleanup", "delete local cleanup targets")


def test_query_service_cache_compatibility_detects_old_duplicate_signature():
    class OldService:
        def get_duplicate_candidates(self, threshold_days=2):
            return []

    class NewService:
        def get_duplicate_candidates(self, threshold_days=2, start_date=None, end_date=None):
            return []

    assert not _query_service_supports_period_quality(OldService())
    assert _query_service_supports_period_quality(NewService())


def test_logical_account_groups_do_not_guess_that_distinct_slugs_are_one_account():
    accounts = [
        {
            "account_id": "old",
            "institution": "harbor",
            "account_number": "harbor-chequing",
            "account_type": "checking",
            "account_subtype": "Chequing",
            "first_transaction_date": date(2025, 1, 2),
            "last_transaction_date": date(2025, 12, 31),
            "transaction_count": 208,
            "avg_monthly_transactions": Decimal("17.3"),
            "is_active": False,
        },
        {
            "account_id": "new",
            "institution": "harbor",
            "account_number": "2026-04-23-harbor-deposit-chequing-1001",
            "account_type": "checking",
            "account_subtype": "Chequing",
            "first_transaction_date": date(2026, 3, 25),
            "last_transaction_date": date(2026, 4, 23),
            "transaction_count": 15,
            "avg_monthly_transactions": Decimal("7.5"),
            "is_active": True,
        },
    ]

    logical_accounts = _logical_account_groups(accounts)

    assert len(logical_accounts) == 2
    assert {tuple(account["account_numbers"]) for account in logical_accounts} == {
        ("harbor-chequing",),
        ("2026-04-23-harbor-deposit-chequing-1001",),
    }
    assert sum(account["transaction_count"] for account in logical_accounts) == 223


def test_account_label_marks_inactive_single_accounts():
    account = {
        "account_id": "orbit",
        "institution": "harbor",
        "account_number": "harbor-orbit-visa-1003",
        "account_type": "credit_card",
        "account_subtype": "Harbor Orbit Visa Infinite",
        "is_active": False,
    }

    assert _account_label(account) == "harbor - Harbor Orbit Visa Infinite (...1003) - inactive"


def test_account_export_family_uses_inert_declared_product_tokens(monkeypatch):
    from ledger.adapters import registry
    from ledger.ui.pages.accounts import _account_family_token

    monkeypatch.setattr(
        registry,
        "compatibility_policy",
        lambda: {"account_product_tokens": [("---", "unused"), ("orbit", "rewards")]},
    )
    monkeypatch.setattr(
        registry,
        "load_adapters",
        lambda: (_ for _ in ()).throw(AssertionError("must not execute adapters")),
    )
    account = {"account_number": "example-orbit", "account_type": "credit_card"}
    assert _account_family_token(account) == "rewards"
    assert _account_family_token({"account_type": "checking"}) == "chequing"
    assert _account_family_token({"account_subtype": "General credit"}) == "generalcredit"


def test_account_period_kpis_are_scoped_to_linked_account_identities():
    from ledger.ui.pages import accounts

    captured = {}
    expected = {
        "income": Decimal("125.00"),
        "expenses": Decimal("75.00"),
        "net": Decimal("50.00"),
        "activity_count": 6001,
    }

    class FakeService:
        def compute_kpis(self, start_date, end_date, **filters):
            captured.update(start_date=start_date, end_date=end_date, **filters)
            return expected

    helper = getattr(accounts, "_account_period_kpis", None)
    assert helper is not None
    actual = helper(
        FakeService(),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        institution="harbor",
        account_numbers=["old-id", "new-id"],
    )

    assert actual == expected
    assert captured == {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
        "institutions": ["harbor"],
        "accounts": ["old-id", "new-id"],
    }


def test_goal_editor_rows_use_selected_account_latest_balance():
    default_account = "harbor - Chequing (...1001)"
    selected_account = "harbor - Savings (...1002)"
    balances = {
        default_account: {"current_amount": 125.0, "current_balance_date": date(2026, 5, 1)},
        selected_account: {"current_amount": 3400.0, "current_balance_date": date(2026, 5, 7)},
    }

    df = _goal_dataframe([], {}, default_account)
    df = _apply_goal_editor_state(
        df,
        {
            "edited_rows": {
                0: {
                    "account": selected_account,
                    "name": "Savings restore",
                    "target_amount": 5000.0,
                }
            }
        },
    )
    df = _with_goal_account_balances(df, balances)

    assert list(df.columns) == GOAL_COLUMNS
    assert df.loc[0, "account"] == selected_account
    assert df.loc[0, "current_amount"] == 3400.0
    assert df.loc[0, "current_balance_date"] == date(2026, 5, 7)
    assert GOAL_COLUMNS.index("current_amount") < GOAL_COLUMNS.index("target_amount")


def test_goal_progress_is_unassessable_without_absolute_account_balance():
    from ledger.ui.pages import budgets_goals

    helper = getattr(budgets_goals, "_goal_progress_summary", None)
    assert callable(helper)

    summary = helper(
        [
            {
                "target_amount": Decimal("1000.00"),
                "current_amount": None,
            }
        ]
    )

    assert summary == {
        "target": Decimal("1000.00"),
        "current": None,
        "remaining": None,
        "progress_pct": None,
    }


def test_overview_goal_remaining_copy_handles_unassessable_progress():
    from ledger.ui.pages import overview

    helper = getattr(overview, "_goal_remaining_detail", None)
    assert callable(helper)
    assert helper(None) == "Needs absolute value"
    assert helper(Decimal("500.00")) == "$500"


def test_planning_empty_messages_are_code_only_guidance():
    messages = _planning_empty_messages(
        {
            "budgets": {"active": 0},
            "goals": {"active": 0},
        }
    )

    assert "No budgets are configured yet." in messages
    assert "No savings goals are configured yet." in messages


def test_review_decision_preview_counts_changes_and_incomplete_rows():
    original = [
        {
            "review_key": "missing-coffee",
            "status": "pending",
            "merchant": "",
            "category": "Other",
            "scope": "same_raw_merchant_in_run",
            "note": "",
            "count": 2,
        },
        {
            "review_key": "known-grocery",
            "status": "pending",
            "merchant": "MARKET",
            "category": "Groceries",
            "scope": "same_raw_merchant_in_run",
            "note": "",
            "count": 3,
        },
    ]
    edited = [
        {
            "review_key": "missing-coffee",
            "status": "resolved",
            "merchant": "COFFEE BAR",
            "category": "",
            "scope": "same_raw_merchant_in_run",
            "note": "needs category",
            "count": 2,
        },
        {
            "review_key": "known-grocery",
            "status": "reviewed_as_is",
            "merchant": "MARKET",
            "category": "Groceries",
            "scope": "same_raw_merchant_in_run",
            "note": "",
            "count": 3,
        },
    ]

    preview = _review_decision_preview(original, edited)

    assert preview["rows"] == 2
    assert preview["changed"] == 2
    assert preview["resolved"] == 1
    assert preview["reviewed_as_is"] == 1
    assert preview["incomplete"] == 1
    assert preview["affected_transactions"] == 3
    assert preview["errors"] == ["1 resolved row needs merchant and category before save."]


def test_review_action_ready_requires_exact_confirmation_and_no_errors():
    preview = {
        "changed": 1,
        "errors": [],
    }

    assert _review_action_ready(
        {"review_confirm_save": REVIEW_SAVE_CONFIRM},
        "review_confirm_save",
        REVIEW_SAVE_CONFIRM,
        preview,
    )
    assert not _review_action_ready(
        {"review_confirm_save": REVIEW_SAVE_CONFIRM.upper()},
        "review_confirm_save",
        REVIEW_SAVE_CONFIRM,
        preview,
    )
    assert not _review_action_ready(
        {"review_confirm_save": REVIEW_SAVE_CONFIRM},
        "review_confirm_save",
        REVIEW_SAVE_CONFIRM,
        {**preview, "errors": ["missing category"]},
    )


def test_import_center_readiness_rows_show_blockers_and_ready_state():
    stale = {
        "gates": {
            "target_db_unchanged": "fail",
            "staging_db": "pass",
            "csv_root": "pass",
            "statement_validation": "pass",
            "review": "pass",
        },
        "blockers": ["Target database changed since stage; cancel and restage this import."],
    }
    ready = {
        "gates": {
            "target_db_unchanged": "pass",
            "staging_db": "pass",
            "csv_root": "pass",
            "statement_validation": "pass",
            "review": "pass",
        },
        "blockers": [],
    }

    stale_rows = _readiness_gate_rows(stale)
    ready_rows = _readiness_gate_rows(ready)

    target_row = next(row for row in stale_rows if row["Gate"] == "Target DB unchanged")
    assert target_row["Status"] == "fail"
    assert any("cancel and restage" in row["Detail"] for row in stale_rows)
    assert all(row["Status"] == "pass" for row in ready_rows)


def test_admin_evidence_rows_are_plain_render_data():
    evidence = {
        "audit": {
            "status": "pass",
            "detail": "2 PDFs, 10 rows, 0 mismatches",
        },
        "imports": {"status": "pass", "detail": "Latest import 2026-05-16"},
        "review": {"status": "warn", "detail": "2 pending review rows"},
        "quality": {"status": "unknown", "detail": "No data quality evidence found."},
        "snapshots": {"status": "pass", "detail": "Latest snapshot 2026-05-16"},
        "backups": {"status": "pass", "detail": "Latest backup 2026-05-16"},
        "database": {"status": "pass", "detail": "Schema valid"},
    }

    rows = _evidence_rows(evidence)

    assert rows[0] == {
        "Signal": "Statement Audit",
        "Status": "pass",
        "Detail": "2 PDFs, 10 rows, 0 mismatches",
    }
    assert rows[2]["Signal"] == "Review Readiness"
    assert rows[2]["Status"] == "warn"


def test_overview_evidence_status_items_handle_unknown_audit():
    evidence = {
        "audit": {"status": "unknown", "detail": "No statement audit evidence found."},
        "review": {"status": "pass", "detail": "No pending review rows"},
        "quality": {"status": "pass", "detail": "Latest checks pass"},
        "snapshots": {"status": "warn", "detail": "Latest snapshot is stale"},
    }

    items = _evidence_status_items(evidence)

    assert items == [
        {
            "label": "Audit",
            "value": "unknown",
            "detail": "No statement audit evidence found.",
        },
        {"label": "Review", "value": "pass", "detail": "No pending review rows"},
        {"label": "Quality", "value": "pass", "detail": "Latest checks pass"},
        {"label": "Snapshot", "value": "warn", "detail": "Latest snapshot is stale"},
    ]


def test_planning_save_preview_allows_first_row_without_confirmation():
    preview = _planning_save_preview(
        original_rows=[],
        edited_rows=[{"month": "*", "category": "Groceries", "amount": 500.0}],
        kind="budget",
    )

    assert preview == {
        "rows": 1,
        "creates": 1,
        "deletes": 0,
        "requires_confirmation": False,
        "errors": [],
    }
    assert _planning_save_ready({}, "budget_confirm", "save budget replacements", preview)


def test_no_malformed_percent_format_in_ui_pages():
    """Guard: no UI file may use the invalid printf pattern '%.0%'.

    Streamlit NumberColumn format strings are printf patterns applied to the raw
    cell value.  '%.0%' is not a valid conversion — the correct form for an
    integer percentage is '%.0f%%' (fed a 0..100 value).

    We scan for the bare substring '%.0%' so that either quote style
    (format="%.0%" or format='%.0%') is caught.
    """
    import pathlib

    ui_dir = pathlib.Path(__file__).parent.parent / "src" / "ledger" / "ui"
    bad_substring = "%.0%"
    offenders = []
    for py_file in sorted(ui_dir.rglob("*.py")):
        text = py_file.read_text()
        if bad_substring in text:
            offenders.append(str(py_file.relative_to(ui_dir.parent.parent.parent)))
    assert offenders == [], f"Files containing malformed {bad_substring!r}: {offenders}"


def test_planning_save_preview_requires_confirmation_for_replacements_and_deletes():
    preview = _planning_save_preview(
        original_rows=[
            {"month": "*", "category": "Groceries", "amount": 500.0},
            {"month": "*", "category": "Dining", "amount": 200.0},
        ],
        edited_rows=[{"month": "*", "category": "Groceries", "amount": 450.0}],
        kind="budget",
    )

    assert preview["rows"] == 1
    assert preview["creates"] == 0
    assert preview["deletes"] == 1
    assert preview["requires_confirmation"]
    assert not _planning_save_ready({}, "budget_confirm", "save budget replacements", preview)
    assert _planning_save_ready(
        {"budget_confirm": "save budget replacements"},
        "budget_confirm",
        "save budget replacements",
        preview,
    )


def test_spending_by_dow_uses_canonical_spend_universe():
    """The Categories day-of-week chart must match the True Cost universe: expense,
    include_in_cashflow, NOT exclude_from_spending, CAD-only. Otherwise it disagrees
    with the true_cost totals rendered on the same page."""
    from types import SimpleNamespace

    from ledger.ui.pages.categories import _spending_by_dow

    monday = date(2026, 1, 5)  # a Monday

    def txn(
        amount,
        *,
        cashflow_type="expense",
        include=True,
        exclude=False,
        currency="CAD",
    ):
        return SimpleNamespace(
            amount_signed=Decimal(amount),
            cashflow_type=cashflow_type,
            include_in_cashflow=include,
            exclude_from_spending=exclude,
            currency=currency,
            posting_date=monday,
        )

    rows = [
        txn("-60.00"),  # counted
        txn("-40.00", exclude=True),  # excluded from spending
        txn("-25.00", currency="USD"),  # non-CAD, un-FX-converted
        txn("-15.00", include=False),  # not in cashflow
        txn("100.00", cashflow_type="income"),  # income
    ]
    df = _spending_by_dow(rows)
    monday_amount = df.loc[df["Day"] == "Monday", "Amount"].iloc[0]
    assert monday_amount == 60.00


def test_truncation_notice():
    from ledger.ui.components import truncation_notice

    # Below the cap -> no notice.
    assert truncation_notice(4999, 5000) is None
    assert truncation_notice(0, 5000) is None
    # At/over the cap -> a caption naming the window (KPIs/CSV are clipped).
    assert truncation_notice(5000, 5000) is not None
    assert "5,000" in truncation_notice(5000, 5000)
    assert "KPIs and CSV export" in truncation_notice(5000, 5000)
    assert "table and CSV export" in truncation_notice(5000, 5000, kpis_limited=False)
    assert truncation_notice(50000, 50000) is not None


def test_transactions_period_kpis_sum_cad_only_and_report_non_cad():
    """The Transactions headline Income/Expenses/Net must sum CAD rows only.

    A non-CAD row stored in its native currency must not
    be added at par to the CAD totals -- otherwise the headline Net silently
    disagrees with the CAD-only Overview/Insights KPIs for the same window. The
    excluded non-CAD rows are reported so the raw table and KPI row reconcile.
    """
    from types import SimpleNamespace

    from ledger.ui.pages.transactions import _period_kpis

    def txn(amount, *, currency="CAD"):
        return SimpleNamespace(amount_signed=Decimal(amount), currency=currency)

    rows = [
        txn("100.00"),  # CAD income
        txn("-40.00"),  # CAD expense
        txn("50.00", currency="USD"),  # non-CAD income -> excluded
        txn("-30.00", currency="USD"),  # non-CAD expense -> excluded
    ]

    income, expenses, net, non_cad_count = _period_kpis(rows)

    assert income == Decimal("100.00")
    assert expenses == Decimal("40.00")
    assert net == Decimal("60.00")
    assert non_cad_count == 2


def test_transactions_period_kpis_keep_excluded_rows_currency_only():
    """The raw explorer intentionally lists exclude_from_spending rows, so the KPI
    sums filter on CURRENCY only -- a CAD exclude_from_spending expense still
    counts toward the headline Expenses (unlike the spend-universe category page)."""
    from types import SimpleNamespace

    from ledger.ui.pages.transactions import _period_kpis

    rows = [
        SimpleNamespace(
            amount_signed=Decimal("-25.00"), currency="CAD", exclude_from_spending=True
        ),
    ]

    income, expenses, _net, non_cad_count = _period_kpis(rows)

    assert income == Decimal("0")
    assert expenses == Decimal("25.00")
    assert non_cad_count == 0


def test_category_spend_universe_expenses_reconcile_with_true_cost():
    """The Categories breakdown/Avg/Top-Merchants denominator must be the spend
    universe, matching the True Cost KPI numerator. A non-CAD or
    exclude_from_spending expense miscategorized into the selected category
    contributes $0 to True Cost, so it must not appear in Top Merchants nor
    inflate the Avg denominator."""
    from types import SimpleNamespace

    from ledger.ui.pages.categories import _spend_universe_expenses

    def txn(amount, *, include=True, exclude=False, currency="CAD", cashflow="expense"):
        return SimpleNamespace(
            amount_signed=Decimal(amount),
            include_in_cashflow=include,
            exclude_from_spending=exclude,
            currency=currency,
            cashflow_type=cashflow,
        )

    rows = [
        txn("-60.00"),  # kept
        txn("-40.00", exclude=True),  # excluded from spending
        txn("-25.00", currency="USD"),  # non-CAD, un-FX-converted
        txn("-15.00", include=False),  # not in cashflow
        txn("100.00", cashflow="income"),  # income (positive)
    ]

    kept = _spend_universe_expenses(rows)

    assert len(kept) == 1
    assert kept[0].amount_signed == Decimal("-60.00")


def test_merchant_drilldown_reconciles_with_canonical_spend_universe(monkeypatch):
    from types import SimpleNamespace

    import ledger.ui.pages.categories as page

    def txn(
        amount,
        category,
        *,
        include=True,
        exclude=False,
        currency="CAD",
    ):
        return SimpleNamespace(
            amount_signed=Decimal(amount),
            cashflow_type="expense",
            include_in_cashflow=include,
            exclude_from_spending=exclude,
            currency=currency,
            category=category,
        )

    rows = [
        txn("-10.00", "Dining"),
        txn("-100.00", "Travel", currency="USD"),
        txn("-50.00", "Fees", exclude=True),
        txn("-25.00", "Misc", include=False),
    ]

    class Service:
        def get_spending_by_merchant(self, *_args, **_kwargs):
            return {"MERCHANT": Decimal("10.00")}

        def get_merchant_transactions(self, *_args, **_kwargs):
            return rows

        def get_merchant_spending_trend(self, *_args, **_kwargs):
            return []

    class Column:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Streamlit:
        def __init__(self):
            self.figures = []
            self.session_state = {}

        def selectbox(self, _label, *, options, **_kwargs):
            return options[0]

        def columns(self, count, **_kwargs):
            return [Column() for _ in range(count)]

        def subheader(self, *_args, **_kwargs):
            return None

        def info(self, *_args, **_kwargs):
            return None

        def plotly_chart(self, figure, **_kwargs):
            self.figures.append(figure)

    fake_st = Streamlit()
    captured_kpis = []
    monkeypatch.setattr(page, "st", fake_st)
    monkeypatch.setattr(page, "kpi_row", lambda rows: captured_kpis.extend(rows))
    monkeypatch.setattr(page, "render_transaction_table", lambda *_args, **_kwargs: None)

    page._render_merchant(
        SimpleNamespace(
            service=Service(),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
    )

    assert captured_kpis[:4] == [
        ("Spending", "$10", None, "neg"),
        ("Visits", "1", None),
        ("Avg Visit", "$10.00", None),
        ("Categories", "1", None),
    ]
    pie = fake_st.figures[0].data[0]
    assert list(pie.labels) == ["Dining"]
    assert list(pie.values) == [10.0]


# ---------------------------------------------------------------------------
# Stale-health-score notice
#
# The gap between score_date and the newest transaction has two causes, and the
# banner used to name only one: it always advised `ledger analysis refresh`. When
# the real cause is sparse recent data, that refresh recomputes every score and
# still cannot move score_date, so the user follows the instruction, sees nothing
# change, and has no way to learn why.
# ---------------------------------------------------------------------------


def test_no_staleness_notice_when_the_score_keeps_up_with_the_data():
    from ledger.ui.pages.overview import _health_staleness_notice

    assert (
        _health_staleness_notice(
            score_date=date(2046, 2, 28),
            data_through=date(2046, 2, 28),
            active_days=28,
        )
        is None
    )


def test_staleness_notice_advises_refresh_when_the_tail_could_be_scored():
    """Dense recent data ⇒ the scores really are un-recomputed. Keep the old advice."""
    from ledger.ui.pages.overview import _health_staleness_notice

    notice = _health_staleness_notice(
        score_date=date(2046, 2, 18),
        data_through=date(2046, 2, 28),
        active_days=26,
    )

    assert notice is not None
    assert "ledger analysis refresh" in notice
    assert "2046-02-18" in notice and "2046-02-28" in notice


def test_staleness_notice_explains_sparsity_instead_of_advising_a_futile_refresh():
    """Synthetic sparse activity cannot meet the scoring gate through refresh alone."""
    from ledger.ui.pages.overview import _health_staleness_notice

    notice = _health_staleness_notice(
        score_date=date(2046, 2, 18),
        data_through=date(2046, 2, 28),
        active_days=7,
    )

    assert notice is not None
    # The futile instruction must NOT be the advice given.
    assert "Run `ledger analysis refresh`" not in notice
    # Both sides of the gate are stated, so the state explains itself.
    assert "7" in notice and "20" in notice and "30" in notice
    # And it points at the fix that does work.
    assert "import" in notice.lower()


def test_staleness_notice_falls_back_to_refresh_advice_when_activity_is_unknown():
    """A failed/absent day count must not silently claim sparsity."""
    from ledger.ui.pages.overview import _health_staleness_notice

    notice = _health_staleness_notice(
        score_date=date(2046, 2, 18),
        data_through=date(2046, 2, 28),
        active_days=None,
    )

    assert notice is not None
    assert "ledger analysis refresh" in notice


def test_staleness_notice_is_silent_when_either_date_is_missing():
    from ledger.ui.pages.overview import _health_staleness_notice

    assert (
        _health_staleness_notice(score_date=None, data_through=date(2046, 2, 28), active_days=3)
        is None
    )
    assert (
        _health_staleness_notice(score_date=date(2046, 2, 18), data_through=None, active_days=3)
        is None
    )
