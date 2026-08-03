"""Read-only inspection of the live Retell workspace, on the versioned endpoints.

Every question this answers — which agent version is the number *actually*
serving, what LLM drafts exist, did that batch test bill any calls — used to be
answered by a throwaway script written fresh each session, and those scripts
reached for the pre-v2 list routes (list-agents, list-phone-numbers,
list-retell-llms, and v2 list-calls). All four are deprecated; Retell mails a
removal notice naming exactly that usage. None of them was ever in the service
itself, which is why grepping main.py for them finds nothing — the traffic came
from operator scripts that lived and died in a scratchpad. This file is the
durable replacement: one place where the paths are correct and reviewable, and
`tests/test_retell_endpoints.py` fails the build if a legacy one comes back.

Read-only by construction. Nothing here creates, updates, publishes or deletes.
The two write steps that change what a caller hears — publishing a version and
re-pointing the number (see agent/README.md) — stay deliberately manual.

Usage (RETELL_API_KEY in the environment):
    python tools/retell_ops.py numbers            # what each number routes to
    python tools/retell_ops.py agents             # voice agents in the workspace
    python tools/retell_ops.py llms               # LLM versions + published flags
    python tools/retell_ops.py calls --days 7     # recent call metadata
    python tools/retell_ops.py live               # number -> agent version -> LLM
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.retellai.com"

# The migrated list routes, in one place so the next deprecation is a one-line
# edit and the guard test can assert the whole set at once. The comment on each
# line is the route it replaced.
LIST_AGENTS = "/v2/list-agents"            # POST, was GET list-agents
LIST_NUMBERS = "/v2/list-phone-numbers"    # GET,  was GET list-phone-numbers
LIST_LLMS = "/v2/list-retell-llms"         # GET,  was GET list-retell-llms
LIST_CALLS = "/v3/list-calls"              # POST, was POST v2 list-calls


# ── tiny API client (stdlib only, same shape as retell_suite.py) ─────────────
def _api(path, body=None, method=None):
    key = os.environ.get("RETELL_API_KEY", "")
    if not key:
        sys.exit("RETELL_API_KEY is not set")
    req = urllib.request.Request(
        API + path, method=method or ("POST" if body is not None else "GET"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def _paginate(path, method="GET", body=None, limit=100, cap=1000):
    """Walk a versioned list endpoint to exhaustion.

    All four routes now return the same envelope — `items`, `pagination_key`,
    `has_more` — which is the substance of the migration, not the path rewrite:
    the legacy routes returned a bare top-level array with no consistent cursor,
    so callers either invented their own paging or silently took the first page
    and treated it as the whole workspace.
    """
    out, key = [], None
    while True:
        if method == "GET":
            query = {"limit": limit}
            if key:
                query["pagination_key"] = key
            page = _api(path + "?" + urllib.parse.urlencode(query))
        else:
            sent = dict(body or {})
            sent["limit"] = limit
            if key:
                sent["pagination_key"] = key
            page = _api(path, sent, method="POST")
        page = page or {}
        out.extend(page.get("items") or [])
        key = page.get("pagination_key")
        if not page.get("has_more") or not key or len(out) >= cap:
            return out[:cap]


def _redact_url(url):
    """The inbound webhook pins `?key=<API_SECRET>` in cleartext (agent/README.md,
    "Known, not yet fixed"), and that secret also gates the PII endpoints — so the
    query string never reaches stdout, even on an operator's own terminal."""
    if not url:
        return "-"
    head, sep, _ = url.partition("?")
    return head + ("?<redacted>" if sep else "")


def _routes(entries):
    """Render an inbound_agents/outbound_agents list as `agent_id vNN`.

    The version matters more than the id: Retell does not re-route on publish,
    so a number pinning an explicit `agent_version` keeps serving that version
    until the number itself is updated. This line is how you tell what callers
    are hearing, as opposed to what is merely published.
    """
    out = []
    for e in entries or []:
        ver = e.get("agent_version")
        out.append("%s%s" % (e.get("agent_id", "?"), "" if ver is None else " v%s" % ver))
    return ", ".join(out) or "-"


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_numbers():
    nums = _paginate(LIST_NUMBERS)
    print("%d phone number(s)" % len(nums))
    for n in nums:
        print("\n  %s  %s" % (n.get("phone_number_pretty") or n.get("phone_number") or "?",
                              n.get("nickname") or ""))
        print("    inbound  : %s" % _routes(n.get("inbound_agents")))
        print("    outbound : %s" % _routes(n.get("outbound_agents")))
        print("    webhook  : %s" % _redact_url(n.get("inbound_webhook_url")))
    return nums


