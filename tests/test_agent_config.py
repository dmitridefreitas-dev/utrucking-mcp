"""The Retell agent's non-prompt configuration, checked offline.

agent/agent_config.json is the version-controlled half of the agent that isn't the prompt: the
turn-taking knobs, the tool timeouts, and which response fields reach the prompt at all. Three of
the defects Round 20 recorded as "known, not yet fixed" were in that half, and none of them were
visible to any test — they lived in a dashboard. These tests assert the manifest still says what
those fixes decided, and that the drift checker compares it correctly. No network, no API key.
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "agent" / "agent_config.json"

# the tools the LLM actually declares (agent/README.md); a typo'd name in the manifest would
# otherwise report as permanent drift that no --apply could ever fix
DECLARED_TOOLS = {"lookup_student", "get_order_details", "get_quote",
                  "check_availability", "transfer_to_office", "end_call"}


def _suite():
    spec = importlib.util.spec_from_file_location("retell_suite", ROOT / "tools" / "retell_suite.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def suite():
    return _suite()


@pytest.fixture(scope="module")
def cfg(suite):
    return suite.load_config(CONFIG)


# ── what the manifest must keep saying ──────────────────────────────────────
def test_manifest_parses_and_documents_only_real_tools(cfg):
    names = {k for k in cfg["tools"] if not k.startswith("_")}
    assert names and names <= DECLARED_TOOLS, "manifest names a tool the agent doesn't have"
    assert all(not k.startswith("_") for k in cfg)         # load_config strips the top-level prose


def test_the_prose_in_the_manifest_is_never_sent_to_retell(suite, cfg):
    """JSON has no comments, so every decision above is recorded in _why blocks that sit inside the
    same objects the applier PATCHes. They must be invisible to both halves of that path — or the
    checker reports permanent drift on a paragraph and --apply POSTs it to the agent."""
    assert "_why" in json.loads(CONFIG.read_text(encoding="utf-8"))["agent"]
    agent, llm = _live(tools=_full_tools(cfg))
    assert not any(f.startswith("_") for _s, f, _l, _w in suite.config_drift(cfg, agent, llm))
    live = [{"name": "get_order_details", "timeout_ms": 1, "response_variables": {}}]
    assert all(not k.startswith("_") for t in suite._merged_tools(cfg, {"general_tools": live})
               for k in t)


def test_backchannels_are_off_so_they_cannot_land_between_spelled_letters(cfg):
    """The v46 recovery path asks a caller to spell their last name, then their first and last.
    backchannel_frequency 0.8 fired "mm-hmm" into exactly those pauses. Off, not merely rarer:
    at any frequency above zero the ladder still breaks, just less often."""
    assert cfg["agent"]["enable_backchannel"] is False
    assert cfg["agent"]["backchannel_frequency"] == 0


def test_turn_taking_was_left_alone(cfg):
    """Speed came from voice_speed, deliberately not from tightening turn-taking — raising these
    is what makes the agent talk over a caller who is spelling."""
    assert cfg["agent"]["responsiveness"] == 0.7
    assert cfg["agent"]["interruption_sensitivity"] == 0.6
    assert cfg["agent"]["voice_speed"] == 1.15


def test_no_tool_timeout_can_race_the_silence_timer(cfg):
    """Every tool sat at 120000ms — the same value as end_call_after_silence_ms — so a hung tool
    and the silence timer expired together and which one won was a race, the losing outcome being
    a call that ends on a caller waiting for an answer. The invariant, not the number, is the fix."""
    silence = cfg["agent"]["end_call_after_silence_ms"]
    timeouts = [spec["timeout_ms"] for name, spec in cfg["tools"].items()
                if not name.startswith("_") and "timeout_ms" in spec]
    assert timeouts, "no tool timeouts are pinned at all"
    for t in timeouts:
        assert t < silence, "tool timeout %d races end_call_after_silence_ms %d" % (t, silence)


def test_the_prompt_can_see_every_field_it_branches_on(cfg):
    """v45/v46 branch on `reason`: 'unverified' → ask for a different detail, 'locked' → stop and
    transfer, 'error' → the only value meaning the records are really down. `reason` and
    `verify_with` were absent from get_order_details entirely, so nothing could bind them.

    The tool's whole JSON body reaches the LLM regardless of this field, so the prompt's branching
    does not stand on these bindings — they exist for {{template}} reuse later in the call. Pinned
    anyway: a mapping that names a field the server never returns is a slow lie in a config file."""
    details = cfg["tools"]["get_order_details"]["response_variables"]
    for field in ("reason", "verify_with", "verified", "locked", "status"):
        assert details.get(field) == field, "get_order_details drops %s" % field

    lookup = cfg["tools"]["lookup_student"]["response_variables"]
    for field in ("status", "confirmed_name", "verify_with", "identified_by"):
        assert lookup.get(field) == field, "lookup_student drops %s" % field


def test_response_paths_are_dot_notation_because_jsonpath_binds_nothing(cfg):
    """Retell: "Point each variable at a field in the response using dot notation, with array
    indexing where needed — for example `user.name` or `data.items[0].id`." JSONPath is not
    accepted. `$.reason` names a field `$` holding a field `reason`; no response has one, so the
    mapping binds nothing — and nothing raises, because an unresolved path just yields no variable
    and an unset dynamic variable renders as the literal text `{{reason}}`. Every mapping in this
    file was written that way, so all fourteen were dead and `--apply` would have said `applied`."""
    for name, spec in cfg["tools"].items():
        if name.startswith("_"):
            continue
        for var, path in (spec.get("response_variables") or {}).items():
            assert not path.startswith("$"), "%s.%s is JSONPath, which binds nothing" % (name, var)
            assert path and path == path.strip(), "%s.%s is not a usable path" % (name, var)


def test_tool_timeouts_sit_inside_the_range_retell_documents(cfg):
    """"The minimum value allowed is 1000 ms (1 s), and maximum value allowed is 600,000 ms." Out
    of range is an API rejection at --apply time, i.e. a config that half-applies: the agent PATCH
    lands and the LLM PATCH does not."""
    for name, spec in cfg["tools"].items():
        if not name.startswith("_") and "timeout_ms" in spec:
            assert 1000 <= spec["timeout_ms"] <= 600000, "%s.timeout_ms out of range" % name


# ── the drift checker ───────────────────────────────────────────────────────
def _live(agent=None, tools=None):
    return ({"voice_speed": 1.15, "responsiveness": 0.7, "interruption_sensitivity": 0.6,
             "enable_backchannel": False, "backchannel_frequency": 0,
             "end_call_after_silence_ms": 120000, **(agent or {})},
            {"general_tools": tools if tools is not None else []})


def _full_tools(cfg):
    """A live tool array that already matches the manifest exactly."""
    return [{"name": n, **{k: (dict(v) if isinstance(v, dict) else v)
                           for k, v in spec.items() if not k.startswith("_")}}
            for n, spec in cfg["tools"].items() if not n.startswith("_")]


def test_no_drift_when_the_agent_already_matches(suite, cfg):
    agent, llm = _live(tools=_full_tools(cfg))
    assert suite.config_drift(cfg, agent, llm) == []


def test_drift_names_the_setting_the_live_value_and_the_wanted_one(suite, cfg):
    agent, llm = _live({"backchannel_frequency": 0.8}, tools=_full_tools(cfg))
    drift = suite.config_drift(cfg, agent, llm)
    assert ("agent", "backchannel_frequency", 0.8, 0) in drift


def test_a_racing_timeout_is_caught(suite, cfg):
    tools = _full_tools(cfg)
    tools[0]["timeout_ms"] = 120000
    agent, llm = _live(tools=tools)
    drift = suite.config_drift(cfg, agent, llm)
    assert any(f == "timeout_ms" and live == 120000 for _, f, live, _w in drift)


def test_a_missing_response_variable_is_caught_by_name(suite, cfg):
    tools = _full_tools(cfg)
    for t in tools:
        if t["name"] == "get_order_details":
            t["response_variables"].pop("reason")
    agent, llm = _live(tools=tools)
    drift = suite.config_drift(cfg, agent, llm)
    assert ("tool:get_order_details", "response_variables.reason", "<absent>", "reason") in drift


def test_the_live_jsonpath_form_reports_as_drift_and_is_never_normalised(suite, cfg):
    """The live agent still holds the `$.` form this round replaced. `$.reason` and `reason` are
    not two spellings of one path — the first binds nothing — so a checker that folded them
    together would grade the broken agent as correct and there would be nothing left to apply."""
    tools = _full_tools(cfg)
    for t in tools:
        if t["name"] == "get_order_details":
            t["response_variables"]["reason"] = "$.reason"
    agent, llm = _live(tools=tools)
    assert ("tool:get_order_details", "response_variables.reason", "$.reason", "reason") \
        in suite.config_drift(cfg, agent, llm)


def test_extra_response_variables_are_not_drift(suite, cfg):
    """The manifest lists what the prompt branches on and nothing more. Reporting a key someone
    added for a reason this file doesn't know about trains whoever runs this to ignore it."""
    tools = _full_tools(cfg)
    for t in tools:
        if t["name"] == "get_order_details":
            t["response_variables"]["something_else"] = "something_else"
    agent, llm = _live(tools=tools)
    assert suite.config_drift(cfg, agent, llm) == []


