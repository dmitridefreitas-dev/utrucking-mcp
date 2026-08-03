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
| `agent_config.json` | — | intended non-prompt config (Round 21); **not yet applied** |

`agent_config.json` is the other half of the agent: the turn-taking knobs, the
tool timeouts, and which response fields reach the prompt at all. The prompt has
been version-controlled since Round 19, but this half lived only in Retell's
dashboard — unreviewable, and untestable, which is why three of the defects
listed at the bottom of this file sat there for a round. It is checkable now:

    python tools/retell_suite.py config           # report drift, exit 1 if any
    python tools/retell_suite.py config --apply   # write it to the agent draft

`config` needs `RETELL_API_KEY`, `RETELL_AGENT_ID` and `RETELL_LLM_ID`. The
comparison rules are unit-tested offline in `tests/test_agent_config.py`, so a
manifest that would break the agent — a tool timeout that races the silence
timer, a dropped `reason` variable — fails in CI without touching Retell.

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

## Round 21 — the config half, and a gate that vanished when its key did

Round 20 ended with five known-unfixed items. Four are fixed; the fifth needed a
deploy step this repo cannot take on its own.

### The staff gate's failure mode was to disappear

`_authorized` returned `True` for the whole internet whenever `API_SECRET` was unset.
That was a deliberate dormant gate for the first rollout, and the rollout finished many
rounds ago; what it left behind is the one property a gate must never have. This secret
has to land in Render's environment, in every tool `headers` block and in the webhook
URL inside a single window, and rotation is exactly when it is most likely to be missing
from one of them. Missing it in Render published every customer record with a `200` and
no error anywhere — the failure was silent, which is what made it worse than an outage.

The gate was made to fail **closed** — no `API_SECRET` means `503 unconfigured`, a
distinct status from `401` so whoever is debugging the rotation looks at the environment
instead of at the caller — and then **deliberately reverted to dormant before shipping**.
The gated list includes `/lookup_student`, `/get_order_details`, `/verify_identity` and
`/mcp`: every door this agent has. `API_SECRET` is not deployed in Render, so closing the
gate would have ended every call the moment the service redeployed. A silent exposure is
bad; trading it for a silent outage on the phone line is not a fix.

The closed path is built and tested, held behind one line (`ALLOW_OPEN_API` in `main.py`)
rather than an env var, so the gate's state cannot be lost track of across two flags of
opposite polarity. Closing it is: key into Render → same key into every tool `headers`
block → confirm a live call → flip the line. Secrets compare in constant time regardless;
these endpoints are reachable by anyone, so `==` was a public oracle.

### The webhook key and the PII key are separable

The webhook URL carries its key in `?key=`, and Retell cannot attach a header to a
webhook, so that channel is the only one there is — treat the value as disclosed. The
fixable half was that the disclosed value was *also* the staff key. `WEBHOOK_SECRET`
splits them, and once set it is the only value the webhook accepts, so they cannot
quietly re-converge. Unset, it still falls back to `API_SECRET`: deployable before the
env var exists, worth nothing until it lands. See SECURITY.md for the rotation order.

### A 7-digit number could not match anything

Rung 3 of the v46 ladder tells the agent a number "needs at least 7 digits".
`_match_by_phone` compared the last **10** digits of both sides, so 7, 8 and 9 matched
nothing, ever — the rung the whole ladder falls back on was documented as working and
could not fire, and a caller who gave the local number they actually remember was told
no order exists. It now matches on the number of digits supplied. A short number is
looser and can collide across area codes; that is contained, because a multi-name hit
reveals no names and a single hit still faces the identity gate with `phone` excluded.

### The three dashboard-only defects are now a file

`backchannel_frequency: 0.8`, the racing `timeout_ms`, and the missing `reason` /
`verify_with` response variables are recorded in `agent_config.json` with the reasoning
for each, checked by `tools/retell_suite.py config`, and unit-tested offline. Backchannels
are **off** rather than merely rarer: at any frequency above zero the "mm-hmm" still
lands between spelled letters, just less often, and the spelling ladder is the recovery
path for a call that is already going wrong. `backchannel_frequency: 0` is a second
latch, not the fix — Retell applies that field "only when `enable_backchannel` is true",
and its default is 0.8, so pinning it is what stops a dashboard toggle restoring the
defect rather than producing a quiet agent.

### The manifest's response variables were written in a notation Retell doesn't read

Every mapping in the first draft of `agent_config.json` was JSONPath — `"reason": "$.reason"`.
Retell's custom-function docs are explicit that this field is not JSONPath: "Point each
variable at a field in the response using dot notation, with array indexing where needed
— for example `user.name` or `data.items[0].id`." `$.reason` asks for a field named `$`
holding a field named `reason`. Nothing this service returns has one, so all fourteen
mappings bound nothing — and nothing complains, because an unresolved path simply yields
no variable and an unset dynamic variable renders as the literal text `{{reason}}`. The
drift checker was comparing every tool against a value that could never be right, so
`config` would have reported the same drift forever and `--apply` would have written
fourteen dead mappings and printed `applied`. All of them are now plain dot paths.

Scope, because the first write-up of this overstated it: a custom tool's **entire JSON
response body is stringified into the LLM's context regardless of `response_variables`**.
v46's `reason` branching reads the tool result directly and works with this field empty;
`response_variables` only binds values for `{{template}}` reuse later in the call. So this
was a dead config entry, not a broken agent — worth fixing, and not what the failure
taxonomy stands on.

Two guards now sit in `load_config`, so they run on the `--apply` path and not only in
CI: a `$`-prefixed path and a tool timeout that is out of Retell's documented 1000–600000ms
range or greater than or equal to `end_call_after_silence_ms` each raise before any request
is built. Retell answers `200` to all three, which is the only reason they needed catching
somewhere other than a call. The drift check deliberately does **not** normalise `$.reason`
to `reason`: they are not two spellings of one path, and folding them together would grade
the live broken agent as correct.

## Known, not yet fixed

- **`agent_config.json` has not been applied.** It is the intended state, not the live
  one — the manifest exists and is checked, but nothing has been written to Retell.
  Run `python tools/retell_suite.py config` to see the drift, `--apply` to write it,
  then the two publishing steps below. Until then the live agent still has
  `backchannel_frequency: 0.8`, `timeout_ms: 120000`, and no `reason` variable.
- **Three values in the manifest are asserted, not observed.** `responsiveness: 0.7`,
  `interruption_sensitivity: 0.6` and `end_call_after_silence_ms: 120000` are recorded here
  as the agent's existing settings and described as "left alone", but none of them is
  Retell's default (1, 1, and 600000). If the live agent is actually on the defaults, the
  first `--apply` changes all three rather than confirming them — and the tool-timeout
  invariant is stated against a silence timer that may not be the live one. Read them off
  `config`'s drift report before applying; the check is read-only.
- **`WEBHOOK_SECRET` is not deployed**, so the webhook still accepts `API_SECRET` and
  the key in the URL is still the key that gates PII. Code side is done; the remaining
  work is Render env + re-registering the webhook URL + rotating `API_SECRET`.
- The `.env.example` in this repo does not yet list `WEBHOOK_SECRET` or
  `RETELL_AGENT_ID`.
- **`API_SECRET` is not deployed in Render**, so the staff gate is dormant and the
  PII/ops endpoints are open to the internet. This is the single highest-value
  outstanding item: setting it, adding the same value to each Retell tool's
  `headers` block, then flipping `ALLOW_OPEN_API = False` closes it in one pass.
