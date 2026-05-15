import httpx
import json
import os
import asyncio
import csv
import io
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

# ── Config ─────────────────────────────────────────────────────────────────────
RENDER_URL = os.getenv("RENDER_URL", "https://utrucking-mcp.onrender.com")

# Sheet 1 — Master Dispatch (all orders, scheduling)
DISPATCH_SHEET_ID = "1x5MQbsCFMJX5eafA6uoFF0nU4qfvCaK-2EyZNPs__kM"
DISPATCH_SHEET_GID = "602263013"
DISPATCH_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{DISPATCH_SHEET_ID}"
    f"/export?format=csv&gid={DISPATCH_SHEET_GID}"
)

# Sheet 2 — Service Form (completed pickups, actual items)
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


# ── Find matching row by student name ──────────────────────────────────────────
def find_match(rows: list[dict], name_query: str, name_column: str) -> dict | None:
    """Find best match — prefers most recent if multiple."""
    matches = []
    for row in rows:
        name_value = (row.get(name_column) or "").strip()
        if name_value and name_query in name_value.lower():
            matches.append(row)

    if not matches:
        return None

    # Return the last match (sheets typically append, so last = most recent)
    return matches[-1]


# ── Core lookup — checks BOTH sheets and combines ─────────────────────────────
async def do_lookup(student_name: str) -> dict:
    if not student_name:
        return {"found": False, "message": "Please provide a student name."}

    name_query = student_name.strip().lower()

    # Fetch both sheets in parallel
    try:
        dispatch_rows, service_rows = await asyncio.gather(
            fetch_csv_rows(DISPATCH_CSV_URL),
            fetch_csv_rows(SERVICE_CSV_URL),
        )
    except Exception as e:
        return {"found": False, "message": f"Error reading sheets: {str(e)}"}

    dispatch_match = find_match(dispatch_rows, name_query, "Student")
    service_match  = find_match(service_rows, name_query, "Student Name")

    if not dispatch_match and not service_match:
        return {
            "found": False,
            "message": (
                f"No order found for '{student_name}'. "
                "Please double-check the name or contact our team at (314) 266-8878."
            )
        }

    # ── Extract dispatch info (scheduling, location, status) ──
    dispatch_info = {}
    if dispatch_match:
        dispatch_info = {
            "order_id":        dispatch_match.get("ID", "N/A"),
            "student":         dispatch_match.get("Student", "N/A"),
            "phone":           dispatch_match.get("Phone", "N/A"),
            "service":         dispatch_match.get("Service", "N/A"),
            "mode":            dispatch_match.get("Mode", "N/A"),
            "building":        dispatch_match.get("Building", "N/A"),
            "room":            dispatch_match.get("Room", "N/A"),
            "address":         dispatch_match.get("Address", "N/A"),
            "date":            dispatch_match.get("Date", "N/A"),
            "time_slot":       dispatch_match.get("Time Slot", "N/A"),
            "status":          dispatch_match.get("Status", "N/A"),
            "dispatch_status": dispatch_match.get("Dispatch Status", "N/A"),
            "kits":            dispatch_match.get("Kits", "N/A"),
            "kit_check":       dispatch_match.get("Kit ✓", "N/A"),
            "product":         dispatch_match.get("Product", "N/A"),
            "truck":           dispatch_match.get("Truck", "N/A"),
        }

    # ── Extract service form info (actual stored items) ──
    service_info = {}
    if service_match:
        service_info = {
            "service_student":   service_match.get("Student Name", "N/A"),
            "service_order":     service_match.get("Order#:", "N/A"),
            "service_type":      service_match.get("Service Type", "N/A"),
            "service_building":  service_match.get("Building", "N/A"),
            "service_room":      service_match.get("Room", "N/A"),
            "pickup_date":       service_match.get("Date", "N/A"),
            "invoice_id":        service_match.get("Invoice ID", "N/A"),
            "items_list":        service_match.get("Summer Storage Item List", "N/A"),
            "utrucking_boxes":   service_match.get("UTrucking Boxes", ""),
            "luggage":           service_match.get("Luggage", ""),
            "other":             service_match.get("Other", ""),
            "other_description": service_match.get("Other Description", ""),
            "notes":             service_match.get("Notes (heavy, oversized, unboxed)", ""),
            "date_completed":    service_match.get("Date of completion", ""),
        }

    # ── Build the conversational message ──
    parts = []

    # Use whichever student name we have
    student_display = (
        dispatch_info.get("student")
        or service_info.get("service_student")
        or student_name
    )
    parts.append(f"Found order for {student_display}.")

    # Order ID — prefer dispatch order_id, fall back to service order
    order_id = dispatch_info.get("order_id") or service_info.get("service_order")
    if order_id and order_id != "N/A":
        parts.append(f"Order {order_id}.")

    # Service type
    service = dispatch_info.get("service") or service_info.get("service_type")
    if service and service != "N/A":
        parts.append(f"Service: {service}.")

    # Location
    building = dispatch_info.get("building") or service_info.get("service_building")
    room     = dispatch_info.get("room") or service_info.get("service_room")
    if building and building != "N/A":
        parts.append(f"Location: {building}, Room {room}.")

    # Scheduling info (from dispatch)
    if dispatch_info.get("date") and dispatch_info["date"] != "N/A":
        parts.append(f"Scheduled date: {dispatch_info['date']}.")
    if dispatch_info.get("time_slot") and dispatch_info["time_slot"] != "N/A":
        parts.append(f"Time slot: {dispatch_info['time_slot']}.")
    if dispatch_info.get("status") and dispatch_info["status"] != "N/A":
        parts.append(f"Status: {dispatch_info['status']}.")
    if dispatch_info.get("dispatch_status") and dispatch_info["dispatch_status"] != "N/A":
        parts.append(f"Dispatch: {dispatch_info['dispatch_status']}.")

    # Pickup completion info (from service form)
    if service_info.get("date_completed"):
        parts.append(f"Pickup completed: {service_info['date_completed']}.")

    # Stored items (from service form — more accurate than dispatch product)
    if service_info.get("items_list") and service_info["items_list"] != "N/A":
        parts.append(f"Stored items: {service_info['items_list']}.")
    elif dispatch_info.get("product") and dispatch_info["product"] != "N/A":
        parts.append(f"Items: {dispatch_info['product']}.")

    # Item counts from service form if available
    box_count = service_info.get("utrucking_boxes", "")
    lug_count = service_info.get("luggage", "")
    if box_count or lug_count:
        count_parts = []
        if box_count:
            count_parts.append(f"{box_count} UTrucking boxes")
        if lug_count:
            count_parts.append(f"{lug_count} luggage")
        if count_parts:
            parts.append(f"Counts: {', '.join(count_parts)}.")

    # Indicate pickup status
    if service_match and not dispatch_match:
        parts.append("This order has been picked up.")
    elif dispatch_match and not service_match:
        parts.append("This order is scheduled but has not been picked up yet.")
    elif dispatch_match and service_match:
        parts.append("Items have been picked up and stored.")

    message = " ".join(parts)

    # ── Return combined data ──
    return {
        "found": True,
        "pickup_completed": service_match is not None,
        "in_dispatch": dispatch_match is not None,
        "student": student_display,
        "order_id": order_id or "N/A",
        "service": service or "N/A",
        "building": building or "N/A",
        "room": room or "N/A",
        "message": message,
        # Full dispatch fields
        **dispatch_info,
        # Full service form fields
        **service_info,
    }