def cmd_agents(channel="voice"):
    body = {"sort_order": "descending"}
    if channel != "all":
        # The one migration step that is not a path rewrite: the versioned route
        # returns voice and chat agents in a single list, so the voice-only shape
        # the legacy route gave for free now has to be asked for.
        body["filter_criteria"] = {"channel": {"type": "string", "op": "eq", "value": channel}}
    agents = _paginate(LIST_AGENTS, method="POST", body=body)
    print("%d %s agent(s)" % (len(agents), channel))
    for a in agents:
        print("  %-32s %-10s %s" % (a.get("agent_id", "?"), a.get("channel", "?"),
                                    a.get("agent_name") or ""))
    return agents


def cmd_llms():
    llms = _paginate(LIST_LLMS)
    print("%d LLM response engine(s)" % len(llms))
    for l in llms:
        # Length only. The prompt is the product and it is version-controlled in
        # agent/ — dumping it here would just invite an unreviewed copy.
        print("  %-32s v%-4s %-10s published=%-5s prompt=%d bytes" % (
            l.get("llm_id", "?"), l.get("version", "?"), l.get("model") or "?",
            l.get("is_published"), len(l.get("general_prompt") or "")))
    return llms


def cmd_calls(days=7, limit=100):
    since_ms = int((time.time() - days * 86400) * 1000)
    calls = _paginate(
        LIST_CALLS, method="POST", limit=min(limit, 100), cap=limit,
        body={"filter_criteria": {"start_timestamp": {"op": "gt", "type": "number",
                                                      "value": since_ms}},
              "sort_order": "descending"})
    print("%d call(s) in the last %d day(s)" % (len(calls), days))
    for c in calls:
        # Metadata only: transcripts carry customer PII and belong on /voiceqa,
        # behind the staff gate, not in a terminal buffer.
        an = c.get("call_analysis") or {}
        print("  %-36s %6ss  %-9s %-8s %s" % (
            c.get("call_id", "?"), round((c.get("duration_ms") or 0) / 1000.0),
            an.get("user_sentiment") or "-", c.get("call_status") or "-",
            c.get("disconnection_reason") or ""))
    return calls


def cmd_live():
    """number -> pinned agent version -> the LLM version that agent runs.

    Rebuilding this chain by hand, every session, is what kept producing the
    legacy-route scripts in the first place.
    """
    for n in cmd_numbers():
        for entry in (n.get("inbound_agents") or []):
            aid, ver = entry.get("agent_id"), entry.get("agent_version")
            if not aid:
                continue
            path = "/get-agent/" + aid + ("?version=%s" % ver if ver is not None else "")
            agent = _api(path) or {}
            engine = agent.get("response_engine") or {}
            lid, lver = engine.get("llm_id"), engine.get("version")
            print("\n  live chain: %s v%s -> llm %s v%s" % (aid, ver, lid, lver))
            if lid:
                lpath = "/get-retell-llm/" + lid + ("?version=%s" % lver if lver is not None else "")
                llm = _api(lpath) or {}
                print("    model=%s published=%s prompt=%d bytes" % (
                    llm.get("model") or "?", llm.get("is_published"),
                    len(llm.get("general_prompt") or "")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["numbers", "agents", "llms", "calls", "live"])
    ap.add_argument("--channel", default="voice", choices=["voice", "chat", "all"],
                    help="agents: which channel to list (default voice)")
    ap.add_argument("--days", type=int, default=7, help="calls: lookback window")
    ap.add_argument("--limit", type=int, default=100, help="calls: max rows")
    a = ap.parse_args()
    if a.action == "numbers":
        cmd_numbers()
    elif a.action == "agents":
        cmd_agents(a.channel)
    elif a.action == "llms":
        cmd_llms()
    elif a.action == "calls":
        cmd_calls(a.days, a.limit)
    else:
        cmd_live()
