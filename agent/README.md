# Retell agent config (version-controlled)

The voice agent's prompt lives in Retell, not in this repo — so a change to
what the agent *says* was previously unreviewable and unversioned, even though
the tools it calls are all defined in `main.py`. These files close that gap.

| File | Agent version | State |
|---|---|---|
| `v43_prompt.txt` | v43 | published — what is live on (314) 804-4864 today |
| `v45_prompt.txt` | v45 | **draft, not yet pushed** — pairs with Round 19 |

- agent: `agent_3a0baebfdb1491185a47238c3e`
- LLM:   `llm_9f9849c5acc548fb83c81d4867d7` (`gpt-5.1`, voice `retell-Cimo`)
- tools: `lookup_student` · `get_order_details` · `get_quote` ·
  `check_availability` · `transfer_to_office` · `end_call`
- webhook: `/retell_webhook` on this service (post-call auto-QA → `/voiceqa`)

## Why v45 exists

Round 19 stopped the server returning near-miss customer names. The v43 prompt
still instructs the agent to read those names aloud ("did you mean {list the
suggestions}?"), so it must be updated in step with the backend — otherwise the
agent asks for a list it no longer receives.

v45 = v43 with exactly two edits:
1. A hard rule: never speak a name the caller has not said.
2. The `status: "confirm"` branch now asks the caller to spell their last name,
   and keeps the phone-number-match case, where offering names IS legitimate.

Diff them with `diff -u agent/v43_prompt.txt agent/v45_prompt.txt`.

## Publishing

Paste `v45_prompt.txt` into the agent's prompt in the Retell dashboard, then
run the regression suite against the new draft BEFORE publishing:

    python tools/retell_suite.py all --version 45

Publish only on a clean pass.
