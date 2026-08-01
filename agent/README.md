# Retell agent config (version-controlled)

The voice agent's prompt lives in Retell, not in this repo — so a change to
what the agent *says* was previously unreviewable and unversioned, even though
the tools it calls are all defined in `main.py`. These files close that gap.

| File | Agent version | State |
|---|---|---|
| `v43_prompt.txt` | v43 | previous published version (superseded) |
| `v44_prompt.txt` | v44 | **published & live** — pairs with Round 19 |

- agent: `agent_3a0baebfdb1491185a47238c3e`
- LLM:   `llm_9f9849c5acc548fb83c81d4867d7` (`gpt-5.1`, voice `retell-Cimo`)
- tools: `lookup_student` · `get_order_details` · `get_quote` ·
  `check_availability` · `transfer_to_office` · `end_call`
- webhook: `/retell_webhook` on this service (post-call auto-QA → `/voiceqa`)

## Why v44 exists

Round 19 stopped the server returning near-miss customer names. The v43 prompt
still instructs the agent to read those names aloud ("did you mean {list the
suggestions}?"), so it must be updated in step with the backend — otherwise the
agent asks for a list it no longer receives.

v44 = v43 with exactly two edits:
1. A hard rule: never speak a name the caller has not said.
2. The `status: "confirm"` branch now asks the caller to spell their last name,
   and keeps the phone-number-match case, where offering names IS legitimate.

Diff them with `diff -u agent/v43_prompt.txt agent/v44_prompt.txt`.

## Publishing

v44 is already published and (314) 804-4864 is routed to it. Retell does NOT
re-route a number on publish - the phone number pins an explicit
`agent_version`, so it must be updated separately via
`PATCH /update-phone-number/{number}`.

Run the regression suite against it:

    python tools/retell_suite.py all --version 44
