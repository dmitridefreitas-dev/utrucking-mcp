"""Offline unit tests for main.py helpers — upsell attach, phone match, multi-order, pretty items."""
import json
import re
import time
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
    main._VERIFY_STRIKES.clear()
    yield
    main._AI_MAP_CACHE.clear()
    main._VERIFY_FAILS.clear()
    main._VERIFY_STRIKES.clear()


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
    # This record has a phone on file, so the STRONGEST verifier is asked for — not the building,
    # which is a weak secret (a classmate knows the dorm). Building is still accepted, just not asked.
    assert "last 4" in reply.lower()
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


# ── Round 19: a PLAUSIBLE non-customer name must not land on a stranger, and near-miss
#    names must never be read back to an unidentified caller ──────────────────────────
_ROSTER = ["Janae Crespo", "Aiden Rogers", "Miles Haider", "Blair Wagner", "Diya Gupta",
           "Kennedy Brown", "Madison Elhaik", "Jordan Miles", "Casey Nguyen", "Morgan Ellis"]


@pytest.mark.parametrize("plausible", [
    "Jaime Rivera",      # measured: matched "Aiden Rogers" outright (first .60 / last .50)
    "Jamie Rivers",      # the live demo call that exposed this
    "John Smith", "Maria Gonzalez", "Marcus Rogen",
])
def test_plausible_non_customer_name_never_confidently_matched(plausible):
    """The gibberish guard only ever probed gibberish. A REAL-looking name that belongs to
    nobody on file is the dangerous case: it used to be returned as a certain match, putting
    the caller into the verification flow for someone else's order.

    All of these sit at 0.46-0.67 whole-name similarity to their nearest record — far enough
    to be a different person, which is exactly the band the floor now rejects."""
    best, _sugg = main.smart_name_match(plausible, _ROSTER)
    assert best is None, "%r confidently matched %r" % (plausible, best)


@pytest.mark.parametrize("near,record", [("Janet Crespi", "Janae Crespo"),
                                         ("Miles Harden", "Miles Haider")])
def test_near_variant_still_matches_by_design(near, record):
    """DELIBERATE, and the boundary of what the matcher can do. These sit at ~0.83 whole-name
    similarity — indistinguishable by string distance from the real typos above (Kennedy Braun
    0.85, Casey Nguyan 0.92), so any floor that rejected them would strand genuine callers.

    That is acceptable precisely because the matcher is a CONVENIENCE, not the security
    boundary: the agent still only says "I've got an order under X - is that you?", and
    nothing about the order is revealed until _verify_answer passes. A wrong person here
    costs one 'no, that's not me', not a disclosure."""
    assert main.smart_name_match(near, _ROSTER)[0] == record
    red = main._redact_lookup(main._build_order_result(near, *_roster_rows()))
    for pii in main._PII_FIELDS:                 # still nothing beyond the name to confirm
        assert pii not in red, pii


def test_real_typos_still_match_after_the_floor():
    """The floor must not cost genuine callers their match."""
    assert main.smart_name_match("Diya Guta", _ROSTER)[0] == "Diya Gupta"
    assert main.smart_name_match("Kennedy Braun", _ROSTER)[0] == "Kennedy Brown"
    assert main.smart_name_match("Jordan Miles", _ROSTER)[0] == "Jordan Miles"
    assert main.smart_name_match("Casey Nguyan", _ROSTER)[0] == "Casey Nguyen"


def _roster_rows():
    D = [{"Student": n, "ID": "#8%04d-TS" % i, "Service": "Summer Storage", "Building": "Marlow",
          "Room": str(100 + i), "Date": "5/6/2026", "Phone": "555%07d" % i, "Status": "Booked"}
         for i, n in enumerate(_ROSTER)]
    S = [{"Student Name": n, "Order#:": "8%04d-TS" % i, "Building": "Marlow"}
         for i, n in enumerate(_ROSTER)]
    return D, S


@pytest.mark.parametrize("miss", ["Jamie Rivers", "Jaime Rivera", "John Smith", "Marcus Rogen"])
def test_near_miss_never_discloses_other_customers_names(miss):
    """A caller who misspeaks a name must not hear three real students' names. Checked on the
    raw record, the redacted phone payload AND the chat reply — all three used to leak."""
    D, S = _roster_rows()
    rec = main._build_order_result(miss, D, S)
    assert rec.get("status") in ("confirm", "not_found")
    assert rec.get("suggestions") == []
    red = main._redact_lookup(rec)
    chat, _ = main._lookup_flow(miss, {}, D, S)
    for name in _ROSTER:
        assert name not in json.dumps(rec), "raw record leaked %s" % name
        assert name not in json.dumps(red), "redacted lookup leaked %s" % name
        assert name not in chat, "chat reply leaked %s" % name
    assert "spell" in chat.lower()


def test_near_miss_keeps_a_pii_free_signal():
    """Dropping the names must not drop the fact that it was a near miss (QA/logging)."""
    D, S = _roster_rows()
    rec = main._build_order_result("Jamie Rivers", D, S)
    assert rec["status"] == "confirm" and rec.get("near_miss", 0) >= 1


def test_a_shared_phone_number_never_lists_names(monkeypatch):
    """A phone lookup must NEVER return names.

    This test previously asserted the opposite, on the premise that a number matched by caller ID
    belongs to the caller, so offering the names on it is legitimate. That premise does not hold
    here: nothing in this service ever supplies the inbound caller ID — there is no from_number or
    ANI anywhere — so `phone` has exactly one source, digits the caller read out loud. A number
    someone reads out is no evidence they hold it, and the agent prompt's "confirm" handler tells
    the agent to say `message` aloud, so returning names handed a stranger a ready-to-speak list of
    real customers. Same disclosure Round 19 closed for names, reached through the phone rung.

    If real caller ID is ever wired up it must arrive as its OWN parameter, and only that one may
    return names.
    """
    import asyncio
    D = [{"Student": "Alex Reed", "ID": "#1-TS", "Service": "Summer Storage", "Building": "Marlow",
          "Phone": "3145551234", "Date": "5/6/2026"},
         {"Student": "Sam Reed", "ID": "#2-TS", "Service": "Summer Storage", "Building": "Marlow",
          "Phone": "3145551234", "Date": "5/6/2026"}]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else []
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    r = asyncio.run(main.do_lookup_student("", phone="314-555-1234"))
    assert r["status"] == "confirm"
    assert r["suggestions"] == []
    assert r.get("near_miss") == 2            # the signal survives for QA/logging
    for name in ("Alex", "Sam", "Reed"):      # and no name leaks through the spoken message
        assert name.lower() not in r["message"].lower()


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


