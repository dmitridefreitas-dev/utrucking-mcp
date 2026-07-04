"""Offline unit tests for main.py helpers — upsell attach, phone match, multi-order, pretty items."""
import engines
import main

BOOK = {"utrucking box": 22.0, "mini fridge": 23.0, "plastic container": 18.0,
        "rolling cart": 23.0, "mattress": 33.0, "bike": 39.0}


# ---------- upsell attach ----------
def _svc(items):
    return {"Summer Storage Item List": "; ".join(
        "%s (Amount: %.2f USD, Quantity: %d)" % (n, a, q) for n, a, q in items)}


def _upsell_data():
    rows = [_svc([("UTrucking Box", 22, 1), ("Mini Fridge", 23, 1)]) for _ in range(6)]
    rows += [_svc([("UTrucking Box", 22, 1), ("Plastic Container", 18, 1)]) for _ in range(4)]
    return engines.upsell_pairs(rows)


def test_attach_upsell_suggests_partner():
    up = _upsell_data()
    q = engines.quote("a mini fridge", BOOK)
    main._attach_upsell(q, up, BOOK)
    assert q["upsell"]["items"]
    assert q["upsell"]["items"][0]["item"].lower() == "utrucking box"


def test_upsell_never_suggests_item_already_in_cart():
    up = _upsell_data()
    q = engines.quote("a mini fridge and a box and a plastic container", BOOK)
    main._attach_upsell(q, up, BOOK)
    have = {l["item"].lower() for l in q["line_items"]}
    for it in (q.get("upsell") or {}).get("items", []):
        assert it["item"].lower() not in have


def test_upsell_reply_line_appended():
    up = _upsell_data()
    q = engines.quote("a mini fridge", BOOK)
    main._attach_upsell(q, up, BOOK)
    txt = main._quote_reply_text(q)
    assert "Most people also add" in txt


# ---------- phone matching ----------
def _drow(name, phone):
    return {"Student": name, "Phone": phone}


def test_phone_digits_and_formats():
    D = [_drow("Jordan Miles", "(540) 207-8205")]
    for fmt in ["5402078205", "+15402078205", "540-207-8205", "1 540 207 8205"]:
        assert "Jordan Miles" in main._match_by_phone(fmt, D)


def test_phone_fragment_rejected():
    D = [_drow("Jordan Miles", "5402078205")]
    assert main._match_by_phone("8205", D) == []          # too short to be a real number


def test_phone_unknown_number():
    D = [_drow("Jordan Miles", "5402078205")]
    assert main._match_by_phone("9990001234", D) == []


# ---------- pretty items ----------
def test_pretty_items_humanizes_machine_string():
    s = "UTrucking Box (Amount: 22.00 USD, Quantity: 4); Mattress (Amount: 33.00 USD, Quantity: 1)"
    out = main._pretty_items(s)
    assert out == "UTrucking Box x4, Mattress"           # qty 1 shown without xN


def test_pretty_items_falls_back_on_plain_text():
    assert main._pretty_items("some free text") == "some free text"
    assert main._pretty_items("") == ""


# ---------- multi-order lookup ----------
def _mo_data():
    D = [
        {"Student": "Jordan Miles", "ID": "#13851-SS", "Service": "Summer Storage",
         "Building": "Umrath", "Room": "204", "Date": "5/6/2026", "Phone": "3145551234", "Status": "Scheduled"},
        {"Student": "Jordan Miles", "ID": "#14990-RR", "Service": "Return Delivery",
         "Building": "Umrath", "Room": "204", "Date": "8/20/2026", "Phone": "3145551234", "Status": "Scheduled"},
        {"Student": "Casey Nguyen", "ID": "#13777-SS", "Service": "Summer Storage",
         "Building": "Eliot", "Room": "12", "Date": "5/7/2026", "Phone": "3145559876", "Status": "Scheduled"},
    ]
    S = [
        {"Student Name": "Jordan Miles", "Order#:": "13851-SS", "Service Type": "Summer Storage",
         "Building": "Umrath", "Invoice ID": "INV-1", "Date": "5/6/2026",
         "Summer Storage Item List": "UTrucking Box (Amount: 22.00 USD, Quantity: 2); Total: $44.00"},
        {"Student Name": "Jordan Miles", "Order#:": "14990-RR", "Service Type": "Return Delivery",
         "Building": "Umrath", "Invoice ID": "INV-2", "Date": "8/20/2026", "Summer Storage Item List": "Total: $40.00"},
        {"Student Name": "Casey Nguyen", "Order#:": "13777-SS", "Service Type": "Summer Storage",
         "Building": "Eliot", "Invoice ID": "INV-3", "Date": "5/7/2026",
         "Summer Storage Item List": "Bike (Amount: 39.00 USD, Quantity: 1); Total: $39.00"},
    ]
    return D, S


def test_repeat_customer_asks_which_order():
    D, S = _mo_data()
    r = main._build_order_result("Jordan Miles", D, S)
    assert r["needs_order_choice"] is True
    assert r["order_count"] == 2


def test_order_hint_resolves_single_order():
    D, S = _mo_data()
    r = main._build_order_result("Jordan Miles", D, S, order_hint="the return")
    assert not r.get("needs_order_choice")
    assert r["order_id"] == "#14990-RR"
    assert r["invoice_id"] == "INV-2"


def test_single_order_customer_has_no_choice():
    D, S = _mo_data()
    r = main._build_order_result("Casey Nguyen", D, S)
    assert not r.get("needs_order_choice")
    assert "order_count" not in r
    assert r["building"] == "Eliot"
