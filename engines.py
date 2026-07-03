"""
UTrucking Wave A/B/C business engines — pure logic, no I/O.
Callers (main.py / tests) pass already-parsed sheet rows (list[dict]).

  A) quote        — price an itemized list; learn the price book from history
  B) availability — per-day booking load vs capacity + alternative dates
  C) billing_audit / should_block — flag $0 / missing-invoice / missing-order leakage
"""
import re, difflib, datetime
from collections import Counter, defaultdict

# ============================ A. QUOTE ENGINE ============================
_ITEM_RE = re.compile(r'([A-Za-z][A-Za-z0-9 \-\/&]*?)\s*\(Amount:\s*([\d.]+)\s*USD,\s*Quantity:\s*(\d+)')
_TOTAL_RE = re.compile(r'Total:\s*\$?\s*([\d,]+\.\d{2})')

def _canon(name): return " ".join((name or "").strip().lower().split())

# Common student items not always present in invoice history — priced to match the
# existing tiers ($15 small · $18 med · $23 electronics · $27–33 furniture · $39 large).
# Learned history ALWAYS wins on overlap, so real recorded prices are never overridden.
EXTRA_PRICES = {
    "monitor": 18.0, "printer": 18.0, "computer": 23.0, "fan": 15.0, "speaker": 18.0,
    "nightstand": 27.0, "table": 33.0, "filing cabinet": 33.0, "cabinet": 33.0,
    "futon": 39.0, "wardrobe": 39.0, "crate": 18.0, "toolbox": 18.0,
}

def build_price_book(service_rows, item_col="Summer Storage Item List"):
    """Learn {item_name -> unit_price} as the most common price seen per item, seeded with
    EXTRA_PRICES for common items missing from history (recorded history wins on overlap)."""
    prices = defaultdict(Counter)
    for r in service_rows:
        for name, amt, qty in _ITEM_RE.findall(r.get(item_col, "") or ""):
            prices[_canon(name)][float(amt)] += 1
    learned = {name: ctr.most_common(1)[0][0] for name, ctr in prices.items()}
    book = dict(EXTRA_PRICES)
    book.update(learned)          # history overrides the seeds
    return book

# spoken / written aliases -> canonical item name (used only if the canonical exists in the learned book)
ALIASES = {
    "box":"utrucking box","boxes":"utrucking box","utrucking box":"utrucking box",
    "fridge":"mini fridge","minifridge":"mini fridge","mini fridge":"mini fridge",
    "duffel":"camp duffel","duffel bag":"camp duffel","camp duffel":"camp duffel",
    "container":"plastic container","bin":"plastic container","tub":"plastic container",
    "plastic container":"plastic container",
    "suitcase":"luggage","luggage":"luggage",
    "cart":"rolling cart","rolling cart":"rolling cart",
    "shelf":"bookshelf","bookshelf":"bookshelf","bookcase":"bookshelf","dresser":"dresser","drawers":"dresser",
    "hamper":"hamper/laundry basket","laundry basket":"hamper/laundry basket","laundry hamper":"hamper/laundry basket",
    "mattress":"mattress","ottoman":"ottoman","footstool":"ottoman","foot stool":"ottoman",
    "shoe rack":"shoe rack","headboard":"headboard",
    # common synonyms that map onto items already in the learned price book
    "couch":"couch","sofa":"couch","loveseat":"couch",
    "desk":"desk","bike":"bike","bicycle":"bike",
    "tv":"tv","television":"tv","flatscreen":"tv","flat screen":"tv",
    "chair":"swivel/arm chair","armchair":"swivel/arm chair","office chair":"swivel/arm chair","desk chair":"swivel/arm chair",
    "beanbag":"beanbag chair","bean bag":"beanbag chair","beanbag chair":"beanbag chair",
    "microwave":"microwave","lamp":"lamp","rug":"rug","carpet":"rug","mirror":"mirror",
    "vacuum":"vacuum cleaner","vacuum cleaner":"vacuum cleaner",
    "guitar":"guitar","keyboard":"keyboard","skateboard":"skateboard",
    "trunk":"trunk","footlocker":"trunk","duffle":"camp duffel","duffle bag":"camp duffel",
    "poster":"framed art","painting":"framed art","art":"framed art","framed art":"framed art",
    "tote":"plastic container",
    # electronics + extra furniture (seeded in EXTRA_PRICES)
    "pc":"computer","desktop":"computer","computer tower":"computer","cpu":"computer","laptop":"computer",
    "screen":"monitor","display":"monitor",
    "speakers":"speaker","subwoofer":"speaker","amp":"speaker","amplifier":"speaker",
    "box fan":"fan","standing fan":"fan",
    "night stand":"nightstand","bedside table":"nightstand",
    "coffee table":"table","end table":"table","side table":"table","dining table":"table",
    "file cabinet":"filing cabinet","closet":"wardrobe","armoire":"wardrobe","tool box":"toolbox",
    # generic names an AI vision model tends to return for a photo -> map onto the catalog
    "cardboard box":"utrucking box","moving box":"utrucking box","packing box":"utrucking box",
    "storage box":"utrucking box","shipping box":"utrucking box","carton":"utrucking box","u-haul box":"utrucking box",
    "storage bin":"plastic container","tote bin":"plastic container","commercial bin":"plastic container",
    "storage container":"plastic container","dorm fridge":"mini fridge","refrigerator":"mini fridge",
}

