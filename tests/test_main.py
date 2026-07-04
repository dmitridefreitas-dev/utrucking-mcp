"""Offline unit tests for main.py helpers — upsell attach, phone match, multi-order, pretty items."""
import pytest
import engines
import main

BOOK = {"utrucking box": 22.0, "mini fridge": 23.0, "plastic container": 18.0,
        "rolling cart": 23.0, "mattress": 33.0, "bike": 39.0}


@pytest.fixture(autouse=True)
def _clear_ai_cache():
    # the AI-map cache is process-global by design; reset it so tests stay independent
    main._AI_MAP_CACHE.clear()
    yield
    main._AI_MAP_CACHE.clear()


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


def test_value_weighted_upsell_prefers_high_lift_partner():
    # Rolling cart and mini fridge co-occur with boxes EQUALLY often, but the rolling-cart basket is
    # far more valuable. Value-weighting must surface the rolling cart first; raw co-occurrence (no lift)
    # ties and falls to alpha order (mini fridge) — so the flip proves the $ weighting took effect.
    rows  = [_svc([("UTrucking Box", 22, 3), ("Rolling Cart", 23, 1), ("Desk", 39, 1)]) for _ in range(20)]
    rows += [_svc([("UTrucking Box", 22, 3), ("Mini Fridge", 23, 1)]) for _ in range(20)]
    up, lift = engines.upsell_pairs(rows), engines.upsell_value(rows)
    q = engines.quote("5 boxes", BOOK)
    main._attach_upsell(q, up, BOOK, lift)
    assert q["upsell"]["items"][0]["item"].lower() == "rolling cart", q["upsell"]["items"]
    q2 = engines.quote("5 boxes", BOOK)
    main._attach_upsell(q2, up, BOOK)                 # no lift -> co-occurrence tie -> alpha
    assert q2["upsell"]["items"][0]["item"].lower() == "mini fridge", q2["upsell"]["items"]


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


# ---------- AI second-chance mapping merges, never duplicates a line ----------
def test_ai_map_merges_into_existing_line(monkeypatch):
    import asyncio
    async def fake_gen(key, parts, temp=None, json_out=False):
        return '{"kayak": "box"}'                      # maps onto an item already in the cart
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    q = engines.quote("2 boxes, 1 kayak", BOOK)
    q = asyncio.run(main._ai_map_unmatched(q, BOOK))
    box = [l for l in q["line_items"] if l["item"] == "Utrucking Box"]
    assert len(box) == 1 and box[0]["qty"] == 3        # merged, not a second line
    assert abs(q["total"] - 66.0) < 0.01
    assert any(mp["from"] == "kayak" and mp["to"] == "Utrucking Box" for mp in q.get("matched", []))
    assert "kayak" not in (q.get("unmatched") or [])


def test_ai_map_new_item_gets_its_own_line(monkeypatch):
    import asyncio
    async def fake_gen(key, parts, temp=None, json_out=False):
        return '{"kayak": "mattress"}'                 # not already in the cart
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    q = engines.quote("2 boxes, 1 kayak", BOOK)
    q = asyncio.run(main._ai_map_unmatched(q, BOOK))
    mat = [l for l in q["line_items"] if l["item"] == "Mattress"]
    assert len(mat) == 1 and mat[0].get("matched_from") == "kayak" and mat[0].get("ai_matched")
    assert mat[0].get("confidence") == "ai" and q.get("review_count") == 1     # #6: flagged for review
    assert "kayak" not in (q.get("unmatched") or [])


def test_ai_map_cache_serves_repeat_without_second_model_call(monkeypatch):
    import asyncio
    calls = {"n": 0}
    async def fake_gen(key, parts, temp=None, json_out=False):
        calls["n"] += 1
        return '{"kayak": "mattress"}'
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    q1 = asyncio.run(main._ai_map_unmatched(engines.quote("1 kayak", BOOK), BOOK))
    assert calls["n"] == 1 and any(l["item"] == "Mattress" for l in q1["line_items"])
    # a repeat of the same unknown is served from the learned cache — the model is NOT called again
    q2 = asyncio.run(main._ai_map_unmatched(engines.quote("1 kayak", BOOK), BOOK))
    assert calls["n"] == 1                                  # no second Gemini call
    assert any(l["item"] == "Mattress" and l.get("confidence") == "ai" for l in q2["line_items"])


def test_ai_map_cache_hit_works_without_api_key(monkeypatch):
    import asyncio
    async def fake_gen(key, parts, temp=None, json_out=False):
        return '{"kayak": "mattress"}'
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    asyncio.run(main._ai_map_unmatched(engines.quote("1 kayak", BOOK), BOOK))   # warm the cache
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)                          # key now gone
    q = asyncio.run(main._ai_map_unmatched(engines.quote("1 kayak", BOOK), BOOK))
    assert any(l["item"] == "Mattress" for l in q["line_items"])                 # still resolved, free


