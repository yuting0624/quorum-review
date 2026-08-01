"""Where the code goes, and saying so in the run.

"Does our source leave region X" is a question somebody has to answer in
writing before this can be installed anywhere regulated, and the honest answer
for the default configuration is "we don't control it": `global` routes to
whichever Vertex region has capacity. That is the right default — it is the
endpoint most likely to have the model available — but it is not a residency
guarantee, and until now it was not configurable through the action at all.
Only Claude's region was an input; Gemini's was an environment variable nobody
documented.
"""

from __future__ import annotations

import pytest

from quorum_review import report
from quorum_review.providers.base import ProviderUnavailable
from quorum_review.providers.vertex import (
    VertexProvider,
    claude_region,
    gemini_location,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (
        "QUORUM_VERTEX_REGION",
        "QUORUM_CLAUDE_REGION",
        "QUORUM_GEMINI_LOCATION",
        "CLAUDE_VERTEX_REGION",
        "GOOGLE_CLOUD_LOCATION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")


# -- resolving ---------------------------------------------------------------


def test_the_default_is_global_for_both():
    assert gemini_location() == "global"
    assert claude_region() == "global"


def test_one_setting_pins_both(monkeypatch):
    """What an organisation with a residency requirement actually wants: one
    value, applied everywhere, rather than remembering there are two."""
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")

    assert gemini_location() == "europe-west4"
    assert claude_region() == "europe-west4"


def test_a_per_model_setting_wins(monkeypatch):
    """Model Garden entitlements can be region-scoped, so Claude sometimes has
    to sit somewhere Gemini does not. The per-model *input* is what overrides
    the pin; the vendor variable sits below it, so that a region inherited from
    a runner image cannot quietly undo a pin written in the workflow."""
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")
    monkeypatch.setenv("QUORUM_CLAUDE_REGION", "us-east5")

    assert gemini_location() == "europe-west4"
    assert claude_region() == "us-east5"


def test_an_empty_value_is_not_a_setting(monkeypatch):
    """Actions passes "" for an input the caller left out, so an unset input
    must not read as a region named empty string."""
    monkeypatch.setenv("CLAUDE_VERTEX_REGION", "")
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")

    assert claude_region() == "europe-west4"


def test_whitespace_is_not_a_setting(monkeypatch):
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "   ")
    assert claude_region() == "global"


# -- refusing a value that is not a region ----------------------------------


@pytest.mark.parametrize(
    "bad", ["europe west4", "https://europe-west4-aiplatform.googleapis.com", "EU", "."]
)
def test_a_value_that_is_not_a_region_is_refused_up_front(monkeypatch, bad: str):
    """Otherwise it becomes a hostname and the failure arrives as a connection
    error, which reads like the network rather than the configuration."""
    monkeypatch.setenv("QUORUM_VERTEX_REGION", bad)

    with pytest.raises(ProviderUnavailable, match="not a Vertex region"):
        VertexProvider()


@pytest.mark.parametrize(
    "good", ["global", "us-east5", "europe-west4", "asia-northeast1"]
)
def test_a_real_region_is_accepted(monkeypatch, good: str):
    monkeypatch.setenv("QUORUM_VERTEX_REGION", good)
    assert VertexProvider()._claude_region == good


# -- and saying so ----------------------------------------------------------


def test_only_models_that_ran_are_named(monkeypatch):
    """Naming a region for a model that was configured and never reached is a
    claim about traffic that did not happen."""
    provider = VertexProvider()
    assert provider.regions == {}


def test_the_summary_states_where_each_model_ran():
    run = report.RunReport(models=["gemini-3.6-flash", "claude-opus-5"])
    run.regions = {"gemini-3.6-flash": "europe-west4", "claude-opus-5": "us-east5"}
    rendered = report.render(run)

    assert "`gemini-3.6-flash` in `europe-west4`" in rendered
    assert "`claude-opus-5` in `us-east5`" in rendered


def test_global_is_named_as_global_rather_than_dressed_up():
    """It routes to whichever region has capacity. Calling that a location
    would be the misleading part."""
    run = report.RunReport(models=["claude-opus-5"])
    run.regions = {"claude-opus-5": "global"}

    assert "`claude-opus-5` in `global`" in report.render(run)


def test_a_run_with_no_regions_recorded_still_renders():
    """`direct` mode has no Vertex region, and the footer is not optional."""
    run = report.RunReport(models=["claude-opus-5"])
    assert "quorum-review" in report.render(run)


# -- not shadowing what the caller set --------------------------------------


def test_an_unset_input_does_not_shadow_the_vendor_variable(monkeypatch):
    """The action passes its inputs under QUORUM_ names. An unset input arrives
    as "", and writing GOOGLE_CLOUD_LOCATION="" into the step environment would
    have overruled a value the caller set at the job level — the action
    silently discarding the configuration it was given."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("QUORUM_GEMINI_LOCATION", "")

    assert gemini_location() == "europe-west4"


def test_the_same_for_claude(monkeypatch):
    monkeypatch.setenv("CLAUDE_VERTEX_REGION", "us-east5")
    monkeypatch.setenv("QUORUM_CLAUDE_REGION", "")

    assert claude_region() == "us-east5"


def test_the_input_wins_when_it_is_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    monkeypatch.setenv("QUORUM_GEMINI_LOCATION", "asia-northeast1")

    assert gemini_location() == "asia-northeast1"


def test_a_pin_beats_a_variable_that_was_merely_inherited(monkeypatch):
    """An ambient GOOGLE_CLOUD_LOCATION — a runner image, an org-level `env:`,
    a devcontainer — used to sit above the shared pin, so it silently defeated
    a `vertex-region` written into the workflow. A residency pin someone wrote
    down has to beat something they inherited."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")

    assert gemini_location() == "europe-west4"


def test_the_vendor_variable_still_works_on_its_own(monkeypatch):
    """Below the pin, not gone. Someone running the module directly with
    GOOGLE_CLOUD_LOCATION set and no action in sight should still get it."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    assert gemini_location() == "us-central1"


def test_a_per_model_input_still_beats_the_pin(monkeypatch):
    """Region-scoped Model Garden entitlements are why the override exists."""
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")
    monkeypatch.setenv("QUORUM_CLAUDE_REGION", "us-east5")

    assert claude_region() == "us-east5"


# -- multi-region locations -------------------------------------------------


@pytest.mark.parametrize("multi", ["us", "eu"])
def test_a_multi_region_is_a_location(monkeypatch, multi: str):
    """Vertex serves `us` and `eu` as multi-regions. The first version of the
    check rejected both, which would have refused a valid configuration at
    startup."""
    monkeypatch.setenv("QUORUM_VERTEX_REGION", multi)
    assert VertexProvider()._claude_region == multi


# -- only traffic that actually happened ------------------------------------


def test_a_model_whose_calls_all_failed_is_not_listed(monkeypatch):
    """The engine is built before the first request and survives every one of
    them failing. Listing it would put a region in the audit line for traffic
    that never arrived."""
    provider = VertexProvider()
    provider._engine("claude-opus-5")

    assert provider.regions == {}


def test_a_model_that_completed_a_call_is_listed(monkeypatch):
    monkeypatch.setenv("QUORUM_VERTEX_REGION", "europe-west4")
    provider = VertexProvider()
    provider._engine("claude-opus-5").usage.calls = 1

    assert provider.regions == {"claude-opus-5": "europe-west4"}
