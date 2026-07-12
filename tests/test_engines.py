"""Offline unit tests for engines.py — pricing, parsing, upsell, dispatch. No network, no secrets."""
import engines

BOOK = {
    "utrucking box": 22.0, "mini fridge": 23.0, "plastic container": 18.0,
    "mattress": 33.0, "desk": 39.0, "bike": 39.0, "rolling cart": 23.0,
    "luggage": 23.0, "ottoman": 20.0,
}


# ---------- pricing / parsing ----------
def test_quote_prices_known_items():
    q = engines.quote("5 boxes and a mini fridge", BOOK)
    items = {l["item"].lower(): l for l in q["line_items"]}
    assert items["utrucking box"]["qty"] == 5
    assert items["mini fridge"]["qty"] == 1
    assert q["total"] == 5 * 22 + 23


def test_number_words():
    q = engines.quote("twenty boxes", BOOK)
    assert q["line_items"][0]["qty"] == 20


def test_qty_clamped_to_200():
    q = engines.quote([("box", 999999)], BOOK)
    assert q["line_items"][0]["qty"] == 200
    assert q.get("capped") == 200


def test_zero_qty_becomes_one():
    q = engines.quote([("box", 0)], BOOK)
    assert q["line_items"][0]["qty"] == 1


def test_unmatched_item_is_surfaced_not_dropped():
    q = engines.quote([("box", 2), ("flibbertigibbet", 1)], BOOK)
    assert any(l["item"].lower() == "utrucking box" for l in q["line_items"])
    assert "flibbertigibbet" in [u.lower() for u in q["unmatched"]]


def test_alias_resolves():
    q = engines.quote("a fridge", BOOK)
    assert q["line_items"][0]["item"].lower() == "mini fridge"


def test_never_silently_drops_an_item():
    q = engines.quote("a couch, a dresser, a bike, a mini fridge and 12 boxes", BOOK)
    priced = {l["item"].lower() for l in q["line_items"]}
    assert "bike" in priced and "mini fridge" in priced and "utrucking box" in priced


# ---------- upsell ----------
def _svc(items):
    return {"Summer Storage Item List": "; ".join(
        "%s (Amount: %.2f USD, Quantity: %d)" % (n, a, q) for n, a, q in items)}


def test_upsell_pairs_learns_cooccurrence():
    rows = [_svc([("UTrucking Box", 22, 2), ("Mini Fridge", 23, 1)]) for _ in range(5)]
    rows += [_svc([("UTrucking Box", 22, 1), ("Plastic Container", 18, 1)]) for _ in range(3)]
    up = engines.upsell_pairs(rows)
    partners = dict(up["utrucking box"])
    assert partners.get("mini fridge") == 5
    assert partners.get("plastic container") == 3


# ---------- dispatch / sequencing ----------
def _disp(n, building, room, date="5/6/2026"):
    return {"Student": "S%d" % n, "Building": building, "Room": room,
            "ID": "#%d" % n, "Service": "Summer Storage", "Date": date}


def test_room_natural_sort():
    assert engines._room_key("2") < engines._room_key("10") < engines._room_key("12A")
    # mixed alpha/number rooms never raise
    for r in ["204", "B12", "12A", "", "3rd floor", "Suite 4-A"]:
        engines._room_key(r)


def test_dispatch_plan_sequences_and_preserves_stops():
    rows = [_disp(1, "Umrath", "204"), _disp(2, "Umrath", "12"),
            _disp(3, "Umrath", "2"), _disp(4, "Eliot", "5")]
    p = engines.dispatch_plan(rows, "2026-05-06")
    assert p["total_stops"] == 4
    umrath = next(r for r in p["route"] if r["building"] == "Umrath")
    seqs = [o["seq"] for o in umrath["orders"]]
    rooms = [o["room"] for o in umrath["orders"]]
    assert seqs == [1, 2, 3]
    assert rooms == ["2", "12", "204"]                       # natural order
    assert sum(len(r["orders"]) for r in p["route"]) == p["total_stops"]