# ---- two DIFFERENT people share a name: reveal only the order the caller can verify ----
def _dup_data():
    D = [
        {"Student": "John Smith", "ID": "#70001-TS", "Service": "Summer Storage",
         "Building": "Danforth", "Room": "100", "Date": "5/6/2026", "Phone": "5550001111", "Status": "Complete"},
        {"Student": "John Smith", "ID": "#70002-TS", "Service": "Summer Storage",
         "Building": "Eliot", "Room": "200", "Date": "5/7/2026", "Phone": "5550002222", "Status": "Scheduled"},
    ]
    S = [{"Student Name": "John Smith", "Order#:": "70001-TS", "Building": "Danforth"},
         {"Student Name": "John Smith", "Order#:": "70002-TS", "Building": "Eliot"}]
    return D, S

def test_same_name_reveals_only_the_matching_person(monkeypatch):
    import asyncio
    D, S = _dup_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    a = asyncio.run(main.do_get_order_details("John Smith", "Danforth"))
    assert a.get("verified") is True and a.get("order_id") == "#70001-TS" and a.get("building") == "Danforth"
    main._VERIFY_FAILS.clear()
    b = asyncio.run(main.do_get_order_details("John Smith", "Eliot"))
    assert b.get("verified") is True and b.get("order_id") == "#70002-TS" and b.get("building") == "Eliot"

def test_same_name_by_phone_last4_picks_right_person(monkeypatch):
    import asyncio
    D, S = _dup_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    r = asyncio.run(main.do_get_order_details("John Smith", "my last four are 2222"))
    assert r.get("verified") is True and r.get("order_id") == "#70002-TS"

def test_same_name_wrong_building_reveals_nothing(monkeypatch):
    import asyncio
    D, S = _dup_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    r = asyncio.run(main.do_get_order_details("John Smith", "Umrath"))
    assert r.get("verified") is not True
    for pii in main._PII_FIELDS:
        assert pii not in r

def test_same_name_lookup_does_not_disclose_a_strangers_orders():
    D, S = _dup_data()
    full = main._build_order_result("John Smith", D, S)
    assert full.get("distinct_people") is True                 # different phones => different people
    red = main._redact_lookup(full)
    assert "order_choices" not in red and not red.get("needs_order_choice")   # no pre-verify disclosure
    for pii in main._PII_FIELDS:
        assert pii not in red

def _dup_same_building_data():
    D = [
        {"Student": "John Smith", "ID": "#71001-TS", "Service": "Summer Storage",
         "Building": "Danforth", "Room": "100", "Date": "5/6/2026", "Phone": "5550001111", "Status": "Complete"},
        {"Student": "John Smith", "ID": "#71002-TS", "Service": "Summer Storage",
         "Building": "Danforth", "Room": "200", "Date": "5/7/2026", "Phone": "5550002222", "Status": "Scheduled"},
    ]
    S = [{"Student Name": "John Smith", "Order#:": "71001-TS", "Building": "Danforth"},
         {"Student Name": "John Smith", "Order#:": "71002-TS", "Building": "Danforth"}]
    return D, S

def test_same_name_same_building_refuses_weak_verifier_but_phone_resolves(monkeypatch):
    """Two different people share a name AND a building — the building can't tell them apart, so it
    must reveal NOTHING; a distinguishing verifier (phone last-4) then reveals the right person."""
    import asyncio
    D, S = _dup_same_building_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    amb = asyncio.run(main.do_get_order_details("John Smith", "Danforth"))   # matches BOTH -> ambiguous
    assert amb.get("verified") is not True
    for pii in main._PII_FIELDS:
        assert pii not in amb
    main._VERIFY_FAILS.clear()
    r = asyncio.run(main.do_get_order_details("John Smith", "the last four are 2222"))
    assert r.get("verified") is True and r.get("order_id") == "#71002-TS"


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


# ---------- chat <-> voice architecture PARITY (the web chat must gate/route like the phone) ----------
def _oid_re(s):
    """Any order-number-looking token in a string (e.g. '#13851-SS' or '70002-TS')."""
    return re.search(r"#?\d{4,6}-?[A-Za-z]{2}\b", s)


def test_chat_multi_order_lists_no_order_numbers():
    """PARITY (security): a repeat customer's own multiple orders are listed by service+date only —
    never the order number, which is itself a valid verifier. Mirrors the phone's _redact_lookup."""
    D, S = _mo_data()                                            # one person, two orders (same phone)
    reply, state = main._lookup_flow("Jordan Miles", {}, D, S)
    assert state.get("step") == "order"                          # still asks which order
    assert _oid_re(reply) is None, "chat leaked an order number pre-verification: %r" % reply
    assert "13851" not in reply and "14990" not in reply


def test_chat_shared_name_different_people_lists_nothing():
    """PARITY (security): when a name matches DIFFERENT people, the chat must NOT list their orders
    (that discloses a stranger's order + a valid verifier). It goes straight to a verify prompt,
    exactly like the phone's _redact_lookup (which drops order_choices when distinct_people)."""
    D, S = _dup_data()                                           # two DIFFERENT John Smiths
    assert main._build_order_result("John Smith", D, S).get("distinct_people") is True
    reply, state = main._lookup_flow("John Smith", {}, D, S)
    assert state.get("step") == "verify"                         # NOT "order" — no listing
    assert _oid_re(reply) is None and "70001" not in reply and "70002" not in reply


def test_chat_and_voice_reveal_the_same_thing_pre_verify():
    """The redacted phone lookup and the chat's first reply must disclose the SAME set of order
    numbers pre-verification: none. Direct 1:1 comparison over both multi-order shapes."""
    for D, S, name in ((_mo_data() + ("Jordan Miles",)), (_dup_data() + ("John Smith",))):
        vred = main._redact_lookup(main._build_order_result(name, D, S))
        creply, _ = main._lookup_flow(name, {}, D, S)
        assert _oid_re(str(vred)) is None                        # voice: no order # (already guarded)
        assert _oid_re(creply) is None                           # chat: no order # (now guarded too)


def test_chat_shared_name_verify_reveals_only_the_matching_person(monkeypatch):
    """PARITY: after going straight to verify, a building answer reveals ONLY the order it matches —
    the same guarantee do_get_order_details gives the phone."""
    D, S = _dup_data()
    reply, state = main._lookup_flow("John Smith", {}, D, S)     # -> verify
    r1, _ = main._lookup_flow("Danforth", state, D, S)
    assert "verified" in r1.lower() and "Danforth" in r1 and "Eliot" not in r1
    main._VERIFY_FAILS.clear()
    reply2, state2 = main._lookup_flow("John Smith", {}, D, S)
    r2, _ = main._lookup_flow("Eliot", state2, D, S)
    assert "verified" in r2.lower() and "Eliot" in r2 and "Danforth" not in r2


