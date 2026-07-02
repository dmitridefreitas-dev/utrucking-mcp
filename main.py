import httpx
import json
import os
import asyncio
import csv
import io
import difflib
import re
import base64
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from engines import build_price_book, quote as _quote_items, availability as _availability, billing_audit as _billing_audit, dispatch_plan as _dispatch_plan

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
    return JSONResponse({
        "service": "UTrucking MCP Server",
        "status": "running",
        "endpoints": ["/lookup_student", "/health"]
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

async def _vision_items(provider, key, img_b64):
    async with httpx.AsyncClient(timeout=60.0) as c:
        if provider == "groq":
            r = await c.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={"model": "llama-3.2-90b-vision-preview", "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img_b64}}]}]})
            r.raise_for_status(); txt = r.json()["choices"][0]["message"]["content"]
        elif provider == "anthropic":
            r = await c.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1024, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}}]}]})
            r.raise_for_status(); txt = r.json()["content"][0]["text"]
        else:  # gemini (free tier at aistudio.google.com)
            # Key goes in a header, NOT the URL, so it can never leak into an error/log line.
            r = await c.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": [{"text": _VISION_PROMPT},
                    {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}]}]})
            r.raise_for_status(); txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    m = re.search(r'\{.*\}', txt, re.S)
    return (json.loads(m.group(0)).get("items", []) if m else [])


@mcp.custom_route("/photo_quote", methods=["POST", "GET"])
async def photo_quote_endpoint(request: Request):
    """A) Photo -> vision item detection -> itemized quote. Uses a FREE vision provider via env key."""
    if request.method == "GET":
        return JSONResponse({"endpoint": "/photo_quote", "method": "POST",
            "expects": {"args": {"image_url": "https://...", "image_base64": "...(alternative)"}},
            "env": {"VISION_PROVIDER": "gemini | groq | anthropic  (default gemini)",
                    "GEMINI_API_KEY": "free at aistudio.google.com"}})
    try: body = await request.json()
    except Exception: body = {}
    args = _extract_args(body)
    provider = os.getenv("VISION_PROVIDER", "gemini").lower()
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"status": "not_configured",
            "message": "Photo quotes need a free vision key. Set GEMINI_API_KEY (free at aistudio.google.com)."})
    img_b64 = args.get("image_base64")
    if not img_b64 and args.get("image_url"):
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                resp = await c.get(args["image_url"]); img_b64 = base64.b64encode(resp.content).decode()
        except Exception:
            return JSONResponse({"status": "error", "message": "Could not fetch image_url."})
    if not img_b64:
        return JSONResponse({"status": "error", "message": "Provide image_url or image_base64."})
    try:
        detected = await _vision_items(provider, key, img_b64)
    except Exception as e:
        # Never echo the API key back to a (public) caller, even if a provider puts it in an error.
        msg = str(e)[:200].replace(key, "***")
        return JSONResponse({"status": "error", "message": "Vision call failed: " + msg})
    service_rows = await fetch_csv_rows(SERVICE_CSV_URL)
    book = build_price_book(service_rows) if service_rows else {}
    result = _quote_items([(d.get("name", ""), d.get("qty", 1)) for d in detected], book)
    result["detected"] = detected
    return JSONResponse(result)


# ── Customer-facing instant-estimate page (photo OR text) ───────────
_ESTIMATE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>UTrucking - Instant Storage Estimate</title>
<style>
 :root{--navy:#14335f;--orange:#f5a623;--ink:#1f2933;--mut:#5b6b7f;--line:#e3e9f2}
 *{box-sizing:border-box} body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;color:var(--ink);background:#f5f7fb}
 .bar{height:6px;background:var(--orange)}
 header{background:var(--navy);color:#fff;padding:22px 20px}
 header .ey{text-transform:uppercase;letter-spacing:.16em;font-size:11px;font-weight:700;color:var(--orange)}
 header h1{margin:4px 0 0;font-size:22px} header p{margin:6px 0 0;color:#cdd9ee;font-size:14px}
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
 <div class="card"><h2>&#128247; Estimate from a photo</h2>
  <p class="hint">Take or upload one photo of your items. We detect them and price it automatically.</p>
  <input id="photo" class="file" type="file" accept="image/*" capture="environment"></div>
 <div class="or">- or -</div>
 <div class="card"><h2>&#9000; Estimate from a description</h2>
  <p class="hint">e.g. "five boxes, a mini fridge and two duffels"</p>
  <textarea id="items" placeholder="Tell us what you are storing..."></textarea>
  <button class="btn" onclick="quoteText()">Get my estimate</button></div>
 <div class="card" id="result"><h2>Your estimate</h2><div id="detected"></div><div id="body"></div></div>
</main>
<script>
 const $=id=>document.getElementById(id);
 async function postJSON(p,d){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});return r.json();}
 function toB64(f){return new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(String(fr.result).split(',')[1]);fr.onerror=rej;fr.readAsDataURL(f);});}
 function show(h){$('result').style.display='block';$('body').innerHTML=h;$('result').scrollIntoView({behavior:'smooth'});}
 function loading(m){$('detected').innerHTML='';show('<div class=spin>'+m+'</div>');}
 function render(data,fromPhoto){
  if(!data||data.status==='error'){show('<div class=err>Sorry - '+((data&&data.message)||'something went wrong')+'</div>');return;}
  if(data.status==='not_configured'){show('<div class=err>Photo estimates are not switched on yet. Try the text box above.</div>');return;}
  let det='';
  if(fromPhoto){const items=(data.detected||[]);
   det=items.length?'<p class=hint>We spotted: '+items.map(d=>'<span class=tag>'+(d.qty||1)+"x "+(d.name||'item')+'</span>').join('')+'</p>'
     :'<p class=hint>We could not clearly identify items in that photo - try the text box for an exact estimate.</p>';}
  $('detected').innerHTML=det;
  const li=data.line_items||[];
  if(!li.length&&!fromPhoto){show('<div class=err>No items recognized. Try rephrasing.</div>');return;}
  let rows=li.map(x=>'<tr><td>'+x.qty+"x "+x.item+'</td><td class=n>$'+Number(x.amount).toFixed(2)+'</td></tr>').join('');
  let html='<table><thead><tr><th>Item</th><th class=n>Est.</th></tr></thead><tbody>'+rows+'</tbody></table>'
   +'<div class=total><span class=lbl>Estimated total</span><span class=amt>$'+Number(data.total||0).toFixed(2)+'</span></div>'
   +'<p class=note>Instant estimate based on typical UTrucking pricing. Final price is confirmed at pickup. Ready to book? Call us and mention your estimate.</p>';
  show(html);
 }
 async function quoteText(){const t=$('items').value.trim();if(!t)return;loading('Pricing your items...');
  try{render(await postJSON('/quote',{args:{text:t}}),false);}catch(e){show('<div class=err>Network error. Please try again.</div>');}}
 $('photo').addEventListener('change',async e=>{const f=e.target.files[0];if(!f)return;loading('Looking at your photo...');
  try{const b=await toB64(f);render(await postJSON('/photo_quote',{args:{image_base64:b}}),true);}
  catch(err){show('<div class=err>Could not process that photo. Try another or use the text box.</div>');}});
</script></body></html>"""


@mcp.custom_route("/estimate", methods=["GET"])
async def estimate_page(request: Request):
    """Customer-facing instant-estimate mini-app: upload a photo OR type items -> price."""
    return HTMLResponse(_ESTIMATE_HTML)


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
