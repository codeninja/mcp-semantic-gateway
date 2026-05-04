"""Tests for Phase I -- :mod:`mcp_semantic_gateway.ingestion.skill_validator`.

Each negative test mutates a single field on a known-good baseline so the
specific failure under test is the only thing that can trip the report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from mcp_semantic_gateway.ingestion.skill_validator import (
    SkillPackage,
    SkillReference,
    ValidationFailure,
    ValidationReport,
    validate_skill,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


HARVESTED_TOOLS: set[str] = {
    "github_search_issues",
    "github_add_labels",
    "github_create_comment",
    "github_close_issue",
}


VALID_DESCRIPTION = (
    "Search, label, comment, and close GitHub issues based on a triage rule "
    "set covering both stale items and newly opened reports."
)


VALID_BODY = """## When to use this skill

Use this skill when an agent needs to triage a backlog of GitHub issues:
identify candidates, apply labels, post a triage comment, and optionally
close stale items.

## Procedure

1. Identify candidate issues using `github_search_issues` with the relevant
   filters for the repository under triage.
2. Apply the appropriate triage labels via `github_add_labels`.
3. Post a triage comment using `github_create_comment` describing the
   reason for the labelling decision.
4. Close stale issues using `github_close_issue` when the staleness
   threshold has been exceeded.
