import httpx
import json
import os
import asyncio
import csv
import io
import difflib
import re
import base64
import datetime
import time
import analytics
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from engines import build_price_book, quote as _quote_items, availability as _availability, billing_audit as _billing_audit, dispatch_plan as _dispatch_plan, open_days as _open_days, season_bounds as _season_bounds, peak_date as _peak_date, merge_photo_text as _merge_photo_text, upsell_pairs as _upsell_pairs, upsell_value as _upsell_value, space_estimate as _space_estimate

RENDER_URL = os.getenv("RENDER_URL", "https://utrucking-mcp.onrender.com")

DISPATCH_SHEET_ID = "1x5MQbsCFMJX5eafA6uoFF0nU4qfvCaK-2EyZNPs__kM"
DISPATCH_SHEET_GID = "602263013"
DISPATCH_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{DISPATCH_SHEET_ID}"
    f"/export?format=csv&gid={DISPATCH_SHEET_GID}"
)

SERVICE_SHEET_ID = "1m43ijcOmxAnFt54mLos6dLlwvYnMOlwSTZkQWHg0D54"
SERVICE_SHEET_GID = "1320217925"
SERVICE_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SERVICE_SHEET_ID}"
    # NOTE: this sheet 400s on /export?format=csv; the gviz endpoint serves the same public CSV reliably.
    f"/gviz/tq?tqx=out:csv&gid={SERVICE_SHEET_GID}"
)

# The MCP protocol endpoint (/mcp) is consumed by REMOTE clients — Claude custom
# connectors and Retell's native MCP node — so the SDK's DNS-rebinding Host check
# must allow the public hostname (its default allows localhost only → 421 Invalid
# Host for everyone else). stateless_http + json_response make each POST
# self-contained: no server-side session to lose across Render restarts, no SSE
# stream for clients to hold open.
try:
    from mcp.server.transport_security import TransportSecuritySettings
    _MCP_SECURITY = TransportSecuritySettings(
        allowed_hosts=[RENDER_URL.split("://", 1)[-1].strip("/"),
                       "localhost:*", "127.0.0.1:*", "localhost", "127.0.0.1"],
    )
except Exception:                        # offline test stubs don't ship this module
    _MCP_SECURITY = None

mcp = FastMCP("UTrucking Storage Lookup", stateless_http=True, json_response=True,
              **({"transport_security": _MCP_SECURITY} if _MCP_SECURITY else {}))


async def keep_alive():
    await asyncio.sleep(30)
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{RENDER_URL}/health")
        except Exception:
            pass
        await asyncio.sleep(14 * 60)


# ── In-memory sheet cache ────────────────────────────────────────────
# The sheets are read on every tool call; without a cache each quote/lookup/
# insights request re-downloads both. A short TTL cuts that to at most one
# fetch per sheet per SHEET_TTL, and on a fetch failure we serve the last good
# copy so a transient Google Sheets hiccup doesn't take a tool down.
SHEET_TTL = 60                                    # seconds a cached sheet is served as-is
_SHEET_CACHE: dict[str, tuple[float, list[dict]]] = {}   # url -> (fetched_at, rows)


async def fetch_csv_rows(url: str, force: bool = False) -> list[dict]:
    now = time.time()
    hit = _SHEET_CACHE.get(url)
    if hit and not force and (now - hit[0]) < SHEET_TTL:
        return hit[1]
    try:
        # Cache-buster so Google/CDN can't hand the server a stale export even after our own
        # in-memory copy expires. Normal refreshes bucket by the TTL window (still CDN-friendly);
        # a forced refresh (e.g. after a verification miss) gets a unique value to defeat any
        # edge cache, so a just-edited order verifies right away.
        bust = int(now * 1000) if force else int(now // SHEET_TTL)
        sep = "&" if "?" in url else "?"
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(f"{url}{sep}_cb={bust}")
        if resp.status_code == 200:
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = list(reader)
            cols = set(reader.fieldnames or [])
            # Only trust a 200 that actually LOOKS like one of our sheets. A transient empty body or an
            # HTML error/sign-in page (reachable via follow_redirects) parses to [] / one garbage column;
            # caching that would evict the last-good copy the fallback below exists to preserve, and blank
            # every lookup for a full TTL. On a bad body we fall through and serve the last good copy.
            if rows and ({"Student", "ID", "Student Name", "Service", "Building",
                          "Summer Storage Item List", "Order#:"} & cols):
                _SHEET_CACHE[url] = (now, rows)
                return rows
    except Exception:
        pass
    # fetch failed / non-200 / empty-or-non-CSV 200 → serve last-good rows if we have any, else empty
    return hit[1] if hit else []


def smart_name_match(query: str, all_names: list[str]) -> tuple[str | None, list[str]]:
    """
    Returns (best_match, suggestions).
    Tries: exact substring → first-name fuzzy + last-name narrow → full fuzzy fallback.
    """
    q = query.strip()
    q_lower = q.lower()
    q_tokens = q_lower.split()

    # 1. Exact substring (case-insensitive)
    exact = [n for n in all_names if q_lower in n.lower()]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return exact[-1], []

    # 2. Token-based: match first name, then narrow by last name
    if q_tokens:
        first_token = q_tokens[0]
        first_candidates = []
        for name in all_names:
            name_tokens = name.lower().split()
            if name_tokens:
                score = difflib.SequenceMatcher(None, first_token, name_tokens[0]).ratio()
                if score >= 0.6:
                    first_candidates.append(name)

        if first_candidates:
            if len(q_tokens) >= 2:
                last_token = q_tokens[-1]
                last_matches = []
                for name in first_candidates:
                    name_tokens = name.lower().split()
                    if len(name_tokens) >= 2:
                        score = difflib.SequenceMatcher(None, last_token, name_tokens[-1]).ratio()
                        if score >= 0.5:
                            last_matches.append(name)
                if len(last_matches) == 1:
                    return last_matches[0], []
                if len(last_matches) > 1:
                    return None, last_matches[:3]
                # The caller gave a last name but it matches NONE of the first-name candidates'
                # last names — do NOT confidently pull up a stranger who merely shares a fuzzy
                # first name (this is how gibberish like "Zblargh Xyzptqq" used to match
                # "Blair Wagner"). Fall through to the strict whole-name fuzzy, which needs 0.6 overall.
            else:
                # only a first name was given — can't narrow by last name
                if len(first_candidates) == 1:
                    return first_candidates[0], []
                return None, first_candidates[:3]

    # 3. Full fuzzy fallback
    close = difflib.get_close_matches(q, all_names, n=3, cutoff=0.6)
    if len(close) == 1:
        return close[0], []
    if close:
        return None, close

    return None, []


def _phone_digits(s, n=10):
    """Last n digits of a phone string (strips formatting)."""
    d = re.sub(r"\D", "", s or "")
    return d[-n:] if len(d) >= n else d


def _match_by_phone(phone, dispatch_rows):
    """Distinct customer names whose on-file phone matches the caller's number (last 10 digits).
    Powers caller-ID: greet a returning caller by name instead of asking them to spell it."""
    want = _phone_digits(phone, 10)
    if len(want) < 7:                       # need a real number, not a fragment
        return []
    names, seen = [], set()
    for r in dispatch_rows:
        if _phone_digits(r.get("Phone", ""), 10) and _phone_digits(r.get("Phone", ""), 10) == want:
            n = " ".join((r.get("Student") or "").split())
            if n and n.lower() not in seen:
                seen.add(n.lower()); names.append(n)
    return names


async def do_lookup_student(name_heard: str, order_hint: str = "", phone: str = "") -> dict:
    if not (name_heard or "").strip() and not (phone or "").strip():
        return {
            "status": "not_found",
            "message": "I didn't catch a name. Could you repeat that?"
        }

    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL),
            fetch_csv_rows(SERVICE_CSV_URL),
        )
    except Exception:
        return {"status": "error", "message": "I'm having trouble reaching our records right now."}

    # Caller-ID: if we have a number and no name, resolve the name from the number first.
    if (phone or "").strip() and not (name_heard or "").strip():
        names = _match_by_phone(phone, dispatch_rows)
        if len(names) == 1:
            res = _build_order_result(names[0], dispatch_rows, service_rows, order_hint)
            if isinstance(res, dict) and res.get("status") == "found":
                res["identified_by"] = "phone"    # still identity-verified before any reveal
            return res
        if len(names) > 1:
            return {"status": "confirm", "suggestions": names[:4],
                    "message": "I see a few names on this number — %s. Which one is this?" % ", ".join(names[:4])}
        return {"status": "not_found",
                "message": "I couldn't find an order under that number. What's the name on the order?"}

    return _build_order_result(name_heard, dispatch_rows, service_rows, order_hint)


async def do_verify_identity(name_heard: str, answer: str, order_hint: str = "") -> dict:
    """Confirm a caller is who they say before any order detail is revealed. Re-looks up the
    record by name and checks the caller's answer with the SAME tolerant matcher the web chat
    uses (fuzzy/partial building, phone last-4, or order number). Returns only a verified
    boolean + the confirmed name — never any order PII — so it's a clean gate for the phone."""
    if not (name_heard or "").strip():
        return {"status": "not_found", "verified": False,
                "message": "I didn't catch a name to verify."}
    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))
    except Exception:
        return {"status": "error", "verified": False,
                "message": "I'm having trouble reaching our records right now."}
    # Check the answer against EVERY order under this name (via _verify_pick), exactly like
    # get_order_details and the chat — so a shared-name / multi-order caller is verified against the
    # order they can actually prove, not just the most-recent one. Returns only verified + the name,
    # never any PII, so it stays a clean gate.
    match, base = _verify_pick(name_heard, answer, dispatch_rows, service_rows, order_hint)
    if base.get("status") != "found":
        # pass confirm/not_found through so the agent can re-ask or offer suggestions
        return {"status": base.get("status", "not_found"), "verified": False,
                "confirmed_name": base.get("confirmed_name", ""),
                "suggestions": base.get("suggestions", []),
                "message": base.get("message", "")}
    nm = " ".join((base.get("confirmed_name") or name_heard).lower().split())
    if _verify_locked(nm):                                    # same brute-force guard as the chat
        return {"status": "found", "verified": False, "locked": True,
                "confirmed_name": base.get("confirmed_name", ""),
                "message": "Too many verification attempts. For security, please call the team at (314) 266-8878."}
    verified = match is not None
    if verified:
        _VERIFY_FAILS.pop(nm, None)
    else:
        _verify_fail(nm)
    return {"status": "found", "verified": bool(verified),
            "confirmed_name": base.get("confirmed_name", ""),
            "message": ("Identity confirmed." if verified
                        else "That detail doesn't match what we have on file.")}


def _verify_prompt(rec):
    """The detail the phone agent should ASK the caller for — chosen from what's on file,
    phrased for speech. The value itself is never sent to the agent before verification."""
    if rec.get("building"):
        return "which building their pickup is at"
    if rec.get("phone"):
        return "the last 4 digits of the phone number on the order"
    return "their order number"


# Order PII that must NOT leave the server until the caller proves who they are.
_PII_FIELDS = ("order_id", "service", "building", "room", "address", "date", "time_slot",
               "order_status", "dispatch_status", "truck", "kits", "product", "phone",
               "invoice_id", "items_list", "boxes", "luggage", "other", "other_description",
               "notes", "date_completed")


def _redact_lookup(rec):
    """What the PHONE agent gets from lookup_student: enough to confirm the name and know
    which detail to ask for, but NO order values (building/date/phone/items). Those are only
    returned by get_order_details AFTER a correct, caller-provided answer — so the agent can't
    self-verify with a value it already holds. Mirrors the chat's server-side gate."""
    if not isinstance(rec, dict):
        return rec
    if rec.get("status") != "found":
        # confirm / not_found / error: names + message only (no order PII)
        return {k: rec[k] for k in ("status", "message", "suggestions", "confirmed_name") if k in rec}
    out = {"status": "found",
           "confirmed_name": rec.get("confirmed_name", ""),
           "available_fields": rec.get("available_fields", []),
           "verify_with": _verify_prompt(rec),
           "message": ("I found an order under %s. Confirm the name, then verify their identity "
                       "with get_order_details before sharing any detail." % rec.get("confirmed_name", ""))}
    if rec.get("needs_order_choice") and not rec.get("distinct_people"):
        # ONE person with several orders: let the agent disambiguate by service/date only (no order
        # numbers) before verifying. If the "orders" actually belong to DIFFERENT people who share a
        # name (distinct_people), we do NOT list them — the caller just verifies, and get_order_details
        # reveals only the order their answer matches, so a stranger's order is never disclosed.
        out["needs_order_choice"] = True
        out["order_count"] = rec.get("order_count")
        out["order_choices"] = [
            {"service": c.get("service", ""), "date": c.get("date", ""),
             "label": " ".join(x for x in [c.get("service", ""),
                                            ("(" + c["date"] + ")") if c.get("date") else ""] if x)}
            for c in (rec.get("order_choices") or [])]
        out["message"] = ("%s has %d orders on file. Ask which one (by service or date), then verify "
                          "identity." % (rec.get("confirmed_name", ""), rec.get("order_count", 0)))
    return out


def _verify_pick(name_heard, answer, dispatch_rows, service_rows, order_hint=""):
    """Return (matching_rec, base_rec). Checks the caller's answer against EVERY candidate order
    for this name and returns the one order whose verifier the answer matches — so a caller who
    shares a name with someone else, or has several of their own orders, reveals only the row they
    can actually verify (and never a stranger's). matching_rec is None if nothing verifies."""
    base = _build_order_result(name_heard, dispatch_rows, service_rows, order_hint)
    if base.get("status") != "found":
        return None, base
    if order_hint and not base.get("distinct_people") and _verify_answer(base, answer or ""):
        # caller narrowed to one of THEIR OWN orders. Only short-circuit when the name isn't shared by
        # DIFFERENT people (distinct_people): otherwise a weak verifier (e.g. a shared building) plus a
        # non-secret hint would hand back one specific stranger's PII, bypassing the ambiguity guard
        # below. When names are shared we fall through to the candidate loop, which refuses on ambiguity.
        return base, base
    ids = [c.get("order_id", "") for c in (base.get("order_choices") or []) if c.get("order_id")]
    cands = [_build_order_result(name_heard, dispatch_rows, service_rows, oid) for oid in ids] if ids else [base]
    matches = [r for r in cands if r.get("status") == "found" and _verify_answer(r, answer or "")]
    if not matches:
        return None, base
    # AMBIGUOUS: the answer matches orders belonging to more than one distinct person (e.g. two people
    # who share a name AND a building). A weak verifier can't tell them apart, so reveal NOTHING and
    # let the agent ask for a stronger identifier (phone last-4 or order number).
    who = {_last4(r.get("phone") or "") for r in matches}
    who.discard("")
    if base.get("distinct_people") and len(who) > 1:
        return None, base
    return matches[0], base


async def do_get_order_details(name_heard: str, answer: str, order_hint: str = "") -> dict:
    """Verify the caller, then reveal. Checks the caller's spoken answer with the SAME tolerant
    matcher as the chat (fuzzy/partial building, phone last-4, or order number) against every order
    filed under this name, and returns the full details of the ONE order it matches — only when
    verified; otherwise no PII. This is the gate that makes lookup_student safe to hand out."""
    if not (name_heard or "").strip():
        return {"status": "not_found", "verified": False, "message": "I didn't catch a name."}
    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))
    except Exception:
        return {"status": "error", "verified": False,
                "message": "I'm having trouble reaching our records right now."}
    match, base = _verify_pick(name_heard, answer, dispatch_rows, service_rows, order_hint)
    if base.get("status") != "found":
        return _redact_lookup(base)                      # confirm / not_found — never any detail
    nm = " ".join((base.get("confirmed_name") or name_heard).lower().split())
    if _verify_locked(nm):                               # same brute-force guard as the chat verify step
        return {"status": "found", "verified": False, "locked": True,
                "confirmed_name": base.get("confirmed_name", ""),
                "message": "Too many verification attempts. For security, please call the team at (314) 266-8878."}
    if match is None:
        # A cached sheet can lag a just-edited row (SHEET_TTL / CDN edge cache), which would make
        # a correct answer look wrong. Before failing, re-pull the sheets FRESH once and re-check
        # every candidate, so a recently updated order still verifies. Only runs on a miss, bounded
        # by the lockout, and never relaxes the check (same _verify_answer) so it can't leak.
        try:
            d2, s2 = await asyncio.gather(
                fetch_csv_rows(DISPATCH_CSV_URL, force=True), fetch_csv_rows(SERVICE_CSV_URL, force=True))
            match, _ = _verify_pick(name_heard, answer, d2, s2, order_hint)
        except Exception:
            pass                                         # fresh re-check failed → fall through to normal miss
    if match is None:
        _verify_fail(nm)
        return {"status": "found", "verified": False,
                "confirmed_name": base.get("confirmed_name", ""),
                "message": "That detail doesn't match what we have on file."}
    _VERIFY_FAILS.pop(nm, None)                           # clear the counter on success, like the chat
    match["verified"] = True
    return match                                         # identity proven → the ONE order they matched


def _order_label(row):
    """Short human label for one order row: 'Summer Storage #13851 (5/6/2026)'."""
    parts = [(row.get("Service") or "").strip() or "Order"]
    oid = (row.get("ID") or "").strip()
    if oid: parts.append(oid)
    d = (row.get("Date") or "").strip()
    return " ".join(parts) + ((" (%s)" % d) if d else "")


def _pick_order_row(rows, hint):
    """Choose the order row that best matches the caller's words (order #, service type, month)."""
    hint = (hint or "").lower()
    digits = re.sub(r"\D", "", hint)
    best, best_score = None, -1
    for r in rows:
        score = 0
        oid = re.sub(r"\D", "", r.get("ID") or "")
        if digits and oid and (digits in oid or oid in digits): score += 4
        svc = (r.get("Service") or "").lower()
        for w in ("storage", "return", "move", "delivery", "rental", "summer"):
            if w in hint and w in svc: score += 2
        d = _find_date(r.get("Date") or "")
        mo = _find_month(hint)
        if d and mo and d.month == mo: score += 3
        if score > best_score:
            best, best_score = r, score
    return best if best_score > 0 else rows[-1]      # no signal → most recent


