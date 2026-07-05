"""Offline unit tests for main.py helpers — upsell attach, phone match, multi-order, pretty items."""
import pytest
import engines
import main

BOOK = {"utrucking box": 22.0, "mini fridge": 23.0, "plastic container": 18.0,
        "rolling cart": 23.0, "mattress": 33.0, "bike": 39.0}


@pytest.fixture(autouse=True)
def _clear_ai_cache():
    # process-global by design; reset so tests stay independent
    main._AI_MAP_CACHE.clear()
    main._VERIFY_FAILS.clear()
    yield
    main._AI_MAP_CACHE.clear()
    main._VERIFY_FAILS.clear()


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


# ---------- chat identity flow + phone verification (fictional data only, no real customers) ----------
def _id_data():
    D = [
        {"Student": "Jamie Rivers", "ID": "#90001-TS", "Service": "Summer Storage",
         "Building": "Northgate B", "Room": "1205", "Date": "5/6/2026", "Phone": "5550100200", "Status": "Complete"},
        {"Student": "Morgan Ellis", "ID": "#90002-TS", "Service": "Summer Storage",
         "Building": "", "Room": "", "Date": "5/9/2026", "Phone": "", "Status": "Scheduled"},
    ]
    S = [{"Student Name": "Jamie Rivers", "Order#:": "90001-TS", "Building": "Northgate B"},
         {"Student Name": "Morgan Ellis", "Order#:": "90002-TS"}]
    return D, S


def test_bare_name_starts_verification():
    D, S = _id_data()
    reply, state = main._chat_reply("Jamie Rivers", {}, D, S, BOOK)
    assert state.get("step") == "verify"
    assert "building" in reply.lower()
    assert state.get("name", "").lower() == "jamie rivers"


def test_bare_name_typo_still_routes_to_verify():
    D, S = _id_data()
    _, state = main._chat_reply("Jamie Rivrs", {}, D, S, BOOK)      # missing 'e'
    assert state.get("step") == "verify"


def test_quote_and_courtesy_are_not_treated_as_names():
    D, S = _id_data()
    _, s1 = main._chat_reply("mini fridge", {}, D, S, BOOK)
    assert not s1
    _, s2 = main._chat_reply("thank you", {}, D, S, BOOK)
    assert s2.get("step") != "verify"


def test_unknown_nameish_goes_to_lookup_not_menu():
    D, S = _id_data()
    reply, state = main._chat_reply("Marguerite Vanderhoff", {}, D, S, BOOK)
    assert state.get("intent") == "lookup"
    assert "couldn't find" in reply.lower()


@pytest.mark.parametrize("answer", ["Northgate B", "northgate", "Northgat B", "Northgate", " NORTHGATE  B "])
def test_building_verify_tolerates_misspellings(answer):
    D, S = _id_data()
    _, state = main._chat_reply("Jamie Rivers", {}, D, S, BOOK)
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" in reply


@pytest.mark.parametrize("answer", ["Westwood", "Umrath", "zzz", "the dorm"])
def test_building_verify_rejects_wrong_building(answer):
    D, S = _id_data()
    _, state = main._chat_reply("Jamie Rivers", {}, D, S, BOOK)
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" not in reply


@pytest.mark.parametrize("answer", ["90002", "#90002-TS", "90002-ts", "order 90002"])
def test_order_number_verifies_when_no_building_or_phone(answer):
    D, S = _id_data()
    ask, state = main._chat_reply("Morgan Ellis", {}, D, S, BOOK)
    assert "order number" in ask.lower()
    reply, _ = main._lookup_flow(answer, state, D, S)
    assert "You're verified" in reply


def test_order_number_wrong_is_rejected():
    D, S = _id_data()
    _, state = main._chat_reply("Morgan Ellis", {}, D, S, BOOK)
    reply, _ = main._lookup_flow("00000", state, D, S)
    assert "You're verified" not in reply


# ---- name matcher must not confidently pull up a stranger who only shares a fuzzy first name ----
_NAMES = ["Blair Wagner", "Diya Gupta", "Kennedy Brown", "Madison Elhaik"]

@pytest.mark.parametrize("gibberish", ["Zblargh Xyzptqq", "Grumbo Snerptwang", "Aaaa Bbbb", "Qwerty Asdfgh"])
def test_gibberish_full_name_not_confidently_matched(gibberish):
    best, _sugg = main.smart_name_match(gibberish, _NAMES)
    assert best is None, (gibberish, best)          # never a confident stranger match

def test_real_typo_names_still_match():
    assert main.smart_name_match("Diya Guta", _NAMES)[0] == "Diya Gupta"       # dropped a letter
    assert main.smart_name_match("Kennedy Braun", _NAMES)[0] == "Kennedy Brown"  # misspelled surname
    assert main.smart_name_match("Blair Wagner", _NAMES)[0] == "Blair Wagner"    # exact

def test_first_name_only_still_offers_a_match():
    best, sugg = main.smart_name_match("Diya", _NAMES)
    assert best == "Diya Gupta" or "Diya Gupta" in sugg