"""


def make_valid_package(**overrides: Any) -> SkillPackage:
    """Return a baseline package that passes every validator pass.

    Keyword overrides replace fields on the baseline so each negative
    test isolates the single mutation it cares about.
    """

    fields: dict[str, Any] = {
        "skill_id": "github-issue-triage",
        "name": "github-issue-triage",
        "description": VALID_DESCRIPTION,
        "body_markdown": VALID_BODY,
        "tool_dependencies": [
            "github_search_issues",
            "github_add_labels",
            "github_create_comment",
            "github_close_issue",
        ],
        "references": [],
        "server_id": "github-api",
        "source_hash": "sha256:abc",
        "cluster_id": "cluster-github-api-3",
        "cluster_hash": "sha256:def",
        "use_case_ids": ["uc-github-api-issues-0-1"],
        "generated_by": "claude-sonnet-4-6@v1",
        "generated_at": datetime(2026, 5, 4, 10, 24, 11, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return SkillPackage(**fields)


def reasons(report: ValidationReport) -> list[str]:
    return [f.reason for f in report.failures]


def by_reason(report: ValidationReport, reason: str) -> ValidationFailure:
    matches = [f for f in report.failures if f.reason == reason]
    assert matches, f"no failure with reason={reason!r} (got {reasons(report)!r})"
    assert len(matches) == 1, f"expected one failure with reason={reason!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. All passes succeed
# ---------------------------------------------------------------------------


def test_valid_package_passes_all_three_passes() -> None:
    package = make_valid_package()

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is True
    assert report.failures == []
    assert set(report.passes) == {
        "spec-conformance",
        "tool-grounding",
        "length-bounds",
    }
    assert len(report.passes) == 3


def test_valid_package_accepts_list_for_harvested_tools() -> None:
    """The API contract permits ``set[str] | list[str]``."""

    package = make_valid_package()

    report = validate_skill(package, list(HARVESTED_TOOLS))

    assert report.passed is True


# ---------------------------------------------------------------------------
# 2. spec-conformance failures
# ---------------------------------------------------------------------------


def test_invalid_name_format_fails_spec_conformance() -> None:
    package = make_valid_package(name="Bad Name 1")

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "name_format")
    assert failure.pass_name == "spec-conformance"
    assert failure.details == {"name": "Bad Name 1"}
    assert "spec-conformance" not in report.passes
    # Other passes still report success.
    assert "tool-grounding" in report.passes
    assert "length-bounds" in report.passes


def test_description_too_short_fails_spec_conformance() -> None:
    package = make_valid_package(description="too short.")  # 10 chars

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "description_length_out_of_bounds")
    assert failure.pass_name == "spec-conformance"
    assert failure.details["length"] == 10
    assert failure.details["min"] == 50
    assert failure.details["max"] == 600


def test_description_too_long_fails_spec_conformance() -> None:
    package = make_valid_package(description="x" * 700)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "description_length_out_of_bounds")
    assert failure.details["length"] == 700


# ---------------------------------------------------------------------------
# 3. length-bounds failures
# ---------------------------------------------------------------------------


def test_body_too_short_fails_length_bounds() -> None:
    # 50 chars: under the 100 lower bound. Includes the declared tool
    # name so tool-grounding is unaffected.
    short_body = "Use `github_search_issues` to triage. Done sample."
    assert len(short_body) == 50
    package = make_valid_package(body_markdown=short_body)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "body_length_out_of_bounds")
    assert failure.pass_name == "length-bounds"
    assert failure.details["length"] == 50
    assert failure.details["min"] == 100
    assert failure.details["max"] == 8000


def test_body_too_long_fails_length_bounds() -> None:
    # Pad the valid body with grounded backticked tool references so
    # only length-bounds trips.
    padding = " `github_search_issues`" * 400
    long_body = VALID_BODY + padding
    assert len(long_body) > 8000
    package = make_valid_package(body_markdown=long_body)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "body_length_out_of_bounds")
    assert failure.details["length"] == len(long_body)


# ---------------------------------------------------------------------------
# 4. tool-grounding failures
# ---------------------------------------------------------------------------


def test_empty_tool_dependencies_fails_tool_grounding() -> None:
    package = make_valid_package(tool_dependencies=[])

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "empty_tool_dependencies")
    assert failure.pass_name == "tool-grounding"


def test_hallucinated_tool_in_dependencies_fails_tool_grounding() -> None:
    package = make_valid_package(
        tool_dependencies=[
            "github_search_issues",
            "github_add_labels",
            "github_create_comment",
            "github_close_issue",
            "github_summon_unicorn",  # not harvested
        ]
    )

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "tool_name_unresolved")
    assert failure.pass_name == "tool-grounding"
    assert failure.details["unresolved"] == ["github_summon_unicorn"]


def test_hallucinated_tool_in_body_backticks_fails_tool_grounding() -> None:
    body = VALID_BODY + "\n\nAlso consider `github_invent_tool` as a fallback.\n"
    package = make_valid_package(body_markdown=body)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "tool_name_unresolved")
    assert failure.details["unresolved"] == ["github_invent_tool"]


# ---------------------------------------------------------------------------
# 5. reference length bounds
# ---------------------------------------------------------------------------


def test_reference_too_long_fails_length_bounds() -> None:
    oversized = SkillReference(
        filename="overview.md", content="x" * 13000
    )
    package = make_valid_package(references=[oversized])

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure = by_reason(report, "reference_length_out_of_bounds")
    assert failure.pass_name == "length-bounds"
    assert failure.details["length"] == 13000
    assert failure.details["filename"] == "overview.md"


# ---------------------------------------------------------------------------
# 6. Aggregation: multiple failures accrue
# ---------------------------------------------------------------------------


def test_multiple_failures_accrue_in_single_report() -> None:
    """Two failures in the same pass dedupe correctly in ``passes``."""

    package = make_valid_package(
        name="Bad Name 1",          # name_format
        description="too short.",   # description_length_out_of_bounds
    )

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    assert len(report.failures) == 2
    assert set(reasons(report)) == {
        "name_format",
        "description_length_out_of_bounds",
    }
    assert all(f.pass_name == "spec-conformance" for f in report.failures)
    # ``passes`` excludes the failed pass exactly once but keeps the
    # others.
    assert "spec-conformance" not in report.passes
    assert set(report.passes) == {"tool-grounding", "length-bounds"}


def test_failures_across_multiple_passes_accrue() -> None:
    package = make_valid_package(
        description="too short.",                  # spec-conformance
        body_markdown="short body, only 47 chars long! `oops`",  # length + grounding
        tool_dependencies=[],                      # tool-grounding
    )

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is False
    failure_passes = {f.pass_name for f in report.failures}
    assert failure_passes == {
        "spec-conformance",
        "tool-grounding",
        "length-bounds",
    }
    assert report.passes == []


# ---------------------------------------------------------------------------
# 7. Report.passed reflects failures
# ---------------------------------------------------------------------------


def test_report_passed_true_when_no_failures() -> None:
    report = validate_skill(make_valid_package(), HARVESTED_TOOLS)
    assert report.passed is True
    assert report.failures == []


def test_report_passed_false_when_any_failure() -> None:
    report = validate_skill(
        make_valid_package(name="Bad Name 1"), HARVESTED_TOOLS
    )
    assert report.passed is False
    assert len(report.failures) >= 1


# ---------------------------------------------------------------------------
# 8. Backtick filter -- short identifiers are not flagged
# ---------------------------------------------------------------------------


def test_short_backticked_identifiers_are_ignored() -> None:
    """Identifiers under 3 chars are too generic to flag (e.g. ``id``)."""

    body = VALID_BODY + "\n\nWhen referencing `id` or `it`, prefer the long form.\n"
    package = make_valid_package(body_markdown=body)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert report.passed is True


@pytest.mark.parametrize(
    "bad_name",
    [
        "Bad Name 1",   # spaces + uppercase
        "BadName",      # uppercase
        "1bad",         # leading digit
        "-bad",         # leading hyphen
        "a",            # too short (the regex requires >= 2 chars)
        "x" * 65,       # too long
        "with_under",   # underscores not allowed
    ],
)
def test_name_format_rejects_invalid_names(bad_name: str) -> None:
    package = make_valid_package(name=bad_name)

    report = validate_skill(package, HARVESTED_TOOLS)

    assert any(f.reason == "name_format" for f in report.failures), (
        f"expected name_format failure for {bad_name!r}"
    )