@pytest.mark.parametrize("msg", [
    "I need to cancel my pickup",
    "cancel my order please",
    "Can I reschedule my pickup to next week?",
    "I moved — can you change my address on file?",
    "please update the date on my order",
    "Can you email me all my order details?",
    "text me my invoice",
])
def test_chat_routes_account_changes_to_team(msg):
    """PARITY with the phone agent's v42 prompt: cancel / reschedule / change-a-detail /
    email-or-text-my-details go to the team — the assistant never implies it makes the change."""
    D, S = _id_data()
    reply, state = main._chat_reply(msg, {}, D, S, BOOK)
    assert "266-8878" in reply or "info@utrucking.com" in reply, "did not route to team: %r" % reply
    assert state == {}                                           # not pulled into a make-the-change flow


@pytest.mark.parametrize("msg", [
    "my order status", "where is my order", "look up my order", "what's the status of my order",
])
def test_chat_readonly_status_still_reaches_lookup(msg):
    """The account-change router must NOT swallow a read-only status request (no change verb)."""
    D, S = _id_data()
    reply, state = main._chat_reply(msg, {}, D, S, BOOK)
    assert state.get("intent") == "lookup" and state.get("step") == "name"


@pytest.mark.parametrize("msg", [
    "quote 5 boxes and a mini fridge", "what days are open?", "hi", "Jamie Rivers",
])
def test_chat_account_change_router_does_not_hijack_other_intents(msg):
    """Quotes, availability, greetings and bare names must still work — the change router is targeted."""
    D, S = _id_data()
    reply, _ = main._chat_reply(msg, {}, D, S, BOOK)
    assert "handles changes like that" not in reply


def test_chat_api_force_refreshes_a_stale_cache_on_verify(monkeypatch):
    """PARITY with the phone: on a verify miss the chat re-pulls the sheets FRESH once and re-checks,
    so a just-edited order still verifies in chat — never relaxing the check."""
    import asyncio
    fresh_D, fresh_S = _id_data()
    stale_D = [{**fresh_D[0], "Building": "", "Room": "", "Phone": "", "ID": ""}]
    stale_S = [{"Student Name": "Jamie Rivers", "Order#:": "", "Building": ""}]
    calls = {"forced": 0}
    async def fake_fetch(url, force=False):
        if url == main.DISPATCH_CSV_URL:
            if force: calls["forced"] += 1; return fresh_D
            return stale_D
        return fresh_S if force else stale_S
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    main._VERIFY_FAILS.clear()

    class Req:
        client = type("C", (), {"host": "7.7.7.7"})()
        def __init__(self, msg, state): self._b = {"args": {"message": msg, "state": state}}
        async def json(self): return self._b

    verify_state = {"intent": "lookup", "step": "verify", "name": "Jamie Rivers"}
    r = asyncio.run(main.chat_api(Req("Northgate B", verify_state)))
    body = r[1][0]
    assert "verified" in body["reply"].lower(), body["reply"]   # fresh re-check verified it
    assert calls["forced"] >= 1                                 # it actually re-fetched fresh


# ==================== round 17 review regressions ====================
class _Req:
    def __init__(self, msg, state, host="8.8.8.8"):
        self._b = {"args": {"message": msg, "state": state}}
        self.client = type("C", (), {"host": host})()
    async def json(self):
        return self._b


def test_order_hint_does_not_bypass_shared_name_guard(monkeypatch):
    """A non-secret order_hint + a weak shared verifier (building) must NOT short-circuit the
    distinct_people ambiguity guard and hand back one specific stranger's PII."""
    import asyncio
    D, S = _dup_same_building_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    leaked = asyncio.run(main.do_get_order_details("John Smith", "Danforth", "storage"))
    assert leaked.get("verified") is not True
    for pii in main._PII_FIELDS:
        assert pii not in leaked, pii
    # a DISTINGUISHING verifier (phone last-4) + the same hint still resolves the right person
    main._VERIFY_FAILS.clear()
    ok = asyncio.run(main.do_get_order_details("John Smith", "the last four are 2222", "storage"))
    assert ok.get("verified") is True and ok.get("order_id") == "#71002-TS"


def test_verify_identity_checks_every_order_for_shared_name(monkeypatch):
    """verify_identity must check the answer against EVERY order under a shared name (via _verify_pick),
    so each real person verifies with THEIR own detail — and it never returns order PII."""
    import asyncio
    D, S = _dup_data(); _patch_rows(monkeypatch, D, S)     # two John Smiths, different buildings/phones
    main._VERIFY_FAILS.clear()
    a = asyncio.run(main.do_verify_identity("John Smith", "Danforth"))
    assert a.get("verified") is True                        # person A's building
    main._VERIFY_FAILS.clear()
    b = asyncio.run(main.do_verify_identity("John Smith", "Eliot"))
    assert b.get("verified") is True                        # person B's building (was False before fix)
    main._VERIFY_FAILS.clear()
    p = asyncio.run(main.do_verify_identity("John Smith", "my last four are 1111"))
    assert p.get("verified") is True                        # person A's phone
    for pii in main._PII_FIELDS:                            # the gate never returns order values
        assert pii not in a and pii not in b and pii not in p, pii


def test_find_month_ignores_the_modal_may():
    assert main._find_month("May I get a quote for 10 boxes?") is None
    assert main._find_month("I may need a couple boxes") is None
    assert main._find_month("may i speak to a representative") is None
    assert main._find_month("what days are open in may?") == 5        # real month use still works
    assert main._find_month("may 12") == 5


def test_status_update_phrasing_reaches_lookup_not_change_deflection():
    D, S = _id_data()
    book = engines.build_price_book(S)
    reply, state = main._chat_reply("any update on my order?", {}, D, S, book)
    assert "team handles changes" not in reply.lower()
    assert state.get("intent") == "lookup"                            # routed to order lookup
    for change in ("cancel my pickup", "update my address"):          # genuine changes still deflect
        r, _ = main._chat_reply(change, {}, D, S, book)
        assert "team handles changes" in r.lower(), change


def test_chat_freshness_recheck_counts_a_miss_only_once(monkeypatch):
    """On a genuine verify miss the chat re-pulls fresh sheets once; that re-check must NOT double-
    count the failure — parity with the phone's single count per attempt (else chat locks out early)."""
    import asyncio
    D, S = _id_data()
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else S
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    main._VERIFY_FAILS.clear(); main._IP_FAILS.clear()
    state = {"intent": "lookup", "step": "verify", "name": "Jamie Rivers"}
    body = asyncio.run(main.chat_api(_Req("Umrath", state)))[1][0]    # wrong building -> genuine miss
    assert "doesn't match" in body["reply"].lower()
    assert main._VERIFY_FAILS.get("jamie rivers", [0])[0] == 1        # counted once, not twice