def test_dispatch_plan_crew_split_balances_and_reports_capacity():
    rows = []
    for b, n in [("A", 8), ("B", 5), ("C", 3)]:
        rows += [_disp(i, b, str(i)) for i in range(n)]
    p = engines.dispatch_plan(rows, "2026-05-06")
    assert sum(c["stops"] for c in p["crew_plan"]) == p["total_stops"] == 16
    # no building assigned twice
    placed = [x for c in p["crew_plan"] for x in c["buildings"]]
    assert len(placed) == len(set(placed)) == 3
    assert p["capacity"] == p["crews_available"] * p["jobs_per_crew"]


def test_empty_day_is_safe():
    p = engines.dispatch_plan([_disp(1, "A", "1")], "2026-12-25")
    assert p["total_stops"] == 0


# ---------- compound-noun guards (a modifier that's itself an alias must not split the line) ----------
CBOOK = dict(engines.EXTRA_PRICES)
CBOOK.update({"utrucking box": 22.0, "lamp": 18.0, "bookshelf": 33.0, "beanbag chair": 27.0,
              "fan": 15.0, "mirror": 18.0, "table": 33.0, "swivel/arm chair": 27.0,
              "hamper/laundry basket": 18.0, "camp duffel": 33.0, "plastic container": 18.0,
              "nightstand": 27.0})


def test_compound_nouns_resolve_to_one_item():
    for phrase, item in [("golf shoes", "Utrucking Box"), ("ski boots", "Utrucking Box"),
                         ("table lamp", "Lamp"), ("desk fan", "Fan"), ("book shelf", "Bookshelf"),
                         ("bean bag chair", "Beanbag Chair"), ("bike helmet", "Sports Equipment"),
                         ("clothes hamper", "Hamper/Laundry Basket"), ("floor mirror", "Mirror")]:
        q = engines.quote("1 " + phrase, CBOOK)
        assert len(q["line_items"]) == 1, (phrase, q["line_items"])
        assert q["line_items"][0]["item"] == item, (phrase, q["line_items"][0]["item"])
        assert q["line_items"][0]["qty"] == 1


def test_two_unaliased_adjacent_knowns_stay_separate():
    q = engines.quote("3 dresser mirror", {"dresser": 39.0, "mirror": 18.0})
    assert {l["item"]: l["qty"] for l in q["line_items"]} == {"Dresser": 3, "Mirror": 1}


def test_new_catalog_items_priced_in_tiers():
    for name, key in [("pillow", "Pillow"), ("comforter", "Bedding"), ("toaster", "Small Appliance"),
                      ("dumbbells", "Dumbbells"), ("treadmill", "Treadmill"), ("bed frame", "Bed Frame")]:
        q = engines.quote("1 " + name, CBOOK)
        assert q["line_items"] and q["line_items"][0]["item"] == key, (name, q.get("line_items"))


def test_no_orphan_alias_targets():
    # Structural guard: every alias must point to a real catalog item — one that is either seeded in
    # EXTRA_PRICES or declared canonical by a self-map (alias[x] == x). Catches a typo'd target offline
    # (e.g. adding "x":"beanbg chair") before it silently fails to price in production.
    known = set(engines.EXTRA_PRICES) | {v for k, v in engines.ALIASES.items() if k == v}
    orphans = sorted({t for t in engines.ALIASES.values() if t not in known})
    assert orphans == [], orphans


# ---------- a quantity glued to an item must still bind (never silently drop it) ----------
def test_qty_glued_to_item_splits():
    b = {"utrucking box": 22.0, "mattress": 33.0}
    assert {l["item"]: l["qty"] for l in engines.quote("3bed", b)["line_items"]} == {"Mattress": 3}
    assert {l["item"]: l["qty"] for l in engines.quote("4box", b)["line_items"]} == {"Utrucking Box": 4}
    assert {l["item"]: l["qty"] for l in engines.quote("2boxes", b)["line_items"]} == {"Utrucking Box": 2}
    # letter->digit is preserved so model names / the x3 qty form survive
    q = engines.quote("box x3", b)
    assert q["line_items"][0]["qty"] == 3


