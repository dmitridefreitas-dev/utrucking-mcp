# Security

This service handles real student customer records — names, phone numbers,
buildings, room numbers, stored items. Most of the code exists to keep those
from reaching the wrong caller.

## How configuration is supplied

Nothing that locates or unlocks customer data belongs in this repo. It is a
public repository; anything committed here is public permanently, including in
git history after deletion.

All of it comes from the environment — see `.env.example`. CI enforces this:
`.github/workflows/security.yml` runs gitleaks over full history and rejects
hardcoded Google file IDs, Retell `llm_`/`agent_` IDs, and secrets pasted into
URL query strings.

## The layers

**Identity gate.** `lookup_student` returns only a confirmed name and which
detail to ask for — never order values. Details come from `get_order_details`
and only after the caller supplies a matching verifier (phone last-4, order
number, or building). `_verify_field` asks for the strongest one on file: the
phone last-4 is a 10,000-value space, the building roughly 60, and a classmate
knows which dorm you live in.

**Shared names fail closed.** When one name matches orders belonging to
different people, a weak verifier that can't separate them reveals nothing. An
order with no phone on file counts as an *unknown* identity, not a matching
one — absence of evidence is not evidence of sameness.

**No name disclosure.** A near miss never returns the names it nearly matched;
it asks the caller to spell theirs. A "confident" fuzzy match must clear a real
similarity floor, so a plausible name belonging to nobody on file cannot land
on a stranger's record. Names matched from the caller's *own phone number* are
the one exception, and are safe by construction.

**Brute force.** Five wrong verifications lock a name for 15 minutes, and each
lock served doubles the next window (to a 24h cap) so waiting it out stops
working. A per-IP limiter catches one machine rotating through many names. Any
successful verification clears the slate, so a genuine caller who fumbles is
never punished.

**Staff gate.** Endpoints returning PII or ops data require `x-utrucking-key`,
matched against `API_SECRET` in constant time. The gate **fails closed**: with
no `API_SECRET` deployed they answer `503 unconfigured` rather than serving
records, because the likeliest way to lose this secret is to miss it in one
place during a rotation, and a gate whose failure mode is to disappear is not a
gate. Running open is still possible for local work — `UTRUCKING_ALLOW_OPEN_API=1`
— but only as an affirmative act, never as the consequence of an omission. The
`/mcp` endpoint rides the same rule, since it serves the same lookup tools.
`/sample_ids` verifiers additionally require the key to be *armed*, so they fail
closed in every open configuration, deliberate or not.

**Post-call QA.** Every call is scored by an LLM judge, including whether the
identity gate held. `/voiceqa`.

## Reporting

Email info@utrucking.com. Please don't open a public issue for anything
affecting customer data.

---

## Known exposure — open

**The Google Sheets are readable by anyone with the link, and the links were
public.** Both sheet IDs were hardcoded in `main.py` in this repo and in the
public `utruckingai` and `utrucking-ai` mirrors. Verified: an unauthenticated
`GET` returns ~2,500 dispatch rows and ~2,750 service rows as CSV — the entire
customer base, no login.

The identity gate protects the *phone* channel. It does nothing here: the sheet
is a side door that hands over every record at once.

Moving the IDs to environment variables stops the repo advertising them. **It
does not revoke access.** The IDs remain in git history and in the mirrors, and
anyone who already has one keeps it.

Closing it requires, in order:

1. Set both sheets to **Restricted** in Google Drive. This immediately breaks
   the live service — `fetch_csv_rows` currently does an unauthenticated
   `httpx.get`.
2. Give the service a Google service account, share the sheets with it, and
   have `fetch_csv_rows` send an OAuth token. Keep the 60s cache and the
   last-good-copy fallback.
3. Because the IDs are already disclosed, restricting alone is only sound if
   the sheets stay restricted. If they must ever be link-shared again, copy
   them to new files with new IDs first.
4. Scrub or take down the `utrucking-ai` / `utruckingai` mirrors, which also
   carry an un-scrubbed Retell LLM ID.

**The webhook key is disclosed and rotating it is a deploy step.** The Retell
post-call webhook is registered as a URL, and Retell cannot attach a custom
header to one — so the key has exactly one channel, `?key=`, and a URL stored in
Retell's config, rendered in its dashboard and echoed by every proxy log between
here and there should be assumed known.

What is fixed is the blast radius: `retell_webhook` now reads `WEBHOOK_SECRET`,
and once that is set it is the *only* value the webhook accepts — `API_SECRET`
stops working there, so the two cannot silently re-converge. What remains is the
deploy: set `WEBHOOK_SECRET` in Render, re-register the webhook URL with the new
value, then rotate `API_SECRET` (which should be considered disclosed, since it
has been travelling in that URL). Until `WEBHOOK_SECRET` is set the webhook
still falls back to `API_SECRET` — the code change is deployable ahead of the
env var, but it buys nothing until the env var lands.
