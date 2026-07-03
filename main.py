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
import analytics
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from engines import build_price_book, quote as _quote_items, availability as _availability, billing_audit as _billing_audit, dispatch_plan as _dispatch_plan, open_days as _open_days, season_bounds as _season_bounds, peak_date as _peak_date, merge_photo_text as _merge_photo_text

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

mcp = FastMCP("UTrucking Storage Lookup")


async def keep_alive():
    await asyncio.sleep(30)
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{RENDER_URL}/health")
        except Exception:
            pass
        await asyncio.sleep(14 * 60)


async def fetch_csv_rows(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        return []
    reader = csv.DictReader(io.StringIO(resp.text))
    return [row for row in reader]


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


async def do_lookup_student(name_heard: str) -> dict:
    if not name_heard or not name_heard.strip():
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
    return _build_order_result(name_heard, dispatch_rows, service_rows)


def _build_order_result(name_heard: str, dispatch_rows, service_rows) -> dict:
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

    dispatch_match = None
    for row in dispatch_rows:
        if clean(row.get("Student") or "").lower() == confirmed_lower:
            dispatch_match = row  # keep iterating — last row = most recent order

    service_match = None
    for row in service_rows:
        if clean(row.get("Student Name") or "").lower() == confirmed_lower:
            service_match = row  # keep iterating — last row = most recent order

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

    return {
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


def _extract_args(body: dict) -> dict:
    if "args" in body and isinstance(body["args"], dict):
        return body["args"]
    return body


@mcp.custom_route("/lookup_student", methods=["POST", "GET"])
async def lookup_student_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/lookup_student",
            "method": "POST",
            "expects": {"args": {"name_heard": "string"}},
            "returns": {
                "status": "found | confirm | not_found | error",
                "confirmed_name": "exact name from records",
                "message": "short summary (name, order, service)",
                "available_fields": ["order status", "pickup location", "..."],
                "...": "all order fields for agent follow-up answers"
            }
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    return JSONResponse(await do_lookup_student(args.get("name_heard", "")))


@mcp.custom_route("/debug_sheets", methods=["GET"])
async def debug_sheets(request: Request):
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


@mcp.tool()
async def lookup_student(name_heard: str) -> str:
    """
    Look up a UTrucking student order by the name heard over the phone.
    Handles fuzzy/misspelled names. Returns a short message (name, order ID, service)
    plus all order fields so the agent can answer any follow-up question without
    calling another function. Also returns available_fields listing what data exists.
    """
    return json.dumps(await do_lookup_student(name_heard))


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
    return JSONResponse(_quote_items(payload, book))


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
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    return JSONResponse(_billing_audit(service_rows))


@mcp.tool()
async def get_quote(items_text: str) -> str:
    """Estimate a storage/moving quote from a free-text item description
    (e.g. 'five boxes, a mini fridge and two duffels'). Returns itemized lines + total."""
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    book = build_price_book(service_rows) if service_rows else {}
    return json.dumps(_quote_items(items_text, book))


@mcp.tool()
async def check_availability(date: str, capacity: int = 0) -> str:
    """Check how busy a pickup date is and suggest open nearby days (steers callers off peak days)."""
    dispatch_rows = await fetch_csv_rows(DISPATCH_CSV_URL)
    return json.dumps(_availability(dispatch_rows, date, capacity_per_day=(capacity or None)))


@mcp.custom_route("/dispatch_plan", methods=["POST", "GET"])
async def dispatch_plan_endpoint(request: Request):
    """B-ops: cluster a day's pickups by building + suggested crew split (route optimizer)."""
    if request.method == "GET":
        return JSONResponse({"endpoint": "/dispatch_plan", "method": "POST",
                             "expects": {"args": {"date": "5/7/2026"}}})
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


async def _vision_items(provider, key, img_b64, mime="image/jpeg"):
    async with httpx.AsyncClient(timeout=60.0) as c:
        if provider == "groq":
            r = await _post_retry(c, "https://api.groq.com/openai/v1/chat/completions",
                {"Authorization": "Bearer " + key},
                {"model": "llama-3.2-90b-vision-preview", "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + img_b64}}]}]})
            txt = r.json()["choices"][0]["message"]["content"]
        elif provider == "anthropic":
            r = await _post_retry(c, "https://api.anthropic.com/v1/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
                {"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": img_b64}}]}]})
            txt = r.json()["content"][0]["text"]
        else:  # gemini (free tier at aistudio.google.com)
            # gemini-2.5-flash: multimodal + a live free tier (2.0-flash's free quota 429s).
            # Key goes in a header, NOT the URL, so it can never leak into an error/log line.
            model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            r = await _post_retry(c,
                "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                {"x-goog-api-key": key},
                {"contents": [{"parts": [{"text": _VISION_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": img_b64}}]}]})
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    m = re.search(r'\{.*\}', txt, re.S)
    return (json.loads(m.group(0)).get("items", []) if m else [])


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
    result["detected"] = detected
    return JSONResponse(result)


# ── Customer-facing instant-estimate page (photo OR text) ───────────
_ESTIMATE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>UTrucking - Instant Storage Estimate</title>
<style>
 :root{--navy:#14335f;--orange:#f5a623;--ink:#1f2933;--mut:#5b6b7f;--line:#e3e9f2;--space0:#070d1a;--space1:#16305c}
 *{box-sizing:border-box} body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;color:var(--ink);background:#f5f7fb}
 .bar{height:5px;background:linear-gradient(90deg,var(--orange),#ffc45e)}
 header{background:linear-gradient(150deg,var(--space0) 0%,#122a52 65%,var(--space1) 100%);color:#fff;padding:22px 20px;position:relative;overflow:hidden}
 header:after{content:"";position:absolute;inset:0;pointer-events:none;background-image:
  radial-gradient(1.2px 1.2px at 12% 30%,rgba(255,255,255,.7),transparent 60%),
  radial-gradient(1px 1px at 30% 72%,rgba(255,255,255,.5),transparent 60%),
  radial-gradient(1.4px 1.4px at 47% 22%,rgba(255,217,149,.8),transparent 60%),
  radial-gradient(1px 1px at 64% 62%,rgba(255,255,255,.45),transparent 60%),
  radial-gradient(1.2px 1.2px at 80% 32%,rgba(255,255,255,.6),transparent 60%),
  radial-gradient(1px 1px at 93% 70%,rgba(255,217,149,.6),transparent 60%)}
 header>*{position:relative}
 header .ey{text-transform:uppercase;letter-spacing:.28em;font-size:11px;font-weight:700;color:var(--orange)}
 header h1{margin:4px 0 0;font-size:22px;font-weight:650} header p{margin:6px 0 0;color:#aebfda;font-size:14px}
 .cardh{display:flex;align-items:center;gap:9px}
 .cardh svg{width:19px;height:19px;stroke:var(--navy);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:none}
 main{max-width:640px;margin:0 auto;padding:18px 16px 60px}
 .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 1px 3px rgba(20,51,95,.06)}
 .card h2{margin:0 0 4px;font-size:16px;color:var(--navy)} .card .hint{margin:0 0 12px;color:var(--mut);font-size:13px}
 textarea{width:100%;min-height:72px;border:1px solid var(--line);border-radius:10px;padding:10px;font:inherit;resize:vertical}
 .btn{background:var(--navy);color:#fff;border:0;border-radius:10px;padding:12px 18px;font-weight:700;font-size:15px;cursor:pointer;margin-top:10px}
 .btn:active{transform:translateY(1px)} .file{display:block;margin-top:6px;font:inherit}
 .or{text-align:center;color:var(--mut);font-size:12px;margin:6px 0;text-transform:uppercase;letter-spacing:.12em}
 table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
 th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 td.n,th.n{text-align:right}
 .total{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:12px;border-top:2px solid var(--navy)}
 .total .lbl{font-weight:700;color:var(--navy)} .total .amt{font-weight:800;font-size:22px;color:var(--navy)}
 .note{color:var(--mut);font-size:12px;margin-top:10px} .err{color:#b23b3b;font-size:14px;margin-top:8px}
 .spin{color:var(--mut);font-size:14px;margin-top:8px} #result{display:none}
 .tag{display:inline-block;background:#eef3fb;color:var(--navy);border-radius:20px;padding:3px 10px;font-size:12px;margin:3px 4px 0 0}
</style></head><body>
<div class="bar"></div>
<header><div class="ey">University Trucking</div>
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
   +'</td><td class=n>$'+Number(x.amount).toFixed(2)+'</td></tr>').join('');
  let extra=un.length?'<p class=note>Not priced (call us for these): '+un.join(', ')+'.</p>':'';
  if(data.capped) extra+='<p class=note>For more than '+data.capped+' of one item, call (314) 266-8878 for a bulk quote.</p>';
  let html='<table><thead><tr><th>Item</th><th class=n>Est.</th></tr></thead><tbody>'+rows+'</tbody></table>'
   +'<div class=total><span class=lbl>Estimated total</span><span class=amt>$'+Number(data.total||0).toFixed(2)+'</span></div>'
   +extra
   +'<p class=note>Instant estimate based on typical UTrucking pricing. Final price is confirmed at pickup. Ready to book? Call (314) 266-8878 and mention your estimate.</p>';
  show(html);
 }
 let photoB64=null;
 async function quoteNow(){
  const t=$('items').value.trim();
  if(photoB64){loading(t?'Combining your photo and notes...':'Looking at your photo...');
   try{const args={image_base64:photoB64};if(t)args.text=t;render(await postJSON('/photo_quote',{args:args}),true);}
   catch(e){show('<div class=err>Network error. Please try again.</div>');}
   return;}
  if(!t){show('<div class=err>Add a photo or tell us what you are storing.</div>');return;}
  loading('Pricing your items...');
  try{render(await postJSON('/quote',{args:{text:t}}),false);}catch(e){show('<div class=err>Network error. Please try again.</div>');}}
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


def _find_month(text):
    for name, mo in _MONTH_NAMES.items():
        if re.search(r"\b" + name + r"\b", text, re.I):
            return mo
    return None


def _last4(s):
    ds = re.sub(r"\D", "", s or "")
    return ds[-4:] if len(ds) >= 4 else ""


def _cv(x):
    """Drop placeholder values so the reveal reads cleanly."""
    x = (x or "").strip()
    return "" if x.lower() in ("(no date)", "n/a", "na", "-", "tbd", "none", "null") else x


def _reveal_order(rec):
    out = ["You're verified. Here's your order:"]
    st = _cv(rec.get("order_status")) or _cv(rec.get("dispatch_status"))
    if st: out.append("• Status: " + st)
    when = " ".join(x for x in [_cv(rec.get("date")), _cv(rec.get("time_slot"))] if x)
    where = " ".join(x for x in [_cv(rec.get("building")), _cv(rec.get("room"))] if x)
    if when or where:
        out.append("• Pickup: " + (when or "date TBD") + (" at " + where if where else ""))
    items = _cv(rec.get("items_list")) or _cv(rec.get("product")) or _cv(rec.get("boxes"))
    if items: out.append("• Items: " + items[:160])
    if _cv(rec.get("invoice_id")): out.append("• Invoice: " + rec["invoice_id"])
    if _cv(rec.get("order_id")): out.append("• Order #: " + rec["order_id"])
    out.append("Anything else?")
    return "\n".join(out)


def _lookup_flow(text, state, dispatch_rows, service_rows):
    if state.get("step") == "verify":
        rec = _build_order_result(state.get("name", ""), dispatch_rows, service_rows)
        if rec.get("status") != "found":
            return ("Sorry, I lost that record — what's the name again?", {"intent": "lookup", "step": "name"})
        b = (rec.get("building") or "").lower(); low = text.lower().strip()
        ok = bool(b) and len(low) >= 3 and (low in b or b in low)
        if rec.get("phone") and _last4(text) and _last4(text) == _last4(rec["phone"]):
            ok = True
        if ok:
            return (_reveal_order(rec), {})
        return ("That doesn't match what we have, so I can't share the order details. Please call the team at (314) 266-8878.", {})
    rec = _build_order_result(text, dispatch_rows, service_rows)
    if rec.get("status") == "found":
        ask = ("what building is your pickup at" if rec.get("building")
               else ("the last 4 digits of your phone" if rec.get("phone") else "your order number"))
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
    if _RE_LOOKUP.search(low):
        return ("Sure — I can check your order. What's the name on the order?", {"intent": "lookup", "step": "name"})
    if _RE_HOURS.search(low) and not _find_date(low) and not _find_month(low):
        return ("You can reach the UTrucking team at (314) 266-8878. Summer storage pickups run May–June. Want a quote, a pickup date, or your order status?", {})
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
        return ("Here's your estimate:\n%s\nTotal: about $%.2f.%s\nWant a pickup date?" % (lines, q["total"], ums), {})
    if q.get("unmatched"):
        return ("I couldn't find a price for: %s. I can price boxes, fridges, duffels, TVs, desks, couches, mattresses and more — what do you have?" % ", ".join(q["unmatched"]), {})
    return ("I can give you an instant quote, check pickup dates, or look up your order. Try \"quote 5 boxes and a mini fridge\", \"what days are open?\", or \"my order status\".", {})


@mcp.custom_route("/chat_api", methods=["POST"])
async def chat_api(request: Request):
    """Brain for the /chat SMS preview: quote + availability + identity-gated order lookup."""
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    state = args.get("state") if isinstance(args.get("state"), dict) else {}
    dispatch_rows, service_rows = await asyncio.gather(
        fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))
    book = build_price_book(service_rows) if service_rows else {}
    reply, new_state = _chat_reply(args.get("message", ""), state, dispatch_rows, service_rows, book)
    return JSONResponse({"reply": reply, "state": new_state})


# ── SMS-style web preview of the assistant (server-driven brain) ──
_CHAT_HTML = r"""<!doctype html><html lang=en><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>UTrucking Assistant - SMS Preview</title>
<style>
 :root{--navy:#14335f;--orange:#f5a623;--bot:#eef1f6;--me:#1e5aa8;--space0:#070d1a;--space1:#16305c}
 *{box-sizing:border-box} html,body{height:100%}
 body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;background:#f5f7fb;display:flex;flex-direction:column;height:100vh;height:100dvh}
 header{background:linear-gradient(150deg,var(--space0) 0%,#122a52 65%,var(--space1) 100%);color:#fff;padding:14px 16px;position:relative;overflow:hidden}
 header:after{content:"";position:absolute;inset:0;pointer-events:none;background-image:
  radial-gradient(1.2px 1.2px at 12% 30%,rgba(255,255,255,.7),transparent 60%),
  radial-gradient(1px 1px at 30% 72%,rgba(255,255,255,.5),transparent 60%),
  radial-gradient(1.4px 1.4px at 47% 22%,rgba(255,217,149,.8),transparent 60%),
  radial-gradient(1px 1px at 64% 62%,rgba(255,255,255,.45),transparent 60%),
  radial-gradient(1.2px 1.2px at 80% 32%,rgba(255,255,255,.6),transparent 60%),
  radial-gradient(1px 1px at 93% 70%,rgba(255,217,149,.6),transparent 60%)}
 header .ey{position:relative;text-transform:uppercase;letter-spacing:.28em;font-size:10px;font-weight:700;color:var(--orange)}
 header b{position:relative;font-size:16px;display:block;margin-top:2px} header .s{position:relative;display:block;color:#aebfda;font-size:12px;margin-top:2px}
 .note{background:#fff7e6;color:#8a6d3b;font-size:12px;text-align:center;padding:6px 10px}
 #log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;-webkit-overflow-scrolling:touch}
 .b{max-width:82%;padding:9px 13px;border-radius:16px;font-size:15px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}
 .bot{background:var(--bot);color:#1f2933;align-self:flex-start;border-bottom-left-radius:4px}
 .me{background:var(--me);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
 form{display:flex;gap:8px;padding:10px;background:#fff;border-top:1px solid #e3e9f2}
 input{flex:1;min-width:0;border:1px solid #cdd6e4;border-radius:20px;padding:11px 14px;font:inherit;font-size:16px}
 button{background:var(--navy);color:#fff;border:0;border-radius:20px;padding:0 18px;font-weight:700;cursor:pointer;font-family:inherit}
 #mic{flex:none;width:44px;height:44px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;
  background:rgba(20,51,95,.08);border:1px solid #cdd6e4;color:var(--navy)}
 #mic svg{width:19px;height:19px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
 #mic.rec{background:var(--orange);border-color:var(--orange);color:#fff;animation:recpulse 1.2s ease-in-out infinite}
 @keyframes recpulse{0%,100%{box-shadow:0 0 0 0 rgba(245,166,35,.5)}50%{box-shadow:0 0 0 8px rgba(245,166,35,0)}}
</style></head><body>
<header><span class=ey>University Trucking</span><b>Assistant</b><span class=s>SMS preview - test chat</span></header>
<div class=note>Preview only - no real texts are sent. Order lookups verify your identity, like the phone line.</div>
<div id=log></div>
<form id=f><button type=button id=mic title="Talk" aria-label="Talk"><svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></button><input id=t autocomplete=off placeholder="Text a message..."><button>Send</button></form>
<script>
 const log=document.getElementById('log');let state={};
 var VOICE=location.search.indexOf('voice=1')>=0;
 function bubble(cls,val){const d=document.createElement('div');d.className='b '+cls;d.textContent=val;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
 function speak(t){try{if('speechSynthesis'in window){window.speechSynthesis.cancel();var u=new SpeechSynthesisUtterance(String(t).replace(/[*_#]/g,''));u.rate=1.03;window.speechSynthesis.speak(u);}}catch(e){}}
 async function api(msg){const r=await fetch('/chat_api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({args:{message:msg,state:state}})});return r.json();}
 async function send(t){bubble('me',t);const wait=bubble('bot','...');
  try{const r=await api(t);state=r.state||{};wait.remove();var rep=r.reply||'...';bubble('bot',rep);if(VOICE)speak(rep);}
  catch(e){wait.remove();bubble('bot','Sorry, something went wrong - try again.');}}
 document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();var inp=document.getElementById('t');var t=inp.value.trim();if(!t){return;}inp.value='';send(t);});
 (function(){var SR=window.SpeechRecognition||window.webkitSpeechRecognition;var mic=document.getElementById('mic');
   if(!SR){mic.style.display='none';return;}
   var rec=new SR();rec.lang='en-US';rec.interimResults=false;rec.maxAlternatives=1;
   rec.onresult=function(e){var t=e.results[0][0].transcript;if(t)send(t);};
   rec.onend=function(){mic.classList.remove('rec');};rec.onerror=function(){mic.classList.remove('rec');};
   mic.addEventListener('click',function(){try{if(window.speechSynthesis)window.speechSynthesis.cancel();mic.classList.add('rec');rec.start();}catch(e){mic.classList.remove('rec');}});
 })();
 var GREET='Hi! I am the UTrucking assistant. I can quote items, check pickup dates, or look up your order. Try: "quote 5 boxes and a mini fridge", "what days are open?", or "where is my order?"';
 bubble('bot',GREET);
 if(VOICE){document.querySelector('header .s').textContent='Voice preview - tap the mic and talk';}
</script></body></html>"""


@mcp.custom_route("/chat", methods=["GET"])
async def chat_page(request: Request):
    """SMS-style web preview of the assistant (quote + availability). No PII, no real texts."""
    return HTMLResponse(_CHAT_HTML)


# ── Ideas #1-#7: analytics, Ask-your-data copilot, insights dashboard ──
async def _load_rows():
    return await asyncio.gather(fetch_csv_rows(DISPATCH_CSV_URL), fetch_csv_rows(SERVICE_CSV_URL))


@mcp.custom_route("/insights_api", methods=["GET"])
async def insights_api(request: Request):
    d, s = await _load_rows()
    return JSONResponse(analytics.compute_metrics(d, s) if (d or s) else {})


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
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await _post_retry(c, "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
                {"x-goog-api-key": key}, {"contents": [{"parts": [{"text": _ASK_PROMPT % (brief, q)}]}]})
        return JSONResponse({"answer": r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()})
    except Exception as e:
        return JSONResponse({"answer": "Couldn't reach the analyst model right now. Here's the data brief:\n\n" + brief,
                             "error": str(e)[:120].replace(key, "***")})


_ASK_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Ask Your Data - UTrucking</title><style>
:root{--navy:#14335f;--orange:#f5a623;--line:#e3e9f2;--mut:#5b6b7f;--space0:#070d1a;--space1:#16305c}
*{box-sizing:border-box}body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;background:#f5f7fb;color:#1f2933}
header{background:linear-gradient(150deg,var(--space0) 0%,#122a52 65%,var(--space1) 100%);color:#fff;padding:16px 18px;position:relative;overflow:hidden}
header:after{content:"";position:absolute;inset:0;pointer-events:none;background-image:
 radial-gradient(1.2px 1.2px at 12% 30%,rgba(255,255,255,.7),transparent 60%),
 radial-gradient(1px 1px at 30% 72%,rgba(255,255,255,.5),transparent 60%),
 radial-gradient(1.4px 1.4px at 47% 22%,rgba(255,217,149,.8),transparent 60%),
 radial-gradient(1px 1px at 64% 62%,rgba(255,255,255,.45),transparent 60%),
 radial-gradient(1.2px 1.2px at 80% 32%,rgba(255,255,255,.6),transparent 60%),
 radial-gradient(1px 1px at 93% 70%,rgba(255,217,149,.6),transparent 60%)}
header>*{position:relative}header b{font-size:17px}header .s{display:block;color:#aebfda;font-size:12px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.28em;font-size:10px;font-weight:700;color:var(--orange);margin-bottom:2px}
main{max-width:720px;margin:0 auto;padding:18px 16px 60px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.chip{background:#eef3fb;color:var(--navy);border:1px solid var(--line);border-radius:20px;padding:8px 13px;font-size:13px;cursor:pointer}
.chip:hover{border-color:var(--orange)}
form{display:flex;gap:8px;margin-top:10px}
input{flex:1;min-width:0;border:1px solid #cdd6e4;border-radius:10px;padding:12px;font:inherit;font-size:16px}
button{background:var(--navy);color:#fff;border:0;border-radius:10px;padding:0 18px;font-weight:700;cursor:pointer;font-family:inherit}
#ans{white-space:pre-wrap;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:14px;line-height:1.45;display:none}
.mut{color:var(--mut);font-size:13px}
</style></head><body>
<header><span class=ey>University Trucking</span><b>Ask Your Data</b><span class=s>Internal analyst - aggregate business stats</span></header>
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
:root{--navy:#14335f;--orange:#f5a623;--line:#e3e9f2;--mut:#5b6b7f;--space0:#070d1a;--space1:#16305c}
*{box-sizing:border-box}body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;background:#f5f7fb;color:#1f2933}
header{background:linear-gradient(150deg,var(--space0) 0%,#122a52 65%,var(--space1) 100%);color:#fff;padding:16px 18px;position:relative;overflow:hidden}
header:after{content:"";position:absolute;inset:0;pointer-events:none;background-image:
 radial-gradient(1.2px 1.2px at 12% 30%,rgba(255,255,255,.7),transparent 60%),
 radial-gradient(1px 1px at 30% 72%,rgba(255,255,255,.5),transparent 60%),
 radial-gradient(1.4px 1.4px at 47% 22%,rgba(255,217,149,.8),transparent 60%),
 radial-gradient(1px 1px at 64% 62%,rgba(255,255,255,.45),transparent 60%),
 radial-gradient(1.2px 1.2px at 80% 32%,rgba(255,255,255,.6),transparent 60%),
 radial-gradient(1px 1px at 93% 70%,rgba(255,217,149,.6),transparent 60%)}
header>*{position:relative}header b{font-size:17px}header .s{display:block;color:#aebfda;font-size:12px}
header .ey{display:block;text-transform:uppercase;letter-spacing:.28em;font-size:10px;font-weight:700;color:var(--orange);margin-bottom:2px}
main{max-width:900px;margin:0 auto;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:12px 0}
.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}
.stat .n{font-size:22px;font-weight:800;color:var(--navy)}.stat .l{color:var(--mut);font-size:12px;margin-top:2px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}
.card h3{margin:0 0 10px;color:var(--navy);font-size:15px}
.row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:13px}
.row .lab{width:130px;flex:none}.row .barwrap{flex:1;background:#eef3fb;border-radius:6px;height:16px;overflow:hidden}
.row .bar{height:16px;background:linear-gradient(90deg,var(--navy),#2c5aa0)}.row .val{width:78px;flex:none;text-align:right;color:var(--mut)}
.mut{color:var(--mut);font-size:12px}
@media (max-width:480px){.row .lab{width:96px;font-size:12px}.row .val{width:64px;font-size:12px}.stat .n{font-size:18px}}
</style></head><body>
<header><span class=ey>University Trucking</span><b>Business Insights</b><span class=s>Live from the DISPATCH + SERVICE sheets</span></header>
<main id=root><p class=mut>Loading live data...</p></main>
<script>
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function stat(n,l){return '<div class=stat><div class=n>'+esc(n)+'</div><div class=l>'+esc(l)+'</div></div>';}
function bars(items,labKey,valKey,fmt){var mx=Math.max.apply(null,items.map(function(x){return x[valKey];}).concat([1]));
 return items.map(function(x){var w=Math.round(100*x[valKey]/mx);return '<div class=row><div class=lab>'+esc(x[labKey])+'</div><div class=barwrap><div class=bar style="width:'+w+'%"></div></div><div class=val>'+(fmt?fmt(x[valKey]):x[valKey])+'</div></div>';}).join('');}
function money(n){return '$'+Number(n).toLocaleString();}
function card(t,i){return '<div class=card><h3>'+t+'</h3>'+i+'</div>';}
function render(m){var o=m.overview||{},dq=m.data_quality||{},fn=m.funnel||{},dem=m.demand||{},rp=m.repeat||{};var h='';
 h+='<div class=grid>'+stat(money(o.revenue),'Revenue (season)')+stat(money(o.avg_order),'Avg order')+stat(o.dispatch_orders,'Dispatch orders')+stat(rp.repeat_rate_pct+'%','Repeat customers')+stat(m.avg_items_per_order,'Avg items/order')+stat(dq.unknown_building,'Unknown buildings')+'</div>';
 h+=card('Revenue by building',bars(m.revenue_by_building||[],'building','revenue',money));
 h+=card('Top stored items',bars(m.top_items||[],'item','count'));
 h+=card('Frequently stored together (upsell signals)',(m.top_pairs||[]).map(function(x){return '<div class=row><div class=lab style="width:auto;flex:1">'+esc(x.a)+' + '+esc(x.b)+'</div><div class=val>'+x.count+' orders</div></div>';}).join(''));
 h+=card('Demand by month',bars(dem.by_month||[],'month','orders'));
 h+=card('Completion funnel','<div class=mut>orders '+fn.orders+' &rarr; dispatched '+fn.dispatched+' &rarr; completed '+fn.completed+' &rarr; invoiced '+fn.invoiced+' &middot; '+fn.flagged_billing+' billing flags</div>');
 h+=card('Data-quality scorecard','<div class=row><div class=lab style="flex:1">Unknown building</div><div class=val>'+dq.unknown_building+' ('+dq.unknown_building_pct+'%)</div></div><div class=row><div class=lab style="flex:1">Missing phone</div><div class=val>'+dq.missing_phone+' ('+dq.missing_phone_pct+'%)</div></div><div class=row><div class=lab style="flex:1">Missing invoice</div><div class=val>'+dq.missing_invoice+'</div></div><div class=row><div class=lab style="flex:1">$0 / missing total</div><div class=val>'+dq.zero_or_missing_total+'</div></div>');
 document.getElementById('root').innerHTML=h;}
fetch('/insights_api').then(function(r){return r.json();}).then(render).catch(function(){document.getElementById('root').innerHTML='<p class=mut>Could not load insights.</p>';});
</script></body></html>"""


_DASH_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>UTrucking &mdash; AI Toolkit</title><style>
:root{--bg0:#070d1a;--bg1:#0c1830;--panel:rgba(15,30,58,.55);--edge:rgba(146,171,205,.16);
 --ink:#e9eff9;--mut:#8ca3c0;--orange:#f5a623;--orange2:#ffc45e;--ring:rgba(245,166,35,.55)}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;color:var(--ink);
 background:radial-gradient(1200px 700px at 70% -10%,#16305c 0%,var(--bg1) 45%,var(--bg0) 100%);
 min-height:100vh;display:flex;flex-direction:column;overflow-x:hidden}
#stars{position:fixed;inset:0;z-index:0;pointer-events:none}
#home{position:relative;z-index:1;flex:1;overflow:auto;-webkit-overflow-scrolling:touch}
.wrap{max-width:900px;margin:0 auto;padding:40px 20px 56px}
.ey{text-transform:uppercase;letter-spacing:.34em;font-size:11px;font-weight:700;color:var(--orange);text-align:center}
h1{margin:10px 0 0;font-size:clamp(26px,5vw,38px);text-align:center;font-weight:650;letter-spacing:.01em}
.sub{margin:10px auto 0;color:var(--mut);font-size:15px;text-align:center;max-width:460px;line-height:1.5}
/* hub */
.hubwrap{display:flex;justify-content:center;margin:30px 0 8px}
.hub{position:relative;width:104px;height:104px}
.hub .core{position:absolute;inset:14px;border-radius:50%;display:flex;align-items:center;justify-content:center;
 font-weight:800;font-size:26px;letter-spacing:.02em;color:#0b1526;
 background:radial-gradient(circle at 34% 30%,var(--orange2),var(--orange) 62%,#c77f10);
 box-shadow:0 0 34px rgba(245,166,35,.35),0 0 90px rgba(245,166,35,.14);animation:pulse 4.5s ease-in-out infinite}
.hub .ring{position:absolute;inset:0;border-radius:50%;border:1px solid rgba(146,171,205,.35)}
.hub .ring2{position:absolute;inset:-18px;border-radius:50%;border:1px dashed rgba(146,171,205,.16)}
.hub .orb{position:absolute;inset:-18px;animation:spin 16s linear infinite}
.hub .orb i{position:absolute;top:-3px;left:50%;margin-left:-3px;width:7px;height:7px;border-radius:50%;
 background:var(--orange2);box-shadow:0 0 10px var(--orange)}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{box-shadow:0 0 30px rgba(245,166,35,.32),0 0 80px rgba(245,166,35,.12)}
 50%{box-shadow:0 0 44px rgba(245,166,35,.5),0 0 110px rgba(245,166,35,.2)}}
/* cards */
.cards{margin-top:34px;display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
.tool{position:relative;display:flex;gap:14px;align-items:flex-start;text-align:left;width:100%;
 background:var(--panel);border:1px solid var(--edge);border-radius:16px;padding:18px 34px 18px 16px;cursor:pointer;
 color:var(--ink);font:inherit;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
 transition:transform .25s ease,border-color .25s ease,box-shadow .25s ease;
 opacity:0;transform:translateY(16px);animation:rise .6s cubic-bezier(.2,.7,.3,1) forwards;will-change:transform}
.tool:nth-child(1){animation-delay:.05s}.tool:nth-child(2){animation-delay:.12s}.tool:nth-child(3){animation-delay:.19s}
.tool:nth-child(4){animation-delay:.26s}.tool:nth-child(5){animation-delay:.33s}
@keyframes rise{to{opacity:1;transform:translateY(0)}}
.tool:hover,.tool:focus-visible{border-color:var(--ring);box-shadow:0 6px 30px rgba(0,0,0,.35),0 0 0 1px var(--ring) inset;outline:none}
.tool:active{transform:translateY(1px) !important}
.tool .ic{flex:none;width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;
 background:rgba(245,166,35,.12);border:1px solid rgba(245,166,35,.25)}
.tool .ic svg{width:22px;height:22px;stroke:var(--orange2);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.tool h2{margin:1px 0 4px;font-size:16.5px;font-weight:650}
.tool p{margin:0;color:var(--mut);font-size:13px;line-height:1.45}
.tool .go{position:absolute;right:14px;top:50%;transform:translateY(-50%);color:var(--mut);font-size:18px;transition:transform .25s,color .25s}
.tool:hover .go{transform:translateY(-50%) translateX(3px);color:var(--orange2)}
.foot{margin-top:30px;color:var(--mut);font-size:12px;text-align:center;line-height:1.6}
.foot b{color:#b9c9de;font-weight:600}
/* tool view */
#view{display:none;position:relative;z-index:1;flex:1;flex-direction:column;height:100vh;height:100dvh}
#bar{background:rgba(9,17,33,.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
 border-bottom:1px solid var(--edge);display:flex;align-items:center;gap:12px;padding:10px 14px}
#bar button{display:flex;align-items:center;gap:7px;background:rgba(245,166,35,.14);color:var(--orange2);
 border:1px solid rgba(245,166,35,.3);border-radius:10px;padding:9px 14px;font-weight:700;cursor:pointer;font-size:14px;font-family:inherit}
#bar button:hover{background:rgba(245,166,35,.22)}
#bar .t{font-weight:600;font-size:15px}
#bar .esc{margin-left:auto;color:var(--mut);font-size:11.5px;letter-spacing:.06em}
#frame{flex:1;border:0;width:100%;background:#f5f7fb}
@media (max-width:560px){
 .wrap{padding:30px 14px 44px}.cards{grid-template-columns:1fr;gap:12px}
 .hub{width:88px;height:88px}.hub .core{font-size:22px}
 #bar .esc{display:none}}
@media (prefers-reduced-motion:reduce){
 .hub .orb,.hub .core{animation:none}.tool{animation:none;opacity:1;transform:none;transition:none}}
</style></head><body>
<canvas id=stars aria-hidden=true></canvas>
<div id=home>
 <div class=wrap>
  <div class=ey>University Trucking</div>
  <h1>AI Toolkit</h1>
  <p class=sub>Five tools, one place &mdash; quotes, pickups, order lookup and live business intelligence.</p>
  <div class=hubwrap><div class=hub aria-hidden=true>
   <div class=ring></div><div class=ring2></div>
   <div class=orb><i></i></div>
   <div class=core>UT</div>
  </div></div>
  <div class=cards>
   <button class=tool onclick="op('/chat','Assistant chat')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-8 8H4l2.4-2.7A8 8 0 1 1 21 12z"/><path d="M8.5 10.5h7M8.5 13.5h4.5"/></svg></span>
    <span><h2>Assistant chat</h2><p>Quotes, pickup dates &amp; identity-verified order lookup.</p></span><span class=go>&rsaquo;</span></button>
   <button class=tool onclick="op('/chat?voice=1','Voice assistant')">
    <span class=ic><svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></span>
    <span><h2>Voice assistant</h2><p>Talk to the same brain in your browser &mdash; hands-free.</p></span><span class=go>&rsaquo;</span></button>
   <button class=tool onclick="op('/estimate','Instant estimate')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.4"/></svg></span>
    <span><h2>Instant estimate</h2><p>Photo, description, or both &rarr; an itemized price in seconds.</p></span><span class=go>&rsaquo;</span></button>
   <button class=tool onclick="op('/ask','Ask your data')">
    <span class=ic><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2M11 8.2a2.8 2.8 0 1 1-.01 5.6"/></svg></span>
    <span><h2>Ask your data</h2><p>Plain-English questions on revenue, demand &amp; pricing.</p></span><span class=go>&rsaquo;</span></button>
   <button class=tool onclick="op('/insights','Business insights')">
    <span class=ic><svg viewBox="0 0 24 24"><path d="M4 20V9M10 20V4M16 20v-8M21 20H3"/></svg></span>
    <span><h2>Business insights</h2><p>Live revenue, funnel, demand and data-quality board.</p></span><span class=go>&rsaquo;</span></button>
  </div>
  <p class=foot><b>Live data.</b> Order details are only shared after identity verification &mdash; the same gate as the phone line.</p>
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
/* starfield with slow drift + pointer parallax */
(function(){
 var cv=document.getElementById('stars'),cx=cv.getContext('2d'),stars=[],W,H,px=0,py=0,tx=0,ty=0;
 var still=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
 function size(){var d=Math.min(window.devicePixelRatio||1,2);
  W=cv.width=innerWidth*d;H=cv.height=innerHeight*d;cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';
  stars=[];var n=Math.min(170,Math.floor(innerWidth*innerHeight/9000));
  for(var i=0;i<n;i++)stars.push({x:Math.random()*W,y:Math.random()*H,z:.3+Math.random()*.7,r:(.4+Math.random()*1.1)*d,tw:Math.random()*6.28});}
 function frame(t){cx.clearRect(0,0,W,H);px+=(tx-px)*.04;py+=(ty-py)*.04;
  for(var i=0;i<stars.length;i++){var s=stars[i];
   if(!still){s.x-=.014*s.z*(W/1200);if(s.x<0)s.x=W;}
   var a=.35+.45*(still?1:Math.abs(Math.sin(t/1400+s.tw)));
   cx.globalAlpha=a*s.z;cx.fillStyle=i%9==0?'#ffd995':'#dbe7f7';
   cx.beginPath();cx.arc(s.x+px*s.z*18,s.y+py*s.z*18,s.r,0,6.29);cx.fill();}
  cx.globalAlpha=1;if(!still)requestAnimationFrame(frame);}
 size();addEventListener('resize',size);
 addEventListener('pointermove',function(e){tx=(e.clientX/innerWidth-.5);ty=(e.clientY/innerHeight-.5);},{passive:true});
 if(still){frame(0);}else{requestAnimationFrame(frame);}
})();
/* gentle 3D tilt on pointer devices */
(function(){
 if(matchMedia('(hover: none)').matches)return;
 document.querySelectorAll('.tool').forEach(function(c){
  c.addEventListener('pointermove',function(e){var r=c.getBoundingClientRect();
   var x=(e.clientX-r.left)/r.width-.5,y=(e.clientY-r.top)/r.height-.5;
   c.style.transform='perspective(700px) rotateX('+(-y*5)+'deg) rotateY('+(x*6)+'deg) translateY(-2px)';});
  c.addEventListener('pointerleave',function(){c.style.transform='';});});
})();
</script></body></html>"""


@mcp.custom_route("/ask", methods=["GET"])
async def ask_page(request: Request):
    return HTMLResponse(_ASK_HTML)


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
