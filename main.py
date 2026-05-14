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

# Google Sheet ID (from the URL: docs.google.com/spreadsheets/d/SHEET_ID/edit)
SHEET_ID = os.getenv("SHEET_ID", "1x5MQbsCFMJX5eafA6uoFF0nU4qfvCaK-2EyZNPs__kM")
SHEET_GID = os.getenv("SHEET_GID", "602263013")

# Public CSV export URL — works when sheet is shared as "Anyone with the link"
SHEET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={SHEET_GID}"
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


# ── Google Sheets fetcher ──────────────────────────────────────────────────────
async def fetch_sheet_rows() -> list[dict]:
    """Pull the master sheet as CSV and return as list of dicts."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(SHEET_CSV_URL)

    if resp.status_code != 200:
        return []

    text = resp.text
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader]


# ── Core lookup logic ──────────────────────────────────────────────────────────
async def do_lookup(student_name: str) -> dict:
    if not student_name:
        return {"found": False, "message": "Please provide a student name."}

    name_query = student_name.strip().lower()

    try:
        rows = await fetch_sheet_rows()
    except Exception as e:
        return {"found": False, "message": f"Error reading the master sheet: {str(e)}"}

    if not rows:
        return {"found": False, "message": "Could not load the master sheet right now."}

    # Find matching row by student name
    match = None
    for row in rows:
        name_value = (row.get("Student") or "").strip()
        if name_value and name_query in name_value.lower():
            match = row
            break

    if not match:
        return {
            "found": False,
            "message": (
                f"No order found for '{student_name}'. "
                "Please double-check the name or contact our team at (314) 266-8878."
            )
        }

    # Pull fields from the row
    order_id        = match.get("ID", "N/A")
    date            = match.get("Date", "N/A")
    mode            = match.get("Mode", "N/A")
    truck           = match.get("Truck", "N/A")
    building        = match.get("Building", "N/A")
    room            = match.get("Room", "N/A")
    student         = match.get("Student", "N/A")
    phone           = match.get("Phone", "N/A")
    time_slot       = match.get("Time Slot", "N/A")
    kits            = match.get("Kits", "N/A")
    status          = match.get("Status", "N/A")
    address         = match.get("Address", "N/A")
    service         = match.get("Service", "N/A")
    product         = match.get("Product", "N/A")
    kit_check       = match.get("Kit ✓", "N/A")
    dispatch_status = match.get("Dispatch Status", "N/A")
    completed_at    = match.get("Completed At", "")
    mover_notes     = match.get("Mover Notes", "")
    customer_note   = match.get("Customer Note", "")

    # Build a conversational message for the agent to read
    message_parts = [f"Found order for {student}."]

    if order_id and order_id != "N/A":
        message_parts.append(f"Order ID: {order_id}.")
    if service and service != "N/A":
        message_parts.append(f"Service: {service}.")
    if building and building != "N/A" and room and room != "N/A":
        message_parts.append(f"Location: {building}, Room {room}.")
    if date and date != "N/A":
        message_parts.append(f"Scheduled date: {date}.")
    if time_slot and time_slot != "N/A":
        message_parts.append(f"Time slot: {time_slot}.")
    if status and status != "N/A":
        message_parts.append(f"Status: {status}.")
    if dispatch_status and dispatch_status != "N/A":
        message_parts.append(f"Dispatch: {dispatch_status}.")
    if product and product != "N/A":
        message_parts.append(f"Items: {product}.")

    message = " ".join(message_parts)

    return {
        "found": True,
        "order_id": order_id,
        "student": student,
        "phone": phone,
        "service": service,
        "mode": mode,
        "building": building,
        "room": room,
        "address": address,
        "date": date,
        "time_slot": time_slot,
        "status": status,
        "dispatch_status": dispatch_status,
        "kits": kits,
        "kit_check": kit_check,
        "product": product,
        "truck": truck,
        "completed_at": completed_at,
        "mover_notes": mover_notes,
        "customer_note": customer_note,
        "message": message
    }


# ── REST endpoint for Retell custom function ───────────────────────────────────
@mcp.custom_route("/lookup", methods=["POST", "GET", "OPTIONS"])
async def lookup_endpoint(request: Request):
    if request.method == "GET":
        return JSONResponse({
            "endpoint": "/lookup",
            "method": "POST",
            "expects": {"args": {"student_name": "string"}}
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
        "service": "UTrucking MCP Server (Google Sheets)",
        "status": "running",
        "endpoints": ["/mcp", "/lookup", "/health"]
    })


# ── MCP tool ───────────────────────────────────────────────────────────────────
@mcp.tool()
async def lookup_storage_order(student_name: str) -> str:
    """Look up a student's UTrucking order from the master sheet by their name."""
    result = await do_lookup(student_name)
    return json.dumps(result)


# ── App setup with keep-alive lifespan ─────────────────────────────────────────
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

# Middleware for Render compatibility
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