# ---- a non-building SENTENCE must never satisfy the building check (false-accept guard) ----
@pytest.mark.parametrize("bldg", ["Danforth B", "Northgate", "Eliot A", "Umrath House", "Village East"])
@pytest.mark.parametrize("sentence", [
    "my last four are 3851", "the last four digits are 0200", "my order number is 12345",
    "I don't know", "just tell me my status", "can you please look it up", "yes that's me",
])
def test_building_check_rejects_filler_sentences(bldg, sentence):
    assert main._building_matches(sentence, bldg) is False, (sentence, bldg)

@pytest.mark.parametrize("answer", ["Danforth", "danforth", "Danforth B", "Danfrth", "it's Danforth",
                                    "I'm in Danforth", "Danforth, room 4405"])
def test_building_check_still_accepts_real_answers(answer):
    assert main._building_matches(answer, "Danforth B") is True, answer

def test_building_check_rejects_other_buildings():
    for wrong in ["Northgate", "Umrath", "Eliot", "the village"]:
        assert main._building_matches(wrong, "Danforth B") is False, wrong


# ---- the phone gate: lookup returns NO PII; details come only after a correct answer ----
def test_lookup_is_redacted_no_pii():
    D, S = _id_data()
    full = main._build_order_result("Jamie Rivers", D, S)
    red = main._redact_lookup(full)
    assert red["status"] == "found" and red["confirmed_name"] == "Jamie Rivers"
    assert red.get("verify_with")                                  # tells the agent what to ask
    for pii in main._PII_FIELDS:                                   # none of the values leak
        assert pii not in red, pii


def _patch_rows(monkeypatch, D, S):
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else S
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)


def test_get_order_details_blocks_the_bypass(monkeypatch):
    """The real bug: the agent answered 'Yes' (name confirm) and used a value it already knew.
    Details must NOT come back unless the CALLER's answer actually matches."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    for bogus in ("Yes", "", "that's me", "sure"):
        r = asyncio.run(main.do_get_order_details("Jamie Rivers", bogus))
        assert r.get("verified") is False, bogus
        for pii in main._PII_FIELDS:
            assert pii not in r, (bogus, pii)


def test_get_order_details_reveals_only_after_correct_answer(monkeypatch):
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    ok = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgat B"))   # misspelled but right
    assert ok.get("verified") is True
    assert ok.get("building") == "Northgate B" and ok.get("order_status") == "Complete"
    byid = asyncio.run(main.do_get_order_details("Morgan Ellis", "order 90002"))
    assert byid.get("verified") is True and byid.get("order_id") == "#90002-TS"


def test_get_order_details_unknown_name(monkeypatch):
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    r = asyncio.run(main.do_get_order_details("Nobody McGhost", "Northgate B"))
    assert r.get("verified") is not True
    for pii in main._PII_FIELDS:
        assert pii not in r


def test_get_order_details_force_refreshes_a_stale_cache(monkeypatch):
    """A just-edited order can lag in the cached sheet (SHEET_TTL / CDN). On a verification
    miss the endpoint re-pulls FRESH once and re-checks, so the correct answer still verifies —
    without ever relaxing the check (a truly wrong answer still fails)."""
    import asyncio
    fresh_D, fresh_S = _id_data()
    stale_D = [{**fresh_D[0], "Building": "", "Room": "", "Phone": "", "ID": ""}]  # cached copy missing the details
    stale_S = [{"Student Name": "Jamie Rivers", "Order#:": "", "Building": ""}]
    calls = {"forced": 0}
    async def fake_fetch(url, force=False):
        if url == main.DISPATCH_CSV_URL:
            if force:
                calls["forced"] += 1
                return fresh_D
            return stale_D
        return fresh_S if force else stale_S
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    main._VERIFY_FAILS.clear()
    ok = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))   # correct, but stale-cache misses first
    assert ok.get("verified") is True and ok.get("building") == "Northgate B"
    assert calls["forced"] >= 1                                                   # it actually re-fetched fresh
    # a genuinely wrong answer must STILL fail even after the fresh re-check
    main._VERIFY_FAILS.clear()
    bad = asyncio.run(main.do_get_order_details("Jamie Rivers", "Westwood Hall"))
    assert bad.get("verified") is False
    for pii in main._PII_FIELDS:
        assert pii not in bad


def test_phone_verify_has_bruteforce_lockout_like_chat(monkeypatch):
    """Parity: the chat locks a name after 5 wrong verify tries; get_order_details must too
    (shared _VERIFY_FAILS), else the open phone endpoint could be brute-forced."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    for i in range(5):
        r = asyncio.run(main.do_get_order_details("Jamie Rivers", "wrong%d" % i))
        assert r.get("verified") is False
    locked = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))  # correct but locked
    assert locked.get("verified") is False and locked.get("locked") is True
    for pii in main._PII_FIELDS:
        assert pii not in locked
    main._VERIFY_FAILS.clear()
    ok = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))
    assert ok.get("verified") is True