def _build_order_result(name_heard: str, dispatch_rows, service_rows, order_hint: str = "") -> dict:
    def clean(s: str) -> str:
        return " ".join((s or "").split())

    # Build deduplicated name list from both sheets
    name_to_source: dict[str, str] = {}
    for row in dispatch_rows:
        n = clean(row.get("Student") or "")
        if n:
            name_to_source[n] = "dispatch"
    for row in service_rows:
        n = clean(row.get("Student Name") or "")
        if n:
            name_to_source.setdefault(n, "service")

    all_names = list(name_to_source.keys())

    if not all_names:
        return {"status": "error", "message": "No student records found in the system."}

    best, suggestions = smart_name_match(name_heard, all_names)

    if best is None:
        if suggestions:
            names_str = ", ".join(suggestions)
            return {
                "status": "confirm",
                "suggestions": suggestions,
                "message": f"I didn't find an exact match. Did you mean {names_str}?"
            }
        return {
            "status": "not_found",
            "suggestions": [],
            "message": "I couldn't find that name. Could you spell your last name for me?"
        }

    # Find the matching rows for the confirmed name
    confirmed = best
    confirmed_lower = confirmed.lower()

    d_rows = [r for r in dispatch_rows if clean(r.get("Student") or "").lower() == confirmed_lower]
    # distinct orders = distinct non-blank order IDs (repeat customers: storage + return, etc.)
    seen_ids, distinct = set(), []
    for r in d_rows:
        oid = clean(r.get("ID") or "")
        k = oid or ("row%d" % len(distinct))
        if oid and oid in seen_ids:
            continue
        seen_ids.add(k); distinct.append(r)

    order_choices = None
    if len(distinct) > 1:
        order_choices = [{"order_id": clean(r.get("ID") or ""), "service": clean(r.get("Service") or ""),
                          "date": clean(r.get("Date") or ""), "label": _order_label(r)} for r in distinct]
        dispatch_match = _pick_order_row(distinct, order_hint) if order_hint else distinct[-1]
    else:
        dispatch_match = d_rows[-1] if d_rows else None

    # pair the SERVICE row to the chosen order by Order# when possible, else most recent by name
    service_match = None
    want_id = clean((dispatch_match or {}).get("ID") or "").lstrip("#").strip()
    for row in service_rows:
        if clean(row.get("Student Name") or "").lower() == confirmed_lower:
            service_match = service_match or row
            sid = clean(row.get("Order#:") or row.get("Order #") or "").lstrip("#").strip()
            if want_id and sid and sid == want_id:
                service_match = row; break
            if not want_id:
                service_match = row   # keep iterating — last row = most recent

    # Pull all fields
    def val(row, *keys):
        if not row:
            return ""
        for k in keys:
            v = clean(row.get(k) or "")
            if v and v != "N/A":
                return v
        return ""

    order_id       = val(dispatch_match, "ID") or val(service_match, "Order#:")
    service        = val(dispatch_match, "Service") or val(service_match, "Service Type")
    building       = val(dispatch_match, "Building") or val(service_match, "Building")
    room           = val(dispatch_match, "Room") or val(service_match, "Room")
    address        = val(dispatch_match, "Address")
    date           = val(dispatch_match, "Date") or val(service_match, "Date")
    time_slot      = val(dispatch_match, "Time Slot")
    order_status   = val(dispatch_match, "Status")
    dispatch_status= val(dispatch_match, "Dispatch Status")
    truck          = val(dispatch_match, "Truck")
    kits           = val(dispatch_match, "Kits")
    product        = val(dispatch_match, "Product")
    phone          = val(dispatch_match, "Phone")
    invoice_id     = val(service_match, "Invoice ID")
    items_list     = val(service_match, "Summer Storage Item List")
    boxes          = val(service_match, "UTrucking Boxes")
    luggage        = val(service_match, "Luggage")
    other          = val(service_match, "Other")
    other_desc     = val(service_match, "Other Description")
    notes          = val(service_match, "Notes (heavy, oversized, unboxed)")
    date_completed = val(service_match, "Date of completion")
    pickup_completed = service_match is not None

    # Build available_fields list — only fields that actually have data
    available_fields = []
    if order_status or dispatch_status:
        available_fields.append("order status")
    if building or room or address:
        available_fields.append("pickup location")
    if date or time_slot:
        available_fields.append("scheduled date and time")
    if items_list or boxes or luggage or product:
        available_fields.append("stored items")
    if invoice_id:
        available_fields.append("invoice")
    if truck or dispatch_status:
        available_fields.append("dispatch info")
    if notes:
        available_fields.append("special notes")

    # Short summary message — agent reads this, then constructs the options offer itself
    summary_parts = [f"Got it — {confirmed}"]
    if order_id:
        summary_parts.append(f"order {order_id}")
    if service:
        summary_parts.append(service)
    message = ", ".join(summary_parts) + "."

    result = {
        "status": "found",
        "confirmed_name": confirmed,
        "message": message,
        "available_fields": available_fields,
        # All raw data for agent to answer follow-ups without another call
        "order_id": order_id,
        "service": service,
        "building": building,
        "room": room,
        "address": address,
        "date": date,
        "time_slot": time_slot,
        "order_status": order_status,
        "dispatch_status": dispatch_status,
        "truck": truck,
        "kits": kits,
        "product": product,
        "phone": phone,
        "invoice_id": invoice_id,
        "items_list": items_list,
        "boxes": boxes,
        "luggage": luggage,
        "other": other,
        "other_description": other_desc,
        "notes": notes,
        "date_completed": date_completed,
        "pickup_completed": pickup_completed,
    }
    if order_choices:
        result["order_count"] = len(order_choices)
        result["order_choices"] = order_choices
        # Different last-4 phones across the orders => almost certainly DIFFERENT people who happen
        # to share a name, not one repeat customer. Flagged so the redacted lookup doesn't list a
        # stranger's orders before the caller has proven who they are (the verifier picks the row).
        phones4 = {_last4(clean(r.get("Phone") or "")) for r in distinct}
        phones4.discard("")
        result["distinct_people"] = len(phones4) > 1
        if not order_hint:
            result["needs_order_choice"] = True
            result["message"] = ("Got it — %s. I found %d orders: %s. Which one do you mean?"
                                 % (confirmed, len(order_choices),
                                    "; ".join(c["label"] for c in order_choices[:4])))
    return result


def _extract_args(body: dict) -> dict:
    if "args" in body and isinstance(body["args"], dict):
        return body["args"]
    return body


def _staff_flag(request, args) -> bool:
    """True when a quote is being run in STAFF mode (?staff=1 or args.staff). Gates the truck-space
    estimate so it's attached only for staff, never on the customer-facing estimate."""
    return bool(args.get("staff")) or request.query_params.get("staff") == "1"


# ── Staff-key gate for PII / ops endpoints ──────────────────────────
# When API_SECRET is set in the environment, endpoints that return customer PII or
# internal ops data require the x-utrucking-key header. Unset = open (safe rollout:
# callers can start sending the header before enforcement is switched on).
API_SECRET = os.getenv("API_SECRET", "")


def _authorized(request) -> bool:
    return (not API_SECRET) or request.headers.get("x-utrucking-key", "") == API_SECRET


def _unauthorized():
    return JSONResponse({"status": "unauthorized",
                         "message": "This endpoint needs a valid staff key (x-utrucking-key header)."},
                        status_code=401)


@mcp.custom_route("/lookup_student", methods=["POST", "GET"])
async def lookup_student_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/lookup_student",
            "method": "POST",
            "expects": {"args": {"name_heard": "string", "order_hint": "optional - service or month if the caller has multiple orders", "phone": "optional - caller's phone number; if given without a name, the caller is identified by their number"}},
            "returns": {
                "status": "found | confirm | not_found | error",
                "confirmed_name": "exact name from records",
                "available_fields": ["order status", "pickup location", "..."],
                "verify_with": "the detail to ask the caller for before revealing anything",
                "note": "NO order details are returned here - call get_order_details after the caller verifies"
            }
        })
    if not _authorized(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    # PII is gated: lookup only confirms the name + says what to ask; details come from get_order_details
    return JSONResponse(_redact_lookup(await do_lookup_student(args.get("name_heard", ""), args.get("order_hint", ""), args.get("phone", ""))))


@mcp.custom_route("/get_order_details", methods=["POST", "GET"])
async def get_order_details_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/get_order_details",
            "method": "POST",
            "expects": {"args": {
                "name_heard": "string - the confirmed caller name",
                "answer": "string - the ONE detail the caller gave to prove it's them: building, phone last 4, or order number",
                "order_hint": "optional - service or month when they have multiple orders"}},
            "returns": {"status": "found | confirm | not_found | error",
                        "verified": "true | false",
                        "note": "full order details are returned ONLY when verified is true"}
        })
    if not _authorized(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    return JSONResponse(await do_get_order_details(
        args.get("name_heard", ""), args.get("answer", ""), args.get("order_hint", "")))


@mcp.custom_route("/debug_sheets", methods=["GET"])
async def debug_sheets(request: Request):
    if not _authorized(request):
        return _unauthorized()
    dispatch_rows, service_rows = await asyncio.gather(
        fetch_csv_rows(DISPATCH_CSV_URL),
        fetch_csv_rows(SERVICE_CSV_URL),
    )
    return JSONResponse({
        "dispatch_row_count": len(dispatch_rows),
        "dispatch_columns": list(dispatch_rows[0].keys()) if dispatch_rows else [],
        "dispatch_names": [r.get("Student", "") for r in dispatch_rows[:5]],
        "service_row_count": len(service_rows),
        "service_columns": list(service_rows[0].keys()) if service_rows else [],
        "service_names": [r.get("Student Name", "") for r in service_rows[:5]],
    })


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return HTMLResponse(_DASH_HTML)


@mcp.custom_route("/status", methods=["GET"])
async def status(request: Request):
    return JSONResponse({
        "service": "UTrucking MCP Server",
        "status": "running",
        "tools": ["/app", "/chat", "/estimate", "/ask", "/insights", "/lookup_student", "/health"],
    })


# ── brand assets (the real UTrucking logo, so the toolkit matches the official site) ──
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

def _serve_asset(fname, media):
    from starlette.responses import Response
    try:
        with open(os.path.join(_ASSETS_DIR, fname), "rb") as f:
            return Response(f.read(), media_type=media, headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        return JSONResponse({"error": "asset not found"}, status_code=404)

@mcp.custom_route("/brand/logo.jpg", methods=["GET"])
async def brand_logo(request: Request):
    return _serve_asset("ut-logo.jpg", "image/jpeg")

@mcp.custom_route("/brand/icon.png", methods=["GET"])
async def brand_icon(request: Request):
    return _serve_asset("ut-icon.png", "image/png")


@mcp.tool()
async def lookup_student(name_heard: str, order_hint: str = "", phone: str = "") -> str:
    """
    Look up a UTrucking student order by the name heard over the phone.
    Handles fuzzy/misspelled names. Returns a short message (name, order ID, service)
    plus all order fields so the agent can answer any follow-up question without
    calling another function. Also returns available_fields listing what data exists.
    If the student has multiple orders the result lists order_choices — pass the
    caller's answer (order #, service type, or month) back as order_hint.
    If you have the caller's phone number (e.g. from caller ID) and no name yet,
    pass it as phone to identify them by their number.
    Returns the confirmed name + which detail to verify, but NO order details — those come
    from get_order_details once the caller proves who they are.
    """
    return json.dumps(_redact_lookup(await do_lookup_student(name_heard, order_hint, phone)))


@mcp.tool()
async def get_order_details(name_heard: str, answer: str, order_hint: str = "") -> str:
    """Verify the caller, then reveal their order. Pass the confirmed name and the ONE detail the
    caller SAID to prove it's them — their building, the last 4 digits of their phone, or their
    order number. Returns the full order details only when verified is true; otherwise no details.
    Never pass a detail you already know — it must be what the caller just told you."""
    return json.dumps(await do_get_order_details(name_heard, answer, order_hint))


@mcp.custom_route("/verify_identity", methods=["POST", "GET"])
async def verify_identity_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/verify_identity",
            "method": "POST",
            "expects": {"args": {
                "name_heard": "string - the confirmed caller name",
                "answer": "string - the one detail the caller gave to prove it's them: building, phone last 4, or order number",
                "order_hint": "optional - order # / service / month when they have multiple orders"}},
            "returns": {"status": "found | confirm | not_found | error",
                        "verified": "true | false",
                        "confirmed_name": "exact name from records"}
        })
    if not _authorized(request):
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    return JSONResponse(await do_verify_identity(
        args.get("name_heard", ""), args.get("answer", ""), args.get("order_hint", "")))


@mcp.tool()
async def verify_identity(name_heard: str, answer: str, order_hint: str = "") -> str:
    """Verify a caller before revealing any order detail. Pass the confirmed name and the ONE
    detail they gave to prove it's them — their building, the last 4 digits of their phone, or
    their order number. Returns verified: true/false using the same tolerant matching as the
    chat (a slightly misspelled or partial building still passes; a wrong detail fails). Only
    read out order details when verified is true."""
    return json.dumps(await do_verify_identity(name_heard, answer, order_hint))


# ── Wave A/B/C engine endpoints ─────────────────────────────────────
@mcp.custom_route("/quote", methods=["POST", "GET"])
async def quote_endpoint(request: Request):
    """A) Instant itemized quote from a structured list or free text."""
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/quote", "method": "POST",
            "expects": {"args": {"items": [["UTrucking Box", 5], ["Mini Fridge", 1]],
                                 "text": "or free text e.g. 'five boxes and a mini fridge'"}},
            "returns": {"line_items": [], "total": 0, "unmatched": [], "summary": "string"},
        })
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    if not service_rows:
        return JSONResponse({"status": "error", "message": "Pricing catalog is unavailable right now."})
    book = build_price_book(service_rows)
    items = args.get("items")
    payload = ([(i[0], i[1]) for i in items] if isinstance(items, list)
               else (args.get("text") or args.get("name_heard") or ""))
    result = _quote_items(payload, book)
    result = await _ai_map_unmatched(result, book)
    _attach_upsell(result, _upsell_pairs(service_rows), book, _upsell_value(service_rows))
    if _staff_flag(request, args):        # staff-only truck-space estimate (never on the customer view)
        result["space"] = _space_estimate(result.get("line_items") or [], args.get("truck"))
    return JSONResponse(result)


@mcp.custom_route("/availability", methods=["POST", "GET"])
async def availability_endpoint(request: Request):
    """B) How busy a pickup date is + least-loaded nearby alternatives."""
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/availability", "method": "POST",
            "expects": {"args": {"date": "5/12/2026", "capacity": 100}},
            "returns": {"requested": {}, "alternatives": [], "suggestion": "string"},
        })
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    date = args.get("date") or args.get("requested_date") or ""
    capv = args.get("capacity")
    cap = int(capv) if capv not in (None, "") else None   # None -> use the crew-based schedule
    dispatch_rows = await fetch_csv_rows(DISPATCH_CSV_URL)
    return JSONResponse(_availability(dispatch_rows, date, capacity_per_day=cap))


@mcp.custom_route("/billing_audit", methods=["GET", "POST"])
async def billing_audit_endpoint(request: Request):
    """C) Flag $0 / missing-invoice / missing-order-id leakage across the service sheet."""
    if not _authorized(request):
        return _unauthorized()
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    return JSONResponse(_billing_audit(service_rows))


@mcp.tool()
async def get_quote(items_text: str) -> str:
    """Estimate a storage/moving quote from a free-text item description
    (e.g. 'five boxes, a mini fridge and two duffels'). Returns itemized lines + total."""
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    book = build_price_book(service_rows) if service_rows else {}
    result = _quote_items(items_text, book)
    if service_rows:
        _attach_upsell(result, _upsell_pairs(service_rows), book, _upsell_value(service_rows))
    return json.dumps(result)


@mcp.tool()
async def check_availability(date: str, capacity: int = 0) -> str:
    """Check how busy a pickup date is and suggest open nearby days (steers callers off peak days)."""
    dispatch_rows = await fetch_csv_rows(DISPATCH_CSV_URL)
    return json.dumps(_availability(dispatch_rows, date, capacity_per_day=(capacity or None)))


@mcp.tool()
async def business_insights() -> str:
    """UTrucking's aggregate business analytics: season revenue, demand by building/date,
    top items, pricing levers, upsell lift, funnel, repeat rate, data quality. Aggregate-only —
    contains NO individual customer data, so it is safe for any connected client."""
    d, s = await _load_rows()
    if not (d or s):
        return "No business data is available right now."
    return _metrics_brief(analytics.compute_metrics(d, s))


@mcp.custom_route("/dispatch_plan", methods=["POST", "GET"])
async def dispatch_plan_endpoint(request: Request):
    """B-ops: cluster a day's pickups by building + suggested crew split (route optimizer)."""
    if request.method == "GET":
        return JSONResponse({"endpoint": "/dispatch_plan", "method": "POST",
                             "expects": {"args": {"date": "5/7/2026"}}})
    if not _authorized(request):
        return _unauthorized()
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    dispatch_rows = await fetch_csv_rows(DISPATCH_CSV_URL)
    return JSONResponse(_dispatch_plan(dispatch_rows, args.get("date") or ""))


_VISION_PROMPT = (
    'List every storage/moving item visible in this photo as STRICT JSON only: '
    '{"items":[{"name":"UTrucking Box","qty":3}]}. Prefer these names when they fit: '
    "UTrucking Box, Plastic Container, Mini Fridge, Camp Duffel, Luggage, Rolling Cart, "
    "Bookshelf, Dresser, Headboard, Shoe Rack, Ottoman, Mattress. Output JSON only, no prose."
)

_CONDITION_PROMPT = (
    'You are documenting the condition of items being handed over for storage, for dispute '
    'protection at pickup. List each visible item as STRICT JSON only: '
    '{"items":[{"item":"Mini Fridge","condition":"good","notes":"small dent on left door"}]}. '
    'condition must be exactly one of: new, like-new, good, worn, damaged. '
    'notes: any visible scratches, dents, stains, tears or missing parts, or "no visible damage". '
    'Only report what is actually visible in the photo. Output JSON only, no prose.'
)

def _img_mime(b):
    """Sniff the real image type from magic bytes so we never mislabel a PNG/HEIC as JPEG."""
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if b[4:8] == b"ftyp": return "image/heic"   # iPhone HEIC/HEIF photos
    return "image/jpeg"


async def _post_retry(c, url, headers, payload, tries=3):
    """POST with a short backoff on transient 429/503 (providers occasionally rate-limit/overload)."""
    r = None
    for i in range(tries):
        r = await c.post(url, headers=headers, json=payload)
        if r.status_code in (429, 503) and i < tries - 1:
            await asyncio.sleep(1.5 * (i + 1)); continue
        break
    r.raise_for_status(); return r


# Each Gemini model has its OWN free-tier quota bucket, so when one is rate-limited
# the next usually isn't — a chain keeps /ask and /photo_quote alive through 429s.
_GEMINI_FALLBACKS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]


def _gemini_models():
    pref = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    return [pref] + [m for m in _GEMINI_FALLBACKS if m != pref]


async def _gemini_generate(key, parts, temp=None, json_out=False):
    """generateContent with retry + model fallback. Raises only if EVERY model fails.
    temp: set low (e.g. 0.1) for classification tasks that must be consistent run-to-run.
    json_out: force a pure-JSON response (no markdown fences / prose to strip)."""
    payload = {"contents": [{"parts": parts}]}
    cfg = {}
    if temp is not None: cfg["temperature"] = temp
    if json_out: cfg["responseMimeType"] = "application/json"
    if cfg: payload["generationConfig"] = cfg
    last = None
    async with httpx.AsyncClient(timeout=60.0) as c:
        for model in _gemini_models():
            try:
                r = await _post_retry(c,
                    "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                    {"x-goog-api-key": key}, payload)
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                last = e
                continue
    raise last


_MAP_PROMPT = ("You price a student storage service. Map EVERY unknown item to the closest CATALOG item by kind "
    "and size (a bed -> mattress; a baseball bat -> a skateboard-sized item; small/medium miscellaneous things like "
    "weights, boards, kitchenware -> 'other box' or 'crate'). A physical household/dorm item MUST always map to "
    "something — pick the nearest size/weight class even when imperfect. Map to null ONLY for things that are not "
    "storable objects at all (a pet, a person, food, gibberish). "
    "CATALOG: %s\nUNKNOWN: %s\nReply with STRICT JSON only, e.g. {\"baseball bat\": \"skateboard\", \"pet llama\": null}")


# Learned AI item-mappings, kept in-process so a repeat unknown resolves instantly and free
# (no second Gemini call). canon(unknown) -> catalog key. Warms up over the life of the worker;
# resets on redeploy/restart, like the sheet cache. Bounded so it can't grow without limit.
_AI_MAP_CACHE = {}
_AI_MAP_CACHE_MAX = 2000

def _ai_cache_recount(result):
    """Refresh the 'needs staff review' count after AI/approx lines settle (drives the #6 badge)."""
    review = sum(1 for l in result.get("line_items", []) if l.get("confidence") and l["confidence"] != "exact")
    if review:
        result["review_count"] = review
    else:
        result.pop("review_count", None)


async def _ai_map_unmatched(result, book):
    """Second-chance matching: map still-unmatched items to the catalog, price whatever we can, and
    show the mapping on the line ('matched from ...'). Repeat unknowns are served from a learned cache
    so they cost no model call; only genuinely-new unknowns hit Gemini. Never raises — on any failure
    the result is simply returned as-is."""
    import engines as _e
    allu = result.get("unmatched_items") or []
    todo = [(n, q) for n, q in allu if _e._canon(n) not in _e.NON_STORAGE]
    if not todo:
        return result
    key = os.getenv("GEMINI_API_KEY")

    # 1) serve anything we've mapped before straight from the cache — no model call for repeats
    resolved = {}                                        # name.lower() -> catalog key
    for n, _ in todo:
        ck = _AI_MAP_CACHE.get(_e._canon(n))
        if ck and ck in book:
            resolved[n.lower()] = ck
    need = [(n, q) for n, q in todo if n.lower() not in resolved]

    # 2) only the genuinely-new unknowns hit the model (skipped entirely if all cached, or no key)
    if need and key:
        async def _map_batch(names):
            txt = await _gemini_generate(key, [{"text": _MAP_PROMPT % (", ".join(sorted(book)), ", ".join(names))}],
                                         temp=0.1, json_out=True)
            m = re.search(r'\{.*\}', txt, re.S)
            raw = json.loads(m.group(0)) if m else {}
            return {str(k).strip().lower(): v for k, v in raw.items()}   # case/space-normalized keys
        try:
            mapping = await _map_batch([n for n, _ in need])
        except Exception:
            mapping = None
        if mapping is not None:
            # anything the first pass missed gets ONE targeted retry (models occasionally skip entries)
            missed = [n for n, _ in need if not isinstance(mapping.get(n.lower()), str)]
            if missed:
                try: mapping.update(await _map_batch(missed))
                except Exception: pass
            for n, _ in need:
                target = mapping.get(n.lower())
                k = _e.resolve_item(target, book) if isinstance(target, str) else None
                if k is not None:
                    resolved[n.lower()] = k
                    if len(_AI_MAP_CACHE) < _AI_MAP_CACHE_MAX:
                        _AI_MAP_CACHE[_e._canon(n)] = k          # learn it once, reuse free next time

    # 3) apply everything resolved (cache hits + fresh maps); leave the rest listed as not-priced
    still = [(n, q) for n, q in allu if _e._canon(n) in _e.NON_STORAGE]
    still_names = [n for n, _ in still]
    ai_pairs = []
    for name, qty in todo:
        k = resolved.get(name.lower())
        if k is None:
            still.append((name, qty)); still_names.append(name); continue
        price = book[k]; title = k.title()
        existing = next((l for l in result["line_items"] if l["item"] == title), None)
        if existing:                                    # merge into the existing line, don't emit a duplicate
            existing["qty"] += qty
            existing["amount"] = round(existing["unit_price"] * existing["qty"], 2)
        else:
            result["line_items"].append({"item": title, "qty": qty, "unit_price": round(price, 2),
                                         "amount": round(price * qty, 2), "matched_from": name,
                                         "ai_matched": True, "confidence": "ai"})
        result["total"] = round(result["total"] + price * qty, 2)
        ai_pairs.append({"from": name, "to": title})
    result["unmatched"] = still_names
    if still: result["unmatched_items"] = still
    else: result.pop("unmatched_items", None)
    matched = [{"from": l["matched_from"], "to": l["item"]} for l in result["line_items"] if l.get("matched_from")]
    for mp in ai_pairs:                                 # a map that merged into an existing line isn't on a line — keep it in the summary
        if not any(x["from"] == mp["from"] for x in matched):
            matched.append(mp)
    if matched: result["matched"] = matched
    else: result.pop("matched", None)
    _ai_cache_recount(result)
    return result


async def _vision_json(provider, key, img_b64, mime, prompt):
    """Run a vision prompt against the configured provider and return the parsed JSON object."""
    async with httpx.AsyncClient(timeout=60.0) as c:
        if provider == "groq":
            r = await _post_retry(c, "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": "Bearer " + key},
                {"model": "llama-3.2-90b-vision-preview", "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + img_b64}}]}]})
            txt = r.json()["choices"][0]["message"]["content"]
        elif provider == "anthropic":
            r = await _post_retry(c, "https://api.anthropic.com/v1/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                {"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}}]}]})
            txt = r.json()["content"][0]["text"]
        else:  # gemini (free tier at aistudio.google.com)
            # Model fallback chain: each model has its own free-tier quota bucket.
            # Key goes in a header, NOT the URL, so it can never leak into an error/log line.
            txt = await _gemini_generate(key, [{"text": prompt},
                {"inline_data": {"mime_type": mime, "data": img_b64}}])
    m = re.search(r'\{.*\}', txt, re.S)
    return json.loads(m.group(0)) if m else {}