def test_reported_glued_input_prices_everything():
    b = {"utrucking box": 22.0, "mattress": 33.0, "table": 33.0, "keyboard": 23.0}
    q = engines.quote("keyboard, table, 3bed, 4u trucking box", b)
    got = {l["item"]: l["qty"] for l in q["line_items"]}
    assert got == {"Keyboard": 1, "Table": 1, "Mattress": 3, "Utrucking Box": 4}, got


# ---------- staff truck-space estimate (Sprinter vs 26-ft U-Haul, real cargo capacity) ----------
def test_space_estimate_reports_both_trucks_with_real_capacity():
    s = engines.space_estimate([{"item": "Utrucking Box", "qty": 100, "unit_price": 22.0}])  # 300 cu ft
    assert s["cubic_ft"] == 300.0 and s["box_equiv"] == 100
    assert set(s["trucks"]) == {"sprinter", "uhaul26"}
    assert s["trucks"]["uhaul26"]["cuft"] == 1682.0        # U-Haul 26' spec
    assert s["trucks"]["sprinter"]["cuft"] == 488.0        # Sprinter 170" high-roof
    # 300/1682 ≈ 18%, 300/488 ≈ 61% — the same load fills more of the smaller van
    assert s["trucks"]["uhaul26"]["pct"] == 18 and s["trucks"]["sprinter"]["pct"] == 61
    assert s["trucks"]["sprinter"]["pct"] > s["trucks"]["uhaul26"]["pct"]
    assert s["default"] == "sprinter"


def test_space_estimate_empty_is_zero_not_crash():
    s = engines.space_estimate([])
    assert s["cubic_ft"] == 0 and s["box_equiv"] == 0
    assert s["trucks"]["uhaul26"]["pct"] == 0 and s["trucks"]["sprinter"]["loads"] == 0.0


def test_space_estimate_overflows_smaller_truck_first():
    s = engines.space_estimate([{"item": "Utrucking Box", "qty": 200, "unit_price": 22.0}])  # 600 cu ft
    assert s["trucks"]["sprinter"]["loads"] > 1.0          # needs more than one Sprinter
    assert s["trucks"]["uhaul26"]["loads"] < 1.0           # still one U-Haul


# ---------- round 17 review regressions: quantity + spell-fix hardening ----------
PB = dict(engines.EXTRA_PRICES)
PB.update({"utrucking box": 22.0, "mattress": 33.0, "lamp": 15.0, "desk": 39.0,
           "swivel/arm chair": 33.0})


def test_article_plus_count_not_undercounted():
    # "a couple / a few X" must keep the count, not collapse to 1 (silent under-quote)
    assert engines.parse_freetext_ex("a couple of boxes", PB) == [("utrucking box", 2)]
    assert engines.parse_freetext_ex("a few lamps", PB) == [("lamp", 3)]
    both = dict(engines.parse_freetext_ex("a couple of boxes and a few lamps", PB))
    assert both.get("utrucking box") == 2 and both.get("lamp") == 3
    assert engines.parse_freetext_ex("a box", PB) == [("utrucking box", 1)]   # bare article unchanged


def test_nx_prefix_count_not_applied_to_the_next_item():
    # "3x box lamp" is 3 boxes + 1 lamp, not 3 of each (phantom double-count / overcharge)
    got = dict(engines.parse_freetext_ex("3x box lamp", PB))
    assert got.get("utrucking box") == 3 and got.get("lamp") == 1
    got2 = dict(engines.parse_freetext_ex("2x lamp desk", PB))
    assert got2.get("lamp") == 2 and got2.get("desk") == 1
    assert engines.parse_freetext_ex("6x box", PB) == [("utrucking box", 6)]   # plain Nx still fine


def test_real_word_not_snapped_to_confident_lookalike():
    # "tablet" must NOT become a confident exact "Table" — it falls through instead of a silent misprice
    key, kind = engines.resolve_item_ex("tablet", PB)
    assert not (key == "table" and kind in ("exact", "alias"))
    q = engines.quote("2 tablets", PB)
    assert not [l for l in q["line_items"] if l["item"] == "Table" and l.get("confidence") == "exact"]
    # genuine typos still resolve confidently
    assert engines.resolve_item_ex("matress", PB) == ("mattress", "exact")
