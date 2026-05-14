from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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

JOTFORM_API_KEY = os.getenv("JOTFORM_API_KEY", "YOUR_JOTFORM_API_KEY")
FORM_ID = "260590779679074"
JOTFORM_BASE = "https://api.jotform.com"

FIELD_STUDENT_NAME = "5"
FIELD_SERVICE_TYPE = "2"
FIELD_BUILDING     = "80"
FIELD_ROOM         = "4"
FIELD_ORDER_NUMBER = "83"
FIELD_ITEMS        = "65"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mcp")
async def mcp_handler(request: Request):
    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "utrucking_jotform", "version": "1.0.0"}
            }
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "lookup_storage_order",
                        "description": "Look up a student's summer storage order by their name. Returns service type, building, room number, order number, and full list of stored items with quantities.",
                        "inputSchema": {
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
        }

    if method == "tools/call":
        tool_name = body.get("params", {}).get("name")
        arguments = body.get("params", {}).get("arguments", {})

        if tool_name == "lookup_storage_order":
            result = await do_lookup(arguments.get("student_name", ""))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}]
                }
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


async def do_lookup(student_name: str) -> dict:
    if not student_name:
        return {"found": False, "message": "Please provide a student name to look up."}

    name_query = student_name.strip().lower()
    url = (
        f"{JOTFORM_BASE}/form/{FORM_ID}/submissions"
        f"?apiKey={JOTFORM_API_KEY}&limit=100&orderby=created_at&direction=DESC"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)

    if resp.status_code != 200:
        return {"found": False, "message": "Error connecting to JotForm."}

    submissions = resp.json().get("content", [])

    match = None
    for sub in submissions:
        name_value = sub.get("answers", {}).get(FIELD_STUDENT_NAME, {}).get("answer", "")
        if name_value and name_query in name_value.lower():
            match = sub
            break

    if not match:
        return {"found": False, "message": f"No storage order found for '{student_name}'. Please check the name and try again."}

    answers = match.get("answers", {})
    student_name_val = answers.get(FIELD_STUDENT_NAME, {}).get("answer", "N/A")
    service_type     = answers.get(FIELD_SERVICE_TYPE, {}).get("answer", "N/A")
    building         = answers.get(FIELD_BUILDING, {}).get("answer", "N/A")
    room             = answers.get(FIELD_ROOM, {}).get("answer", "N/A")
    order_number     = answers.get(FIELD_ORDER_NUMBER, {}).get("answer", "N/A")

    items_raw = answers.get(FIELD_ITEMS, {}).get("answer", {})
    items_list = []
    total = "N/A"

    if isinstance(items_raw, dict):
        payment_array = items_raw.get("paymentArray", {})
        if isinstance(payment_array, str):
            try:
                payment_array = json.loads(payment_array)
            except Exception:
                payment_array = {}
        if isinstance(payment_array, dict):
            items_list = payment_array.get("product", [])
            total = payment_array.get("total", "N/A")

    items_str = ", ".join(items_list) if items_list else "No items recorded"

    return {
        "found": True,
        "student_name": student_name_val,
        "order_number": order_number,
        "service_type": service_type,
        "building": building,
        "room": room,
        "items": items_str,
        "total": f"${total}",
        "message": (
            f"Found order for {student_name_val}. "
            f"Order {order_number}, {service_type}. "
            f"Building: {building}, Room: {room}. "
            f"Items: {items_str}. Total: ${total}."
        )
    }