# ── REST endpoint for Retell custom function ───────────────────────────────────
@mcp.custom_route("/lookup", methods=["POST", "GET", "OPTIONS"])
async def lookup_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/lookup",
            "method": "POST",
            "expects": {"args": {"student_name": "string"}},
            "sources": ["master_dispatch", "service_form"]
        })

    try:
        body = await request.json()
    except Exception:
        body = {}

    student_name = ""
    if "args" in body and isinstance(body["args"], dict):
        student_name = body["args"].get("student_name", "")
    if not student_name:
        student_name = body.get("student_name", "")

    result = await do_lookup(student_name)
    return JSONResponse(result)


# ── Health + root ──────────────────────────────────────────────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({
        "service": "UTrucking MCP Server (Dual Sheets)",
        "status": "running",
        "endpoints": ["/mcp", "/lookup", "/health"],
        "data_sources": {
            "dispatch": "Master Dispatch — all orders, scheduling, status",
            "service_form": "Service Form — completed pickups with actual items"
        }
    })


# ── MCP tool ───────────────────────────────────────────────────────────────────
@mcp.tool()
async def lookup_storage_order(student_name: str) -> str:
    """
    Look up a UTrucking storage order. Checks both the master dispatch sheet
    (scheduling and status) and the service form sheet (completed pickups
    with actual stored items). Returns a combined view of the order.
    """
    result = await do_lookup(student_name)
    return json.dumps(result)


# ── App setup ──────────────────────────────────────────────────────────────────
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