# ---------- bilingual (Spanish) chat ----------
def test_looks_spanish_detects_spanish_and_not_english():
    for es in ["¿cuánto cuesta?", "hola, necesito almacenamiento", "quiero cinco cajas",
               "dónde está mi pedido", "gracias, por favor"]:
        assert main._looks_spanish(es), es
    for en in ["how much is it", "5 boxes and a mini fridge", "where is my order",
               "what days are open", "hi there", "a couch and a desk"]:
        assert not main._looks_spanish(en), en


def test_translate_uses_model_and_falls_back_on_empty(monkeypatch):
    import asyncio
    async def fake_gen(key, parts, temp=None, json_out=False):
        return "¿Qué días están abiertos?"
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    assert "días" in asyncio.run(main._translate("What days are open?", "es", "stub"))
    async def empty_gen(key, parts, temp=None, json_out=False):
        return ""
    monkeypatch.setattr(main, "_gemini_generate", empty_gen)
    assert asyncio.run(main._translate("hello", "es", "stub")) == "hello"   # empty -> original


def test_chat_api_spanish_roundtrip(monkeypatch):
    import asyncio, json as _json
    async def no_rows(url):
        return []
    monkeypatch.setattr(main, "fetch_csv_rows", no_rows)
    async def fake_gen(key, parts, temp=None, json_out=False):
        p = parts[0]["text"]
        if "to English" in p: return "what days are open?"
        if "to Spanish" in p: return "Estos son los días disponibles."
        return ""
    monkeypatch.setattr(main, "_gemini_generate", fake_gen)
    monkeypatch.setenv("GEMINI_API_KEY", "stub")

    class Req:
        client = type("C", (), {"host": "9.9.9.9"})()
        async def json(self):
            return {"args": {"message": "¿qué días están abiertos?", "state": {}}}

    r = asyncio.run(main.chat_api(Req()))
    body = r[1][0]                                            # conftest stub: JSONResponse(payload) -> ("JSON",(payload,),{})
    assert body["state"].get("lang") == "es"                 # language stays sticky
    assert "días" in body["reply"].lower()                   # reply came back translated


# ---------- chat identity flow: bare-name routing + fuzzy verification ----------
def _id_data():
    D = [
        {"Student": "Dalen Ainsworth", "ID": "#13851-SS", "Service": "Summer Storage",
         "Building": "Eliot A", "Room": "3091", "Date": "5/6/2026", "Phone": "3145551234", "Status": "Complete"},
        {"Student": "Nora Vance", "ID": "#20777-SS", "Service": "Summer Storage",
         "Building": "", "Room": "", "Date": "5/9/2026", "Phone": "", "Status": "Scheduled"},
    ]
    S = [{"Student Name": "Dalen Ainsworth", "Order#:": "13851-SS", "Building": "Eliot A"},
         {"Student Name": "Nora Vance", "Order#:": "20777-SS"}]
    return D, S


def test_bare_name_starts_verification():
    D, S = _id_data()
    reply, state = main._chat_reply("Dalen Ainsworth", {}, D, S, BOOK)
    assert state.get("step") == "verify"
    assert "building" in reply.lower()
    assert state.get("name", "").lower() == "dalen ainsworth"


def test_bare_name_typo_still_routes_to_verify():
    D, S = _id_data()
    _, state = main._chat_reply("Dalen Ainswrth", {}, D, S, BOOK)   # missing 'o'
    assert state.get("step") == "verify"


def test_quote_and_courtesy_are_not_treated_as_names():
    D, S = _id_data()
    _, s1 = main._chat_reply("mini fridge", {}, D, S, BOOK)
    assert not s1                                                    # a quote, no lookup state
    _, s2 = main._chat_reply("thank you", {}, D, S, BOOK)
    assert s2.get("step") != "verify"


def test_unknown_nameish_goes_to_lookup_not_menu():
    D, S = _id_data()
    reply, state = main._chat_reply("Marguerite Vanderhoff", {}, D, S, BOOK)
    assert state.get("intent") == "lookup"
    assert "couldn't find" in reply.lower()


@pytest.mark.parametrize("answer", ["Eliot A", "eliot", "Elliot A", "Elliott", " ELIOT  A "])
def test_building_verify_tolerates_misspellings(answer):
    D, S = _id_data()
    _, state = main._chat_reply("Dalen Ainsworth", {}, D, S, BOOK)
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" in reply


@pytest.mark.parametrize("answer", ["Umrath", "Gregg", "zzz", "the dorm"])
def test_building_verify_rejects_wrong_building(answer):
    D, S = _id_data()
    _, state = main._chat_reply("Dalen Ainsworth", {}, D, S, BOOK)
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" not in reply


@pytest.mark.parametrize("answer", ["20777", "#20777-SS", "20777-ss", "order 20777"])
def test_order_number_verifies_when_no_building_or_phone(answer):
    D, S = _id_data()
    ask, state = main._chat_reply("Nora Vance", {}, D, S, BOOK)
    assert "order number" in ask.lower()                            # asked for the order #
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" in reply


def test_order_number_wrong_is_rejected():
    D, S = _id_data()
    _, state = main._chat_reply("Nora Vance", {}, D, S, BOOK)
    reply, _ = main._lookup_flow("00000", state, D, S)
    assert "You're verified" not in reply