def test_a_tool_missing_from_the_agent_is_reported_not_skipped(suite, cfg):
    agent, llm = _live(tools=[t for t in _full_tools(cfg) if t["name"] != "get_order_details"])
    drift = suite.config_drift(cfg, agent, llm)
    assert ("tool:get_order_details", "*", "<not on the agent>", "configured") in drift


# ── apply must not destroy what it doesn't know about ───────────────────────
def test_merge_keeps_tools_and_variables_the_manifest_says_nothing_about(suite, cfg):
    """Retell replaces the whole general_tools array on update, so an apply that sent only the
    manifest's tools would silently delete transfer_to_office and end_call from the agent."""
    live = _full_tools(cfg) + [{"name": "transfer_to_office", "timeout_ms": 120000},
                               {"name": "end_call"}]
    for t in live:
        if t["name"] == "get_order_details":
            t["response_variables"] = {"status": "$.status", "custom": "custom"}
            t["timeout_ms"] = 120000
    merged = {t["name"]: t for t in suite._merged_tools(cfg, {"general_tools": live})}

    assert set(merged) == {t["name"] for t in live}          # nothing dropped
    assert merged["transfer_to_office"]["timeout_ms"] == 120000   # untouched: not in the manifest
    got = merged["get_order_details"]
    assert got["timeout_ms"] == 30000                        # manifest wins on what it pins
    assert got["response_variables"]["reason"] == "reason"        # missing key added
    assert got["response_variables"]["status"] == "status"        # dead `$.` form overwritten
    assert got["response_variables"]["custom"] == "custom"        # unknown key preserved


