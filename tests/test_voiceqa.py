"""Offline tests for the Voice QA loop (webhook -> judge -> scoreboard -> staff API),
the business_insights MCP tool, and the /mcp staff-key middleware. Fictional data only."""
import asyncio
import json
import types
import pytest
import main


@pytest.fixture(autouse=True)
def _clean_qa(monkeypatch):
    main._QA_CALLS.clear()
    monkeypatch.setattr(main, "API_SECRET", "")
    yield
    main._QA_CALLS.clear()


class Req:
    """Minimal stand-in for a Starlette request (query_params/headers are dicts)."""
    def __init__(self, body=None, query=None, headers=None):
        self._b = body or {}
        self.query_params = query or {}
        self.headers = headers or {}
        self.client = type("C", (), {"host": "9.9.9.9"})()

    async def json(self):
        return self._b


def _call(call_id="call_a1", transcript="Agent: Hello!\nUser: What are your hours?"):
    return {
        "call_id": call_id,
        "start_timestamp": 1751980000000,
        "duration_ms": 65000,
        "transcript": transcript,
        "call_analysis": {"user_sentiment": "Positive", "call_successful": True},
        "latency": {"e2e": {"p50": 820}},
        "call_cost": {"combined_cost": 12.5},
        "disconnection_reason": "user_hangup",
    }


_JUDGE_JSON = json.dumps({
    "score": 92, "identity_gate_held": True, "over_promised": False,
    "wrong_info": False, "caller_frustrated": False, "issues": [],
    "one_line": "Clean informational call."})


def _stub_judge_model(monkeypatch, reply=_JUDGE_JSON):
    calls = {"n": 0}
    async def fake_gen(key, parts, temp=None, json_out=False):
        calls["n"] += 1
        return reply
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    return calls


# ---------- judge ----------
def test_judge_parses_model_json(monkeypatch):
    _stub_judge_model(monkeypatch)
    j = asyncio.run(main._judge_transcript("Agent: hi"))
    assert j["score"] == 92 and j["identity_gate_held"] is True and j["issues"] == []


