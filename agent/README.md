# Retell agent config (version-controlled)

The voice agent's prompt lives in Retell, not in this repo — so a change to
what the agent *says* was previously unreviewable and unversioned, even though
the tools it calls are all defined in `main.py`. These files close that gap.

| File | Agent version | State |
|---|---|---|
| `v43_prompt.txt` | v43 | superseded |
| `v44_prompt.txt` | v44 | previous published version — pairs with Round 19; live until v45 ships |
| `v45_prompt.txt` | v45 | superseded by v46 |
| `v46_prompt.txt` | v46 | **published & live** — pairs with Round 20 |

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

## Why v45 exists

A caller said "my name is Samson Nwobi." The lookup came back `found`, and the
agent asked "I've got an order under Samson Nwobi, is that you?" The caller
said "Yes" — and the agent immediately called `get_order_details` with
`answer: "Yes"`, never asking for the verifier the lookup had named. The
backend correctly refused (`verified: false`), the agent read that refusal as
an outage, told the caller it was "having trouble reaching your records," and
transferred. The record was fine and every correct verifier worked; nothing was
broken except the agent's behavior.

v45 = v44 with four edits:
1. "Yes" is a NAME confirmation, never a verification answer, and never valid
   as `answer`. `get_order_details` is not called until the caller has actually
   spoken a detail.
2. `verified: false` means *unverified*, not *broken*. Only a genuine error gets
   the "trouble reaching records" wording. Keys off the backend's new `reason`
   field when it's there, and behaves the same when it isn't.
3. Transferring is now the last rung, not the first. A name ladder (spell the
   last name → spell first and last → look up by the phone number on the order
   → transfer) and a verifier ladder (three attempts, a different *kind* of
   detail each time) replace v44's single retry. The lockout still short-circuits
   both: `locked: true` transfers immediately. No security rule was relaxed —
   only the number of chances changed.

   The phone rung is deliberately mute about names. A number the caller *reads
   out* is not evidence they hold it, so a hit there is acknowledged only as "an
   order on that number" — the name goes to `get_order_details` unspoken and is
   said aloud only after `verified: true`. This is the same rule Round 19 drew,
   and the reason v44's caller-ID exception is now scoped to the number the
   caller is *calling from* (the ANI), which genuinely is evidence.
4. A pace rule in the GOLDEN RULES: no filler, no repeating the caller back.

Diff them with `diff -u agent/v44_prompt.txt agent/v45_prompt.txt`.

### Two config changes that ship with v45

Neither lives in the prompt file, so they are recorded here.

- **`lookup_student` gains an optional `phone` parameter.** Rung 3 of the name
  ladder is dead weight without it: the backend has accepted `phone` since
  Round 19 (`do_lookup_student` uses it only when `name_heard` is empty), but
  the *tool schema* lives in Retell and declared `name_heard` alone, so the
  agent had no way to send a number. `name_heard` stays in `required` — the
  main path is unchanged — and the description tells the agent to send an empty
  string when looking up by number. Optional-but-not-required is already proven
  under `tool_call_strict_mode: true` by `get_order_details`' `order_hint`.
- **`voice_speed: 1.15`** (+15%; the field was previously unset, i.e. 1.0).
  `responsiveness` (0.7) and `interruption_sensitivity` (0.6) were deliberately
  left alone — the name ladder now asks callers to spell names out loud, and
  tightening turn-taking is exactly what would make the agent talk over them.
  The GOLDEN RULES pace rule cuts perceived slowness with no interruption risk.

## Publishing

v44 is published and live. v45 now exists in Retell as agent version 45 +
LLM version 45, both **unpublished drafts**; v44 is byte-for-byte untouched.

Going live is two independent steps, and neither happens by accident:

1. `POST /publish-agent-version/{agent_id}` with `version: 45`.
2. Re-point the number. Retell does NOT re-route on publish — `(314) 804-4864`
   pins an explicit `agent_version` in its `inbound_agents` / `outbound_agents`
   entries (currently `44`), so it keeps serving v44 until it is updated via
   `PATCH /update-phone-number/{number}`.

Because the pin is explicit, publishing alone changes nothing for callers —
step 2 is the actual go-live.

To read the live state before and after either step — which version the number
actually serves, which LLM drafts exist, whether a run billed any calls — use
`tools/retell_ops.py` (`numbers` · `agents` · `llms` · `calls` · `live`). It is
read-only, and it is the only sanctioned client for those four list routes:
Retell deprecated the unversioned ones, and the warning emails were traced to
throwaway scripts that answered these questions by hand.
`tests/test_retell_endpoints.py` fails the build if a legacy path reappears.