def test_a_reintroduced_jsonpath_path_is_refused_before_it_can_be_applied(suite, cfg):
    """The tests above pin this manifest as committed. This pins the next edit to it: `--apply`
    PATCHes the live phone agent, and Retell answers 200 to a mapping that binds nothing, so the
    only place a `$.` can still be caught is before the request is built."""
    bad = json.loads(json.dumps(cfg))
    bad["tools"]["get_order_details"]["response_variables"]["reason"] = "$.reason"
    with pytest.raises(ValueError, match="not dot notation"):
        suite.validate_config(bad)


def test_a_timeout_that_races_the_silence_timer_is_refused_at_load(suite, cfg):
    """Same reasoning as the notation guard, for the other silent one. Retell accepts a tool
    timeout equal to end_call_after_silence_ms; the caller is the one who finds out."""
    bad = json.loads(json.dumps(cfg))
    bad["tools"]["get_quote"]["timeout_ms"] = bad["agent"]["end_call_after_silence_ms"]
    with pytest.raises(ValueError, match="races end_call_after_silence_ms"):
        suite.validate_config(bad)


def test_a_timeout_outside_retells_range_is_refused_at_load(suite, cfg):
    """Above 600000 the LLM PATCH is rejected — after the agent PATCH has already landed, which
    leaves the agent in a state that is neither the old config nor the manifest."""
    bad = json.loads(json.dumps(cfg))
    bad["tools"]["get_quote"]["timeout_ms"] = 900
    with pytest.raises(ValueError, match="outside Retell's allowed"):
        suite.validate_config(bad)


def test_the_committed_manifest_passes_its_own_validator(suite, cfg):
    assert suite.validate_config(cfg) is cfg


def test_merge_leaves_the_live_object_untouched(suite, cfg):
    """_merged_tools builds the payload; it must not mutate what was read back from the API, or a
    second call in the same process would compare the manifest against itself and report no drift."""
    live = [{"name": "get_order_details", "timeout_ms": 120000, "response_variables": {}}]
    suite._merged_tools(cfg, {"general_tools": live})
    assert live[0]["timeout_ms"] == 120000 and live[0]["response_variables"] == {}