def test_judge_none_without_key_or_transcript(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert asyncio.run(main._judge_transcript("Agent: hi")) is None
    _stub_judge_model(monkeypatch)
    assert asyncio.run(main._judge_transcript("   ")) is None


def test_judge_survives_garbage_model_reply(monkeypatch):
    _stub_judge_model(monkeypatch, reply="I refuse to answer in JSON")
    assert asyncio.run(main._judge_transcript("Agent: hi")) is None


# ---------- webhook ----------
def test_webhook_ingests_and_judges_once(monkeypatch):
    calls = _stub_judge_model(monkeypatch)
    r = asyncio.run(main.retell_webhook(Req({"event": "call_ended", "call": _call()})))
    assert r[1][0] == {"ok": True}
    rec = main._QA_CALLS["call_a1"]
    assert rec["judge"]["score"] == 92 and rec["sentiment"] == "Positive"
    # the follow-up call_analyzed event must NOT trigger a second model call
    asyncio.run(main.retell_webhook(Req({"event": "call_analyzed", "call": _call()})))
    assert calls["n"] == 1


def test_webhook_ignores_other_events_and_bad_bodies(monkeypatch):
    _stub_judge_model(monkeypatch)
    asyncio.run(main.retell_webhook(Req({"event": "call_started", "call": _call("c2")})))
    assert "c2" not in main._QA_CALLS
    r = asyncio.run(main.retell_webhook(Req(body=None)))
    assert r[1][0] == {"ok": True}                       # never errors back at Retell


def test_webhook_key_gate(monkeypatch):
    _stub_judge_model(monkeypatch)
    monkeypatch.setattr(main, "API_SECRET", "sek")
    r = asyncio.run(main.retell_webhook(Req({"event": "call_ended", "call": _call("c3")})))
    assert r[1][0]["status"] == "unauthorized" and "c3" not in main._QA_CALLS
    r = asyncio.run(main.retell_webhook(
        Req({"event": "call_ended", "call": _call("c3")}, query={"key": "sek"})))
    assert r[1][0] == {"ok": True} and "c3" in main._QA_CALLS


def test_qa_store_bounded(monkeypatch):
    monkeypatch.setattr(main, "_QA_MAX", 5)
    for i in range(9):
        main._qa_store("id%d" % i, {"call_id": "id%d" % i, "ts": i})
    assert len(main._QA_CALLS) == 5
    assert "id8" in main._QA_CALLS and "id0" not in main._QA_CALLS   # oldest evicted


# ---------- staff QA API ----------
def test_voice_qa_api_requires_staff_key_when_gate_active(monkeypatch):
    monkeypatch.setattr(main, "API_SECRET", "sek")
    r = asyncio.run(main.voice_qa_api(Req()))
    assert r[1][0]["status"] == "unauthorized"
    r = asyncio.run(main.voice_qa_api(Req(headers={"x-utrucking-key": "sek"})))
    assert "summary" in r[1][0]


def test_voice_qa_api_unconfigured_serves_webhook_fallback(monkeypatch):
    monkeypatch.delenv("RETELL_API_KEY", raising=False)
    _stub_judge_model(monkeypatch)
    asyncio.run(main.retell_webhook(Req({"event": "call_analyzed", "call": _call("c9")})))
    body = asyncio.run(main.voice_qa_api(Req()))[1][0]
    assert body["configured"] is False
    assert body["summary"]["calls"] == 1 and body["summary"]["avg_score"] == 92.0
    assert body["calls"][0]["call_id"] == "c9"
    assert "transcript" not in body["calls"][0]           # transcripts never leave the server


def test_voice_qa_api_merges_retell_history_and_judges_on_demand(monkeypatch):
    monkeypatch.setenv("RETELL_API_KEY", "stub")
    _stub_judge_model(monkeypatch)
    async def fake_api(path, body=None, method="POST"):
        assert path == "/v3/list-calls"
        return {"items": [_call("h1"), _call("h2", transcript="")]}
    monkeypatch.setattr(main, "_retell_api", fake_api)
    body = asyncio.run(main.voice_qa_api(Req(query={"judge": "1"})))[1][0]
    assert body["configured"] is True and body["summary"]["calls"] == 2
    assert body["summary"]["judged"] == 1                 # only h1 had a transcript to score
    assert body["summary"]["gate_held_pct"] == 100
    assert body["summary"]["total_cost_usd"] == 0.25      # cents -> dollars
    assert body["summary"]["median_latency_p50_ms"] == 820


# ---------- aggregate insights MCP tool ----------
def test_business_insights_is_aggregate_only(monkeypatch):
    D = [{"Student": "Jamie Rivers", "ID": "#90001-TS", "Service": "Summer Storage",
          "Building": "Northgate B", "Room": "12", "Date": "5/6/2026",
          "Phone": "5550100200", "Status": "Complete"}]
    S = [{"Student": "Jamie Rivers", "Date": "5/6/2026",
          "Summer Storage Item List": "UTrucking Box (Amount: 22.00 USD, Quantity: 4) Total: $88"}]
    async def fake_load():
        return D, S
    monkeypatch.setattr(main, "_load_rows", fake_load)
    out = asyncio.run(main.business_insights())
    assert "Revenue" in out
    assert "Jamie" not in out and "Rivers" not in out and "5550100200" not in out


# ---------- /mcp staff-key middleware ----------
def _run_mw(monkeypatch, secret, path, headers):
    monkeypatch.setattr(main, "API_SECRET", secret)
    hit = {"n": 0}
    async def downstream(scope, receive, send):
        hit["n"] += 1
    sent = []
    async def send(msg):
        sent.append(msg)
    mw = main._McpAuthMiddleware(downstream)
    scope = {"type": "http", "path": path,
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(mw(scope, None, send))
    return hit["n"], sent


def test_mcp_middleware_open_when_gate_dormant(monkeypatch):
    hit, sent = _run_mw(monkeypatch, "", "/mcp", {})
    assert hit == 1 and not sent


def test_mcp_middleware_blocks_without_key(monkeypatch):
    hit, sent = _run_mw(monkeypatch, "sek", "/mcp", {})
    assert hit == 0 and sent[0]["status"] == 401


@pytest.mark.parametrize("headers", [{"x-utrucking-key": "sek"},
                                     {"authorization": "Bearer sek"},
                                     {"Authorization": "Bearer sek"}])
def test_mcp_middleware_accepts_either_header(monkeypatch, headers):
    hit, sent = _run_mw(monkeypatch, "sek", "/mcp", headers)
    assert hit == 1 and not sent


def test_mcp_middleware_leaves_other_paths_alone(monkeypatch):
    hit, sent = _run_mw(monkeypatch, "sek", "/quote", {})
    assert hit == 1 and not sent
