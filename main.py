import httpx
import json
import os
import asyncio
import csv
import io
import difflib
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
    f"/export?format=csv&gid={SERVICE_SHEET_GID}"
)

mcp = FastMCP("UTrucking Storage Lookup")


# ── Keep-alive ─────────────────────────────────────────────────────────────────
async def keep_alive():
    await asyncio.sleep(30)
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.get(f"{RENDER_URL}/health")
        except Exception:
            pass
        await asyncio.sleep(14 * 60)


# ── Sheet fetchers ─────────────────────────────────────────────────────────────
async def fetch_csv_rows(url: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        return []
    reader = csv.DictReader(io.StringIO(resp.text))
    return [row for row in reader]


def find_match(rows: list[dict], query: str, columns: list[str]) -> dict | None:
    q = query.strip().lower()
    matches = []
    for row in rows:
        for col in columns:
            value = (row.get(col) or "").strip().lower()
            if value and q in value:
                matches.append(row)
                break
    return matches[-1] if matches else None


def all_student_names() -> tuple[list[str], dict[str, str]]:
    """Helper — returns no values directly, used inside the verify endpoint."""
    return [], {}


# ── TOOL 1: verify_name ────────────────────────────────────────────────────────
async def do_verify_name(student_name: str) -> dict:
    """
    Check if a student name (or close match) exists in either sheet.
    Returns the exact spelling as recorded so the agent can confirm with the caller.
    Uses fuzzy matching to catch misspellings.
    """
    if not student_name:
        return {"verified": False, "message": "Please provide a student name."}

    query = student_name.strip().lower()

    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL),
            fetch_csv_rows(SERVICE_CSV_URL),
        )
    except Exception:
        return {"verified": False, "message": "Error reading sheets."}

    # Collect all unique student names from both sheets
    all_names = set()
    for row in dispatch_rows:
        name = (row.get("Student") or "").strip()
        if name:
            all_names.add(name)
    for row in service_rows:
        name = (row.get("Student Name") or "").strip()
        if name:
            all_names.add(name)

    if not all_names:
        return {"verified": False, "message": "No student records found in the system."}

    # 1. Exact match (case-insensitive substring)
    exact_matches = [n for n in all_names if query in n.lower()]
    if exact_matches:
        # Best exact match — longest matching name wins (more specific)
        best = sorted(exact_matches, key=len)[0]
        return {
            "verified": True,
            "exact_match": True,
            "confirmed_name": best,
            "message": f"Found a match. Is your name {best}?"
        }

    # 2. Fuzzy match — use difflib to find close names
    names_list = list(all_names)
    close = difflib.get_close_matches(student_name, names_list, n=3, cutoff=0.65)
    if close:
        if len(close) == 1:
            return {
                "verified": False,
                "exact_match": False,
                "suggestions": close,
                "message": f"I didn't find an exact match, but did you mean {close[0]}?"
            }
        return {
            "verified": False,
            "exact_match": False,
            "suggestions": close,
            "message": f"I didn't find an exact match. Did you mean one of these: {', '.join(close)}?"
        }

    # 3. No match at all
    return {
        "verified": False,
        "exact_match": False,
        "suggestions": [],
        "message": "No matching name found. Please spell it out letter by letter."
    }


# ── TOOL 2: find_order — minimal info ──────────────────────────────────────────
async def do_find_order(student_name: str) -> dict:
    if not student_name:
        return {"found": False, "message": "Please provide a student name."}

    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL),
            fetch_csv_rows(SERVICE_CSV_URL),
        )
    except Exception:
        return {"found": False, "message": "Error reading sheets."}

    dispatch_match = find_match(dispatch_rows, student_name, ["Student"])
    service_match  = find_match(service_rows, student_name, ["Student Name"])

    if not dispatch_match and not service_match:
        return {"found": False, "message": "No order found under that name."}

    order_id = "N/A"
    service = "N/A"

    if dispatch_match:
        order_id = dispatch_match.get("ID", "N/A")
        service  = dispatch_match.get("Service", "N/A")
    elif service_match:
        order_id = service_match.get("Order#:", "N/A")
        service  = service_match.get("Service Type", "N/A")

    return {"found": True, "order_id": order_id, "service": service}