def resolve_item(name, price_book):
    key = _canon(name)
    sing = key[:-1] if key.endswith("s") else key
    for k in (key, sing):
        if k in price_book: return k
        if k in ALIASES and ALIASES[k] in price_book: return ALIASES[k]
    m = difflib.get_close_matches(key, list(price_book), n=1, cutoff=0.82)
    return m[0] if m else None

MAX_QTY = 200  # per-line sanity cap: beyond a dorm-floor's worth, route to a human bulk quote

def price_items(items, price_book):
    """items: list of (name, qty). Resolves names via aliases + fuzzy match.
    Quantities are clamped to [1, MAX_QTY] so garbage/troll input can't produce a $22M estimate;
    a `capped` flag is set when any line was clamped so the caller can add a bulk-quote note."""
    lines, total, unmatched, capped = [], 0.0, [], False
    for name, qty in items:
        try: qty = int(qty)
        except Exception: qty = 1
        if qty < 1: qty = 1
        if qty > MAX_QTY: qty = MAX_QTY; capped = True
        key = resolve_item(name, price_book)
        if key is None: unmatched.append(name); continue
        price = price_book[key]; amt = price * qty; total += amt
        lines.append({"item": key.title(), "qty": qty, "unit_price": round(price, 2), "amount": round(amt, 2)})
    res = {"line_items": lines, "total": round(total, 2), "unmatched": unmatched,
           "summary": "Estimated total ${:.2f} for {} item(s).".format(total, sum(l["qty"] for l in lines))}
    if capped:
        res["capped"] = MAX_QTY
    return res

_ONES  = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9}
_TEENS = {"ten":10,"eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,
          "sixteen":16,"seventeen":17,"eighteen":18,"nineteen":19}