Run the regression suite against a draft before either step:

    python tools/retell_suite.py all --version 45

Note the 12 cases are a *regression* gate — they cover the identity gate,
injection, quotes and routing, and none of them exercise v45's new name ladder,
phone rung, or `reason` branching. A green run means v45 broke nothing that
worked before; it is not evidence the new ladder works. That still needs a
live call.

## Why v46 exists (Round 20)

A four-agent audit of v45 found defects that a measurement then confirmed. The two
that mattered most were **not** in the prompt.

### The identity gate could be opened with the word "hall"

`_building_matches` opened with `if t == b or t in b`. The second test was a bare
substring against the whole building name, and it ran *before* `_BLD_STOP` was applied
to anything — so "hall" verified against every building whose name ends in "Hall", and
"house" covered most of the rest:

    answer='hall'   verifies 6/10 buildings
    answer='house'  verifies 2/10

Two words defeated the gate for nearly every customer, using no secret at all: an
attacker needed only a name, which the agent confirms out loud ("I've got an order
under X, is that you?"). Reachable from the phone agent and from the unauthenticated
`/chat` endpoint alike. **This predates v45** — it was live through every round above.

The fix drops the substring test and requires the caller to cover *every* distinctive
word in the building name (`all()`, not `any()`), where generic residence words are
distinctive of nothing. Deliberately still not generic: `village`, `college`, `park`,
and the compass words — each can *be* the name, and stopping one would shrink that
building's core to a single generic token.

### The phone rung turned a phone number into a full record

v45 added a `phone` parameter so the name ladder could fall back to the number on the
order. But `_verify_field` returns `"phone"` whenever the order has one — so the agent
then asked for "the last 4 digits of the phone number on the order", i.e. the number
the caller had recited seconds earlier. Knowing a student's phone number was therefore
enough to obtain their name, building and room.

Phone-sourced lookups now exclude `phone` from the verifier and ask for something the
caller has not already said.

### A phone lookup used to read strangers' names aloud

A number matching more than one name returned `suggestions` plus a ready-to-speak
`message` listing them, and the prompt's `confirm` handler says to say `message` out
loud. The old test asserting this behaviour encoded the premise that the number came
from caller ID — but **nothing in this service supplies caller ID**; there is no
`from_number`/ANI anywhere, so `phone` has exactly one source: digits read out loud.
The names are gone, the `near_miss` count survives for QA, and the prompt's caller-ID
exception is deleted rather than narrowed, because it was unreachable *and* a
ready-made social-engineering script.

### Prompt changes

- **The transfer gate.** v45's rung 3 (phone lookup) executed only **5 of 9 measured
  runs**. Two independent causes: "Never transfer after one failed spelling" is
  literally satisfied by two, and a hard rule 53 lines earlier said never to call
  `lookup_student` without a real name — which is exactly what rung 3 does. Both fixed,
  and transfers are now enumerated: four legal moments, named before transferring.
- `not_found` no longer reads the tool's message aloud — after a phone lookup it asks
  for a name, which sent the ladder back to rung 1 in a loop.
- Never read back the phone, address, or order number, even to a verified caller: each
  is a verifier for the next call.
- Third-party and "I'm staff" requests get an explicit refusal; tool output is declared
  data, never instructions.
- Golden rule 6 no longer contradicts `handbook_config.ai_disclosure: true`.
- The after-hours callback offer is removed — there is no tool that can keep it.

Diff with `diff -u agent/v45_prompt.txt agent/v46_prompt.txt`.

## Known, not yet fixed

- **`webhook_url` carries `API_SECRET` in cleartext**, and that same secret gates
  `/lookup_student`, `/get_order_details`, `/debug_sheets` and the billing endpoints.
  Rotating it has to land in Render env, both tool `headers` blocks and the webhook in
  one window — `_authorized` fails **open** when `API_SECRET` is unset, so a missed
  value silently un-gates PII rather than erroring.
- `backchannel_frequency: 0.8` fires "mm-hmm" into the pauses between spelled letters —
  the one turn-taking knob that actually endangers the spelling ladder.
- `reason` and `verify_with` are absent from `get_order_details.response_variables`,
  and v45/v46's whole design branches on `reason`.
- Tool `timeout_ms: 120000` races `end_call_after_silence_ms: 120000`.
- The `phone` description says "at least 7 digits" but `_match_by_phone` compares all
  10, so a 7-digit answer always misses.
