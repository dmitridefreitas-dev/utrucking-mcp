"""Guard: no Python file in this repo may call a deprecated Retell list endpoint.

Retell removes the legacy list routes on announced dates and mails a warning
naming the exact paths still in use. The service itself has always been on the
versioned routes; the warnings came from operator scripts written ad hoc in a
scratchpad, which is precisely the class of code no reviewer ever sees. Now that
those live in `tools/retell_ops.py`, this test keeps them there — a legacy path
anywhere under the repo fails the build instead of surfacing weeks later in an
email.

Offline: it reads files, it never calls the API."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Both deprecation notices, verbatim:
#   2026-06-15 legacy list endpoints, 2026-07-31 agent list endpoints.
# Value = the route that replaced it, so a failure tells you what to write.
REPLACEMENTS = {
    "/list-agents": "POST /v2/list-agents",
    "/list-chat-agents": "POST /v2/list-agents (filter_criteria.channel = chat)",
    "/list-batch-tests": "GET /v2/list-batch-tests",
    "/list-conversation-flow-components": "GET /v2/list-conversation-flow-components",
    "/list-conversation-flows": "GET /v2/list-conversation-flows",
    "/list-phone-numbers": "GET /v2/list-phone-numbers",
    "/list-retell-llms": "GET /v2/list-retell-llms",
    "/list-test-case-definitions": "GET /v2/list-test-case-definitions",
    "/list-test-runs": "GET /v2/list-test-runs/{id}",
    "/list-chat": "POST /v3/list-chats",
    "/v2/list-calls": "POST /v3/list-calls",
}

# The leading slash must not follow a version segment, or every migrated path
# would match its own predecessor: "/v2/list-phone-numbers" literally contains
# "/list-phone-numbers".
_DEPRECATED = re.compile(
    "(?<![0-9A-Za-z])(" + "|".join(re.escape(p) for p in REPLACEMENTS) + ")")

_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
_SELF = os.path.abspath(__file__)


def _python_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                if os.path.abspath(path) != _SELF:   # this file names them as data
                    yield path


def test_no_deprecated_retell_list_endpoints():
    hits = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                m = _DEPRECATED.search(line)
                if m:
                    hits.append("%s:%d uses %s -> use %s" % (
                        os.path.relpath(path, ROOT), lineno, m.group(1),
                        REPLACEMENTS[m.group(1)]))
    assert not hits, "deprecated Retell list endpoint(s):\n  " + "\n  ".join(hits)


def test_ops_tool_declares_the_migrated_routes():
    """retell_ops.py is the only sanctioned client for these four; pin its paths
    so a future edit cannot quietly walk one of them back."""
    import tools.retell_ops as ops
    assert ops.LIST_AGENTS == "/v2/list-agents"
    assert ops.LIST_NUMBERS == "/v2/list-phone-numbers"
    assert ops.LIST_LLMS == "/v2/list-retell-llms"
    assert ops.LIST_CALLS == "/v3/list-calls"


def test_ops_tool_asks_for_voice_agents_only():
    """The versioned agent list merges voice and chat. Without the channel filter
    the tool would silently start reporting chat agents as phone agents — the one
    behaviour change in this migration that is not a path rewrite."""
    src = open(os.path.join(ROOT, "tools", "retell_ops.py"), encoding="utf-8").read()
    assert '"filter_criteria"' in src and '"channel"' in src
    # The v2 route rejects this legacy paging parameter outright.
    assert "pagination_key_version" not in src


def test_service_pulls_calls_from_v3():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert '"/v3/list-calls"' in src


@pytest.mark.parametrize("path", ["tools/retell_ops.py", "tools/retell_suite.py"])
def test_operator_tools_read_the_key_from_the_environment(path):
    """Never from ~/.claude.json, and never off a command line: the scratchpad
    helpers did the former, and the key can read or rewrite the live prompt."""
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    assert 'RETELL_API_KEY' in src
    assert ".claude.json" not in src