def test_ip_bruteforce_lockout_across_rotating_names(monkeypatch):
    """The per-IP limiter must stop one connection rotating through many names — the per-name limiter
    resets per name, so this is the only cross-name defense (previously untested)."""
    import asyncio
    names = ["Cust%02d Tester" % i for i in range(main._IP_MAX + 4)]
    D = [{"Student": n, "ID": "#82%03d-TS" % i, "Service": "Summer Storage", "Building": "Marlow",
          "Room": str(100 + i), "Date": "5/6/2026", "Phone": "555%07d" % i, "Status": "Booked"}
         for i, n in enumerate(names)]
    S = [{"Student Name": n, "Order#:": "82%03d-TS" % i, "Building": "Marlow"} for i, n in enumerate(names)]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else S
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    main._VERIFY_FAILS.clear(); main._IP_FAILS.clear()
    last = None
    for i in range(main._IP_MAX + 1):                                # one wrong verify per fresh name
        state = {"intent": "lookup", "step": "verify", "name": names[i]}
        last = asyncio.run(main.chat_api(_Req("Zzzref Place", state, host="6.6.6.6")))[1][0]
    assert "too many verification attempts from this connection" in last["reply"].lower()


# ── Round 18: the verifier itself must not be handed out, and guessing must get expensive ──

def test_sample_ids_never_returns_a_verifier(monkeypatch):
    """/sample_ids is a test aid that returns REAL customer names. It must never return the
    building, room or order number beside the name: those are exactly what _verify_answer accepts,
    so shipping them together hands out the answer key and defeats the identity gate for anyone who
    can reach the endpoint. Regression for the Round-18 finding."""
    import asyncio, json
    D = [{"Student": "Jamie Rivers", "ID": "#90001-TS", "Service": "Summer Storage",
          "Building": "Northgate B", "Room": "1205", "Phone": "5550100200"}]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else []
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)
    monkeypatch.setattr(main, "API_SECRET", "")             # deliberately open; still no verifier
    monkeypatch.setattr(main, "ALLOW_OPEN_API", True)

    class _GetReq:
        query_params = {}
        headers = {}
    res = asyncio.run(main.sample_ids(_GetReq()))
    body = json.dumps(res[1][0] if isinstance(res, tuple) else res)
    assert "Jamie Rivers" in body                          # the name is the point of the endpoint
    for verifier in ("Northgate", "1205", "90001", "5550100200"):
        assert verifier not in body, "sample_ids leaked a verifier: %s" % verifier


def test_verify_field_prefers_the_strong_secret():
    """Ask for the 10,000-value phone last-4 over the ~60-value building whenever one is on file."""
    assert main._verify_field({"phone": "5550100200", "building": "Northgate B",
                               "order_id": "#90001-TS"}) == "phone"
    assert main._verify_field({"building": "Northgate B", "order_id": "#90001-TS"}) == "order_id"
    assert main._verify_field({"building": "Northgate B"}) == "building"
    # a building-only record still gets asked something usable, on both channels
    assert "building" in main._verify_prompt({"building": "Northgate B"})
    assert "building" in main._verify_ask_chat({"building": "Northgate B"})


def test_chat_and_voice_ask_for_the_same_verifier():
    """Parity: the phone and the web chat must choose the SAME detail, or one channel is weaker."""
    for rec in ({"phone": "5550100200", "building": "Northgate B"},
                {"building": "Northgate B", "order_id": "#90001-TS"},
                {"building": "Northgate B"}):
        f = main._verify_field(rec)
        assert main._verify_prompt(rec) == main._VERIFY_ASK_VOICE[f]
        assert main._verify_ask_chat(rec) == main._VERIFY_ASK_CHAT[f]


def test_lockout_escalates_so_waiting_it_out_stops_working():
    """A fixed 15-minute lock is sweepable: ~60 buildings / 5 tries per window = a few hours to a
    guaranteed reveal. Each served lock must earn a strike that doubles the next window."""
    main._VERIFY_FAILS.clear(); main._VERIFY_STRIKES.clear()
    n = "jamie rivers"
    assert main._lock_window(n) == main._VERIFY_WINDOW
    for cycle in range(3):
        for _ in range(main._VERIFY_MAX):
            main._verify_fail(n)
        assert main._verify_locked(n), "should be locked after MAX fails"
        # age the window out — the lock is served, and banks a strike on the way out
        main._VERIFY_FAILS[n][1] = time.time() - main._lock_window(n) - 1
        assert not main._verify_locked(n)
        assert main._strikes(n) == cycle + 1
    assert main._lock_window(n) == main._VERIFY_WINDOW * 8       # 15m → 2h after three locks
    main._VERIFY_FAILS.clear(); main._VERIFY_STRIKES.clear()


def test_lock_window_is_capped_and_strikes_decay():
    main._VERIFY_FAILS.clear(); main._VERIFY_STRIKES.clear()
    n = "someone else"
    main._VERIFY_STRIKES[n] = [main._STRIKE_MAX, time.time()]
    assert main._lock_window(n) == main._VERIFY_WINDOW_CAP        # never grows past the cap
    main._VERIFY_STRIKES[n] = [4, time.time() - main._STRIKE_DECAY - 1]
    assert main._strikes(n) == 0                                  # a quiet day forgives
    assert main._lock_window(n) == main._VERIFY_WINDOW
    main._VERIFY_STRIKES.clear()


def test_successful_verification_clears_strikes():
    """A real caller who fumbled a couple of details must not stay throttled once they prove it."""
    main._VERIFY_FAILS.clear(); main._VERIFY_STRIKES.clear()
    n = "jamie rivers"
    main._VERIFY_FAILS[n] = [3, time.time()]
    main._VERIFY_STRIKES[n] = [2, time.time()]
    main._verify_clear(n)
    assert n not in main._VERIFY_FAILS and main._strikes(n) == 0


def test_sample_ids_verifiers_require_an_ARMED_key(monkeypatch):
    """The staff test panel may show a verifier, but only when the gate is genuinely armed.

    _authorized() alone is NOT sufficient. The endpoint can legitimately be running open — a
    laptop with UTRUCKING_ALLOW_OPEN_API=1 — and that is precisely the configuration the answer
    key must never be served in. So the verifiers view fails CLOSED in every open configuration,
    deliberate or not, even when a caller asks for it.
    """
    import asyncio, json
    D = [{"Student": "Jamie Rivers", "ID": "#90001-TS", "Service": "Summer Storage",
          "Building": "Northgate B", "Room": "1205", "Phone": "5550100200"}]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else []
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)

    class _Q:
        def __init__(self, q, h):
            self.query_params, self.headers = q, h

    def call(q, h, secret, allow_open=True):
        monkeypatch.setattr(main, "API_SECRET", secret)
        monkeypatch.setattr(main, "ALLOW_OPEN_API", allow_open)
        return asyncio.run(main.sample_ids(_Q(q, h)))[1][0]

    # deliberately open — asking for verifiers must still not produce them
    r = call({"verifiers": "1"}, {}, "")
    assert "verify" not in r["sample"][0] and r.get("verifiers") == "locked"
    r = call({"verifiers": "1"}, {"x-utrucking-key": "guessed"}, "")
    assert "verify" not in r["sample"][0]

    # no key deployed and no opt-in → the endpoint refuses outright, as a server fault
    assert call({"verifiers": "1"}, {}, "", allow_open=False).get("status") == "unconfigured"

    # gate ARMED, wrong/absent key → the whole endpoint 401s
    assert call({"verifiers": "1"}, {}, "s3cret").get("status") == "unauthorized"

    # gate ARMED + correct key → verifiers appear, but never the full phone number
    r = call({"verifiers": "1"}, {"x-utrucking-key": "s3cret"}, "s3cret")
    v = r["sample"][0]["verify"]
    assert v["phone_last4"] == "0200" and "5550100200" not in json.dumps(r)
    assert set(v) == {"building", "phone_last4", "order_id"}

    # armed + correct key but NOT requested → still names only
    assert "verify" not in call({}, {"x-utrucking-key": "s3cret"}, "s3cret")["sample"][0]


