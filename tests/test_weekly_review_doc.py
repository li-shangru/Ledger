"""The weekly-review recipe may only describe surfaces that shipped.

`docs/agent/weekly-review.md` is a contract for an operator-side scheduler that
is deliberately out of scope: nothing in this repo runs it, so nothing in this
repo would notice it going stale. These checks are DERIVED from the published
schemas and the shipped tool descriptions rather than from a reading of the
document, so a cut tool, a renamed field, a required input the doc never names,
or a reworded loop breaks the doc that tells an agent to call it.

DERIVED **PER STEP**, NOT PER DOCUMENT. An audit that unions every field of every
tool and then asks "does the doc name something in that union" cannot see the
error it exists to catch: a field named under the WRONG tool. Every check below
that reads wire vocabulary reads it against the one tool whose section it appears
in.
"""

from __future__ import annotations

import re
from pathlib import Path

from ledger.mcp.contracts import tool_schemas
from ledger.mcp.write_contracts import write_tool_schemas

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs/agent/weekly-review.md"

# The recipe, in order (spec §16). Named here so the ORDER is asserted from a
# declaration rather than from whatever order the prose happens to use.
RECIPE = (
    "ledger.status",
    "ledger.changes",
    "ledger.brief",
    "ledger.coverage",
    "ledger.recurring",
    "ledger.balances",
    "ledger.projection",
    "ledger.cash_plan",
    "ledger.explain",
    "ledger.insights",
)

# THE GENERATION'S ROLE IS FROZEN, PARAGRAPH BY PARAGRAPH.
#
# P1 withdrew the generation-pairing loop (spec §8): the snapshot generation
# rotates on EVERY write, so a boundary paired with it mismatches precisely when
# something has arrived, and a tool implementing it answered `rebuilt` after
# every routine import and never reported one.
#
# The regression this guards is ADDITIVE -- a sentence that contradicts the
# correct ones, placed beside them, leaving every quoted-substring assertion
# green. A test that inspects one token in one sentence cannot see it: three
# different generation-gating rewordings passed the previous version of this
# check, including one that told the reader to report every ordinary import as a
# rebuild. So the two paragraphs that mention the generation at all are pinned
# VERBATIM and the doc may contain no third. A pronoun cannot smuggle a fourth
# sentence into a frozen paragraph, and a new paragraph naming the generation
# fails the count.
#
# Rewording either of these is meant to be a deliberate act that updates this
# test, because this test is the record of the correction.
STATUS_GENERATION_PARAGRAPH = (
    "Call it first, every run. It reports the available date range, the snapshot "
    "generation, the coverage statement, and `latest_imported_at`."
)
CHANGES_GENERATION_PARAGRAPH = (
    "`client_generation` is optional and informational: it never changes a count and "
    "never withholds one. Do not pair it with the boundary. It rotates on every write, "
    "so an ordinary import moves it exactly as much as a rebuild does, and a loop gated "
    "on it would fire on precisely the imports this step exists to report."
)


def _doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _published_tools() -> set[str]:
    return {str(tool["name"]) for tool in [*tool_schemas(), *write_tool_schemas()]}


def _tool(name: str) -> dict[str, object]:
    return next(tool for tool in [*tool_schemas(), *write_tool_schemas()] if tool["name"] == name)


def _description(name: str) -> str:
    return _flat(str(_tool(name)["description"]))