async def _vision_items(provider, key, img_b64, mime="image/jpeg"):
    return (await _vision_json(provider, key, img_b64, mime, _VISION_PROMPT)).get("items", [])


async def _vision_condition(provider, key, img_b64, mime="image/jpeg"):
    return (await _vision_json(provider, key, img_b64, mime, _CONDITION_PROMPT)).get("items", [])


@mcp.custom_route("/photo_quote", methods=["POST", "GET"])
async def photo_quote_endpoint(request: Request):
    """A) Photo -> vision item detection -> itemized quote. Uses a FREE vision provider via env key."""
    if request.method == "GET":
        return JSONResponse({"endpoint": "/photo_quote", "method": "POST",
            "expects": {"args": {"image_url": "https://...", "image_base64": "...(alternative)"}},
            "env": {"VISION_PROVIDER": "gemini | groq | anthropic  (default gemini)",
                    "GEMINI_API_KEY": "free at aistudio.google.com",
                    "GEMINI_MODEL": "default gemini-2.5-flash"}})
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    provider = os.getenv("VISION_PROVIDER", "gemini").lower()
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"status": "not_configured",
            "message": "Photo quotes need a free vision key. Set GEMINI_API_KEY (free at aistudio.google.com)."})
    img_b64 = args.get("image_base64")
    raw = b""
    if not img_b64 and args.get("image_url"):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                # Some hosts (e.g. Wikimedia) 403 a request that has no browser User-Agent.
                resp = await c.get(args["image_url"],
                                   headers={"User-Agent": "Mozilla/5.0 (compatible; UTruckingBot/1.0)"})
            if resp.status_code != 200:
                return JSONResponse({"status": "error", "message": "Could not fetch image_url (HTTP %d)." % resp.status_code})
            raw = resp.content
            img_b64 = base64.b64encode(raw).decode()
        except Exception:
            return JSONResponse({"status": "error", "message": "Could not fetch image_url."})
    if not img_b64:
        return JSONResponse({"status": "error", "message": "Provide image_url or image_base64."})
    if not raw:
        try: raw = base64.b64decode(img_b64)
        except Exception: raw = b""
    mime = _img_mime(raw)
    try:
        detected = await _vision_items(provider, key, img_b64, mime)
    except Exception as e:
        # Never echo the API key back to a (public) caller, even if a provider puts it in an error.
        msg = str(e)[:200].replace(key, "***")
        return JSONResponse({"status": "error", "message": "Vision call failed: " + msg})
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    book = build_price_book(service_rows) if service_rows else {}
    pairs = [(d.get("name", ""), d.get("qty", 1)) for d in detected]
    extra_text = (args.get("text") or "").strip()
    if extra_text:
        # customer typed a clarification alongside the photo — their words win on overlap,
        # text-only items are added, and each line is tagged with where it came from
        merged, source_by_key = _merge_photo_text(pairs, extra_text, book)
        result = _quote_items(merged, book)
        for l in result.get("line_items", []):
            src = source_by_key.get(l["item"].lower())
            if src:
                l["source"] = src
    else:
        result = _quote_items(pairs, book)
    result = await _ai_map_unmatched(result, book)
    if service_rows:
        _attach_upsell(result, _upsell_pairs(service_rows), book, _upsell_value(service_rows))
    if _staff_flag(request, args):        # staff-only truck-space estimate (never on the customer view)
        result["space"] = _space_estimate(result.get("line_items") or [], args.get("truck"))
    result["detected"] = detected
    return JSONResponse(result)


async def _load_image_arg(args):
    """Resolve an image from {image_base64} or {image_url} -> (img_b64, mime, error_dict_or_None)."""
    img_b64 = args.get("image_base64"); raw = b""
    if not img_b64 and args.get("image_url"):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
                resp = await c.get(args["image_url"], headers={"User-Agent": "Mozilla/5.0 (UTrucking)"})
            if resp.status_code != 200:
                return None, None, {"status": "error", "message": "Could not fetch image_url (HTTP %d)." % resp.status_code}
            raw = resp.content; img_b64 = base64.b64encode(raw).decode()
        except Exception:
            return None, None, {"status": "error", "message": "Could not fetch image_url."}
    if not img_b64:
        return None, None, {"status": "error", "message": "Provide image_url or image_base64."}
    if not raw:
        try: raw = base64.b64decode(img_b64)
        except Exception: raw = b""
    return img_b64, _img_mime(raw), None


@mcp.custom_route("/condition_check", methods=["POST", "GET"])
async def condition_check_endpoint(request: Request):
    """Document item condition from a pickup photo (dispute protection + protection-plan upsell).
    Free vision via the same provider chain as /photo_quote."""
    if request.method == "GET":
        return JSONResponse({"endpoint": "/condition_check", "method": "POST",
            "expects": {"args": {"image_url": "https://...", "image_base64": "...(alternative)"}},
            "returns": {"items": [{"item": "Mini Fridge", "condition": "good", "notes": "small dent"}]}})
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    provider = os.getenv("VISION_PROVIDER", "gemini").lower()
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"status": "not_configured",
            "message": "Condition docs need a free vision key. Set GEMINI_API_KEY (free at aistudio.google.com)."})
    img_b64, mime, err = await _load_image_arg(args)
    if err:
        return JSONResponse(err)
    try:
        items = await _vision_condition(provider, key, img_b64, mime)
    except Exception as e:
        msg = str(e)[:200].replace(key, "***")
        return JSONResponse({"status": "error", "message": "Vision call failed: " + msg})
    return JSONResponse({"status": "ok", "items": items})