# ── Round 20: a refusal must never be reportable as an outage ────────────────────────
# The live call: the agent asked "is that you?", the caller said "Yes", and the agent sent THAT to
# get_order_details as the verifier — it never asked for the detail the tool named. The refusal came
# back correct, and the agent read it out as "I'm having trouble reaching your records right now"
# and transferred to a human. Every failure looked identical from the outside, so `reason` now names
# which one it is, and only "error" ever means the records are the problem.

def test_name_confirmation_is_reported_as_no_answer_not_an_outage(monkeypatch):
    """The exact transcript bug. "Yes" is a NAME confirmation, not a verifier — the tool must say so
    in a field a prompt can branch on, tell the agent to go and ask, and still reveal nothing."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    for bogus in ("Yes", "yeah", "That's me!", "correct", "sure", "yep", "uh huh", "", "   "):
        main._VERIFY_FAILS.clear()
        r = asyncio.run(main.do_get_order_details("Jamie Rivers", bogus))
        assert r.get("verified") is False, bogus
        assert r.get("reason") == "no_answer_supplied", (bogus, r.get("reason"))
        assert r.get("verify_with"), bogus                       # names the detail still to ask for
        assert "ask the caller" in r["message"].lower(), bogus   # and says so in the prose too
        for pii in main._PII_FIELDS:
            assert pii not in r, (bogus, pii)


def test_each_failure_reason_is_returned_on_its_own_path(monkeypatch):
    """The four outcomes must be impossible to conflate: a wrong detail, a name we can't resolve, the
    lockout and a genuine records failure each carry their own reason."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    wrong = asyncio.run(main.do_get_order_details("Jamie Rivers", "Westwood Hall"))
    assert wrong.get("verified") is False and wrong.get("reason") == "unverified"
    unknown = asyncio.run(main.do_get_order_details("Nobody McGhost", "Northgate B"))
    assert unknown.get("verified") is False and unknown.get("reason") == "unverified"  # NOT an outage

    main._VERIFY_FAILS.clear()
    for i in range(main._VERIFY_MAX):
        asyncio.run(main.do_get_order_details("Jamie Rivers", "wrong%d" % i))
    locked = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))
    assert locked.get("locked") is True and locked.get("reason") == "locked"

    # only these last two are the system's fault: the fetch blew up, or the sheets came back empty
    # (a missing sheet ID / a sign-in page reads exactly like this).
    async def boom(url, force=False):
        raise RuntimeError("sheets unreachable")
    monkeypatch.setattr(main, "fetch_csv_rows", boom)
    main._VERIFY_FAILS.clear()
    down = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))
    assert down["status"] == "error" and down["reason"] == "error"

    async def empty(url, force=False):
        return []
    monkeypatch.setattr(main, "fetch_csv_rows", empty)
    blank = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))
    assert blank["status"] == "error" and blank["reason"] == "error"


def test_verify_identity_classifies_failures_the_same_way(monkeypatch):
    """Parity: the gate-only tool must return the SAME reason vocabulary, or a prompt that trusts
    `reason` behaves differently depending on which tool the agent happened to reach for."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    nope = asyncio.run(main.do_verify_identity("Jamie Rivers", "Yes"))
    assert nope.get("verified") is False and nope.get("reason") == "no_answer_supplied"
    assert nope.get("verify_with")
    main._VERIFY_FAILS.clear()
    wrong = asyncio.run(main.do_verify_identity("Jamie Rivers", "Westwood Hall"))
    assert wrong.get("reason") == "unverified"
    ok = asyncio.run(main.do_verify_identity("Jamie Rivers", "Northgate B"))
    assert ok.get("verified") is True and "reason" not in ok        # success carries no failure reason
    main._VERIFY_FAILS.clear()
    for i in range(main._VERIFY_MAX):
        asyncio.run(main.do_verify_identity("Jamie Rivers", "nope%d" % i))
    assert asyncio.run(main.do_verify_identity("Jamie Rivers", "Northgate B")).get("reason") == "locked"

    async def boom(url, force=False):
        raise RuntimeError("sheets unreachable")
    monkeypatch.setattr(main, "fetch_csv_rows", boom)
    assert asyncio.run(main.do_verify_identity("Jamie Rivers", "Northgate B")).get("reason") == "error"
    for r in (nope, wrong, ok):                                    # still a clean gate: no order PII
        for pii in main._PII_FIELDS:
            assert pii not in r, pii


def test_a_confirmation_word_that_really_is_the_verifier_still_verifies(monkeypatch):
    """The no_answer label is a LABEL. It is applied only after _verify_answer has already refused, so
    a record whose building happens to contain one of those words behaves exactly as it did before —
    the answer verifies and the order comes back. The label can no more withhold a reveal than grant one."""
    import asyncio
    D = [{"Student": "Robin Vale", "ID": "#60009-TS", "Service": "Summer Storage", "Building": "Wright Hall",
          "Room": "12", "Date": "5/6/2026", "Phone": "", "Status": "Scheduled"}]
    S = [{"Student Name": "Robin Vale", "Order#:": "60009-TS", "Building": "Wright Hall"}]
    _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    assert main._is_non_answer("right") is True                    # it IS on the list...
    r = asyncio.run(main.do_get_order_details("Robin Vale", "right"))
    assert r.get("verified") is True and r.get("building") == "Wright Hall"   # ...and still verifies
    assert "reason" not in r


def test_a_confirmation_word_plus_a_real_guess_is_still_a_real_attempt(monkeypatch):
    """The exemption is WHOLE-STRING only. "yes, Danforth" carries a guess, so it counts against the
    brute-force budget like any other wrong answer — otherwise the label would buy free guesses."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    r = asyncio.run(main.do_get_order_details("Jamie Rivers", "yes, Danforth"))
    assert r.get("reason") == "unverified"
    assert main._VERIFY_FAILS["jamie rivers"][0] == 1
    for probe in ("yeah 0200", "sure, Northgat", "correct - 90001"):
        assert main._is_non_answer(probe) is False, probe


