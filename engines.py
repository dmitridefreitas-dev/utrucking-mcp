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

def build_price_book(service_rows, item_col="Summer Storage Item List"):
    """Learn {item_name -> unit_price} as the most common price seen per item."""
    prices = defaultdict(Counter)
    for r in service_rows:
        for name, amt, qty in _ITEM_RE.findall(r.get(item_col, "") or ""):
            prices[_canon(name)][float(amt)] += 1
    return {name: ctr.most_common(1)[0][0] for name, ctr in prices.items()}

# spoken / written aliases -> canonical item name (used only if the canonical exists in the learned book)
ALIASES = {
    "box":"utrucking box","boxes":"utrucking box","utrucking box":"utrucking box",
    "fridge":"mini fridge","minifridge":"mini fridge","mini fridge":"mini fridge",
    "duffel":"camp duffel","duffel bag":"camp duffel","camp duffel":"camp duffel",
    "container":"plastic container","bin":"plastic container","tub":"plastic container",
    "plastic container":"plastic container",
    "suitcase":"luggage","luggage":"luggage",
    "cart":"rolling cart","rolling cart":"rolling cart",
    "shelf":"bookshelf","bookshelf":"bookshelf","dresser":"dresser",
    "hamper":"hamper/laundry basket","laundry basket":"hamper/laundry basket",
    "mattress":"mattress","ottoman":"ottoman","shoe rack":"shoe rack","headboard":"headboard",
}

def resolve_item(name, price_book):
    key = _canon(name)
    sing = key[:-1] if key.endswith("s") else key
    for k in (key, sing):
        if k in price_book: return k
        if k in ALIASES and ALIASES[k] in price_book: return ALIASES[k]
    m = difflib.get_close_matches(key, list(price_book), n=1, cutoff=0.82)
    return m[0] if m else None

def price_items(items, price_book):
    """items: list of (name, qty). Resolves names via aliases + fuzzy match."""
    lines, total, unmatched = [], 0.0, []
    for name, qty in items:
        try: qty = int(qty)
        except Exception: qty = 1
        key = resolve_item(name, price_book)
        if key is None: unmatched.append(name); continue
        price = price_book[key]; amt = price * qty; total += amt
        lines.append({"item": key.title(), "qty": qty, "unit_price": round(price, 2), "amount": round(amt, 2)})
    return {"line_items": lines, "total": round(total, 2), "unmatched": unmatched,
            "summary": "Estimated total ${:.2f} for {} item(s).".format(total, sum(l["qty"] for l in lines))}

_NUM = {"a":1,"an":1,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
        "couple":2,"few":3}
def parse_freetext(text, price_book):
    """Best-effort '<qty> <item>' extraction for voice/chat. Longest phrases first, spans consumed."""
    text = " " + (text or "").lower() + " "
    phrases = sorted(set(list(ALIASES.keys()) + list(price_book.keys())), key=len, reverse=True)
    agg = defaultdict(int)
    for ph in phrases:
        key = resolve_item(ph, price_book)
        if not key: continue
        pat = re.compile(r'(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few)\s+'
                         + re.escape(ph) + r's?\b')
        def repl(m, _key=key):
            q = m.group(1); agg[_key] += int(q) if q.isdigit() else _NUM.get(q, 1); return "  "
        text = pat.sub(repl, text)
    return [(k, q) for k, q in agg.items()]

def quote(items_or_text, price_book):
    items = items_or_text if isinstance(items_or_text, list) else parse_freetext(items_or_text, price_book)
    return price_items(items, price_book)

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

def availability(dispatch_rows, requested_date, capacity_per_day=100, window=4):
    """Return the requested day's load + the least-loaded, non-full nearby alternatives."""
    load = day_load(dispatch_rows)
    req = requested_date if isinstance(requested_date, datetime.date) else _parse_date(requested_date)
    out = {"requested": _slot(req, load.get(req, 0), capacity_per_day) if req else None, "alternatives": []}
    if req:
        cands = [req + datetime.timedelta(days=k) for k in range(-window, window + 1) if k != 0]
        alts = [_slot(d, load.get(d, 0), capacity_per_day) for d in cands]
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