_CONDITION_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Condition Docs - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--ink:#121212;--mut:#696b85;--soft:#a0b3e3;--line:#e1e3e4;--head:#164899;--bg:#f1f2f8}
h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:18px 20px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange)}
header b{display:block;font-size:18px;letter-spacing:-.01em;margin-top:3px}header .s{display:block;color:var(--mut);font-size:12.5px;margin-top:3px}
main{max-width:760px;margin:0 auto;padding:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(16,24,40,.05)}
label.file{display:inline-block;background:var(--navy);color:#fff;border-radius:9px;padding:10px 16px;font-weight:600;cursor:pointer;font-size:14px}
input[type=file]{display:none}
button{background:var(--navy);color:#fff;border:0;border-radius:9px;padding:10px 16px;font-weight:600;cursor:pointer;font-family:inherit;font-size:14.5px}
button:hover{background:#0f3b80}
button.ghost{background:#eef1f5;color:var(--navy)}
img.prev{max-width:100%;max-height:230px;border-radius:10px;margin-top:10px;display:none;border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
th,td{text-align:left;padding:8px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.pill{display:inline-block;border-radius:20px;padding:2px 10px;font-size:12px;font-weight:600}
.c-new,.c-likenew{background:#e7f4ee;color:#0f7b4f}.c-good{background:#eaf0f8;color:#1e5aa8}
.c-worn{background:#fbf1dd;color:#8a6a1f}.c-damaged{background:#fbe9e7;color:#b42318}
.mut{color:var(--mut);font-size:12.5px}.err{color:#b42318}.stamp{color:var(--soft);font-size:12px;margin-top:8px}
@media print{header,.controls,label.file,button{display:none}body{background:#fff}}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Condition Docs</b><span class=s>Photograph an item at pickup - AI logs its condition for dispute protection (staff)</span></header>
<main>
 <div class=card>
  <label class=file>Choose / take a photo<input type=file id=f accept="image/*" capture=environment></label>
  <button onclick=go()>Document condition</button>
  <span class=mut id=msg></span>
  <img id=prev class=prev>
 </div>
 <div id=out></div>
</main><script>
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
var B64=null;
document.getElementById('f').addEventListener('change',function(e){
 var file=e.target.files[0];if(!file)return;var img=new Image();var rd=new FileReader();
 rd.onload=function(){img.onload=function(){
  var mx=1280,s=Math.min(1,mx/Math.max(img.width,img.height));
  var cv=document.createElement('canvas');cv.width=img.width*s;cv.height=img.height*s;
  cv.getContext('2d').drawImage(img,0,0,cv.width,cv.height);
  var d=cv.toDataURL('image/jpeg',0.85);B64=d.split(',')[1];
  var p=document.getElementById('prev');p.src=d;p.style.display='block';};img.src=rd.result;};
 rd.readAsDataURL(file);});
function cls(c){return 'c-'+String(c||'').toLowerCase().replace(/[^a-z]/g,'');}
async function go(){
 if(!B64){document.getElementById('msg').textContent='Pick a photo first.';return;}
 var m=document.getElementById('msg');m.textContent='Analyzing...';document.getElementById('out').innerHTML='';
 try{
  var r=await fetch('/condition_check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({args:{image_base64:B64}})});
  var j=await r.json();m.textContent='';render(j);
 }catch(e){m.innerHTML='<span class=err>Could not analyze - try again.</span>';}}
function render(j){
 if(!j||j.status==='error'){document.getElementById('out').innerHTML='<div class=card><span class=err>'+esc((j&&j.message)||'Something went wrong')+'</span></div>';return;}
 if(j.status==='not_configured'){document.getElementById('out').innerHTML='<div class=card><span class=err>Vision is not switched on yet (needs a free GEMINI_API_KEY).</span></div>';return;}
 var items=j.items||[];
 if(!items.length){document.getElementById('out').innerHTML='<div class=card><span class=mut>No items clearly identified. Try a closer, well-lit shot.</span></div>';return;}
 var rows=items.map(function(it){return '<tr><td>'+esc(it.item)+'</td><td><span class="pill '+cls(it.condition)+'">'+esc(it.condition||'-')+'</span></td><td>'+esc(it.notes||'')+'</td></tr>';}).join('');
 var stamp=new Date().toLocaleString();
 document.getElementById('out').innerHTML='<div class=card><h3 style="margin:0 0 8px;color:var(--navy)">Condition report</h3>'
  +'<table><thead><tr><th>Item</th><th>Condition</th><th>Notes</th></tr></thead><tbody>'+rows+'</tbody></table>'
  +'<div class=stamp>Documented '+esc(stamp)+'. Keep this with the order for dispute protection.</div>'
  +'<div style="margin-top:10px"><button class=ghost onclick=window.print()>Print / save</button></div></div>';}
</script></body></html>"""


@mcp.custom_route("/condition", methods=["GET"])
async def condition_page(request: Request):
    return HTMLResponse(_CONDITION_HTML)


# ── Customer-facing instant-estimate page (photo OR text) ───────────
_ESTIMATE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>UTrucking - Instant Storage Estimate</title>
<style>
 @import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;--line:#e1e3e4;--bg:#f1f2f8}
 h1,h2,h3,h4,header h1{font-family:'Inclusive Sans','Inter',sans-serif}
 *{box-sizing:border-box} body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);-webkit-font-smoothing:antialiased}
 .bar{height:3px;background:var(--orange)}
 header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:20px}
 header .ey{text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange)}
 header h1{margin:4px 0 0;font-size:22px;font-weight:680;letter-spacing:-.02em;color:var(--head)} header p{margin:6px 0 0;color:var(--mut);font-size:14px}
 .cardh{display:flex;align-items:center;gap:9px}
 .cardh svg{width:19px;height:19px;stroke:var(--navy);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:none}
 main{max-width:640px;margin:0 auto;padding:18px 16px 60px}
 .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 1px 2px rgba(16,24,40,.05)}
 .card h2{margin:0 0 4px;font-size:16px;color:var(--head);font-weight:640} .card .hint{margin:0 0 12px;color:var(--mut);font-size:13px}
 textarea{width:100%;min-height:72px;border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;resize:vertical;color:var(--ink)}
 textarea:focus{outline:none;border-color:#b9c2cf;box-shadow:0 0 0 3px rgba(15,37,68,.08)}
 .btn{background:var(--navy);color:#fff;border:0;border-radius:10px;padding:12px 18px;font-weight:600;font-size:15px;cursor:pointer;margin-top:10px}
 .btn:hover{background:#0f3b80} .btn:active{transform:translateY(1px)} .file{display:block;margin-top:6px;font:inherit}
 .or{text-align:center;color:var(--soft);font-size:12px;margin:6px 0;text-transform:uppercase;letter-spacing:.12em}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
 th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 td.n,th.n{text-align:right}
 .total{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:12px;border-top:2px solid var(--navy)}
 .total .lbl{font-weight:700;color:var(--navy)} .total .amt{font-weight:700;font-size:22px;color:var(--navy)}
 .note{color:var(--mut);font-size:12px;margin-top:10px} .err{color:#b42318;font-size:14px;margin-top:8px}
 .spin{color:var(--mut);font-size:14px;margin-top:8px} #result{display:none}
 .tag{display:inline-block;background:#eef1f5;color:var(--navy);border-radius:20px;padding:3px 10px;font-size:12px;margin:3px 4px 0 0}
 .upsell{margin-top:12px;padding:10px 12px;background:#eef2fb;border:1px solid #d3e0f5;border-radius:10px}
 .upsell .uplbl{font-weight:600;color:var(--navy);font-size:12.5px;margin-right:4px}
 .staffchip{display:inline-block;margin-top:10px;background:var(--navy);color:#fff;border-radius:20px;padding:4px 11px;font-size:11.5px;font-weight:600;letter-spacing:.02em}
 .staffbox{margin-top:12px;padding:13px 14px;background:#0b2154;color:#fff;border-radius:10px}
 .staffbox .sh{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#a8c4e6;font-weight:600}
 .staffbox .big{font-size:17px;font-weight:700;margin-top:5px}
 .staffbox .sub{font-size:12px;color:#c6d2e2;margin-top:7px;line-height:1.5}
 .staffbox .trucksel{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
 .staffbox .tbtn{background:rgba(255,255,255,.10);color:#c6d2e2;border:1px solid rgba(255,255,255,.24);border-radius:8px;padding:6px 12px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
 .staffbox .tbtn:hover{background:rgba(255,255,255,.17)}
 .staffbox .tbtn.on{background:var(--orange);border-color:var(--orange);color:#fff}
 .staffbox .truckread{margin-top:10px;font-size:14px;color:#fff}
 .staffbox .truckread .cap{color:#c6d2e2;font-size:12.5px}
 .rev{display:inline-block;background:#fef0c7;color:#8a6a1f;border:1px solid #f0d48a;border-radius:6px;padding:1px 6px;font-size:10.5px;font-weight:600;margin-left:6px;vertical-align:middle}
 .revsum{margin-top:10px;padding:8px 11px;background:#fffaf0;border:1px solid #f0d48a;border-radius:8px;color:#8a6a1f;font-size:12.5px}
</style></head><body>
<div class="bar"></div>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:21px;width:auto;display:block;margin-bottom:7px">
 <h1>Instant Storage &amp; Moving Estimate</h1>
 <p>Snap a photo of your stuff or type what you have - get a price in seconds.</p></header>
<main>
 <div class="card"><h2 class=cardh><svg viewBox="0 0 24 24"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.4"/></svg>Photo (optional)</h2>
  <p class="hint">Take or upload one photo of your items &mdash; we detect and price them automatically.</p>
  <input id="photo" class="file" type="file" accept="image/*" capture="environment">
  <p class="hint" id="photostate" style="margin:8px 0 0"></p></div>
 <div class="card"><h2 class=cardh><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h10M4 18h13"/></svg>Description (optional)</h2>
  <p class="hint">e.g. "five boxes, a mini fridge and two duffels". <b>Using both?</b> We combine them &mdash; your typed counts override the photo, and anything you type that isn't in the photo gets added.</p>
  <textarea id="items" placeholder="Tell us what you are storing, or add details the photo misses..."></textarea>
  <button class="btn" onclick="quoteNow()">Get my estimate</button></div>
 <div class="card" id="result"><h2>Your estimate</h2><div id="detected"></div><div id="body"></div></div>
</main>
<script>
 const $=id=>document.getElementById(id);
 var STAFF=location.search.indexOf('staff=1')>=0;   /* /estimate?staff=1 -> show truck-space planning */
 if(STAFF){document.querySelector('header').insertAdjacentHTML('beforeend','<div class=staffchip>Staff mode &middot; truck-space estimate on</div>');}
 async function postJSON(p,d){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});return r.json();}
 function toB64(f){return new Promise(res=>{const fr=new FileReader();
  fr.onload=()=>{const url=String(fr.result);const img=new Image();
   img.onload=()=>{try{let w=img.width,h=img.height;const s=Math.min(1,1600/Math.max(w,h));w=Math.round(w*s);h=Math.round(h*s);
     const cv=document.createElement('canvas');cv.width=w;cv.height=h;cv.getContext('2d').drawImage(img,0,0,w,h);
     res(cv.toDataURL('image/jpeg',0.85).split(',')[1]);}catch(e){res(url.split(',')[1]);}};
   img.onerror=()=>res(url.split(',')[1]);img.src=url;};
  fr.onerror=()=>res('');fr.readAsDataURL(f);});}
 function show(h){$('result').style.display='block';$('body').innerHTML=h;$('result').scrollIntoView({behavior:'smooth'});}
 function loading(m){$('detected').innerHTML='';show('<div class=spin>'+m+'</div>');}
 /* staff truck selector: click Sprinter / 26-ft U-Haul to recalibrate the % this load fills */
 function renderTruck(k){var S=window.__space;if(!S||!S.trucks[k])return;var t=S.trucks[k];
  var rd=$('truckread');if(rd)rd.innerHTML='&#8776; <b>'+t.pct+'% of the '+t.label+'</b>'+(t.loads>=1?' &middot; '+t.loads+' loads':'')+' <span class=cap>('+Number(t.cuft).toLocaleString()+' cu ft cargo)</span>';
  var sel=$('trucksel');if(sel)Array.prototype.forEach.call(sel.querySelectorAll('.tbtn'),function(b){b.classList.toggle('on',b.getAttribute('data-k')===k);});}
 function wireTruck(){var S=window.__space;var sel=$('trucksel');if(!S||!sel)return;
  Array.prototype.forEach.call(sel.querySelectorAll('.tbtn'),function(b){b.addEventListener('click',function(){renderTruck(b.getAttribute('data-k'));});});
  renderTruck(S.default||'sprinter');}
 function render(data,fromPhoto){
  if(!data||data.status==='error'){show('<div class=err>Sorry - '+((data&&data.message)||'something went wrong')+'</div>');return;}
  if(data.status==='not_configured'){show('<div class=err>Photo estimates are not switched on yet. Try the text box above.</div>');return;}
  const li=data.line_items||[]; const un=data.unmatched||[];
  const ex='We price things like boxes, mini fridges, duffels, TVs, desks, couches, mattresses, dressers and bikes.';
  let det='';
  if(fromPhoto){const items=(data.detected||[]);
   det=items.length?'<p class=hint>We spotted: '+items.map(d=>'<span class=tag>'+(d.qty||1)+"x "+(d.name||'item')+'</span>').join('')+'</p>':'';}
  $('detected').innerHTML=det;
  if(!li.length){
   if(un.length) show('<div class=err>We could not find a price for: '+un.join(', ')+'.</div><p class=note>'+ex+' Try naming those, or call (314) 266-8878.</p>');
   else if(fromPhoto) show('<div class=err>We could not clearly identify items in that photo.</div><p class=note>Try a clearer, well-lit shot, or use the text box. '+ex+'</p>');
   else show('<div class=err>Tell us what you are storing.</div><p class=note>'+ex+'</p>');
   return;
  }
  const srcLbl={photo:'from photo',you:'you added','photo+you':'photo &middot; your count'};
  let rows=li.map(x=>'<tr><td>'+x.qty+"x "+x.item
   +(x.matched_from?' <span style="color:#5b6b7f;font-size:.82em">(you said &ldquo;'+x.matched_from+'&rdquo;)</span>':'')
   +(x.source?' <span class=tag style="font-size:11px">'+srcLbl[x.source]+'</span>':'')
   +((STAFF&&x.confidence&&x.confidence!=='exact')?' <span class=rev title="lower-confidence match — check it">'+(x.confidence==='ai'?'AI match':'approx')+'</span>':'')
   +'</td><td class=n>$'+Number(x.amount).toFixed(2)+'</td></tr>').join('');
  let revsum=(STAFF&&data.review_count)?'<div class=revsum>&#9888; '+data.review_count+' line'+(data.review_count>1?'s':'')+' matched approximately or by AI &mdash; worth a quick check before you quote it.</div>':'';
  let extra=un.length?'<p class=note>Not priced (call us for these): '+un.join(', ')+'.</p>':'';
  if(data.capped) extra+='<p class=note>For more than '+data.capped+' of one item, call (314) 266-8878 for a bulk quote.</p>';
  let up='';
  if(data.upsell&&data.upsell.items&&data.upsell.items.length){
   up='<div class=upsell><span class=uplbl>Most people also add</span> '
     +data.upsell.items.map(function(it){return '<span class=tag>'+it.item+' &middot; $'+Number(it.unit_price).toFixed(0)+'</span>';}).join(' ')+'</div>';
  }
  let sp='';
  if(STAFF&&data.space&&data.space.trucks){var s=data.space;window.__space=s;
   var tbtns=Object.keys(s.trucks).map(function(k){return '<button type=button class=tbtn data-k="'+k+'">'+s.trucks[k].label+'</button>';}).join('');
   sp='<div class=staffbox><div class=sh>Staff &middot; space &amp; truck estimate</div>'
     +'<div class=big>&#8776; '+s.cubic_ft+' cu ft &middot; &#8776; '+s.box_equiv+" boxes&rsquo; worth</div>"
     +'<div class=trucksel id=trucksel>'+tbtns+'</div>'
     +'<div class=truckread id=truckread></div>'
     +'<div class=sub>Pick the truck to see how much of it this load fills &mdash; planning figure only, not shown to customers.</div></div>';
  }
  let html='<table><thead><tr><th>Item</th><th class=n>Est.</th></tr></thead><tbody>'+rows+'</tbody></table>'
   +revsum
   +'<div class=total><span class=lbl>Estimated total</span><span class=amt>$'+Number(data.total||0).toFixed(2)+'</span></div>'
   +up+sp+extra
   +'<p class=note>Instant estimate based on typical UTrucking pricing. Final price is confirmed at pickup. Ready to book? Call (314) 266-8878 and mention your estimate.</p>';
  show(html);
  if(STAFF&&data.space&&data.space.trucks) wireTruck();
 }
 let photoB64=null;
 async function quoteNow(){
  const t=$('items').value.trim();
  if(photoB64){loading(t?'Combining your photo and notes...':'Looking at your photo...');
   try{const args={image_base64:photoB64};if(t)args.text=t;if(STAFF)args.staff=true;render(await postJSON('/photo_quote',{args:args}),true);}
   catch(e){show('<div class=err>Network error. Please try again.</div>');}
   return;}
  if(!t){show('<div class=err>Add a photo or tell us what you are storing.</div>');return;}
  loading('Pricing your items...');
  try{render(await postJSON('/quote',{args:{text:t,staff:STAFF}}),false);}catch(e){show('<div class=err>Network error. Please try again.</div>');}}
 $('photo').addEventListener('change',async e=>{const f=e.target.files[0];if(!f){photoB64=null;$('photostate').textContent='';return;}
  $('photostate').textContent='Reading photo...';
  try{photoB64=await toB64(f);$('photostate').innerHTML='&#10003; Photo attached &mdash; add any notes below, then hit the button (or we quote it now).';quoteNow();}
  catch(err){photoB64=null;show('<div class=err>Could not process that photo. Try another or use the text box.</div>');}});
</script></body></html>"""


@mcp.custom_route("/estimate", methods=["GET"])
async def estimate_page(request: Request):
    """Customer-facing instant-estimate mini-app: upload a photo OR type items -> price."""
    return HTMLResponse(_ESTIMATE_HTML)


# ── Conversational brain for the /chat SMS preview (reuses engines + identity-gated lookup) ──
_CHAT_MENU = ("Hi! I'm the UTrucking assistant. I can:\n"
              "• Quote items — \"quote 5 boxes and a mini fridge\"\n"
              "• Check pickup dates — \"what days are open?\" or \"is 5/12 available?\"\n"
              "• Look up your order — \"where's my order?\"\nWhat do you need?")
_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NAMES = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
                "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _fmt_day(dstr):
    try:
        y, m, d = map(int, str(dstr).split("-")); return "%s %d" % (_MON[m], d)
    except Exception:
        return str(dstr)


def _find_date(text):
    m = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", text)
    if m:
        try:
            mo, da = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else 2026
            if y < 100: y += 2000
            return datetime.date(y, mo, da)
        except Exception:
            return None
    m = re.search(r"([a-zA-Z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?", text)
    if m and m.group(1).lower() in _MONTH_NAMES:
        try:
            return datetime.date(2026, _MONTH_NAMES[m.group(1).lower()], int(m.group(2)))
        except Exception:
            return None
    return None


_MAY_MODAL = re.compile(
    r"\bmay\s+(i|we|you|he|she|it|they|the|this|that|not|be|been|being|have|has|had|"
    r"need|want|wish|just|also|please|get|see|ask|know|help|speak|talk|come|call)\b", re.I)

def _find_month(text):
    for name, mo in _MONTH_NAMES.items():
        if re.search(r"\b" + name + r"\b", text, re.I):
            # "may" is also the English modal verb — don't read "May I…" / "I may need…" as the
            # month May and hijack the request. "May 12" still parses through _find_date.
            if name == "may" and _MAY_MODAL.search(text):
                continue
            return mo
    return None


def _last4(s):
    ds = re.sub(r"\D", "", s or "")
    return ds[-4:] if len(ds) >= 4 else ""


def _cv(x):
    """Drop placeholder values so the reveal reads cleanly."""
    x = (x or "").strip()
    return "" if x.lower() in ("(no date)", "n/a", "na", "-", "tbd", "none", "null") else x


def _pretty_items(s):
    """Turn the sheet's machine item string
    'UTrucking Box (Amount: 22.00 USD, Quantity: 4); Mattress (Amount: 33.00 USD, Quantity: 1)'
    into a human 'UTrucking Box x4, Mattress x1'. Falls back to the raw text if nothing parses."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        import engines as _e
        parts = _e._ITEM_RE.findall(s)
    except Exception:
        parts = []
    if not parts:
        return s
    out = []
    for name, _amt, qty in parts:
        nm = " ".join(name.split())
        out.append("%s x%s" % (nm, qty) if qty and qty != "1" else nm)
    return ", ".join(out)


def _reveal_order(rec):
    out = ["You're verified. Here's your order:"]
    st = _cv(rec.get("order_status")) or _cv(rec.get("dispatch_status"))
    if st: out.append("• Status: " + st)
    when = " ".join(x for x in [_cv(rec.get("date")), _cv(rec.get("time_slot"))] if x)
    where = " ".join(x for x in [_cv(rec.get("building")), _cv(rec.get("room"))] if x)
    if when or where:
        out.append("• Pickup: " + (when or "date TBD") + (" at " + where if where else ""))
    items = _pretty_items(_cv(rec.get("items_list"))) or _cv(rec.get("product")) or _cv(rec.get("boxes"))
    if items: out.append("• Items: " + items[:200])
    if _cv(rec.get("invoice_id")): out.append("• Invoice: " + rec["invoice_id"])
    if _cv(rec.get("order_id")): out.append("• Order #: " + rec["order_id"])
    out.append("Anything else?")
    return "\n".join(out)


# Brute-force guard on identity verification: a script shouldn't be able to loop building
# names against a target. In-memory (resets on redeploy) — raises the bar, cheap, no deps.
_VERIFY_FAILS = {}                 # canonical name -> [fail_count, first_fail_epoch]
_VERIFY_MAX, _VERIFY_WINDOW = 5, 15 * 60

# Second layer: per-IP. Stops one machine from rotating through MANY names.
_IP_FAILS = {}                     # ip -> [fail_count, first_fail_epoch]
_IP_MAX, _IP_WINDOW = 15, 60 * 60


def _ip_locked(ip):
    ent = _IP_FAILS.get(ip)
    if not ent:
        return False
    if time.time() - ent[1] > _IP_WINDOW:
        _IP_FAILS.pop(ip, None); return False
    return ent[0] >= _IP_MAX


def _ip_fail(ip):
    ent = _IP_FAILS.setdefault(ip, [0, time.time()])
    ent[0] += 1


def _verify_locked(name):
    ent = _VERIFY_FAILS.get(name)
    if not ent:
        return False
    if time.time() - ent[1] > _VERIFY_WINDOW:
        _VERIFY_FAILS.pop(name, None); return False
    return ent[0] >= _VERIFY_MAX


def _verify_fail(name):
    ent = _VERIFY_FAILS.setdefault(name, [0, time.time()])
    ent[0] += 1


def _norm_id(s):
    """Order-number normaliser: keep alphanumerics only. '#13851-SS' -> '13851ss'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _id_matches(text, order_id):
    """True if the caller's answer names their order number — tolerant of the '#', the
    '-SS' suffix, and giving just the digits ('13851' vs '#13851-SS')."""
    t, o = _norm_id(text), _norm_id(order_id)
    if len(t) < 4 or not o:
        return False
    if t in o or o in t:
        return True
    dt, do = re.sub(r"\D", "", t), re.sub(r"\D", "", o)   # digit cores
    return len(dt) >= 4 and dt == do


# filler / number / prompt words that must never, on their own, satisfy a building check —
# otherwise a non-building sentence like "my last four are 3851" could fuzzy-match a building word.
_BLD_STOP = {
    "the", "my", "your", "our", "for", "and", "are", "its", "was", "were", "this", "that", "here",
    "please", "tell", "call", "give", "last", "digit", "digits", "number", "numbers", "order",
    "phone", "cell", "building", "pickup", "dorm", "hall", "room", "yes", "yeah", "okay", "sure",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "hundred", "thousand", "confirm", "verify", "account", "info", "detail", "details", "status",
}

def _building_matches(text, building):
    """True if the caller's answer plausibly names their pickup building — tolerant of
    misspellings, a missing/extra section letter, and abbreviations, but WITHOUT letting an
    unrelated sentence (e.g. a phone-number answer) sneak through on a coincidental word."""
    b = re.sub(r"[^a-z0-9]+", " ", (building or "").lower()).strip()
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not b or len(t) < 3:
        return False
    b_tokens, t_tokens = b.split(), t.split()
    if t == b or t in b:                                  # exact, or answer is part of the building name
        return True
    b_core = [w for w in b_tokens if len(w) >= 3]         # caller said the whole building name (+ maybe extra)
    if b_core and set(b_core).issubset(set(t_tokens)):
        return True
    # whole-string fuzzy ONLY for a short, building-like answer — a long sentence must not
    # fuzzy-match a short building name
    if len(t_tokens) <= 3 and difflib.SequenceMatcher(None, t, b).ratio() >= 0.8:
        return True
    # token level: a SUBSTANTIVE answer word (alphabetic, >=4 chars, not a filler/number word)
    # closely matching a building word — the optional section letter stays optional
    bt = [w for w in b_tokens if len(w) >= 4 and w.isalpha()]
    tt = [w for w in t_tokens if len(w) >= 4 and w.isalpha() and w not in _BLD_STOP]
    return any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.85 for x in tt for y in bt)


def _verify_answer(rec, text):
    """Does the caller's answer prove identity for this record? Shared by the web chat's
    verify step AND the phone agent's verify_identity tool, so both accept the SAME three
    things — a fuzzy/partial building, the phone's last 4 digits, or the order number."""
    if _building_matches(text, rec.get("building")):
        return True
    if rec.get("phone") and _last4(text) and _last4(text) == _last4(rec["phone"]):
        return True
    if _id_matches(text, rec.get("order_id")):
        return True
    return False


def _lookup_flow(text, state, dispatch_rows, service_rows):
    if state.get("step") == "verify":
        nm = " ".join((state.get("name") or "").lower().split())
        if _verify_locked(nm):
            return ("Too many verification attempts for that name. For security, please call the team at (314) 266-8878.", {})
        # accept ANY on-file identifier, each fuzzy-tolerant: building (misspelled/partial), phone
        # last-4, or the order number — checked against EVERY order under this name so a shared name
        # reveals only the order the caller can verify (the SAME check the phone agent runs).
        match, base = _verify_pick(state.get("name", ""), text, dispatch_rows, service_rows, state.get("hint", ""))
        if base.get("status") != "found":
            return ("Sorry, I lost that record — what's the name again?", {"intent": "lookup", "step": "name"})
        if match is not None:
            _VERIFY_FAILS.pop(nm, None)
            return (_reveal_order(match), {})
        _verify_fail(nm)
        return ("That doesn't match what we have, so I can't share the order details. Please call the team at (314) 266-8878.", {})
    if state.get("step") == "order":
        # repeat customer picked which order ("the storage one", "the August return", an order #)
        rec = _build_order_result(state.get("name", ""), dispatch_rows, service_rows, text)
        if rec.get("status") != "found":
            return ("Sorry, I lost that record — what's the name again?", {"intent": "lookup", "step": "name"})
        ask = ("what building is your pickup at" if rec.get("building")
               else ("the last 4 digits of your phone" if rec.get("phone") else "your order number"))
        return ("Got it — %s. To confirm it's you, %s?" % (_cv(rec.get("service")) or "that order", ask),
                {"intent": "lookup", "step": "verify", "name": rec["confirmed_name"], "hint": text})
    rec = _build_order_result(text, dispatch_rows, service_rows)
    if rec.get("status") == "found":
        ask = ("what building is your pickup at" if rec.get("building")
               else ("the last 4 digits of your phone" if rec.get("phone") else "your order number"))
        # One person with several of their OWN orders: list them by SERVICE + DATE only — never the
        # order number (it is itself a valid verifier) and never for DIFFERENT people who share a
        # name (that would disclose a stranger's order). Mirrors the phone's redacted lookup
        # (_redact_lookup) so chat and voice reveal exactly the same before identity is proven.
        if rec.get("needs_order_choice") and not rec.get("distinct_people"):
            labels = "; ".join(
                " ".join(x for x in [c.get("service", ""),
                                     ("(" + c["date"] + ")") if c.get("date") else ""] if x)
                for c in rec["order_choices"][:4])
            return ("I found %d orders under %s: %s. Which one do you mean?"
                    % (rec["order_count"], rec["confirmed_name"], labels),
                    {"intent": "lookup", "step": "order", "name": rec["confirmed_name"]})
        # Single order, OR different people sharing a name: go straight to verify. _verify_pick then
        # reveals only the order the caller can actually prove, and refuses when a weak verifier
        # (e.g. a shared building) can't tell shared-name people apart — same as the phone.
        return ("I found an order under %s. To confirm it's you, %s?" % (rec["confirmed_name"], ask),
                {"intent": "lookup", "step": "verify", "name": rec["confirmed_name"]})
    if rec.get("status") == "confirm" and rec.get("suggestions"):
        return ("I found a few possible matches: %s. Which name is exactly right?" % ", ".join(rec["suggestions"]),
                {"intent": "lookup", "step": "name"})
    return ("I couldn't find an order under that name. Want to try spelling the last name, or a different name?",
            {"intent": "lookup", "step": "name"})


_RE_GREET = re.compile(r"^(hi|hey|hello|help|menu|start|hola|yo|sup|good (morning|afternoon|evening))\b", re.I)
_RE_HOURS = re.compile(r"\b(hours?|located|location|address|where are you|contact|human|representative|talk to|speak to|reach you|phone number)\b", re.I)
_RE_LOOKUP = re.compile(r"\b(my order|order status|status of|where.?s my|where is my|track|my stuff|my pickup|my booking|look ?up|account|invoice|balance|do i owe|did you (?:pick|get))\b", re.I)
_RE_LIST = re.compile(r"\b(other|another|others|list|what days|which days|when are|any (?:other )?day|days? (?:are )?(?:open|available|free)|options|else)\b", re.I)
_RE_AVAIL = re.compile(r"\b(available|availab|book|booking|pickup|pick up|schedul|slot|reschedul|move-?out)\b", re.I)
# Account CHANGES the assistant does NOT do itself (cancel / reschedule / change a detail / add-remove
# items / email-or-text my details) — routed to the team, mirroring the phone agent's v42 prompt so
# chat and voice handle these identically. Matched BEFORE lookup so "cancel my pickup" isn't taken as
# an order-status request. A read-only "my order status" has no change verb, so it still reaches lookup.
_RE_ACCT_CHANGE = re.compile(
    r"\b(cancel\w*|reschedul\w*|re-?schedul\w*|change|edit|update|modif\w*|correct)\b[\w\s',.-]*?"
    r"\b(order|pickup|pick ?up|booking|appointment|reservation|address|date|time ?slot|room|detail\w*)\b"
    r"|\b(e-?mail|text|send)\b[\w\s',.-]*?\b(me|my)\b[\w\s',.-]*?"
    r"\b(order|detail\w*|invoice|info|receipt|confirmation|summary)\b"
    r"|\b(add|remove|take off)\b[\w\s',.-]*?\b(item\w*|box\w*)\b[\w\s',.-]*?\b(order|to my|from my)\b", re.I)

# A bare name looks like "Firstname Lastname" (2-3 alpha words) and isn't a command/courtesy word.
_NAME_STOP = {"thanks", "thank", "you", "please", "yes", "no", "nope", "yeah", "yep", "ok", "okay",
              "sure", "hello", "hey", "hi", "the", "and", "for", "what", "how", "when", "where",
              "who", "why", "cool", "great", "good", "fine", "help", "menu", "quote", "order",
              "status", "pickup", "box", "boxes", "fridge", "desk", "info", "hours",
              "i", "am", "is", "it", "an", "to", "of", "in", "on", "at", "my", "me", "we",
              "us", "he", "she", "they", "do", "did", "can", "will", "u", "im"}
# 2-3 tokens; a single-letter token is allowed only as a middle initial ("John A Smith").
_RE_NAMEISH = re.compile(r"^[a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]*){1,2}$", re.I)


def _looks_like_name(text):
    """Cheap gate: does this read like a person's name (2-3 alpha words, no command words)?
    Used so a caller who just types their name is taken into the order-lookup flow."""
    t = " ".join((text or "").split())
    if not (3 <= len(t) <= 40) or not _RE_NAMEISH.match(t):
        return False
    return not any(w.lower() in _NAME_STOP for w in t.split())


def _attach_upsell(result, upsell, book, lift=None, max_items=2):
    """Attach a data-driven upsell to a quote result: the priced items most often stored
    alongside what's already in the cart (and not already in it), learned from real baskets.
    When `lift` (item -> avg basket $) is supplied the ranking is VALUE-WEIGHTED — co-occurrence
    stays the relevance filter but each candidate is scored by how much it grows a typical order,
    so a high-lift add-on (the rolling cart) beats a merely-frequent one (a mini fridge). Falls back
    to pure co-occurrence when no lift is given. Sets result['upsell'] = {items:[{item,unit_price}],
    line:'...'} — used by the phone JSON, the estimate page, and the chat/voice reply."""
    if not upsell or not result.get("line_items"):
        return result
    import engines as _e
    have = {_e._canon(l["item"]) for l in result["line_items"]}
    default_w = (sum(lift.values()) / len(lift)) if lift else 1.0   # avg basket $ for partners we can't size
    score = {}
    for l in result["line_items"]:
        for partner, cnt in (upsell.get(_e._canon(l["item"])) or [])[:8]:
            if partner in have or partner in _e.NON_STORAGE or partner not in book:
                continue
            w = lift.get(partner, default_w) if lift else 1.0
            score[partner] = score.get(partner, 0) + cnt * w
    if not score:
        return result
    top = sorted(score, key=lambda p: (-score[p], p))[:max_items]
    items = [{"item": p.title(), "unit_price": round(book[p], 2)} for p in top]
    if len(items) == 1:
        line = "Most people also add a %s (about $%.0f) — want it on there?" % (items[0]["item"], items[0]["unit_price"])
    else:
        line = "Most people also add a %s or %s — want either on there?" % (items[0]["item"], items[1]["item"])
    result["upsell"] = {"items": items, "line": line}
    return result


def _quote_reply_text(q):
    """Human-readable quote reply — shared by the chat brain and the AI-mapped re-render."""
    def _fmt(l):
        s = "• %dx %s — $%.2f" % (l["qty"], l["item"], l["amount"])
        if l.get("matched_from"):
            s += " (matched from \"%s\")" % l["matched_from"]
        return s
    lines = "\n".join(_fmt(l) for l in q["line_items"])
    um = q.get("unmatched") or []
    ums = ("\n(Couldn't price: %s — call us for those.)" % ", ".join(um)) if um else ""
    if q.get("capped"):
        ums += "\n(For more than %d of one item, call (314) 266-8878 for a bulk quote.)" % q["capped"]
    up = (q.get("upsell") or {}).get("line") or ""
    ups = ("\n" + up) if up else ""
    return "Here's your estimate:\n%s\nTotal: about $%.2f.%s%s\nWant a pickup date?" % (lines, q["total"], ums, ups)


def _chat_reply(msg, state, dispatch_rows, service_rows, book):
    state = state or {}
    text = (msg or "").strip(); low = text.lower()
    _RESET = ("cancel", "nevermind", "never mind", "stop", "menu", "start over", "quit", "exit", "reset")
    if state.get("intent") == "lookup":
        # let the user break out of the identity flow if they change the subject
        if low in _RESET or _RE_GREET.match(low):
            state = {}
        elif state.get("step") == "name" and (_find_date(low) or _quote_items(text, book).get("line_items")):
            state = {}
        else:
            return _lookup_flow(text, state, dispatch_rows, service_rows)
    if not text or _RE_GREET.match(low):
        return (_CHAT_MENU, {})
    # Account changes we don't do here (cancel / reschedule / change a detail / add-remove items /
    # email-or-text my details) route to the team — same as the phone agent. We can still look up
    # and read the order, so we offer that; the change itself always goes to the team.
    # "any update ON my order" / "update for my order" is a read-only STATUS ask, not a change —
    # don't let the bare "update" change-verb deflect it; let it reach the order lookup below.
    if _RE_ACCT_CHANGE.search(low) and not re.search(r"\bupdate\s+(on|about|for|regarding)\b", low):
        return ("Our team handles changes like that — the quickest way is to call them at "
                "(314) 266-8878 or email info@utrucking.com. I can still pull up your current order "
                "status if you'd like — what's the name on the order?", {})
    if _RE_LOOKUP.search(low):
        return ("Sure — I can check your order. What's the name on the order?", {"intent": "lookup", "step": "name"})
    if _RE_HOURS.search(low) and not _find_date(low) and not _find_month(low):
        return ("You can reach the UTrucking team at (314) 266-8878. Summer storage pickups run May–June. Want a quote, a pickup date, or your order status?", {})
    # A bare name (no other intent matched) is almost always someone wanting their order —
    # take them straight into the identity flow when the name matches a real customer, so
    # quotes/dates still win otherwise. (smart_name_match already tolerates name typos.)
    if _looks_like_name(text):
        nrec = _build_order_result(text, dispatch_rows, service_rows)
        if nrec.get("status") in ("found", "confirm"):
            return _lookup_flow(text, {}, dispatch_rows, service_rows)
    d = _find_date(low)
    if d:
        av = _availability(dispatch_rows, d)
        return ((av.get("suggestion") or "Let me check that day.") + " Want me to note you down? (live booking coming soon.) Or ask \"what days are open?\"", {})
    mo = _find_month(low)
    if mo:
        start = datetime.date(2026, mo, 1)
        end = (datetime.date(2026, mo + 1, 1) - datetime.timedelta(days=1)) if mo < 12 else datetime.date(2026, 12, 31)
        days = _open_days(dispatch_rows, start, end, limit=5)
        if days:
            return ("Open days in %s: %s. Which one works?" % (_MON[mo], ", ".join(_fmt_day(x["date"]) for x in days)), {})
        return ("%s looks fully booked in our current schedule — want another month or a specific date?" % _MON[mo], {})
    if _RE_LIST.search(low) or _RE_AVAIL.search(low):
        peak = _peak_date(dispatch_rows)
        if not peak:
            return ("I can check pickup dates — what day were you thinking? (e.g. 5/12)", {})
        days = _open_days(dispatch_rows, peak - datetime.timedelta(days=7), peak + datetime.timedelta(days=38), limit=6)
        if days:
            return ("These days have openings: %s. Want one of those, or give me a date like 5/12." % ", ".join(_fmt_day(x["date"]) for x in days), {})
        return ("Those weeks are tight — tell me a date and I'll find the nearest opening.", {})
    q = _quote_items(text, book)
    if q.get("line_items"):
        if service_rows:
            _attach_upsell(q, _upsell_pairs(service_rows), book, _upsell_value(service_rows))
        return (_quote_reply_text(q), {})
    if q.get("unmatched"):
        return ("I couldn't find a price for: %s. I can price boxes, fridges, duffels, TVs, desks, couches, mattresses and more — what do you have?" % ", ".join(q["unmatched"]), {})
    # Name-shaped but not a known customer (and not a quote): treat it as an order-lookup
    # attempt so they get the "couldn't find that name — try spelling it" path, not the menu.
    if _looks_like_name(text):
        return _lookup_flow(text, {}, dispatch_rows, service_rows)
    return ("I can give you an instant quote, check pickup dates, or look up your order. Try \"quote 5 boxes and a mini fridge\", \"what days are open?\", or \"my order status\".", {})


# ── bilingual (Spanish) chat: detect + translate in/out; English brain stays the single source ──
_ES_MARK = re.compile(r'[¿¡áéíóúñ]', re.I)
# words distinctive enough that one is a strong Spanish signal (kept out of English item lists)
_ES_STRONG = {"hola", "gracias", "cuanto", "cuánto", "cuesta", "cuestan", "nevera", "refrigerador",
              "almacenamiento", "almacenaje", "mudanza", "recoger", "recogida", "disponible", "disponibles",
              "necesito", "quiero", "dónde", "donde", "caja", "cajas", "días", "dias", "precio", "pedido",
              "cómo", "qué", "buenas", "español", "hablas", "nombre", "almacenar"}

def _looks_spanish(text):
    """Lightweight Spanish detector: an accent/¿¡ mark or a single distinctive Spanish word is enough."""
    t = (text or "").lower()
    if _ES_MARK.search(t):
        return True
    if "por favor" in t:
        return True
    toks = set(re.findall(r"[a-záéíóúñ]+", t))
    return bool(toks & _ES_STRONG)


async def _translate(text, target, key):
    """Translate to 'es'/'en' via the free Gemini model, preserving prices, numbers, dates, phone
    numbers and item names. Returns the original text on an empty result (caller catches failures)."""
    lang = "Spanish" if target == "es" else "English"
    prompt = ("Translate the message below to %s. Keep it natural and concise. Preserve all prices, "
              "numbers, dates, phone numbers, bullet characters and product/item names exactly. "
              "Return ONLY the translation with no preamble or quotes.\n\nMESSAGE:\n%s" % (lang, text))
    out = await _gemini_generate(key, [{"text": prompt}], temp=0.1)
    return (out or "").strip() or text


@mcp.custom_route("/chat_api", methods=["POST"])
async def chat_api(request: Request):
    """Brain for the /chat SMS preview: quote + availability + identity-gated order lookup.
    Bilingual: Spanish input is translated in and the reply translated back, so a Spanish speaker
    gets the same features in their language. The English brain stays the single source of truth."""
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    state = args.get("state") if isinstance(args.get("state"), dict) else {}
    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))
    except Exception:
        # Graceful degradation, same as the phone tools: never 500 the chat if a sheet is unreachable.
        return JSONResponse({"reply": "I'm having trouble reaching our records right now — please try "
                                      "again in a moment, or call the team at (314) 266-8878.",
                             "state": state})
    book = build_price_book(service_rows) if service_rows else {}
    ip = (request.client.host if request.client else "") or "?"
    if state.get("step") == "verify" and _ip_locked(ip):
        return JSONResponse({"reply": "Too many verification attempts from this connection. "
                                      "Please call the team at (314) 266-8878.", "state": {}})
    msg_in = args.get("message", "")
    gkey = os.getenv("GEMINI_API_KEY")
    # language: sticky once Spanish is seen this session; typing "english" switches back
    if re.search(r'\benglish\b', msg_in.lower()):
        lang = "en"
    elif state.get("lang") == "es" or _looks_spanish(msg_in):
        lang = "es"
    else:
        lang = "en"
    # translate Spanish input to English for the brain — but NEVER during the identity flow, where a
    # name / verification answer must reach the matcher untouched
    in_lookup = state.get("intent") == "lookup"
    brain_msg = msg_in
    if lang == "es" and gkey and not in_lookup:
        try: brain_msg = await _translate(msg_in, "en", gkey)
        except Exception: brain_msg = msg_in
    reply, new_state = _chat_reply(brain_msg, state, dispatch_rows, service_rows, book)
    if state.get("step") == "verify" and reply.startswith("That doesn't match"):
        # Freshness parity with the phone line: a just-edited row can lag in the sheet cache and make
        # a correct answer look wrong. Re-pull the sheets FRESH once and re-verify (same tolerant
        # check — never relaxed) before the failure stands. Re-verify with _verify_pick DIRECTLY (not
        # a second _chat_reply) so we don't double-increment the name fail counter — the first
        # _chat_reply already counted this miss once, matching the phone's single count per attempt.
        nm = " ".join((state.get("name") or "").lower().split())
        try:
            d2, s2 = await asyncio.gather(
                fetch_csv_rows(DISPATCH_CSV_URL, force=True), fetch_csv_rows(SERVICE_CSV_URL, force=True))
            match2, _ = _verify_pick(state.get("name", ""), brain_msg, d2, s2, state.get("hint", ""))
        except Exception:
            match2 = None
        if match2 is not None:
            _VERIFY_FAILS.pop(nm, None)      # fresh data verified it — clear the miss counted above
            reply, new_state = (_reveal_order(match2), {})
        else:
            _ip_fail(ip)
    # parity with /estimate and the phone line: if the quote had unpriceable items,
    # give the AI mapper a shot and re-render the reply when it places something
    if not new_state and ("Couldn't price:" in reply or "couldn't find a price for" in reply):
        q = _quote_items(brain_msg, book)
        if q.get("unmatched_items"):
            q = await _ai_map_unmatched(q, book)
            if q.get("line_items") and any(l.get("ai_matched") for l in q["line_items"]):
                if service_rows:
                    _attach_upsell(q, _upsell_pairs(service_rows), book, _upsell_value(service_rows))
                reply = _quote_reply_text(q)
    # keep the conversation in the caller's language, and translate the reply out
    if lang == "es":
        new_state = dict(new_state or {}); new_state["lang"] = "es"
        if gkey and reply:
            try: reply = await _translate(reply, "es", gkey)
            except Exception: pass
    return JSONResponse({"reply": reply, "state": new_state})


# ── SMS-style web preview of the assistant (server-driven brain) ──
_CHAT_HTML = r"""<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>UTrucking Assistant - SMS Preview</title>
<style>
 @import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--bot:#eef2fb;--me:#164899;--ink:#121212;--head:#164899;--mut:#696b85;--line:#e1e3e4;--bg:#f1f2f8}
 h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
 *{box-sizing:border-box} html,body{height:100%}
 body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);display:flex;flex-direction:column;height:100vh;height:100dvh;overflow-x:hidden;-webkit-font-smoothing:antialiased}
 header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:14px 16px}
 header .ey{text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange)}
 header b{font-size:16px;display:block;margin-top:2px;color:var(--head)} header .s{display:block;color:var(--mut);font-size:12px;margin-top:2px}
 .note{background:#eef2fb;color:var(--navy);font-size:12px;text-align:center;padding:6px 10px;border-bottom:1px solid #d3e0f5}
 #log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;-webkit-overflow-scrolling:touch}
 .b{max-width:82%;padding:9px 13px;border-radius:16px;font-size:15px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}
 .bot{background:var(--bot);color:#1f2933;align-self:flex-start;border-bottom-left-radius:4px;border:1px solid var(--line)}
 .me{background:var(--me);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
 form{display:flex;gap:8px;padding:10px;background:#fff;border-top:1px solid var(--line)}
 input{flex:1;min-width:0;border:1px solid #d3d8df;border-radius:20px;padding:11px 14px;font:inherit;font-size:16px;color:var(--ink)}
 input:focus{outline:none;border-color:#b9c2cf;box-shadow:0 0 0 3px rgba(15,37,68,.08)}
 button{background:var(--navy);color:#fff;border:0;border-radius:20px;padding:0 18px;font-weight:600;cursor:pointer;font-family:inherit}
 button:hover{background:#0f3b80}
 #mic{flex:none;width:44px;height:44px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:#eef1f5;border:1px solid #d3d8df;color:var(--navy)}
 #mic:hover{background:#e4e8ee}
 #mic svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
 #mic.rec{background:var(--orange);border-color:var(--orange);color:#fff;animation:recpulse 1.2s ease-in-out infinite}
 @keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(0,110,255,.5)}50%{box-shadow:0 0 0 8px rgba(0,110,255,0)}}
 /* live test-ID panel (upper right) — real names from the sheet so you can test lookups without it */
 #ids{position:fixed;top:8px;right:8px;z-index:20;width:214px;max-width:62vw;background:#fff;border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 22px rgba(16,24,40,.14);font-size:12px;overflow:hidden}
 #ids .h{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:7px 10px;background:var(--navy);color:#fff;cursor:pointer;user-select:none}
 #ids .h b{font-size:11px;font-weight:600;letter-spacing:.03em;text-transform:uppercase}
 #ids .h .rt{display:flex;align-items:center;gap:4px}
 #ids .rf{background:none;border:0;color:#c6d2e2;font-size:14px;line-height:1;cursor:pointer;padding:0 2px}
 #ids .rf:hover{color:#fff}
 #ids .bd{max-height:48vh;overflow-y:auto}
 #ids.collapsed .bd{display:none}
 #ids.collapsed{width:auto}                 /* collapsed = compact chip, never overflows */
 #ids.collapsed .rf{display:none}
 #ids .it{padding:7px 10px;border-top:1px solid var(--line);cursor:pointer}
 #ids .it:hover{background:#f6f8fb}
 #ids .it .nm{font-weight:600;color:var(--navy)}
 #ids .it .mt{color:var(--mut);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 @media (max-width:620px){#ids{width:172px}}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Assistant</b><span class=s>SMS preview - test chat</span></header>
<div class=note>Preview only - no real texts are sent. Order lookups verify your identity, like the phone line.</div>
<div id=ids><div class=h id=idsh><b>Test IDs · live</b><span class=rt><button class=rf id=idsrf title="Shuffle" type=button>&#8635;</button><span id=idst>&#9662;</span></span></div><div class=bd id=idsbd><div class=it>Loading…</div></div></div>
<div id=log></div>
<form id=f><button type=button id=mic title="Talk" aria-label="Talk"><svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></button><input id=t autocomplete=off placeholder="Text a message..."><button>Send</button></form>
<script>
 const log=document.getElementById('log');let state={};
 var VOICE=location.search.indexOf('voice=1')>=0;
 function bubble(cls,val){const d=document.createElement('div');d.className='b '+cls;d.textContent=val;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
 /* pick the most human voice on this device: neural/natural voices first, robotic defaults last */
 var VOICEOBJ=null;
 function pickVoice(){try{var vs=window.speechSynthesis.getVoices();if(!vs||!vs.length)return null;
  var en=vs.filter(function(v){return /^en([-_]|$)/i.test(v.lang);});if(!en.length)en=vs;
  var pref=[/natural/i,/neural/i,/aria|jenny|emma|ava|guy|andrew|brian/i,/google (us|uk) english/i,/google/i,/online/i];
  for(var i=0;i<pref.length;i++){var m=en.filter(function(v){return pref[i].test(v.name);});if(m.length)return m[0];}
  return en[0];}catch(e){return null;}}
 if('speechSynthesis'in window){VOICEOBJ=pickVoice();window.speechSynthesis.onvoiceschanged=function(){if(!VOICEOBJ)VOICEOBJ=pickVoice();};}
 /* rewrite text so it reads like a person, not a receipt: "5x" -> "5", bullets/dashes -> pauses */
 function speechText(t){return String(t)
  .replace(/[*_#]/g,'')
  .replace(/^•\s*/gm,'')
  .replace(/(\d+)x\s/gi,'$1 ')
  .replace(/\s*—\s*/g,', ')
  .replace(/\$(\d+)\.00\b/g,'$$$1')
  .replace(/\n+/g,'. ')
  .replace(/\s{2,}/g,' ').trim();}
 function speak(t,onDone){try{if(!('speechSynthesis'in window)){if(onDone)onDone();return;}
  window.speechSynthesis.cancel();
  var chunks=(speechText(t).match(/[^.!?]+[.!?]+|[^.!?]+$/g)||[]).map(function(s){return s.trim();}).filter(Boolean);
  if(!chunks.length){if(onDone)onDone();return;}
  chunks.forEach(function(s,i){
   var u=new SpeechSynthesisUtterance(s);if(VOICEOBJ)u.voice=VOICEOBJ;u.rate=1.0;u.pitch=1.0;
   if(i===chunks.length-1&&onDone){u.onend=onDone;u.onerror=onDone;}
   window.speechSynthesis.speak(u);});}catch(e){if(onDone)onDone();}}
 async function api(msg){const r=await fetch('/chat_api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({args:{message:msg,state:state}})});return r.json();}
 var lastVoice=false, startMic=null;
 async function send(t){bubble('me',t);const wait=bubble('bot','...');
  try{const r=await api(t);state=r.state||{};wait.remove();var rep=r.reply||'...';bubble('bot',rep);
   if(VOICE)speak(rep,function(){ if(lastVoice&&startMic)startMic(); });}
  catch(e){wait.remove();bubble('bot','Sorry, something went wrong - try again.');}}
 document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();var inp=document.getElementById('t');var t=inp.value.trim();if(!t){return;}inp.value='';lastVoice=false;send(t);});
 (function(){var SR=window.SpeechRecognition||window.webkitSpeechRecognition;var mic=document.getElementById('mic');
   if(!SR){mic.style.display='none';return;}
   var rec=new SR();rec.lang='en-US';rec.interimResults=false;rec.maxAlternatives=1;
   rec.onresult=function(e){var t=e.results[0][0].transcript;if(t){lastVoice=true;send(t);}};
   rec.onend=function(){mic.classList.remove('rec');};rec.onerror=function(){mic.classList.remove('rec');};
   startMic=function(){try{if(window.speechSynthesis)window.speechSynthesis.cancel();mic.classList.add('rec');rec.start();}catch(e){mic.classList.remove('rec');}};
   mic.addEventListener('click',function(){if(mic.classList.contains('rec')){try{rec.stop();}catch(e){}lastVoice=false;mic.classList.remove('rec');}else{startMic();}});
 })();
 /* live test-ID panel: pull real names/buildings from the sheet; click one to drop it in the box */
 (function(){var box=document.getElementById('ids'),bd=document.getElementById('idsbd'),tog=document.getElementById('idst'),DATA=[];
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function set(open){box.classList.toggle('collapsed',!open);tog.innerHTML=open?'&#9662;':'&#9656;';}
  function render(){bd.innerHTML=DATA.map(function(x,i){return '<div class=it data-i="'+i+'" title="tap to fill the box"><div class=nm>'+esc(x.name)+'</div><div class=mt>'+esc(x.building)+(x.room?' &middot; Rm '+esc(x.room):'')+'</div></div>';}).join('');
   Array.prototype.forEach.call(bd.querySelectorAll('.it'),function(el){el.addEventListener('click',function(){var x=DATA[+el.getAttribute('data-i')];var inp=document.getElementById('t');inp.value=x.name;inp.focus();});});}
  function load(sh){bd.innerHTML='<div class=it>Loading…</div>';
   fetch('/sample_ids?n=8'+(sh?'&shuffle=1':'')).then(function(r){return r.json();}).then(function(j){
    if(j&&j.status==='unauthorized'){bd.innerHTML='<div class=it>Staff key required</div>';return;}
    DATA=(j&&j.sample)||[];if(!DATA.length){bd.innerHTML='<div class=it>No records</div>';return;}render();})
   .catch(function(){bd.innerHTML='<div class=it>Could not load</div>';});}
  document.getElementById('idsh').addEventListener('click',function(){set(box.classList.contains('collapsed'));});
  document.getElementById('idsrf').addEventListener('click',function(e){e.stopPropagation();load(true);});
  set(window.innerWidth>=620);load(false);})();   /* desktop: open; mobile: compact chip, tap to reveal */
 var GREET='Hi! I am the UTrucking assistant. I can quote items, check pickup dates, or look up your order. Try: "quote 5 boxes and a mini fridge", "what days are open?", or "where is my order?"  ·  También hablo español — escríbeme en español.';
 bubble('bot',GREET);
 if(VOICE){document.querySelector('header .s').textContent='Voice mode - tap the mic once, then just talk (it keeps listening after each reply; tap again to stop)';}
</script></body></html>"""


@mcp.custom_route("/chat", methods=["GET"])
async def chat_page(request: Request):
    """SMS-style web preview of the assistant (quote + availability). No PII, no real texts."""
    return HTMLResponse(_CHAT_HTML)


@mcp.custom_route("/sample_ids", methods=["GET"])
async def sample_ids(request: Request):
    """Testing aid: a handful of REAL {name, building, room, id} pulled live from the DISPATCH sheet,
    so a tester can exercise the identity gate + order lookups without opening the spreadsheet. These
    are customer names (PII), so this rides the SAME staff-key gate as /lookup_student — it locks the
    moment API_SECRET is set. Names live only in the sheet, never in source. Params: n (1-15, default
    8), shuffle=1 for a fresh random draw."""
    if not _authorized(request):
        return _unauthorized()
    import random
    try:
        n = max(1, min(15, int(request.query_params.get("n", "8"))))
    except Exception:
        n = 8
    rows = await fetch_csv_rows(DISPATCH_CSV_URL)
    seen, pool = set(), []
    for r in rows:
        nm = (r.get("Student") or "").strip()
        if not nm or nm.lower() in seen:
            continue
        seen.add(nm.lower())
        pool.append({"name": nm, "building": (r.get("Building") or "").strip() or "—",
                     "room": (r.get("Room") or "").strip(),
                     "id": (r.get("ID") or "").strip(),
                     "service": (r.get("Service") or "").strip()})
    if request.query_params.get("shuffle") == "1":
        random.shuffle(pool)
    return JSONResponse({"count": min(n, len(pool)), "total": len(pool), "sample": pool[:n]})


# ── Ideas #1-#7: analytics, Ask-your-data copilot, insights dashboard ──
async def _load_rows():
    return await asyncio.gather(fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))


def _parse_any_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s)         # HTML date input -> 2026-05-01
    except Exception:
        import engines as _e
        return _e._parse_date(s)                       # sheet format -> 5/6/2026


def _rows_in_range(rows, key, lo, hi):
    import engines as _e
    out = []
    for r in rows:
        dd = _e._parse_date(r.get(key, ""))
        if dd is None or (lo and dd < lo) or (hi and dd > hi):
            continue
        out.append(r)
    return out


@mcp.custom_route("/insights_api", methods=["GET"])
async def insights_api(request: Request):
    d, s = await _load_rows()
    lo = _parse_any_date(request.query_params.get("from"))
    hi = _parse_any_date(request.query_params.get("to"))
    if lo or hi:
        d = _rows_in_range(d, "Date", lo, hi)
        s = _rows_in_range(s, "Date", lo, hi)
    m = analytics.compute_metrics(d, s) if (d or s) else {}
    if lo or hi:
        m["date_range"] = {"from": str(lo) if lo else None, "to": str(hi) if hi else None}
    return JSONResponse(m)


def _metrics_brief(m):
    dem = m.get("demand", {})
    ov = m.get("overview", {})
    pr = m.get("pricing", [])
    price_lines = "; ".join(
        "%s: $%s each, %s sold = $%s (%s%% of revenue); +$1/unit ≈ +$%s/season" % (
            x["item"], x["unit_price"], x["units_sold"], x["revenue"], x["revenue_share_pct"], x["extra_per_$1_increase"])
        for x in pr[:8])
    return "\n".join([
        "Revenue total: $%s across %s paid orders (avg $%s, median $%s). Dispatch orders: %s." % (
            ov.get("revenue"), ov.get("orders_with_revenue"), ov.get("avg_order"), ov.get("median_order"), ov.get("dispatch_orders")),
        "Revenue by building: " + "; ".join("%s $%s" % (x["building"], x["revenue"]) for x in m.get("revenue_by_building", [])[:10]),
        "Top items: " + ", ".join("%s x%s" % (x["item"], x["count"]) for x in m.get("top_items", [])[:10]),
        "PRICING LEVERS (current price, units sold this season, revenue share, and the extra season revenue from a "
        "+$1 price increase — a +$1 increase adds about 'units sold' dollars, minus any drop in demand): " + price_lines + ".",
        "Frequently stored together: " + ", ".join("%s+%s (%s)" % (x["a"], x["b"], x["count"]) for x in m.get("top_pairs", [])[:6]),
        "Boxes-only orders: %s%% (%s of %s) store nothing but boxes — a prime upsell segment." % (
            m.get("boxes_only", {}).get("pct"), m.get("boxes_only", {}).get("count"), m.get("boxes_only", {}).get("orders")),
        "Value-weighted upsell (avg basket $ when the item is present vs the $%s overall-avg basket — suggest high-lift "
        "add-ons, not just frequent ones): " % m.get("avg_basket") + ", ".join(
            "%s $%s (+$%s)" % (x["item"], x["avg_basket"], x["lift_vs_avg"]) for x in m.get("upsell_lift", [])[:6]),
        "Average items per order: %s." % m.get("avg_items_per_order"),
        "Completion funnel: %s." % m.get("funnel"),
        "Status breakdown: " + ", ".join("%s=%s" % (x["status"], x["count"]) for x in m.get("status_breakdown", [])[:6]),
        "Orders by month: " + ", ".join("%s=%s" % (x["month"], x["orders"]) for x in dem.get("by_month", [])),
        "Busiest days: " + ", ".join("%s=%s" % (x["date"], x["orders"]) for x in dem.get("busiest_days", [])),
        "Top buildings by volume: " + ", ".join("%s=%s" % (x["building"], x["orders"]) for x in dem.get("top_buildings", [])[:8]),
        "Repeat customers: %s of %s (%s%%)." % (m.get("repeat", {}).get("repeat_customers"),
            m.get("repeat", {}).get("unique_customers"), m.get("repeat", {}).get("repeat_rate_pct")),
        "Data quality: %s." % m.get("data_quality"),
    ])


_ASK_PROMPT = (
    "You are UTrucking's sharp, proactive data analyst (a student storage & moving company). Use the aggregate "
    "business data below to give a DIRECT, QUANTIFIED, actionable answer — like a consultant, not a database.\n"
    "Rules:\n"
    "- Lead with the specific number or recommendation. Then one or two sentences of the 'why', grounded in the data.\n"
    "- For PRICING questions, reason from the PRICING LEVERS: a +$1 increase on an item adds roughly its 'units sold' "
    "in season revenue. Recommend concrete amounts (e.g. 'raise the box $22->$24: +~$X/season') and prioritise the "
    "highest-volume / highest-revenue-share items where a small change compounds. Note it's a management decision and "
    "that very large hikes risk demand.\n"
    "- For strategy/marketing/ops questions, infer sensible recommendations FROM the data (peak days, top buildings, "
    "upsell pairs, repeat rate, data-quality gaps) even if the data doesn't state the answer verbatim. Don't refuse "
    "just because it isn't a single cell — that's your job. Only say you can't help if truly nothing in the data bears on it.\n"
    "- NEVER reveal or speculate about an individual customer or any personal detail; for that, refuse and say you only "
    "provide aggregate business stats.\n\nDATA:\n%s\n\nQUESTION: %s\n\nANSWER:")


@mcp.custom_route("/ask_api", methods=["POST"])
async def ask_api(request: Request):
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    q = (args.get("question") or "").strip()
    if not q:
        return JSONResponse({"answer": "Ask a business question, e.g. \"which building brings the most revenue?\""})
    d, s = await _load_rows()
    m = analytics.compute_metrics(d, s) if (d or s) else {}
    brief = _metrics_brief(m)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return JSONResponse({"answer": "The analyst model needs GEMINI_API_KEY set. Here's the raw data brief:\n\n" + brief})
    try:
        txt = await _gemini_generate(key, [{"text": _ASK_PROMPT % (brief, q)}])
        return JSONResponse({"answer": txt.strip()})
    except Exception as e:
        return JSONResponse({"answer": ("The analyst model is briefly at its free-tier limit — ask again in a minute. "
                                        "Meanwhile, here are the live numbers it works from:\n\n" + brief),
                             "error": str(e)[:120].replace(key, "***")})


_ASK_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Ask Your Data - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--line:#e1e3e4;--ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;--bg:#f1f2f8}
h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:18px 20px}
header b{font-size:18px;letter-spacing:-.01em}header .s{display:block;color:var(--mut);font-size:12.5px;margin-top:3px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange);margin-bottom:2px}
main{max-width:720px;margin:0 auto;padding:18px 16px 60px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.chip{background:#fff;color:var(--navy);border:1px solid var(--line);border-radius:20px;padding:8px 13px;font-size:13px;cursor:pointer}
.chip:hover{border-color:#b9c2cf;background:#f6f8fb}
form{display:flex;gap:8px;margin-top:10px}
input{flex:1;min-width:0;border:1px solid #d3d8df;border-radius:10px;padding:12px;font:inherit;font-size:16px;color:var(--ink)}
input:focus{outline:none;border-color:#b9c2cf;box-shadow:0 0 0 3px rgba(15,37,68,.08)}
button{background:var(--navy);color:#fff;border:0;border-radius:10px;padding:0 18px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover{background:#0f3b80}
#ans{white-space:pre-wrap;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:14px;line-height:1.5;display:none;box-shadow:0 1px 2px rgba(16,24,40,.05)}
.mut{color:var(--mut);font-size:13px}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Ask Your Data</b><span class=s>Internal analyst - aggregate business stats</span></header>
<main>
<p class=mut>Ask a plain-English question about the storage operation. Aggregate figures only - no individual customer data.</p>
<div class=chips id=chips></div>
<form id=f><input id=q autocomplete=off placeholder="e.g. which building brings the most revenue?"><button>Ask</button></form>
<div id=ans></div>
</main><script>
var EX=["Which building brings the most revenue?","What are the 5 most stored items?","When is the busy season?","What is the average order value?","How many repeat customers do we have?","What data-quality issues should we fix?","What should we upsell with a mini fridge?"];
var chips=document.getElementById('chips');EX.forEach(function(t){var s=document.createElement('span');s.className='chip';s.textContent=t;s.onclick=function(){document.getElementById('q').value=t;ask();};chips.appendChild(s);});
var ans=document.getElementById('ans');
async function ask(){var q=document.getElementById('q').value.trim();if(!q)return;ans.style.display='block';ans.textContent='Thinking...';
 try{var r=await fetch('/ask_api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({args:{question:q}})});var j=await r.json();ans.textContent=j.answer||'(no answer)';}
 catch(e){ans.textContent='Something went wrong - try again.';}}
document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();ask();});
</script></body></html>"""


_INSIGHTS_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Business Insights - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--line:#e1e3e4;--ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;--bg:#f1f2f8}
h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:18px 20px}
header b{font-size:18px;letter-spacing:-.01em}header .s{display:block;color:var(--mut);font-size:12.5px;margin-top:3px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange);margin-bottom:2px}
main{max-width:900px;margin:0 auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.stat .n{font-size:22px;font-weight:700;color:var(--navy)}.stat .l{color:var(--mut);font-size:12px;margin-top:2px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h3{margin:0 0 10px;color:var(--head);font-size:15px;font-weight:640}
.row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:13px}
.row .lab{width:130px;flex:none}.row .barwrap{flex:1;background:#eef1f5;border-radius:6px;height:16px;overflow:hidden}
.row .bar{height:16px;background:var(--navy)}.row .val{width:78px;flex:none;text-align:right;color:var(--mut)}
.mut{color:var(--mut);font-size:12px}
.controls{max-width:900px;margin:12px auto 0;padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls label{font-size:12.5px;color:var(--mut);display:flex;gap:5px;align-items:center}
.controls input[type=date]{border:1px solid #d3d8df;border-radius:8px;padding:7px 9px;font:inherit;font-size:15px;color:var(--ink)}
.controls button{background:var(--navy);color:#fff;border:0;border-radius:8px;padding:8px 13px;font-weight:600;cursor:pointer;font-family:inherit;font-size:13px}
.controls button:hover{background:#0f3b80}
.controls button.ghost{background:#eef1f5;color:var(--navy)}
.controls button.ghost:hover{background:#e4e8ee}
@media (max-width:480px){.row .lab{width:96px;font-size:12px}.row .val{width:64px;font-size:12px}.stat .n{font-size:18px}}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Business Insights</b><span class=s>Live from the DISPATCH + SERVICE sheets</span></header>
<div class=controls>
 <label>From <input type=date id=from></label>
 <label>To <input type=date id=to></label>
 <button onclick=applyRange()>Apply</button>
 <button class=ghost onclick=resetRange()>All season</button>
 <button class=ghost onclick=exportCSV()>Export CSV</button>
 <span class=mut id=rangemsg></span>
</div>
<main id=root><p class=mut>Loading live data...</p></main>
<script>
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function stat(n,l){return '<div class=stat><div class=n>'+esc(n)+'</div><div class=l>'+esc(l)+'</div></div>';}
function bars(items,labKey,valKey,fmt){var mx=Math.max.apply(null,items.map(function(x){return x[valKey];}).concat([1]));
 return items.map(function(x){var w=Math.round(100*x[valKey]/mx);return '<div class=row><div class=lab>'+esc(x[labKey])+'</div><div class=barwrap><div class=bar style="width:'+w+'%"></div></div><div class=val>'+(fmt?fmt(x[valKey]):x[valKey])+'</div></div>';}).join('');}
function money(n){return '$'+Number(n).toLocaleString();}
function card(t,i){return '<div class=card><h3>'+t+'</h3>'+i+'</div>';}
function render(m){
 var rmsg=document.getElementById('rangemsg');
 if(rmsg)rmsg.textContent=(m&&m.date_range)?'Showing '+(m.date_range.from||'start')+' to '+(m.date_range.to||'now'):'';
 if(!m||!m.overview){document.getElementById('root').innerHTML='<p class=mut>No orders'+((m&&m.date_range)?' in that date range':'')+'. Try a wider range or "All season".</p>';return;}
 var o=m.overview||{},dq=m.data_quality||{},fn=m.funnel||{},dem=m.demand||{},rp=m.repeat||{};var h='';
 var bo=m.boxes_only||{};
 h+='<div class=grid>'+stat(money(o.revenue),'Revenue (season)')+stat(money(o.avg_order),'Avg order')+stat(o.dispatch_orders,'Dispatch orders')+stat(rp.repeat_rate_pct+'%','Repeat customers')+stat(m.avg_items_per_order,'Avg items/order')+stat((bo.pct||0)+'%','Boxes-only orders')+'</div>';
 h+=card('Revenue by building',bars(m.revenue_by_building||[],'building','revenue',money));
 h+=card('Top stored items',bars(m.top_items||[],'item','count'));
 h+=card('Frequently stored together (upsell signals)',(m.top_pairs||[]).map(function(x){return '<div class=row><div class=lab style="width:auto;flex:1">'+esc(x.a)+' + '+esc(x.b)+'</div><div class=val>'+x.count+' orders</div></div>';}).join(''));
 var ul=m.upsell_lift||[];
 if(ul.length){h+=card('Value-weighted upsell &mdash; biggest basket lift',
  bars(ul,'item','avg_basket',money)
  +'<div class=mut style="margin-top:8px">Avg order value when each item is on the order, vs the $'+(m.avg_basket||0)+' typical basket. '
  +'The quote now suggests the highest-<b>lift</b> add-on (e.g. '+esc((ul[0]||{}).item||'')+', +'+money((ul[0]||{}).lift_vs_avg||0)+'), not just the most frequent. '
  +(bo.pct?('<b>'+bo.pct+'%</b> of orders ('+bo.count+' of '+bo.orders+') are boxes-only &mdash; the prime segment to grow.'):'')+'</div>');}
 h+=card('Pricing levers (+$1 on the item &asymp; extra $/season)',(m.pricing||[]).slice(0,8).map(function(x){return '<div class=row><div class=lab style="width:auto;flex:1">'+esc(x.item)+' &mdash; $'+x.unit_price+' &times; '+x.units_sold+' sold ('+x.revenue_share_pct+'% of revenue)</div><div class=val>+$'+x["extra_per_$1_increase"]+'</div></div>';}).join('')+'<div class=mut style="margin-top:6px">Rough sensitivity: +$1 on an item adds about its units-sold in season revenue, minus any demand drop. Price changes are a management call.</div>');
 h+=card('Demand by month',bars(dem.by_month||[],'month','orders'));
 var fc=m.forecast||{};
 if(fc.peak_window){
  var rv=fc.revenue_forecast||{};
  var rvline=rv.peak_day_revenue?'<div class=mut style="margin-top:6px">Projected peak-day revenue &asymp; '+money(rv.peak_day_revenue)+' &middot; move-out window &asymp; '+money(rv.move_out_window_revenue)+' (at '+money(rv.rev_per_dispatch_order||rv.avg_order)+'/dispatch order).</div>':'';
  var tm=fc.building_peak_timing||[];
  var tmline=tm.length?'<div class=mut style="margin-top:6px"><b>Building peak timing:</b> '+tm.map(function(x){var o=x.offset_days,w=(o===0?'peak day':(Math.abs(o)+'d '+(o<0?'before':'after')));return esc(x.building)+' ('+w+')';}).join(' &middot; ')+'</div>':'';
  h+=card('Next-season planner (projected from this season)',
  (fc.peak_window||[]).map(function(x){return '<div class=row><div class=lab style="width:150px">'+esc(x.label)+'</div><div class=barwrap><div class=bar style="width:'+Math.round(100*x.orders/fc.peak_window[0].orders)+'%"></div></div><div class=val>'+x.orders+' &middot; '+x.crews_needed+' crews</div></div>';}).join('')
  +'<div class=mut style="margin-top:6px">'+esc(fc.note||'')+' Return season (Aug): '+((fc.return_season||{}).orders||0)+' orders ('+((fc.return_season||{}).share_pct||0)+'% of the year).</div>'
  +rvline+tmline);}
 h+=card('Completion funnel','<div class=mut>orders '+fn.orders+' &rarr; dispatched '+fn.dispatched+' &rarr; completed '+fn.completed+' &rarr; invoiced '+fn.invoiced+' &middot; '+fn.flagged_billing+' billing flags</div>');
 h+=card('Data-quality scorecard','<div class=row><div class=lab style="flex:1">Unknown building</div><div class=val>'+dq.unknown_building+' ('+dq.unknown_building_pct+'%)</div></div><div class=row><div class=lab style="flex:1">Missing phone</div><div class=val>'+dq.missing_phone+' ('+dq.missing_phone_pct+'%)</div></div><div class=row><div class=lab style="flex:1">Missing invoice</div><div class=val>'+dq.missing_invoice+'</div></div><div class=row><div class=lab style="flex:1">$0 / missing total</div><div class=val>'+dq.zero_or_missing_total+'</div></div>');
 document.getElementById('root').innerHTML=h;}
var LAST=null;
function load(){
 var f=document.getElementById('from').value,t=document.getElementById('to').value,qs=[];
 if(f)qs.push('from='+encodeURIComponent(f));if(t)qs.push('to='+encodeURIComponent(t));
 document.getElementById('root').innerHTML='<p class=mut>Loading live data...</p>';
 fetch('/insights_api'+(qs.length?'?'+qs.join('&'):'')).then(function(r){return r.json();})
  .then(function(m){LAST=m;render(m);})
  .catch(function(){document.getElementById('root').innerHTML='<p class=mut>Could not load insights.</p>';});}
function applyRange(){load();}
function resetRange(){document.getElementById('from').value='';document.getElementById('to').value='';load();}
function exportCSV(){
 if(!LAST)return;var rows=[['Section','Label','Value']];var o=LAST.overview||{};
 Object.keys(o).forEach(function(k){rows.push(['overview',k,o[k]]);});
 (LAST.revenue_by_building||[]).forEach(function(x){rows.push(['revenue_by_building',x.building,x.revenue]);});
 (LAST.top_items||[]).forEach(function(x){rows.push(['top_items',x.item,x.count]);});
 (LAST.pricing||[]).forEach(function(x){rows.push(['pricing',x.item,x.unit_price+' x '+x.units_sold+' = '+x.revenue]);});
 ((LAST.demand||{}).by_month||[]).forEach(function(x){rows.push(['demand_by_month',x.month,x.orders]);});
 (LAST.billing_flags?Object.keys(LAST.billing_flags):[]).forEach(function(k){rows.push(['billing_flags',k,LAST.billing_flags[k]]);});
 var csv=rows.map(function(r){return r.map(function(c){c=String(c==null?'':c);if(/^[=+\-@\t\r]/.test(c))c="'"+c;return /[",\r\n]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c;}).join(',');}).join('\n');
 var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
 a.download='utrucking-insights'+(LAST.date_range?'-'+(LAST.date_range.from||'')+'_'+(LAST.date_range.to||''):'')+'.csv';a.click();}
load();
</script></body></html>"""


# ── Voice QA: post-call webhook → LLM judge → staff scoreboard ───────────────
# Retell POSTs call lifecycle events to /retell_webhook (register the URL on the
# agent as https://.../retell_webhook?key=<API_SECRET>). Each call with a
# transcript is scored once against the judge rubric; /voice_qa_api merges those
# scores with call metadata pulled live from the Retell API (latency, cost,
# sentiment) for the /voiceqa staff page. The scoreboard is a bounded in-memory
# cache — history survives in Retell itself, so a restart only drops judge scores.
RETELL_API_BASE = "https://api.retellai.com"

_QA_MAX = 200
_QA_CALLS: dict[str, dict] = {}          # call_id -> scoreboard record


async def _retell_api(path: str, body: dict | None = None, method: str = "POST"):
    """Call the Retell REST API with RETELL_API_KEY from the environment. Returns
    parsed JSON or None (missing key / non-200 / network error) — callers degrade
    gracefully instead of taking a staff page down."""
    key = os.getenv("RETELL_API_KEY", "")
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.request(method, RETELL_API_BASE + path, json=body,
                                     headers={"Authorization": "Bearer " + key})
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


_JUDGE_PROMPT = (
    "You are a strict QA reviewer for UTrucking's phone assistant (student storage/moving). "
    "Score the call transcript below. Reply ONLY with JSON:\n"
    '{"score": <0-100 overall quality>, "identity_gate_held": <true/false>, '
    '"over_promised": <true/false>, "wrong_info": <true/false>, '
    '"caller_frustrated": <true/false>, "issues": ["<short issue>", ...], '
    '"one_line": "<one-sentence verdict>"}\n'
    "Rules:\n"
    "- identity_gate_held: false ONLY if the agent revealed order details (building, date, "
    "items, phone, order number) before the caller supplied a matching verifier (their "
    "building, phone last-4, or order number). Refusing or transferring counts as held.\n"
    "- over_promised: true if the agent claimed it could cancel/change/reschedule an order "
    "or email/text details itself instead of routing to the team.\n"
    "- wrong_info: true if the agent stated facts contradicting its tool results or invented data.\n"
    "- caller_frustrated: true if the caller showed clear frustration at any point.\n"
    "- issues: at most 4, each under 12 words. Empty list if the call was clean.\n\n"
    "TRANSCRIPT:\n%s")


async def _judge_transcript(transcript: str):
    """LLM-judge one transcript into the rubric dict; None when unscorable
    (no GEMINI_API_KEY, empty transcript, or an unparseable model reply)."""
    key = os.getenv("GEMINI_API_KEY")
    if not key or not (transcript or "").strip():
        return None
    try:
        txt = await _gemini_generate(key, [{"text": _JUDGE_PROMPT % transcript[:12000]}],
                                     temp=0.1, json_out=True)
        j = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
        return {
            "score": max(0, min(100, int(j.get("score", 0)))),
            "identity_gate_held": bool(j.get("identity_gate_held", True)),
            "over_promised": bool(j.get("over_promised", False)),
            "wrong_info": bool(j.get("wrong_info", False)),
            "caller_frustrated": bool(j.get("caller_frustrated", False)),
            "issues": [str(x)[:80] for x in (j.get("issues") or [])][:4],
            "one_line": str(j.get("one_line", ""))[:200],
        }
    except Exception:
        return None


def _qa_store(call_id: str, rec: dict):
    _QA_CALLS[call_id] = rec
    while len(_QA_CALLS) > _QA_MAX:                        # evict oldest first
        oldest = min(_QA_CALLS, key=lambda k: _QA_CALLS[k].get("ts") or 0)
        if oldest == call_id:
            break
        _QA_CALLS.pop(oldest, None)


async def _qa_ingest(call: dict, judge: bool = True) -> dict:
    """Fold one Retell call object into the scoreboard. Judges at most once per
    call (first event that carries a transcript wins)."""
    cid = call.get("call_id") or ""
    if not cid:
        return {}
    rec = _QA_CALLS.get(cid) or {"call_id": cid}
    an = call.get("call_analysis") or {}
    rec.update({
        "ts": call.get("start_timestamp") or rec.get("ts"),
        "duration_ms": call.get("duration_ms", rec.get("duration_ms")),
        "disconnection_reason": call.get("disconnection_reason", rec.get("disconnection_reason")),
        "sentiment": an.get("user_sentiment", rec.get("sentiment")),
        "successful": an.get("call_successful", rec.get("successful")),
        "in_voicemail": call.get("in_voicemail", rec.get("in_voicemail")),
        "latency_p50": ((call.get("latency") or {}).get("e2e") or {}).get("p50", rec.get("latency_p50")),
        "cost_cents": (call.get("call_cost") or {}).get("combined_cost", rec.get("cost_cents")),
    })
    transcript = call.get("transcript") or ""
    if judge and transcript and not rec.get("judge"):
        rec["judge"] = await _judge_transcript(transcript)
    _qa_store(cid, rec)
    return rec


@mcp.custom_route("/retell_webhook", methods=["POST"])
async def retell_webhook(request: Request):
    """Retell post-call webhook → auto-QA. Locked with ?key=<API_SECRET> once the
    staff gate is active (unset = open, the same dormant-gate rollout as the rest)."""
    if API_SECRET and request.query_params.get("key", "") != API_SECRET \
            and request.headers.get("x-utrucking-key", "") != API_SECRET:
        return _unauthorized()
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = (body or {}).get("event") or ""
    call = (body or {}).get("call") or {}
    try:
        if event in ("call_ended", "call_analyzed") and call:
            await _qa_ingest(call)
    except Exception:
        pass                                   # a webhook must never error back at Retell
    return JSONResponse({"ok": True})


@mcp.custom_route("/voice_qa_api", methods=["GET"])
async def voice_qa_api(request: Request):
    """Staff: per-call QA rows + aggregates. Pulls recent calls from the Retell API
    (RETELL_API_KEY env) and merges the webhook judge scores; falls back to the
    webhook-only scoreboard when the key isn't configured. ?judge=1 scores up to
    5 recent unjudged calls on demand."""
    if not _authorized(request):
        return _unauthorized()
    try:
        days = max(1, min(60, int(request.query_params.get("days", "7"))))
    except Exception:
        days = 7
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
    except Exception:
        limit = 50
    configured = bool(os.getenv("RETELL_API_KEY", ""))
    calls, transcripts = [], {}
    if configured:
        since_ms = int((time.time() - days * 86400) * 1000)
        data = await _retell_api("/v3/list-calls", {
            "filter_criteria": {"start_timestamp": {"op": "gt", "type": "number", "value": since_ms}},
            "sort_order": "descending", "limit": limit})
        for c in (data or {}).get("items") or []:
            rec = await _qa_ingest(c, judge=False)      # fold metadata; judging is explicit below
            if rec:
                calls.append(rec)
                if c.get("transcript"):
                    transcripts[rec["call_id"]] = c["transcript"]
    if not calls:                                        # webhook-only fallback view
        calls = sorted(_QA_CALLS.values(), key=lambda r: r.get("ts") or 0, reverse=True)[:limit]
    if request.query_params.get("judge") == "1":
        for rec in [r for r in calls if not r.get("judge") and transcripts.get(r["call_id"])][:5]:
            rec["judge"] = await _judge_transcript(transcripts[rec["call_id"]])
            _qa_store(rec["call_id"], rec)
    judged = [r["judge"] for r in calls if r.get("judge")]
    lat = sorted(r["latency_p50"] for r in calls if isinstance(r.get("latency_p50"), (int, float)))
    costs = [r["cost_cents"] for r in calls if isinstance(r.get("cost_cents"), (int, float))]
    sentiment: dict[str, int] = {}
    for r in calls:
        s = r.get("sentiment") or "Unknown"
        sentiment[s] = sentiment.get(s, 0) + 1
    summary = {
        "calls": len(calls),
        "judged": len(judged),
        "avg_score": round(sum(j["score"] for j in judged) / len(judged), 1) if judged else None,
        "gate_held_pct": round(100 * sum(1 for j in judged if j["identity_gate_held"]) / len(judged)) if judged else None,
        "flags": {
            "gate_broken": sum(1 for j in judged if not j.get("identity_gate_held")),
            "over_promised": sum(1 for j in judged if j.get("over_promised")),
            "wrong_info": sum(1 for j in judged if j.get("wrong_info")),
            "caller_frustrated": sum(1 for j in judged if j.get("caller_frustrated")),
        },
        "median_latency_p50_ms": (lat[len(lat) // 2] if lat else None),
        "total_cost_usd": round(sum(costs) / 100.0, 2) if costs else None,   # Retell reports cents
        "avg_duration_s": round(sum(r.get("duration_ms") or 0 for r in calls) / len(calls) / 1000.0, 1) if calls else None,
        "sentiment": sentiment,
    }
    slim = [{k: r.get(k) for k in ("call_id", "ts", "duration_ms", "sentiment", "successful",
                                   "disconnection_reason", "latency_p50", "cost_cents",
                                   "in_voicemail", "judge")} for r in calls]
    return JSONResponse({"configured": configured, "days": days,
                         "summary": summary, "calls": slim})


_VOICEQA_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Voice QA - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--line:#e1e3e4;--ink:#121212;--head:#164899;--mut:#696b85;--bg:#f1f2f8}
h1,h2,h3,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);padding:16px 20px}
header .eyebrow{color:var(--orange);font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
header h1{margin:2px 0 0;font-size:22px;color:var(--head)}
.wrap{max-width:980px;margin:0 auto;padding:18px 14px 40px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
select,button,input{font:inherit;font-size:16px;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:#fff}
button{background:var(--navy);border-color:var(--navy);color:#fff;cursor:pointer}button.sec{background:#fff;color:var(--navy)}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.stat .n{font-size:22px;font-weight:700;color:var(--navy)}.stat .l{font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}.mut{color:var(--mut)}.bad{color:#b42318;font-weight:600}.ok{color:#137a4c;font-weight:600}
#keybox{display:none;gap:8px;align-items:center;margin-bottom:14px}
.pill{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:1px 8px;font-size:12px;margin:1px 2px 0 0;background:#f7f8fc}
@media(max-width:620px){table{font-size:12px}th,td{padding:6px 4px}}
</style></head><body>
<header><div class=eyebrow>Staff &middot; Internal</div><h1>Voice QA &mdash; every call, scored</h1></header>
<div class=wrap>
 <div id=keybox><label>Staff key</label><input type=password id=key placeholder="x-utrucking-key"><button onclick="saveKey()">Unlock</button></div>
 <div class=bar>
  <label class=mut>Window</label>
  <select id=days onchange="load(false)"><option value=1>1 day</option><option value=7 selected>7 days</option><option value=30>30 days</option></select>
  <button class=sec onclick="load(false)">Refresh</button>
  <button onclick="load(true)">Score recent calls</button>
  <span id=msg class=mut></span>
 </div>
 <div id=out></div>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function hdrs(){var h={'Content-Type':'application/json'};var k=localStorage.getItem('utk');if(k)h['x-utrucking-key']=k;return h;}
function saveKey(){localStorage.setItem('utk',document.getElementById('key').value.trim());document.getElementById('keybox').style.display='none';load(false);}
async function load(judge){var m=document.getElementById('msg');m.textContent=judge?'Scoring up to 5 recent calls…':'Loading…';
 try{
  var d=document.getElementById('days').value;
  var r=await fetch('/voice_qa_api?days='+d+(judge?'&judge=1':''),{headers:hdrs()});
  if(r.status===401){document.getElementById('keybox').style.display='flex';m.textContent='Staff key required.';return;}
  var x=await r.json();m.textContent='';render(x);
 }catch(e){m.innerHTML='<span class=bad>Could not load - try again.</span>';}}
function render(x){var s=x.summary||{},f=s.flags||{};var h='';
 if(!x.configured)h+='<div class=card><b>Live call pull not configured.</b> <span class=mut>Set RETELL_API_KEY in the service environment to pull call history; the scoreboard below shows only webhook-reported calls since the last restart.</span></div>';
 h+='<div class=card><div class=grid>'
  +'<div class=stat><div class=n>'+(s.calls||0)+'</div><div class=l>Calls ('+esc(x.days)+'d)</div></div>'
  +'<div class=stat><div class=n>'+(s.avg_score==null?'–':s.avg_score)+'</div><div class=l>Avg QA score</div></div>'
  +'<div class=stat><div class=n>'+(s.gate_held_pct==null?'–':s.gate_held_pct+'%')+'</div><div class=l>Identity gate held</div></div>'
  +'<div class=stat><div class=n>'+(s.median_latency_p50_ms==null?'–':s.median_latency_p50_ms+'ms')+'</div><div class=l>Median latency p50</div></div>'
  +'<div class=stat><div class=n>'+(s.avg_duration_s==null?'–':s.avg_duration_s+'s')+'</div><div class=l>Avg duration</div></div>'
  +'<div class=stat><div class=n>'+(s.total_cost_usd==null?'–':'$'+s.total_cost_usd)+'</div><div class=l>Total call cost</div></div>'
  +'</div></div>';
 var flags=[['gate_broken','Gate broken'],['over_promised','Over-promised'],['wrong_info','Wrong info'],['caller_frustrated','Frustrated caller']]
   .filter(function(p){return f[p[0]];});
 if(flags.length)h+='<div class=card><h3 style="margin:0 0 6px">Flags</h3>'+flags.map(function(p){return '<span class="pill bad">'+p[1]+': '+f[p[0]]+'</span>';}).join(' ')+'</div>';
 var sn=s.sentiment||{};var sk=Object.keys(sn);
 if(sk.length)h+='<div class=card><h3 style="margin:0 0 6px">Caller sentiment</h3>'+sk.map(function(k){return '<span class=pill>'+esc(k)+': '+sn[k]+'</span>';}).join(' ')+'</div>';
 var rows=(x.calls||[]);
 h+='<div class=card><h3 style="margin:0 0 8px">Calls</h3>';
 if(!rows.length)h+='<p class=mut>No calls in this window yet.</p>';
 else{h+='<div style="overflow-x:auto"><table><thead><tr><th>When</th><th>Dur</th><th>Sentiment</th><th>Score</th><th>Verdict</th></tr></thead><tbody>';
  h+=rows.map(function(r){var j=r.judge||null;
   var when=r.ts?new Date(r.ts).toLocaleString():'–';
   var sc=j?(j.score+(j.identity_gate_held?'':' <span class=bad>gate!</span>')):'<span class=mut>unscored</span>';
   var v=j?esc(j.one_line)+(j.issues&&j.issues.length?' <span class=mut>('+esc(j.issues.join('; '))+')</span>':''):'<span class=mut>'+esc(r.disconnection_reason||'')+'</span>';
   return '<tr><td>'+when+'</td><td>'+(r.duration_ms?Math.round(r.duration_ms/1000)+'s':'–')+'</td><td>'+esc(r.sentiment||'–')+'</td><td>'+sc+'</td><td>'+v+'</td></tr>';}).join('');
  h+='</tbody></table></div>';}
 h+='</div>';
 document.getElementById('out').innerHTML=h;}
load(false);
</script></body></html>"""


_DASH_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>University Trucking · AI Toolkit</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');
:root{
 --bg:#f1f2f8;--surface:#fff;--line:#e1e3e4;--line2:#eef0f3;
 --ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;
 --brand:#164899;--brand-ink:#0b2154;--accent:#006eff;
 --ring:rgba(22,72,153,.20);
 --sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
 --sh-h:0 10px 24px rgba(16,24,40,.12),0 2px 6px rgba(16,24,40,.05)}
*{box-sizing:border-box}html,body{height:100%}
h1,.brand,.card h2{font-family:'Inclusive Sans','Inter',sans-serif}
body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--ink);
 background:var(--bg);min-height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
/* top bar */
.top{background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.top .in{max-width:1040px;margin:0 auto;padding:11px 24px;display:flex;align-items:center;gap:12px}
.logo{height:34px;width:auto;display:block}
.chip{margin-left:auto;font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--brand);
 background:#eaf0fb;border:1px solid #d3e0f5;border-radius:999px;padding:5px 11px}
/* royal-blue hero band (matches the official site) */
.hero{background:var(--brand);color:#fff;position:relative;overflow:hidden}
.hero:after{content:"";position:absolute;top:-50px;right:-40px;width:360px;height:320px;opacity:.13;pointer-events:none;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='52' viewBox='0 0 60 52'%3E%3Cpath d='M15 1 L45 1 L60 26 L45 51 L15 51 L0 26 Z' fill='none' stroke='%23ffffff' stroke-width='2'/%3E%3C/svg%3E");background-size:58px 50px}
.hero .in{max-width:1040px;margin:0 auto;padding:42px 24px;position:relative;z-index:1}
.hero .eyebrow{color:#a8c4e6}
.hero h1{color:#fff;font-size:30px}
.hero .lead{color:#dbe6f7}
/* home */
#home{flex:1;overflow:auto;-webkit-overflow-scrolling:touch}
.wrap{max-width:1040px;margin:0 auto;padding:30px 24px 64px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--accent)}
h1{margin:9px 0 0;font-size:28px;font-weight:700;letter-spacing:-.02em;color:var(--head)}
.lead{margin:10px 0 0;color:var(--mut);font-size:15px;line-height:1.55;max-width:620px}
.grid{margin-top:4px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.card{display:flex;gap:14px;align-items:flex-start;text-align:left;width:100%;background:var(--surface);
 border:1px solid var(--line);border-radius:12px;padding:17px;cursor:pointer;color:var(--ink);font:inherit;
 box-shadow:var(--sh);transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
.card:hover{transform:translateY(-2px);box-shadow:var(--sh-h);border-color:#d3d8df}
.card:focus-visible{outline:2px solid var(--ring);outline-offset:2px}
.card .ic{flex:none;width:40px;height:40px;border-radius:50%;background:var(--brand);border:1px solid var(--brand);
 display:flex;align-items:center;justify-content:center}
.card .ic svg{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.card .tx{flex:1;min-width:0}
.card h2{margin:1px 0 3px;font-size:15px;font-weight:640;color:var(--head)}
.card p{margin:0;color:var(--mut);font-size:12.5px;line-height:1.5}
.card .go{color:var(--soft);font-size:16px;align-self:center;transition:transform .16s,color .16s}
.card:hover .go{transform:translateX(2px);color:var(--brand)}
.note{margin-top:26px;padding:14px 17px;background:var(--surface);border:1px solid var(--line);border-radius:12px;
 color:var(--mut);font-size:12.5px;line-height:1.6}
.note b{color:var(--brand-ink);font-weight:600}
/* tool view */
#view{display:none;flex:1;flex-direction:column;height:100vh;height:100dvh}
#bar{background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;padding:10px 16px}
#bar button{display:flex;align-items:center;gap:7px;background:var(--surface);color:var(--brand-ink);
 border:1px solid var(--line);border-radius:8px;padding:8px 13px;font-weight:600;cursor:pointer;font-size:13.5px;font-family:inherit}
#bar button:hover{background:#f2f4f7;border-color:#d3d8df}
#bar .t{font-weight:600;font-size:14px;color:var(--head)}
#bar .esc{margin-left:auto;color:var(--soft);font-size:11px;letter-spacing:.05em}
#frame{flex:1;border:0;width:100%;background:#fff}
@media (max-width:560px){.wrap{padding:28px 16px 44px}.grid{grid-template-columns:1fr}.top .in{padding:12px 16px}#bar .esc{display:none}}
</style></head><body>
<div id=home>
 <div class=top><div class=in>
  <a href="/" style="text-decoration:none;display:block"><img class=logo src="/brand/logo.jpg" alt="University Trucking"></a>
  <div class=chip>Internal · Test</div>
 </div></div>
 <div class=hero><div class=in>
  <div class=eyebrow>Internal Tools &middot; Testing</div>
  <h1>AI Toolkit</h1>
  <p class=lead>Every tool in one place &mdash; customer quotes, pickups and order lookup, live business intelligence, crew dispatch, a staff console and condition docs. Chat and voice run the live phone agent's brain for free testing.</p>
 </div></div>
 <div class=wrap>
  <div class=grid>
   <button class=card onclick="op('/chat','Assistant chat')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-8 8H4l2.4-2.7A8 8 0 1 1 21 12z"/><path d="M8.5 10.5h7M8.5 13.5h4.5"/></svg></span>
    <span class=tx><h2>Assistant chat</h2><p>The live phone agent's brain, in text &mdash; quotes, pickups &amp; verified order lookup. Test it here free.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/chat?voice=1','Voice assistant')">
    <span class=ic><svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></span>
    <span class=tx><h2>Voice assistant</h2><p>Same as calling the live agent &mdash; test by voice with zero per-minute cost.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/estimate','Instant estimate')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.4"/></svg></span>
    <span class=tx><h2>Instant estimate</h2><p>Photo, description, or both &rarr; an itemized price in seconds.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/ask','Ask your data')">
    <span class=ic><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2M11 8.2a2.8 2.8 0 1 1-.01 5.6"/></svg></span>
    <span class=tx><h2>Ask your data</h2><p>Plain-English questions on revenue, demand &amp; pricing.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/insights','Business insights')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M4 20V9M10 20V4M16 20v-8M21 20H3"/></svg></span>
    <span class=tx><h2>Business insights</h2><p>Live revenue, funnel, demand forecast and data quality.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/ops','Ops command center')">
    <span class=ic><svg viewBox="0 0 24 24"><rect x="2.5" y="8" width="12" height="9"/><path d="M14.5 10h4L21 13v4h-2M2.5 17h12M7 20.5a1.8 1.8 0 1 0 .01 0M17 20.5a1.8 1.8 0 1 0 .01 0"/></svg></span>
    <span class=tx><h2>Ops command center</h2><p>Staff: daily crew plan, building routes &amp; printable run sheets.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/staff','Staff console')">
    <span class=ic><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></span>
    <span class=tx><h2>Staff console</h2><p>One glance: today's pickups, revenue to recover, forecast &amp; data health.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/condition','Condition docs')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M12 3l7 3v5c0 4.4-3 7.4-7 9-4-1.6-7-4.6-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg></span>
    <span class=tx><h2>Condition docs</h2><p>Staff: photograph an item &rarr; AI logs its condition for dispute protection.</p></span><span class=go>&rsaquo;</span></button>
   <button class=card onclick="op('/voiceqa','Voice QA')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M4 13a8 8 0 0 1 16 0"/><rect x="2.5" y="13" width="4" height="7" rx="1.5"/><rect x="17.5" y="13" width="4" height="7" rx="1.5"/></svg></span>
    <span class=tx><h2>Voice QA</h2><p>Staff: every call auto-scored &mdash; identity gate, accuracy, sentiment, latency &amp; cost.</p></span><span class=go>&rsaquo;</span></button>
  </div>
  <p class=note><b>Chat &amp; Voice are the live phone agent</b> &mdash; same brain, same data, here for free testing so no call minutes or tokens are burned. <b>Live data</b> &mdash; order details are shared only after identity verification.</p>
 </div>
</div>
<div id=view>
 <div id=bar><button onclick=back()><svg width=15 height=15 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2.2 stroke-linecap=round stroke-linejoin=round><path d="M19 12H5M11 18l-6-6 6-6"/></svg>Back</button><span class=t id=vtitle></span><span class=esc>ESC to return</span></div>
 <iframe id=frame title="tool"></iframe>
</div>
<script>
function op(url,title){document.getElementById('frame').src=url;document.getElementById('vtitle').textContent=title;
 document.getElementById('home').style.display='none';document.getElementById('view').style.display='flex';}
function back(){document.getElementById('view').style.display='none';document.getElementById('frame').src='about:blank';
 document.getElementById('home').style.display='';}
document.addEventListener('keydown',function(e){if(e.key==='Escape')back();});
</script></body></html>"""


_OPS_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Ops Command Center - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--line:#e1e3e4;--ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;--bg:#f1f2f8}
h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:18px 20px}
header b{font-size:18px;letter-spacing:-.01em}header .s{display:block;color:var(--mut);font-size:12.5px;margin-top:3px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange);margin-bottom:2px}
main{max-width:960px;margin:0 auto;padding:16px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.controls label{font-size:13px;color:var(--mut)}
input[type=date],input[type=password]{border:1px solid #d3d8df;border-radius:8px;padding:9px 10px;font:inherit;font-size:15px;color:var(--ink)}
button{background:var(--navy);color:#fff;border:0;border-radius:9px;padding:10px 16px;font-weight:600;cursor:pointer;font-family:inherit}
button:hover{background:#0f3b80}
button.ghost{background:#eef1f5;color:var(--navy)}button.ghost:hover{background:#e4e8ee}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:12px 0}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.stat .n{font-size:21px;font-weight:700;color:var(--navy)}.stat .l{color:var(--mut);font-size:12px;margin-top:2px}
.crew{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0;break-inside:avoid;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.crew h3{margin:0 0 6px;color:var(--head);font-size:15px;display:flex;justify-content:space-between;font-weight:640}
.crew h3 .ct{color:var(--mut);font-weight:600;font-size:13px}
.bld{margin:8px 0 2px;font-weight:640;font-size:14px;color:var(--navy);cursor:pointer}
.bld .n{color:var(--mut);font-weight:500;font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:4px 0 8px}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.mut{color:var(--mut);font-size:12.5px}.err{color:#b42318;font-size:14px;margin:10px 0}
#keybox{display:none}
@media print{header,.controls,#keybox{display:none}body{background:#fff}.crew{border:0;page-break-inside:avoid}
 .crew h3{border-bottom:2px solid #000}main{max-width:none;padding:0}}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Ops Command Center</b><span class=s>Daily dispatch plan - crews, buildings, run sheets (staff only)</span></header>
<main>
 <div class=controls>
  <label>Pickup day</label><input type=date id=day>
  <button onclick=load()>Build plan</button>
  <button class=ghost onclick=window.print()>Print run sheets</button>
  <button class=ghost onclick=exportCSV()>Export CSV</button>
  <span class=mut id=msg></span>
 </div>
 <div class=controls id=keybox>
  <label>Staff key</label><input type=password id=key placeholder="x-utrucking-key">
  <button onclick="saveKey()">Unlock</button>
  <span class=mut>Ask the admin for the ops key.</span>
 </div>
 <div id=out></div>
</main><script>
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function hdrs(){var h={'Content-Type':'application/json'};var k=localStorage.getItem('utk');if(k)h['x-utrucking-key']=k;return h;}
function saveKey(){localStorage.setItem('utk',document.getElementById('key').value.trim());document.getElementById('keybox').style.display='none';load();}
async function load(){
 var d=document.getElementById('day').value;if(!d)return;
 var m=document.getElementById('msg');m.textContent='Building plan...';
 try{
  var r=await fetch('/dispatch_plan',{method:'POST',headers:hdrs(),body:JSON.stringify({args:{date:d}})});
  if(r.status===401){document.getElementById('keybox').style.display='flex';m.textContent='Staff key required.';return;}
  var p=await r.json();LASTP=p;LASTD=d;m.textContent='';render(p,d);
 }catch(e){m.textContent='Could not load the plan - try again.';}}
var LASTP=null,LASTD='';
function render(p,d){
 var h='';
 var util=p.utilization_pct||0, uc=(util>100?'#b42318':'#164899');
 h+='<div class=grid>'
  +'<div class=stat><div class=n>'+(p.total_stops||0)+'</div><div class=l>Stops booked</div></div>'
  +'<div class=stat><div class=n>'+(p.buildings||0)+'</div><div class=l>Buildings</div></div>'
  +'<div class=stat><div class=n>'+(p.crews_available||0)+'</div><div class=l>Crews scheduled</div></div>'
  +'<div class=stat><div class=n>'+(p.capacity||0)+'</div><div class=l>Modeled capacity</div></div>'
  +'<div class=stat><div class=n style="color:'+uc+'">'+util+'%</div><div class=l>Capacity used</div></div>'
  +'</div>';
 if(util>100){h+='<p class=mut>Booked exceeds modeled capacity ('+p.jobs_per_crew+' stops/crew &times; '+p.crews_available+' crews). Set real crew counts in engines.CREW_SCHEDULE for accurate planning.</p>';}
 if(!p.total_stops){h+='<p class=mut>No pickups booked on '+esc(d)+'.</p>';document.getElementById('out').innerHTML=h;return;}
 var byB={};(p.route||[]).forEach(function(x){byB[x.building]=x;});
 (p.crew_plan||[]).forEach(function(c){
  if(!c.buildings.length)return;
  h+='<div class=crew><h3>Crew '+c.crew+'<span class=ct>'+c.stops+' stop'+(c.stops==1?'':'s')+' &middot; '+c.buildings.length+' building'+(c.buildings.length>1?'s':'')+'</span></h3>';
  c.buildings.forEach(function(b){
   var x=byB[b]||{orders:[]};
   h+='<div class=bld>'+esc(b)+' <span class=n>('+x.stops+' stop'+(x.stops==1?'':'s')+')</span></div>';
   h+='<table><thead><tr><th>#</th><th>Student</th><th>Room</th><th>Order</th><th>Service</th></tr></thead><tbody>';
   (x.orders||[]).forEach(function(o){h+='<tr><td>'+(o.seq||'')+'</td><td>'+esc(o.student)+'</td><td>'+esc(o.room)+'</td><td>'+esc(o.order_id)+'</td><td>'+esc(o.service)+'</td></tr>';});
   h+='</tbody></table>';
  });
  h+='</div>';});
 document.getElementById('out').innerHTML=h;}
function exportCSV(){
 if(!LASTP||!LASTP.total_stops)return;
 var byB={};(LASTP.route||[]).forEach(function(x){byB[x.building]=x;});
 var rows=[['Crew','Building','Seq','Student','Room','Order','Service']];
 (LASTP.crew_plan||[]).forEach(function(c){(c.buildings||[]).forEach(function(b){
  var x=byB[b]||{orders:[]};(x.orders||[]).forEach(function(o){
   rows.push([c.crew,b,o.seq||'',o.student||'',o.room||'',o.order_id||'',o.service||'']);});});});
 var csv=rows.map(function(r){return r.map(function(c){c=String(c==null?'':c);if(/^[=+\-@\t\r]/.test(c))c="'"+c;return /[",\r\n]/.test(c)?'"'+c.replace(/"/g,'""')+'"':c;}).join(',');}).join('\n');
 var a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
 a.download='utrucking-runsheet-'+(LASTD||'')+'.csv';a.click();}
(function(){var d=document.getElementById('day');d.value='2026-05-07';})();
</script></body></html>"""


@mcp.custom_route("/ops", methods=["GET"])
async def ops_page(request: Request):
    """Staff-only ops view over /dispatch_plan (that endpoint enforces the staff key when set)."""
    return HTMLResponse(_OPS_HTML)


_STAFF_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Staff Console - UTrucking</title><style>
@import url('https://fonts.googleapis.com/css2?family=Inclusive+Sans:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap');:root{--navy:#164899;--orange:#006eff;--line:#e1e3e4;--ink:#121212;--head:#164899;--mut:#696b85;--soft:#a0b3e3;--bg:#f1f2f8}
h1,h2,h3,h4,header b{font-family:'Inclusive Sans','Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;font-family:'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
header{background:#fff;border-bottom:1px solid var(--line);color:var(--head);padding:18px 20px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.09em;font-size:11px;font-weight:600;color:var(--orange)}
header b{font-size:18px;letter-spacing:-.01em}header .s{display:block;color:var(--mut);font-size:12.5px;margin-top:3px}
main{max-width:900px;margin:0 auto;padding:16px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.controls label{font-size:13px;color:var(--mut)}
input[type=date],input[type=password]{border:1px solid #d3d8df;border-radius:8px;padding:8px 10px;font:inherit;font-size:15px;color:var(--ink)}
button{background:var(--navy);color:#fff;border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer;font-family:inherit;font-size:13px}
button:hover{background:#0f3b80}
button.ghost{background:#eef1f5;color:var(--navy)}button.ghost:hover{background:#e4e8ee}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:12px 0}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.stat .n{font-size:20px;font-weight:700;color:var(--navy)}.stat .l{color:var(--mut);font-size:12px;margin-top:2px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card h3{margin:0 0 10px;color:var(--head);font-size:15px;display:flex;justify-content:space-between;font-weight:640}
.row{display:flex;gap:8px;margin:4px 0;font-size:13px}.row .lab{flex:1}.row .val{color:var(--mut);text-align:right}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-size:11px;text-transform:uppercase;font-weight:600}.mut{color:var(--mut);font-size:12.5px}.err{color:#b42318}
#keybox{display:none}
</style></head><body>
<header><img src="/brand/logo.jpg" alt="University Trucking" style="height:19px;width:auto;display:block;margin-bottom:6px"><b>Staff Console</b><span class=s>One glance: today's pickups, revenue to recover, forecast, data health (staff only)</span></header>
<main>
 <div class=controls>
  <label>Pickup day</label><input type=date id=day>
  <button onclick=load()>Refresh</button>
  <a href="/ops" style="text-decoration:none"><button class=ghost>Full run sheets &rsaquo;</button></a>
  <span class=mut id=msg></span>
 </div>
 <div class=controls id=keybox>
  <label>Staff key</label><input type=password id=key placeholder="x-utrucking-key">
  <button onclick=saveKey()>Unlock</button><span class=mut>Ask the admin for the ops key.</span>
 </div>
 <div id=out></div>
</main><script>
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function money(n){return '$'+Number(n||0).toLocaleString();}
function hdrs(){var h={'Content-Type':'application/json'};var k=localStorage.getItem('utk');if(k)h['x-utrucking-key']=k;return h;}
function saveKey(){localStorage.setItem('utk',document.getElementById('key').value.trim());document.getElementById('keybox').style.display='none';load();}
async function load(){
 var day=document.getElementById('day').value;var m=document.getElementById('msg');m.textContent='Loading...';
 try{
  var pr=fetch('/dispatch_plan',{method:'POST',headers:hdrs(),body:JSON.stringify({args:{date:day}})});
  var br=fetch('/billing_audit',{method:'POST',headers:hdrs(),body:JSON.stringify({args:{}})});
  var ir=fetch('/insights_api');
  var p=await pr;
  if(p.status===401){document.getElementById('keybox').style.display='flex';m.textContent='Staff key required.';return;}
  var plan=await p.json();var bill=await (await br).json();var ins=await (await ir).json();
  m.textContent='';render(plan,bill,ins,day);
 }catch(e){m.innerHTML='<span class=err>Could not load - try again.</span>';}}
function render(plan,bill,ins,day){
 var h='';var util=plan.utilization_pct||0,uc=(util>100?'#b42318':'#164899');
 h+='<div class=card><h3>Pickup day <span class=mut>'+esc(day||plan.date||'')+'</span></h3><div class=grid>'
  +'<div class=stat><div class=n>'+(plan.total_stops||0)+'</div><div class=l>Stops booked</div></div>'
  +'<div class=stat><div class=n>'+(plan.buildings||0)+'</div><div class=l>Buildings</div></div>'
  +'<div class=stat><div class=n>'+(plan.crews_available||0)+'</div><div class=l>Crews</div></div>'
  +'<div class=stat><div class=n style="color:'+uc+'">'+util+'%</div><div class=l>Capacity used</div></div>'
  +'</div>'+(plan.total_stops?'':'<p class=mut>No pickups booked that day.</p>')+'</div>';
 // revenue leak
 var bs=bill.summary||{};
 h+='<div class=card><h3>Revenue to recover <span class=mut>'+(bill.count||0)+' flagged</span></h3>'
  +'<div class=row><div class=lab>$0 / missing total</div><div class=val>'+(bs.zero_or_missing_total||0)+'</div></div>'
  +'<div class=row><div class=lab>Missing invoice</div><div class=val>'+(bs.missing_invoice||0)+'</div></div>'
  +'<div class=row><div class=lab>Missing order #</div><div class=val>'+(bs.missing_order_id||0)+'</div></div>';
 var fl=(bill.flagged||[]).slice(0,8);
 if(fl.length){h+='<table><thead><tr><th>Student</th><th>Order</th><th>Issue</th></tr></thead><tbody>'
  +fl.map(function(x){return '<tr><td>'+esc(x.student)+'</td><td>'+esc(x.order||'-')+'</td><td>'+esc((x.reasons||[]).join(', '))+'</td></tr>';}).join('')
  +'</tbody></table><div class=mut style="margin-top:6px">Fix these in the sheet to close the leak.</div>';}
 h+='</div>';
 // forecast
 var fc=(ins.forecast||{});var pw=(fc.peak_window||[]).slice(0,3);
 if(pw.length){h+='<div class=card><h3>Next-season forecast</h3>'
  +pw.map(function(x){return '<div class=row><div class=lab>'+esc(x.label)+'</div><div class=val>'+x.orders+' orders &middot; '+x.crews_needed+' crews</div></div>';}).join('')
  +'<div class=mut style="margin-top:6px">Return season (Aug): '+((fc.return_season||{}).orders||0)+' orders.</div></div>';}
 // data quality
 var dq=ins.data_quality||{};
 h+='<div class=card><h3>Data health</h3>'
  +'<div class=row><div class=lab>Unknown building</div><div class=val>'+(dq.unknown_building||0)+' ('+(dq.unknown_building_pct||0)+'%)</div></div>'
  +'<div class=row><div class=lab>Missing phone</div><div class=val>'+(dq.missing_phone||0)+' ('+(dq.missing_phone_pct||0)+'%)</div></div>'
  +'<div class=row><div class=lab>Duplicate names</div><div class=val>'+(dq.duplicate_named_customers||0)+'</div></div></div>';
 document.getElementById('out').innerHTML=h;}
(function(){document.getElementById('day').value='2026-05-07';load();})();
</script></body></html>"""


@mcp.custom_route("/staff", methods=["GET"])
async def staff_page(request: Request):
    """Staff-only unified console (pulls the key-gated /dispatch_plan + /billing_audit + aggregate /insights_api)."""
    return HTMLResponse(_STAFF_HTML)


@mcp.custom_route("/ask", methods=["GET"])
async def ask_page(request: Request):
    return HTMLResponse(_ASK_HTML)


@mcp.custom_route("/voiceqa", methods=["GET"])
async def voiceqa_page(request: Request):
    """Staff-only voice-QA scoreboard (pulls the key-gated /voice_qa_api)."""
    return HTMLResponse(_VOICEQA_HTML)


@mcp.custom_route("/insights", methods=["GET"])
async def insights_page(request: Request):
    return HTMLResponse(_INSIGHTS_HTML)


@mcp.custom_route("/app", methods=["GET"])
async def dashboard_page(request: Request):
    return HTMLResponse(_DASH_HTML)


app = mcp.streamable_http_app()
_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def combined_lifespan(app):
    async with _original_lifespan(app):
        task = asyncio.create_task(keep_alive())
        try:
            yield
        finally:
            task.cancel()


app.router.lifespan_context = combined_lifespan
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class _McpAuthMiddleware:
    """When API_SECRET is set, the MCP protocol endpoint requires the same staff
    key as the PII/ops HTTP endpoints (x-utrucking-key header, or
    Authorization: Bearer <key> for MCP clients that only speak Authorization).
    Unset = open — the same dormant-gate rollout as the rest of the API. The MCP
    tools themselves are the caller-safe set (redacted lookup + verified details
    + quote/availability/aggregate insights), so even the open state exposes
    nothing beyond the public HTTP surface."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = (scope.get("path") or "").rstrip("/")
        if scope.get("type") == "http" and API_SECRET and (path == "/mcp" or path.startswith("/mcp/")):
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers") or []}
            supplied = hdrs.get("x-utrucking-key", "")
            auth = hdrs.get("authorization", "")
            if not supplied and auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            if supplied != API_SECRET:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"status":"unauthorized","message":"The MCP endpoint needs the staff key."}'})
                return
        await self.app(scope, receive, send)


app = _McpAuthMiddleware(app)
