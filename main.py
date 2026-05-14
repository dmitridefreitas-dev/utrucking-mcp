from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import json
import os

app = FastAPI(title="UTrucking JotForm MCP Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
JOTFORM_API_KEY = os.getenv("JOTFORM_API_KEY", "YOUR_JOTFORM_API_KEY")
FORM_ID = "260590779679074"
JOTFORM_BASE = "https://api.jotform.com"

# --- Field IDs from your JotForm ---
FIELD_STUDENT_NAME = "5"
FIELD_SERVICE_TYPE = "2"
FIELD_BUILDING     = "80"
FIELD_ROOM         = "4"
FIELD_ORDER_NUMBER = "83"
FIELD_ITEMS        = "65"


# ── MCP manifest ──────────────────────────────────────────────────────────────
@app.get("/")
async def mcp_manifest():
    """MCP tool discovery endpoint — Retell reads this to populate the dropdown."""
    return {
        "schema_version": "v1",
        "name": "utrucking_jotform",
        "description": "Look up UTrucking student storage orders from JotForm",
        "tools": [
            {
                "name": "lookup_storage_order",
                "description": (
                    "Look up a student's summer storage order by their name. "
                    "Returns service type, building, room number, order number, "
                    "and full list of stored items with quantities."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_name": {
                            "type": "string",
                            "description": "Full name of the student to look up"
                        }
                    },
                    "required": ["student_name"]
                }
            }
        ]
    }


# ── Tool execution ─────────────────────────────────────────────────────────────
class LookupRequest(BaseModel):
    student_name: str


@app.post("/tools/lookup_storage_order")
async def lookup_storage_order(req: LookupRequest):
    """
    Called by Retell mid-call to look up a student's storage order.
    Searches JotForm submissions by student name (case-insensitive).
    """
    name_query = req.student_name.strip().lower()

    # Fetch recent submissions (up to 100, newest first)
    url = (
        f"{JOTFORM_BASE}/form/{FORM_ID}/submissions"
        f"?apiKey={JOTFORM_API_KEY}&limit=100&orderby=created_at&direction=DESC"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="JotForm API error")

    data = resp.json()
    submissions = data.get("content", [])

    # Find matching submission by student name
    match = None
    for sub in submissions:
        answers = sub.get("answers", {})
        name_field = answers.get(FIELD_STUDENT_NAME, {})
        name_value = name_field.get("answer", "")
        if name_value and name_query in name_value.lower():
            match = sub
            break

    if not match:
        return {
            "found": False,
            "message": f"No storage order found for '{req.student_name}'. "
                       "Please check the name and try again."
        }

    answers = match.get("answers", {})

    # Extract core fields
    student_name   = answers.get(FIELD_STUDENT_NAME, {}).get("answer", "N/A")
    service_type   = answers.get(FIELD_SERVICE_TYPE, {}).get("answer", "N/A")
    building       = answers.get(FIELD_BUILDING, {}).get("answer", "N/A")
    room           = answers.get(FIELD_ROOM, {}).get("answer", "N/A")
    order_number   = answers.get(FIELD_ORDER_NUMBER, {}).get("answer", "N/A")

    # Extract items from payment field
    items_raw = answers.get(FIELD_ITEMS, {}).get("answer", {})
    items_list = []

    if isinstance(items_raw, dict):
        payment_array = items_raw.get("paymentArray", "")
        if isinstance(payment_array, str):
            try:
                payment_array = json.loads(payment_array)
            except Exception:
                payment_array = {}

        if isinstance(payment_array, dict):
            products = payment_array.get("product", [])
            items_list = products  # e.g. ["UTrucking Box (Amount: 22.00, Quantity: 5)", ...]
            total = payment_array.get("total", "N/A")
        else:
            total = "N/A"
    else:
        total = "N/A"

    # Build clean readable items string
    items_str = ", ".join(items_list) if items_list else "No items recorded"

    return {
        "found": True,
        "student_name": student_name,
        "order_number": order_number,
        "service_type": service_type,
        "building": building,
        "room": room,
        "items": items_str,
        "total": f"${total}",
        "message": (
            f"Found order for {student_name}. "
            f"Order {order_number} — {service_type}. "
            f"Building: {building}, Room: {room}. "
            f"Items: {items_str}. Total: ${total}."
        )
    }


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}