def test_a_name_confirmation_does_not_spend_the_callers_attempts(monkeypatch):
    """The five attempts belong to the CALLER and are shared with the web chat. An agent that keeps
    sending "Yes" is repeating its own mistake — it must not lock a genuine customer out of both
    channels before anyone has even asked them for a detail. Nothing is guessed here, so nothing is
    banked; a wrong answer still is (asserted above)."""
    import asyncio
    D, S = _id_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    for _ in range(main._VERIFY_MAX + 2):
        r = asyncio.run(main.do_get_order_details("Jamie Rivers", "Yes"))
        assert r.get("reason") == "no_answer_supplied"
    assert "jamie rivers" not in main._VERIFY_FAILS
    ok = asyncio.run(main.do_get_order_details("Jamie Rivers", "Northgate B"))
    assert ok.get("verified") is True                               # not locked out by the agent's bug


# ── one PERSON, four orders, more than one phone: the distinct_people cost, measured ──
def _one_person_four_orders_data():
    """ONE customer with four orders: their own number on two, a parent's on the third, blank on the
    fourth. Exactly the shape that reads as three identities (0100 / 0199 / unknown) — deliberately."""
    rows = (("#60001-SS", "Summer Storage", "Marlow A", "210", "5/6/2025", "5551110100", "Complete"),
            ("#60002-RR", "Return Delivery", "Marlow A", "210", "8/20/2025", "5551110100", "Complete"),
            ("#60003-SS", "Summer Storage", "Weston C", "118", "5/5/2026", "5552220199", "Scheduled"),
            ("#60004-RR", "Return Delivery", "Danforth D", "118", "8/18/2026", "", "Scheduled"))
    D = [{"Student": "Priya Raman", "ID": oid, "Service": svc, "Building": b, "Room": rm,
          "Date": d, "Phone": ph, "Status": st} for oid, svc, b, rm, d, ph, st in rows]
    S = [{"Student Name": "Priya Raman", "Order#:": oid.lstrip("#"), "Service Type": svc,
          "Building": b, "Invoice ID": "INV-6%02d" % i}
         for i, (oid, svc, b, _rm, _d, _ph, _st) in enumerate(rows)]
    return D, S


def test_one_person_many_orders_is_still_flagged_distinct_people():
    """DELIBERATE, and documented at the `ident` set in _build_order_result. Two phone numbers plus a
    blank read as three identities, so a genuine repeat customer is treated like a shared name and is
    not offered his own order list before he proves who he is. Kept because nothing in these sheets
    separates "a parent booked one order" from "two students share a name" — the only fields that
    would merge them (building, room, address) are the weak, guessable ones the gate already refuses
    to trust, and merging on those re-opens the Round 19 disclosure."""
    D, S = _one_person_four_orders_data()
    full = main._build_order_result("Priya Raman", D, S)
    assert full["order_count"] == 4 and full.get("distinct_people") is True
    red = main._redact_lookup(full)
    assert "order_choices" not in red and not red.get("needs_order_choice")
    assert _oid_re(str(red)) is None                        # and no order number pre-verification
    assert red.get("verify_with")                           # he is simply asked to verify instead
    for pii in main._PII_FIELDS:
        assert pii not in red, pii


@pytest.mark.parametrize("answer,order_id", [
    ("0100", "#60001-SS"),                        # his own last-4 — carried by two of his orders
    ("my last four are 0199", "#60003-SS"),       # spoken, and it's the number a parent booked under
    ("0199", "#60003-SS"),
    ("Marlow A", "#60001-SS"),                    # building shared by the two orders on one number
    ("Weston C", "#60003-SS"),
    ("Danfrth D", "#60004-RR"),                   # misspelt building, on the order with NO phone on file
    ("order 60002", "#60002-RR"),
    ("60004", "#60004-RR"),
])
def test_one_person_many_orders_still_verifies_with_every_real_verifier(monkeypatch, answer, order_id):
    """The cost of the flag must stay confined to the pre-verification listing: every verifier this
    customer actually has still works, and each reveals exactly the one order it proves."""
    import asyncio
    D, S = _one_person_four_orders_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    r = asyncio.run(main.do_get_order_details("Priya Raman", answer))
    assert r.get("verified") is True, (answer, r.get("reason"), r.get("message"))
    assert r.get("order_id") == order_id, (answer, r.get("order_id"))


def test_multi_order_caller_gets_the_order_they_asked_about(monkeypatch):
    """His last-4 verifies two of his own orders. Both carry the SAME number — one identity — so
    choosing between them is convenience, not disclosure, and the order_hint decides. Before this a
    caller asking about the return delivery got whichever came first, because the order_hint
    short-circuit in _verify_pick is (correctly) skipped for anyone flagged distinct_people."""
    import asyncio
    D, S = _one_person_four_orders_data(); _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    ret = asyncio.run(main.do_get_order_details("Priya Raman", "0100", "the return delivery"))
    assert ret.get("verified") is True and ret.get("order_id") == "#60002-RR"
    main._VERIFY_FAILS.clear()
    sto = asyncio.run(main.do_get_order_details("Priya Raman", "0100", "summer storage"))
    assert sto.get("verified") is True and sto.get("order_id") == "#60001-SS"
    # the hint can only pick among orders the ANSWER already proved — it never reaches the orders on
    # the other number, so it cannot be used to steer the reveal onto an unproven identity.
    main._VERIFY_FAILS.clear()
    steer = asyncio.run(main.do_get_order_details("Priya Raman", "0100", "order 60003"))
    assert steer.get("order_id") in ("#60001-SS", "#60002-RR") and steer.get("building") == "Marlow A"


def test_the_residual_cost_of_the_flag_is_paid_where_it_must_be(monkeypatch):
    """The price of keeping distinct_people strict, stated as a test. When one of his own verifiers
    spans orders with DIFFERENT phone identities — here a building shared by the parent-booked order
    and the one with no phone on file — it reveals nothing at all. From the sheet alone that is
    indistinguishable from two students sharing a name, so the refusal is the Round 19 rule working
    as intended; a distinguishing verifier still gets him through."""
    import asyncio
    D, S = _one_person_four_orders_data()
    D[3] = {**D[3], "Building": "Weston C"}                 # same dorm as the parent-booked order
    S[3] = {**S[3], "Building": "Weston C"}
    _patch_rows(monkeypatch, D, S)
    main._VERIFY_FAILS.clear()
    amb = asyncio.run(main.do_get_order_details("Priya Raman", "Weston C"))
    assert amb.get("verified") is not True and amb.get("reason") == "unverified"
    for pii in main._PII_FIELDS:
        assert pii not in amb, pii
    main._VERIFY_FAILS.clear()
    ok = asyncio.run(main.do_get_order_details("Priya Raman", "order 60004"))
    assert ok.get("verified") is True and ok.get("order_id") == "#60004-RR"