def _sections() -> dict[str, str]:
    """The doc split into one body per numbered step, keyed by that step's tool.

    A step's section runs from its own `## n. \\`ledger.x\\`` heading to the next
    heading, so a field named in step 4 is audited against `ledger.recurring` and
    against nothing else.
    """
    headings = list(re.finditer(r"^## \d+\. `(ledger\.[a-z_.]+)`", _doc(), flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(_doc())
        sections[heading.group(1)] = _doc()[heading.start() : end]
    return sections


def _published_vocabulary(schema: object) -> set[str]:
    """Every field name and every enum/const VALUE one tool's schemas publish.

    Both are quoted in the recipe and both can be renamed on the wire, so both
    are audited: `whole_database` is a value of `basis`, not a field, and a doc
    naming a verdict the schema no longer emits is as stale as one naming a
    field that no longer exists.
    """
    if isinstance(schema, dict):
        names = set(schema.get("properties", {}))
        for key in ("enum", "const"):
            value = schema.get(key)
            if isinstance(value, str):
                names.add(value)
            elif isinstance(value, list):
                names |= {item for item in value if isinstance(item, str)}
        for value in schema.values():
            names |= _published_vocabulary(value)
        return names
    if isinstance(schema, list):
        names: set[str] = set()
        for value in schema:
            names |= _published_vocabulary(value)
        return names
    return set()


def _vocabulary_of(name: str) -> set[str]:
    tool = _tool(name)
    return _published_vocabulary(tool.get("outputSchema")) | _published_vocabulary(
        tool.get("inputSchema")
    )


# Backticked lowercase tokens that are English rather than wire vocabulary. Held
# as an explicit, tiny list because the alternative -- only auditing names that
# carry an underscore -- silently exempted `basis`, `gaps`, `delta`, `entries`
# and `status`, five names this recipe instructs the reader to read.
PROSE_IN_BACKTICKS = {"since"}


def _named_fields(text: str) -> set[str]:
    """Backticked wire names in one section, with the tool names removed first.

    `ledger.status` splits into `ledger` and `status` otherwise, and `status` is
    a real field of four other tools -- so the tool name would audit as a field
    of itself and read as a defect that is not there.
    """
    stripped = re.sub(r"`ledger\.[a-z_.]+`", " ", text)
    return {
        part
        for token in re.findall(r"`([a-z][a-z_.]*)`", stripped)
        for part in token.split(".")
        if part not in PROSE_IN_BACKTICKS
    }


def test_the_recipe_names_only_tools_that_shipped() -> None:
    published = _published_tools()
    mentioned = set(re.findall(r"ledger\.[a-z_]+(?:\.[a-z_]+)*", _doc()))

    assert mentioned, "the recipe names no tools at all"
    assert mentioned <= published, sorted(mentioned - published)
    assert set(RECIPE) <= mentioned, sorted(set(RECIPE) - mentioned)


def test_the_recipe_runs_in_the_order_it_documents() -> None:
    """Read off the numbered STEP HEADINGS, not off first mention.

    First mention is not the order: the preamble names six tools while telling
    the reader which of them page.
    """
    assert tuple(_sections()) == RECIPE


def test_every_field_a_step_names_is_published_by_that_step_s_own_tool() -> None:
    """Attribution, not membership.

    The union of fourteen schemas contains almost every plausible field name, so
    an audit against the union passes when step 4 tells the reader to read
    `patterns_past_due` -- a `ledger.projection` field that `ledger.recurring`
    does not emit. Each section is checked against its own tool.
    """
    sections = _sections()

    assert set(sections) == set(RECIPE), sorted(set(RECIPE) ^ set(sections))
    for name, body in sections.items():
        # A section may borrow another tool's vocabulary ONLY where it points the
        # reader at that tool -- by naming it, or by naming its step. That is the
        # difference between "read step 6's `patterns_past_due`", which is true,
        # and "read this tool's `patterns_past_due`", which is not.
        own = _named_fields(body)
        published = _vocabulary_of(name)
        for other in _published_tools():
            cross_step = other in RECIPE and f"step {RECIPE.index(other) + 1}" in body
            if cross_step or (other != name and f"`{other}`" in body):
                published |= _vocabulary_of(other)
        assert own, f"{name}'s section names no wire fields at all"
        assert own <= published, (name, sorted(own - published))


def test_every_step_names_the_inputs_its_tool_requires() -> None:
    """A step that cannot be executed from the document is a dead step.

    `ledger.projection` requires `as_of` AND `target_date`; the error envelope is
    content-free by design, so a call missing either fails naming neither, and an
    implementer working from this document has nothing to recover from. Derived
    from `required`, so a new required input breaks the step that would omit it.

    Checked against the step's own "Requires ..." sentence, which is the
    convention this document keeps. A backticked `as_of` buried in a clause about
    how stale an anchor may be is a remark about the ANSWER, not an instruction
    to pass anything -- the shipped doc satisfied a mere-presence check that way
    while step 5 named no input to supply at all.
    """
    sections = _sections()
    required = {
        name: sorted((_tool(name).get("inputSchema") or {}).get("required") or [])  # type: ignore[union-attr]
        for name in RECIPE
    }

    assert any(required.values()), "no step requires anything; the audit is vacuous"
    for name, inputs in required.items():
        if not inputs:
            continue
        stated = " ".join(re.findall(r"Requires\b[^.]*\.", _flat(sections[name])))
        assert stated, f"{name} names no required inputs to supply: {inputs}"
        missing = [field for field in inputs if f"`{field}`" not in stated]
        assert not missing, (name, missing, stated)


def test_every_paginated_step_says_it_pages_and_none_is_left_out() -> None:
    """The paging disclosure must name every paginated step.

    Six of the ten tools declare a `page` block, and a scheduler that reads
    page one of any of them silently undercounts with no error to announce it.
    Derived from the declared blocks, so a tool that gains pagination breaks the
    paragraph that would have left it out.
    """
    paginated = [
        name
        for name in RECIPE
        if any(
            "page" in (branch.get("properties") or {})
            for branch in ((_tool(name).get("outputSchema") or {}).get("oneOf") or [])  # type: ignore[union-attr]
        )
    ]
    disclosure = next(
        _flat(block) for block in _doc().split("\n\n") if "`page` block" in _flat(block)
    )
    doc = _flat(_doc())

    assert paginated, "no tool declares a page block; the audit is vacuous"
    assert [name for name in RECIPE if name not in paginated] == [
        "ledger.status",
        "ledger.brief",
        "ledger.cash_plan",
        "ledger.explain",
    ]
    for name in paginated:
        assert f"`{name}`" in disclosure, name
    assert "Steps 1, 3, 8, and 9 answer in one call" in doc
    assert "Seven calls" not in doc
    # And the instruction that makes paging safe, not merely the fact of it.
    assert "Follow each step's cursor to completion" in doc
    assert "changing one invalidates the cursor" in doc
    # And the exception, because "every input" would be false: `limit` is
    # deliberately outside `_cursor_fingerprint` and a replay with a different
    # page size is accepted. `tests/test_docs_contract.py` measures both halves
    # against the wire.
    assert "`limit` is deliberately not bound" in doc


def test_the_changes_step_documents_the_loop_the_shipped_tool_describes() -> None:
    """P1 withdrew the generation-pairing loop; the doc must not resurrect it.

    Any write rotates the snapshot generation, so pairing a boundary with a
    generation fired on exactly the imports this surface exists to report. The
    shipped loop echoes `latest_imported_at` back, and these are that tool's own
    words rather than a paraphrase of them.
    """
    doc = _flat(_doc())
    changes = _description("ledger.changes")

    echo = "echo back the `latest_imported_at` this tool returns as the next call's `since`"
    informational = "`client_generation` is optional and informational"
    assert echo.replace("`", "") in changes.replace("`", "")
    assert informational.replace("`", "") in changes.replace("`", "")
    assert echo in doc
    assert informational in doc


def test_the_recipe_never_makes_the_generation_part_of_the_boundary() -> None:
    """Every paragraph that mentions the generation, pinned verbatim.

    The previous version of this check looked for the literal token "`since`"
    inside a sentence containing "generation", which no realistic wording of the
    defect satisfies: three separate generation-gating rewrites passed it,
    including "Persist the snapshot generation beside it, and when this run's
    generation differs from the one the previous run observed, report the counts
    as a rebuild rather than a delta" -- the exact loop P1 withdrew, and a direct
    contradiction of the shipped `ledger.changes` description.

    Pinning the paragraphs makes the regression unreachable rather than
    improbable: a contradicting sentence has to live somewhere, and every
    somewhere either mutates a frozen paragraph or adds a third.
    """
    carrying = [_flat(block) for block in _doc().split("\n\n") if "generation" in _flat(block)]

    assert carrying == [STATUS_GENERATION_PARAGRAPH, CHANGES_GENERATION_PARAGRAPH]


def test_the_recipe_states_that_the_scheduler_itself_is_out_of_scope() -> None:
    doc = _flat(_doc())

    assert "out of scope" in doc
    assert "inert" in doc