_TENS  = {"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,"seventy":70,"eighty":80,"ninety":90}
_WORDS = {"a":1,"an":1,"couple":2,"few":3,"several":3,"dozen":12,"a dozen":12,
          "half dozen":6,"half a dozen":6, **_ONES, **_TEENS, **_TENS}

def _word_to_int(q):
    """'twenty' -> 20, 'twenty-five' -> 25, 'a dozen' -> 12, '7' -> 7. Falls back to 1."""
    q = (q or "").strip().lower()
    if q.isdigit(): return int(q)
    if q in _WORDS: return _WORDS[q]
    parts = re.split(r'[\s-]+', q)              # compound e.g. "twenty five"
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _ONES:
        return _TENS[parts[0]] + _ONES[parts[1]]
    return 1

# quantity phrase: digits, tens(+ones) compounds, teens, ones, dozen forms, or a/an/couple/few
_QTY = (r'\d+'
        r'|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?'
        r'|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen'
        r'|one|two|three|four|five|six|seven|eight|nine'
        r'|half a dozen|half dozen|a dozen|dozen'
        r'|an|a|couple|few|several')

_STOP = set(("and or with plus the a an of for from i im we you my me our your his her their some couple few "
             "several dozen half please thanks thank about also just around approximately roughly maybe more "
             "things thing stuff item items lot lots bunch other others").split())

def parse_freetext_ex(text, price_book):
    """Returns (items, unmatched_words). Matched '<qty> <item>' spans are consumed; any leftover
    '<qty> <noun>' is reported as unmatched so unknown items are surfaced, never silently hidden."""
    text = " " + (text or "").lower() + " "
    phrases = sorted(set(list(ALIASES.keys()) + list(price_book.keys())), key=len, reverse=True)
    agg = defaultdict(int)
    for ph in phrases:
        key = resolve_item(ph, price_book)
        if not key: continue
        pat = re.compile(r'(?:(' + _QTY + r')\s+)?' + re.escape(ph) + r's?\b')
        def repl(m, _key=key):
            agg[_key] += _word_to_int(m.group(1)) if m.group(1) else 1
            return "  "
        text = pat.sub(repl, text)
    leftovers = []
    for m in re.finditer(r'(?:' + _QTY + r')\s+([a-z]{3,}?)s?\b', text):
        w = m.group(1)
        if w not in _STOP and resolve_item(w, price_book) is None:
            leftovers.append(w)
    return [(k, q) for k, q in agg.items()], leftovers

def parse_freetext(text, price_book):
    """Back-compat wrapper — returns the item list only."""
    return parse_freetext_ex(text, price_book)[0]

def quote(items_or_text, price_book):
    if isinstance(items_or_text, list):
        return price_items(items_or_text, price_book)
    items, leftovers = parse_freetext_ex(items_or_text, price_book)
    res = price_items(items, price_book)
    if leftovers:
        res["unmatched"] = sorted(set(leftovers))
    return res

def reprice_book(text, price_book):
    """Re-price a historical item list using the LEARNED book (estimate — ignores size variants)."""
    items = [(name, int(qty)) for name, amt, qty in _ITEM_RE.findall(text or "")]
    return price_items(items, price_book)["total"]

def reprice_own(text):
    """Sum a historical item list using ITS OWN amounts — validates parsing vs the recorded Total."""
    return round(sum(float(amt) * int(qty) for _, amt, qty in _ITEM_RE.findall(text or "")), 2)

# ========================= B. AVAILABILITY ENGINE ========================
def _parse_date(s):
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try: return datetime.datetime.strptime(s, fmt).date()
        except Exception: pass
    return None

def day_load(dispatch_rows, date_col="Date"):
    load = Counter()
    for r in dispatch_rows:
        d = _parse_date(r.get(date_col, ""))
        if d: load[d] += 1
    return load

def _slot(d, used, cap):
    status = "open" if used < cap * 0.8 else ("tight" if used < cap else "full")
    return {"date": str(d), "booked": used, "capacity": cap,
            "open_slots": max(cap - used, 0), "status": status}

# Crews available per day (from ops: peak season ~6, high ~8, tapering to ~3 then ~2 late in the month).
# Edit these ranges / JOBS_PER_CREW to match reality — the engine converts crews -> daily capacity.
CREW_SCHEDULE = [
    ("2026-05-01", "2026-05-13", 6),   # peak move-out week(s)
    ("2026-05-14", "2026-05-20", 3),   # wind-down
    ("2026-05-21", "2026-06-30", 2),   # late month / off-peak
]
JOBS_PER_CREW = 15   # pickups one crew can complete in a day (tune to your ops)

def crews_for(d):
    for s, e, c in CREW_SCHEDULE:
        if _parse_date(s) <= d <= _parse_date(e):
            return c
    return 2

def capacity_for(d):
    return crews_for(d) * JOBS_PER_CREW

def availability(dispatch_rows, requested_date, capacity_per_day=None, window=4):
    """Requested day's load + least-loaded non-full alternatives. Capacity varies by
    date via the crew schedule unless an explicit capacity_per_day is passed."""
    load = day_load(dispatch_rows)
    req = requested_date if isinstance(requested_date, datetime.date) else _parse_date(requested_date)
    if req is None:
        return {"requested": None, "alternatives": [],
                "suggestion": "I couldn't read that date — what day were you thinking? (e.g. May 12th)"}
    def capof(d): return capacity_per_day if capacity_per_day else capacity_for(d)
    out = {"requested": _slot(req, load.get(req, 0), capof(req)) if req else None, "alternatives": []}
    if req:
        cands = [req + datetime.timedelta(days=k) for k in range(-window, window + 1) if k != 0]
        alts = [_slot(d, load.get(d, 0), capof(d)) for d in cands]
        alts = [s for s in sorted(alts, key=lambda s: s["booked"]) if s["status"] != "full"]
        out["alternatives"] = alts[:3]
        if out["requested"]["status"] == "full":
            best = out["alternatives"][0] if out["alternatives"] else None
            out["suggestion"] = ("That day is full. Nearest opening: {} ({} slots).".format(best["date"], best["open_slots"])
                                 if best else "That day is full and nearby days are booked — offer to waitlist.")
        elif out["requested"]["status"] == "tight":
            out["suggestion"] = "That day is nearly full — book now or pick a nearby day."
        else:
            out["suggestion"] = "That day is available."
    return out

def season_bounds(dispatch_rows):
    """Earliest and latest booked pickup dates in the data."""
    load = day_load(dispatch_rows)
    return (min(load), max(load)) if load else (None, None)

def peak_date(dispatch_rows):
    """The single busiest booked date — anchors 'what days are open' near the real season
    instead of a stray outlier date."""
    load = day_load(dispatch_rows)
    return max(load, key=load.get) if load else None

def open_days(dispatch_rows, start, end, limit=6, capacity_per_day=None):
    """Up to `limit` days in [start, end] that still have room (>20% free)."""
    load = day_load(dispatch_rows)
    out, d = [], start
    while d <= end and len(out) < limit:
        cap = capacity_per_day or capacity_for(d)
        used = load.get(d, 0)
        if used < cap * 0.8:
            out.append(_slot(d, used, cap))
        d += datetime.timedelta(days=1)
    return out

def dispatch_plan(dispatch_rows, date):
    """B-ops: cluster a day's pickups by building and suggest crew split (route optimizer core)."""
    d = date if isinstance(date, datetime.date) else _parse_date(date)
    stops = defaultdict(list)
    for r in dispatch_rows:
        if _parse_date(r.get("Date", "")) == d:
            b = (r.get("Building", "") or "").strip() or "Unknown"
            stops[b].append({"student": r.get("Student", ""), "room": r.get("Room", ""),
                             "order_id": r.get("ID", ""), "service": r.get("Service", "")})
    clusters = sorted(stops.items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for v in stops.values())
    crews = crews_for(d) if d else 2
    return {"date": str(d) if d else None, "total_stops": total, "buildings": len(stops),
            "crews_available": crews, "avg_stops_per_crew": round(total / max(crews, 1), 1),
            "route": [{"building": b, "stops": len(v), "orders": v} for b, v in clusters]}

# ========================== C. BILLING GUARD =============================
def _order_total(row):
    m = _TOTAL_RE.search(row.get("Summer Storage Item List", "") or "")
    return float(m.group(1).replace(",", "")) if m else None

def order_flags(row):
    reasons = []
    st = (row.get("Service Type", "") or "").strip()
    total = _order_total(row)
    if st == "Summer Storage" and (total is None or total == 0):
        reasons.append("zero_or_missing_total")
    if not (row.get("Invoice ID", "") or "").strip():
        reasons.append("missing_invoice")
    if not (row.get("Order#:", "") or "").strip():
        reasons.append("missing_order_id")
    return reasons

def billing_audit(service_rows):
    flagged, summary = [], Counter()
    for r in service_rows:
        reasons = order_flags(r)
        if reasons:
            flagged.append({"student": r.get("Student Name", ""), "order": (r.get("Order#:", "") or "").strip(),
                            "invoice": (r.get("Invoice ID", "") or "").strip(),
                            "service": (r.get("Service Type", "") or "").strip(),
                            "total": _order_total(r), "reasons": reasons})
            for x in reasons: summary[x] += 1
    return {"count": len(flagged), "summary": dict(summary), "flagged": flagged}

def should_block(order_row):
    reasons = order_flags(order_row)
    return (len(reasons) > 0, reasons)