# ---- Round 20: a generic residence word is not a secret ----------------------
# `_building_matches` opened with `if t == b or t in b`. That second test was a bare substring
# against the whole building name and ran before _BLD_STOP was applied to anything, so the single
# word "hall" verified against every building whose name ends in "Hall" and "house" covered most of
# the rest. Two words defeated the identity gate for nearly every customer, using no secret at all —
# an attacker needed only a name, which the agent confirms out loud ("I've got an order under X,
# is that you?"). Reachable from the phone agent and from the unauthenticated /chat endpoint alike.
_REAL_BUILDINGS = ["Umrath Hall", "Eliot Hall", "Danforth House", "The Village East",
                   "South 40 Hall", "Northgate B", "Wright Hall", "Shepley Hall",
                   "Park House", "Beaumont Hall"]


@pytest.mark.parametrize("generic", [
    "hall", "house", "the", "room", "dorm", "building", "apartment", "apartments",
    "residence", "suite", "suites", "my building", "the hall", "a house", "yes",
])
def test_a_generic_residence_word_verifies_nothing(generic):
    """No word that identifies nothing may verify ANY building."""
    hits = [b for b in _REAL_BUILDINGS if main._building_matches(generic, b)]
    assert hits == [], "%r verified %s" % (generic, hits)


@pytest.mark.parametrize("said,building", [
    ("Umrath Hall", "Umrath Hall"),          # exact
    ("umrath", "Umrath Hall"),               # distinctive word alone
    ("Umrath Hal", "Umrath Hall"),           # misspelling
    ("I live in Umrath Hall", "Umrath Hall"),# in a sentence
    ("Northgat B", "Northgate B"),           # misspelling + section letter
    ("Northgate", "Northgate B"),            # section letter omitted
    ("right", "Wright Hall"),                # homophone of the real name
    ("Danforth", "Danforth House"),
    ("Village East", "The Village East"),    # leading article omitted
    ("the village east", "The Village East"),
    ("South 40", "South 40 Hall"),
    ("Beaumont", "Beaumont Hall"),
])
def test_a_real_caller_saying_their_building_still_verifies(said, building):
    """The fix must not cost a genuine caller their verifier."""
    assert main._building_matches(said, building) is True


def test_a_building_of_only_generic_words_cannot_verify():
    """Fail closed: if nothing on file is distinctive, the building proves nobody's identity."""
    assert main._building_matches("hall", "Hall") is False
    assert main._building_matches("the house", "The House") is False


def test_one_students_building_does_not_verify_another(monkeypatch):
    """End to end: the gate still refuses a generic word and still admits the real one."""
    import asyncio
    D = [{"Student": "Robin Vale", "ID": "#900-SS", "Service": "Summer Storage",
          "Building": "Umrath Hall", "Room": "412", "Phone": "3145559999", "Date": "5/6/2026"}]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else []
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)

    main._VERIFY_FAILS.clear()
    bad = asyncio.run(main.do_get_order_details("Robin Vale", "hall"))
    assert bad.get("verified") is not True
    for pii in main._PII_FIELDS:
        assert pii not in bad, pii

    main._VERIFY_FAILS.clear()
    good = asyncio.run(main.do_get_order_details("Robin Vale", "Umrath Hall"))
    assert good.get("verified") is True and good.get("room") == "412"


def test_strict_mode_nulls_do_not_crash_the_tool_wrappers():
    """tool_call_strict_mode makes the model send every declared property, so an ordinary name
    lookup arrives as {"name_heard": "...", "order_hint": null, "phone": null}. These wrappers
    must accept that rather than raise a validation error with no status and no reason."""
    import asyncio, json as _json
    for coro in (main.lookup_student("Nobody Here", None, None),
                 main.get_order_details("Nobody Here", "hall", None),
                 main.verify_identity("Nobody Here", "hall", None)):
        out = _json.loads(asyncio.run(coro))
        assert isinstance(out, dict) and out.get("verified") is not True


def test_a_malformed_json_body_does_not_raise():
    """A JSON body need not be an object; _extract_args used to raise straight out and 500."""
    for body in ([1, 2], "args", 5, None, True):
        assert main._extract_args(body) == {}


def test_a_phone_rung_hit_never_asks_for_the_phone_back():
    """Knowing a phone number must not be sufficient to become its owner.

    The v45 phone rung lets a caller reach a record by reciting a number. _verify_field then asked
    for "the last 4 digits of the phone number on the order" - the number they had just said - so
    anyone who knew a student's phone number could obtain their name, building and room. On a
    phone-sourced hit we must ask for something they have not already told us.
    """
    rec = {"status": "found", "confirmed_name": "Robin Vale", "phone": "3145550200",
           "order_id": "#900-SS", "building": "Umrath Hall", "identified_by": "phone"}
    out = main._redact_lookup(rec)
    assert out["identified_by"] == "phone"
    assert "phone" not in out["verify_with"].lower()
    assert "Robin" not in out["message"] and "Vale" not in out["message"]

    no_id = {"status": "found", "confirmed_name": "Robin Vale", "phone": "3145550200",
             "building": "Umrath Hall", "identified_by": "phone"}
    assert "phone" not in main._redact_lookup(no_id)["verify_with"].lower()

    # A normal name lookup is unchanged: phone last-4 is still the strongest verifier.
    by_name = {"status": "found", "confirmed_name": "Robin Vale", "phone": "3145550200",
               "order_id": "#900-SS", "building": "Umrath Hall"}
    assert "phone" in main._redact_lookup(by_name)["verify_with"].lower()


# ── Round 21: the gate's failure mode, and the rung that could not fire ──────────────
# Two of the "known, not yet fixed" items from Round 20. Both are about a stated contract that
# the code did not actually keep: a gate documented as protecting PII that disappeared when its
# secret did, and a phone rung documented as working at 7 digits that only ever worked at 10.

