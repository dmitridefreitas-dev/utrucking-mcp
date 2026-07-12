"""Adversarial edge cases for the newest features: caching, upsell, phone, date-range, ops, vision."""
import asyncio
import types
import engines
import main

BOOK = {"utrucking box": 22.0, "mini fridge": 23.0, "plastic container": 18.0, "rolling cart": 23.0}


# ---------------- sheet cache ----------------
class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _FakeClient:
    seq = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        item = _FakeClient.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_cache_hits_within_ttl_then_serves_stale_on_failure(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(main, "time", types.SimpleNamespace(time=lambda: clock["t"]))
    monkeypatch.setattr(main, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
    main._SHEET_CACHE.clear()
    url = "http://sheet"

    _FakeClient.seq = [_Resp(200, "Student,ID\nAlice,001\n")]
    r1 = asyncio.run(main.fetch_csv_rows(url))
    assert r1 == [{"Student": "Alice", "ID": "001"}]

    # within TTL: served from cache (seq is empty; a network call would IndexError)
    r2 = asyncio.run(main.fetch_csv_rows(url))
    assert r2 == r1

    # past TTL + network throws -> serve last good copy
    clock["t"] += main.SHEET_TTL + 5
    _FakeClient.seq = [RuntimeError("sheets down")]
    r3 = asyncio.run(main.fetch_csv_rows(url))
    assert r3 == r1

    # force=True bypasses the cache and picks up new data
    clock["t"] += 1
    _FakeClient.seq = [_Resp(200, "Student,ID\nBob,002\n")]
    r4 = asyncio.run(main.fetch_csv_rows(url, force=True))
    assert r4 == [{"Student": "Bob", "ID": "002"}]


def test_bad_200_body_does_not_poison_cache(monkeypatch):
    """A 200 with an empty body or an HTML error/sign-in page must NOT evict the last-good copy."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(main, "time", types.SimpleNamespace(time=lambda: clock["t"]))
    monkeypatch.setattr(main, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
    main._SHEET_CACHE.clear()
    url = "http://sheet"

    _FakeClient.seq = [_Resp(200, "Student,ID\nAlice,001\n")]
    good = asyncio.run(main.fetch_csv_rows(url))
    assert good == [{"Student": "Alice", "ID": "001"}]

    # transient empty-body 200 (force to bypass TTL) -> keep serving last good, don't cache []
    clock["t"] += main.SHEET_TTL + 5
    _FakeClient.seq = [_Resp(200, "")]
    assert asyncio.run(main.fetch_csv_rows(url, force=True)) == good

    # HTML sign-in / error page (follow_redirects can land here) -> still last good, not garbage rows
    clock["t"] += main.SHEET_TTL + 5
    _FakeClient.seq = [_Resp(200, "<!DOCTYPE html><html><body>Sign in to continue</body></html>")]
    assert asyncio.run(main.fetch_csv_rows(url, force=True)) == good

    # a real refresh still updates
    clock["t"] += main.SHEET_TTL + 5
    _FakeClient.seq = [_Resp(200, "Student,ID\nBob,002\n")]
    assert asyncio.run(main.fetch_csv_rows(url, force=True)) == [{"Student": "Bob", "ID": "002"}]


def test_cache_empty_when_no_prior_and_fetch_fails(monkeypatch):
    monkeypatch.setattr(main, "time", types.SimpleNamespace(time=lambda: 5.0))
    monkeypatch.setattr(main, "httpx", types.SimpleNamespace(AsyncClient=_FakeClient))
    main._SHEET_CACHE.clear()
    _FakeClient.seq = [_Resp(500, "")]
    assert asyncio.run(main.fetch_csv_rows("http://never-cached")) == []


# ---------------- upsell edge cases ----------------
def test_upsell_noop_when_nothing_priced():
    q = engines.quote([("zzzznotathing", 1)], BOOK)
    main._attach_upsell(q, {"anything": [("mini fridge", 9)]}, BOOK)
    assert "upsell" not in q


def test_upsell_noop_when_all_partners_already_in_cart():
    up = {"mini fridge": [("utrucking box", 9)], "utrucking box": [("mini fridge", 9)]}
    q = engines.quote("a mini fridge and a box", BOOK)
    main._attach_upsell(q, up, BOOK)
    assert "upsell" not in q                                   # only partners are already in the cart


def test_upsell_skips_partner_missing_from_book():
    up = {"mini fridge": [("unicorn saddle", 99)]}            # partner has no price
    q = engines.quote("a mini fridge", BOOK)
    main._attach_upsell(q, up, BOOK)
    assert "upsell" not in q


def test_upsell_single_candidate_phrasing():
    up = {"mini fridge": [("rolling cart", 9)]}
    q = engines.quote("a mini fridge", BOOK)
    main._attach_upsell(q, up, BOOK)
    assert q["upsell"]["line"].count("Rolling Cart") == 1
    assert " or " not in q["upsell"]["line"]


# ---------------- phone edge cases ----------------
def test_phone_edge_inputs():
    D = [{"Student": "A", "Phone": "5402078205"}]
    assert main._match_by_phone("", D) == []
    assert main._match_by_phone("no digits here", D) == []
    assert main._match_by_phone("54", D) == []                # too short
    assert main._match_by_phone("+1 (540) 207-8205", D) == ["A"]          # country code stripped by last-10
    assert main._match_by_phone("5402078205 ext 9", D) == []             # an extension shifts the last-10, no match
    assert main._phone_digits("") == ""


# ---------------- date range edge cases ----------------
def test_rows_in_range_inverted_and_invalid(monkeypatch):
    import datetime
    rows = [{"Date": "5/6/2026"}, {"Date": "5/10/2026"}, {"Date": ""}, {"Date": "garbage"}]
    lo, hi = datetime.date(2026, 5, 5), datetime.date(2026, 5, 7)
    got = main._rows_in_range(rows, "Date", lo, hi)
    assert len(got) == 1                                       # only 5/6 in-window; blanks/garbage dropped
    # inverted range -> nothing
    assert main._rows_in_range(rows, "Date", hi, lo) == []
    # parse helpers
    assert main._parse_any_date("2026-05-06") == datetime.date(2026, 5, 6)
    assert main._parse_any_date("5/6/2026") == datetime.date(2026, 5, 6)
    assert main._parse_any_date("not a date") is None
    assert main._parse_any_date("") is None


# ---------------- ops sequencing robustness ----------------
def test_dispatch_plan_handles_weird_rooms_without_crashing():
    rows = [{"Student": "S%d" % i, "Building": "Hall", "Room": r, "ID": "#%d" % i,
             "Service": "Summer Storage", "Date": "5/6/2026"}
            for i, r in enumerate(["", "12A", "3", "Suite 4-A", "basement", "10", "2-B", None])]
    p = engines.dispatch_plan(rows, "2026-05-06")
    assert p["total_stops"] == len(rows)
    seqs = [o["seq"] for r in p["route"] for o in r["orders"]]
    assert sorted(seqs) == list(range(1, len(rows) + 1))       # every stop numbered exactly once per building


# ---------------- vision helper edge cases ----------------
def test_load_image_arg_requires_an_image():
    b64, mime, err = asyncio.run(main._load_image_arg({}))
    assert err and err["status"] == "error"


def test_pretty_items_handles_quantity_one_and_many():
    s = "Box (Amount: 10.00 USD, Quantity: 1); Fridge (Amount: 20.00 USD, Quantity: 3)"
    assert main._pretty_items(s) == "Box, Fridge x3"