# ── TOOL 3: get_order_detail ───────────────────────────────────────────────────
async def do_get_order_detail(student_name: str, category: str) -> dict:
    if not student_name or not category:
        return {"error": "Both student_name and category required."}

    category = category.strip().lower()

    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL),
            fetch_csv_rows(SERVICE_CSV_URL),
        )
    except Exception:
        return {"error": "Error reading sheets."}

    dispatch_match = find_match(dispatch_rows, student_name, ["Student"])
    service_match  = find_match(service_rows, student_name, ["Student Name"])

    if not dispatch_match and not service_match:
        return {"error": "Order not found."}

    cat_map = {
        "status":   ["status", "order status"],
        "location": ["location", "where", "pickup location", "building", "address"],
        "schedule": ["schedule", "when", "date", "time", "pickup time"],
        "items":    ["items", "stored items", "stuff", "belongings", "contents"],
        "invoice":  ["invoice", "cost", "bill", "price", "how much"],
        "dispatch": ["dispatch", "truck", "driver", "eta", "dispatch status"]
    }

    matched_cat = None
    for cat, synonyms in cat_map.items():
        if any(s in category for s in synonyms):
            matched_cat = cat
            break

    if not matched_cat:
        return {"error": f"Unknown category. Valid: status, location, schedule, items, invoice, dispatch."}

    if matched_cat == "status":
        status = (dispatch_match or {}).get("Status", "N/A") if dispatch_match else "N/A"
        return {"category": "status", "value": status,
                "message": f"The order status is {status}."}

    if matched_cat == "location":
        building = (dispatch_match or service_match or {}).get("Building", "N/A")
        room = (dispatch_match or service_match or {}).get("Room", "N/A")
        return {"category": "location", "building": building, "room": room,
                "message": f"Pickup is at {building}, Room {room}."}

    if matched_cat == "schedule":
        date = (dispatch_match or {}).get("Date", "N/A")
        time_slot = (dispatch_match or {}).get("Time Slot", "N/A")
        return {"category": "schedule", "date": date, "time_slot": time_slot,
                "message": f"Scheduled for {date} during {time_slot}."}

    if matched_cat == "items":
        if service_match:
            items_list = service_match.get("Summer Storage Item List", "")
            if items_list:
                return {"category": "items", "items": items_list, "picked_up": True,
                        "message": f"We picked up: {items_list}."}
            return {"category": "items", "picked_up": True,
                    "message": "Items have been picked up but the detailed list is not yet recorded."}
        return {"category": "items", "picked_up": False,
                "message": "Items have not been picked up yet."}

    if matched_cat == "invoice":
        invoice_id = (service_match or {}).get("Invoice ID", "N/A")
        return {"category": "invoice", "invoice_id": invoice_id,
                "message": f"The invoice is {invoice_id}."}

    if matched_cat == "dispatch":
        dispatch_status = (dispatch_match or {}).get("Dispatch Status", "N/A")
        truck = (dispatch_match or {}).get("Truck", "N/A")
        return {"category": "dispatch", "dispatch_status": dispatch_status, "truck": truck,
                "message": f"Dispatch status is {dispatch_status}. Truck: {truck}."}


# ── REST endpoints ─────────────────────────────────────────────────────────────
def _extract_args(body: dict) -> dict:
    if "args" in body and isinstance(body["args"], dict):
        return body["args"]
    return body


@mcp.custom_route("/verify_name", methods=["POST", "GET"])
async def verify_name_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/verify_name",
            "method": "POST",
            "expects": {"args": {"student_name": "string"}},
            "returns": ["verified", "confirmed_name", "suggestions", "message"]
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    student_name = args.get("student_name", "")
    return JSONResponse(await do_verify_name(student_name))


@mcp.custom_route("/find_order", methods=["POST", "GET"])
async def find_order_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/find_order",
            "method": "POST",
            "expects": {"args": {"student_name": "string"}},
            "returns": ["found", "order_id", "service"]
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    return JSONResponse(await do_find_order(args.get("student_name", "")))


@mcp.custom_route("/get_order_detail", methods=["POST", "GET"])
async def get_detail_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/get_order_detail",
            "method": "POST",
            "expects": {"args": {"student_name": "string", "category": "string"}},
            "valid_categories": ["status", "location", "schedule", "items", "invoice", "dispatch"]
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = _extract_args(body)
    return JSONResponse(await do_get_order_detail(
        args.get("student_name", ""),
        args.get("category", "")
    ))


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({
        "service": "UTrucking MCP Server (3-Tool Architecture)",
        "status": "running",
        "endpoints": ["/verify_name", "/find_order", "/get_order_detail", "/health"]
    })


# MCP tool wrappers
@mcp.tool()
async def verify_name(student_name: str) -> str:
    """Verify that a student name exists in our records. Returns the exact spelling or close suggestions if not found."""
    return json.dumps(await do_verify_name(student_name))


@mcp.tool()
async def find_order(student_name: str) -> str:
    """Find a UTrucking order by student name. Returns ONLY order_id and service type."""
    return json.dumps(await do_find_order(student_name))


@mcp.tool()
async def get_order_detail(student_name: str, category: str) -> str:
    """Get ONE specific detail about an order. Category must be: status, location, schedule, items, invoice, or dispatch."""
    return json.dumps(await do_get_order_detail(student_name, category))


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