def test_the_staff_gate_fails_closed_when_its_secret_is_missing(monkeypatch):
    """An unset API_SECRET used to mean OPEN, so the one deploy mistake most likely to happen —
    missing the value in Render during a rotation — published every customer record with a 200
    and no error anywhere. Absence of a secret must refuse, and must refuse as a SERVER fault so
    whoever is debugging the rotation looks at the environment rather than at the caller."""
    class _R:
        def __init__(self, h=None):
            self.headers, self.query_params = h or {}, {}

    monkeypatch.setattr(main, "API_SECRET", "")
    monkeypatch.setattr(main, "ALLOW_OPEN_API", False)
    assert main._gate_state(_R()) == "unconfigured"
    assert not main._authorized(_R())
    # ... and not openable by guessing, either: with no secret there is no key that works
    assert not main._authorized(_R({"x-utrucking-key": ""}))
    assert not main._authorized(_R({"x-utrucking-key": "anything"}))

    res = main._unauthorized(_R())
    assert res[2]["status_code"] == 503 and res[1][0]["status"] == "unconfigured"
    assert main._unauthorized()[2]["status_code"] == 401      # a real caller error still 401s

    # armed behaves exactly as before
    monkeypatch.setattr(main, "API_SECRET", "s3cret")
    assert main._authorized(_R({"x-utrucking-key": "s3cret"}))
    assert not main._authorized(_R({"x-utrucking-key": "s3cre"}))
    assert main._gate_state(_R()) == "denied"
    assert main._unauthorized(_R())[2]["status_code"] == 401


def test_running_open_requires_an_affirmative_act(monkeypatch):
    """Open is still reachable for local work — but only by setting UTRUCKING_ALLOW_OPEN_API, never
    by forgetting something. That asymmetry is the entire fix: an omission now errors."""
    class _R:
        headers, query_params = {}, {}
    monkeypatch.setattr(main, "API_SECRET", "")
    monkeypatch.setattr(main, "ALLOW_OPEN_API", True)
    assert main._authorized(_R()) and main._gate_state(_R()) == "ok"


def test_health_reports_the_gate_state_without_reporting_the_key(monkeypatch):
    """Closing the gate fixed the breach and left the diagnosis: with no secret deployed every
    gated endpoint answers 503, and from outside the box that looks identical to the sheets being
    down or a wedged process. /health is the one place an operator can see WHICH it is, so it
    carries the deployed state — and, being unauthenticated, carries it as a fixed word rather
    than as anything derived from the key."""
    import asyncio

    class _R:
        headers, query_params = {}, {}

    def health():
        return asyncio.run(main.health(_R()))[1][0]

    monkeypatch.setattr(main, "API_SECRET", "")
    monkeypatch.setattr(main, "ALLOW_OPEN_API", False)
    assert main._staff_gate_status() == "unconfigured"
    assert health()["staff_gate"] == "unconfigured"

    monkeypatch.setattr(main, "ALLOW_OPEN_API", True)          # open, but on purpose
    assert health()["staff_gate"] == "open"

    monkeypatch.setattr(main, "API_SECRET", "s3cret-value")
    for allow_open in (True, False):                            # a real key wins either way
        monkeypatch.setattr(main, "ALLOW_OPEN_API", allow_open)
        assert health()["staff_gate"] == "armed"

    # the deployed state, never the deployment secret: not the value, not a prefix, not the length
    body = health()
    assert "s3cret-value" not in str(body) and "s3cret" not in str(body)
    assert len(str(body["staff_gate"])) == len("armed") != len("s3cret-value")
    assert body["status"] == "ok"                                # liveness stays liveness
    assert body["sheets_configured"] is main.SHEETS_CONFIGURED   # unchanged by any of this

    # and it answers about the ENVIRONMENT, not about whoever is asking — a stranger's guessed
    # key must not change the reading, or /health becomes a free oracle for testing guesses.
    class _Guess:
        headers, query_params = {"x-utrucking-key": "guessed"}, {}
    assert asyncio.run(main.health(_Guess()))[1][0]["staff_gate"] == "armed"


def test_secret_compare_is_constant_time_and_rejects_empties():
    """These comparisons are reachable by anyone, so `==` on a secret is a public timing oracle.
    An empty configured secret must never validate — that is the unconfigured state, not a match."""
    assert main._secret_eq("abc", "abc")
    assert not main._secret_eq("ab", "abc")
    assert not main._secret_eq("", "")
    assert not main._secret_eq("anything", "")
    assert not main._secret_eq(None, "abc")


def test_a_seven_digit_number_reaches_the_order_it_names():
    """Rung 3 of the v46 ladder tells the agent a number "needs at least 7 digits", and
    _match_by_phone compared the last 10 of both sides — so 7, 8 and 9 digits matched nothing at
    all. The rung the ladder falls back on was documented as working and could not fire."""
    rows = [{"Student": "Robin Vale", "Phone": "(314) 555-0200"},
            {"Student": "Robin Vale", "Phone": "3145550200"},          # same person, second order
            {"Student": "Alex Kerr", "Phone": "314-555-9999"}]
    assert main._match_by_phone("5550200", rows) == ["Robin Vale"]     # local 7
    assert main._match_by_phone("555-0200", rows) == ["Robin Vale"]
    assert main._match_by_phone("3145550200", rows) == ["Robin Vale"]  # full 10 still works
    assert main._match_by_phone("13145550200", rows) == ["Robin Vale"]  # leading 1 trimmed
    assert main._match_by_phone("555020", rows) == []                  # 6 digits is a fragment
    assert main._match_by_phone("", rows) == []
    assert main._match_by_phone("5551234", rows) == []                 # no such line

    # a short number is looser, so it can collide across area codes — which must return BOTH
    # names to do_lookup_student, whose multi-hit branch reveals none of them.
    across = [{"Student": "Robin Vale", "Phone": "3145550200"},
              {"Student": "Sam Otieno", "Phone": "6365550200"}]
    assert main._match_by_phone("5550200", across) == ["Robin Vale", "Sam Otieno"]
    assert main._match_by_phone("3145550200", across) == ["Robin Vale"]


def test_a_short_number_collision_still_reveals_nothing(monkeypatch):
    """The looseness above costs a caller an extra question and nothing else: a multi-name hit
    returns no names, and a single hit is still only a redacted record behind the identity gate."""
    import asyncio
    D = [{"Student": "Robin Vale", "ID": "#900-SS", "Service": "Summer Storage",
          "Building": "Umrath Hall", "Room": "412", "Phone": "3145550200"},
         {"Student": "Sam Otieno", "ID": "#901-SS", "Service": "Summer Storage",
          "Building": "Park House", "Room": "5", "Phone": "6365550200"}]
    async def fake_fetch(url, force=False):
        return D if url == main.DISPATCH_CSV_URL else []
    monkeypatch.setattr(main, "fetch_csv_rows", fake_fetch)

    out = asyncio.run(main.do_lookup_student("", phone="5550200"))
    assert out["status"] == "confirm" and out["suggestions"] == [] and out["near_miss"] == 2
    body = str(out)
    assert "Robin" not in body and "Vale" not in body and "Otieno" not in body

    hit = asyncio.run(main.do_lookup_student("", phone="3145550200"))
    assert hit["status"] == "found" and hit["identified_by"] == "phone"
    redacted = main._redact_lookup(hit)
    assert "phone" not in redacted["verify_with"].lower()      # never the digits just recited
    assert "Umrath" not in str(redacted) and "412" not in str(redacted)
