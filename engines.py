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
    # bedding / soft goods (small tier)
    "pillow": 15.0, "bedding": 15.0,
    # counter-top small appliances (routes toaster/blender/kettle/air-fryer to the nearest size, not a spelling lookalike)
    "small appliance": 15.0,
    # sports & fitness — priced by storage footprint so the match is close on cost
    "sports equipment": 15.0, "golf clubs": 18.0, "skis": 18.0, "snowboard": 18.0,
    "surfboard": 23.0, "dumbbells": 18.0, "weight bench": 33.0,
    "exercise bike": 39.0, "treadmill": 39.0,
    # larger dorm / household items
    "bed frame": 39.0, "ironing board": 15.0, "step stool": 15.0,
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


def upsell_pairs(service_rows, item_col="Summer Storage Item List"):
    """Per-item co-occurrence learned from real baskets: item_key -> [(partner_key, orders_together), ...]
    sorted most-common first. Powers the 'people who store X also add Y' suggestion on a quote."""
    co = defaultdict(Counter)
    for r in service_rows:
        names = sorted({_canon(n) for n, a, q in _ITEM_RE.findall(r.get(item_col, "") or "")})
        for i in range(len(names)):
            for j in range(len(names)):
                if i != j:
                    co[names[i]][names[j]] += 1
    return {k: v.most_common() for k, v in co.items()}

# spoken / written aliases -> canonical item name (used only if the canonical exists in the learned book)
ALIASES = {
    "box":"utrucking box","boxes":"utrucking box","utrucking box":"utrucking box",
    "trucking box":"utrucking box","u trucking box":"utrucking box","utruck box":"utrucking box",
    "fridge":"mini fridge","minifridge":"mini fridge","mini fridge":"mini fridge",
    "duffel":"camp duffel","duffel bag":"camp duffel","camp duffel":"camp duffel",
    "container":"plastic container","bin":"plastic container","tub":"plastic container",
    "plastic container":"plastic container",
    "suitcase":"luggage","luggage":"luggage",
    "cart":"rolling cart","rolling cart":"rolling cart",
    "shelf":"bookshelf","bookshelf":"bookshelf","bookcase":"bookshelf","dresser":"dresser","drawers":"dresser",
    "hamper":"hamper/laundry basket","laundry basket":"hamper/laundry basket","laundry hamper":"hamper/laundry basket",
    "hamper/laundry basket":"hamper/laundry basket","swivel/arm chair":"swivel/arm chair",   # self-maps: declare these history canonicals
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
    # common dorm items people say that map onto the closest catalog item
    "bed":"mattress","bunk bed":"mattress","sofa bed":"futon","daybed":"futon",
    "footboard":"headboard",
    "drawer":"dresser","chest of drawers":"dresser","recliner":"swivel/arm chair",
    "garment rack":"rolling cart","clothes rack":"rolling cart","clothing rack":"rolling cart",
    "tv stand":"table","console":"computer","xbox":"computer","playstation":"computer",
    "ps5":"computer","ps4":"computer","wii":"computer","game console":"computer",
    "gaming console":"computer","nintendo switch":"computer","gaming chair":"swivel/arm chair",
    "kitchen cart":"rolling cart","utility cart":"rolling cart",
    # bedding / soft goods
    "pillow":"pillow","pillows":"pillow","throw pillow":"pillow","body pillow":"pillow",
    "bedding":"bedding","comforter":"bedding","comforter set":"bedding","duvet":"bedding",
    "quilt":"bedding","blanket":"bedding","blankets":"bedding","throw blanket":"bedding",
    "sheets":"bedding","bed sheets":"bedding","linens":"bedding","bed linens":"bedding",
    "mattress topper":"bedding","mattress pad":"bedding","sleeping bag":"bedding",
    "books":"utrucking box","box of books":"utrucking box",
    # counter-top small appliances
    "small appliance":"small appliance","toaster":"small appliance","toaster oven":"small appliance",
    "blender":"small appliance","kettle":"small appliance","electric kettle":"small appliance",
    "coffee maker":"small appliance","coffee machine":"small appliance","keurig":"small appliance",
    "espresso machine":"small appliance","air fryer":"small appliance","rice cooker":"small appliance",
    "instant pot":"small appliance","crock pot":"small appliance","slow cooker":"small appliance",
    "hot plate":"small appliance","griddle":"small appliance","waffle maker":"small appliance",
    "iron":"small appliance","humidifier":"small appliance","air purifier":"small appliance",
    "dehumidifier":"small appliance","space heater":"small appliance","heater":"small appliance",
    # sports & fitness
    "sports equipment":"sports equipment","sporting goods":"sports equipment","sports gear":"sports equipment",
    "baseball bat":"sports equipment","bat":"sports equipment","tennis racket":"sports equipment",
    "tennis racquet":"sports equipment","racket":"sports equipment","racquet":"sports equipment",
    "hockey stick":"sports equipment","lacrosse stick":"sports equipment","field hockey stick":"sports equipment",
    "basketball":"sports equipment","soccer ball":"sports equipment","football":"sports equipment",
    "volleyball":"sports equipment","baseball":"sports equipment","softball":"sports equipment",
    "baseball glove":"sports equipment","baseball mitt":"sports equipment","boxing gloves":"sports equipment",
    "helmet":"sports equipment","cleats":"sports equipment","shin guards":"sports equipment",
    "yoga mat":"sports equipment","exercise mat":"sports equipment","jump rope":"sports equipment",
    "frisbee":"sports equipment","roller blades":"sports equipment","rollerblades":"sports equipment",
    "ice skates":"sports equipment","skates":"sports equipment","roller skates":"sports equipment",
    "badminton racket":"sports equipment","ping pong paddle":"sports equipment","scooter":"sports equipment",
    "golf clubs":"golf clubs","golf bag":"golf clubs","golf set":"golf clubs","clubs":"golf clubs","golf":"golf clubs",
    "skis":"skis","ski":"skis","ski poles":"skis","cross country skis":"skis",
    "snowboard":"snowboard","snow board":"snowboard",
    "surfboard":"surfboard","paddleboard":"surfboard","paddle board":"surfboard","longboard surfboard":"surfboard",
    "dumbbells":"dumbbells","dumbbell":"dumbbells","weights":"dumbbells","weight set":"dumbbells",
    "free weights":"dumbbells","hand weights":"dumbbells","kettlebell":"dumbbells","kettle bell":"dumbbells",
    "barbell":"dumbbells","weight plates":"dumbbells",
    "weight bench":"weight bench","workout bench":"weight bench","bench press":"weight bench","gym bench":"weight bench",
    "exercise bike":"exercise bike","stationary bike":"exercise bike","spin bike":"exercise bike",
    "peloton":"exercise bike","recumbent bike":"exercise bike",
    "treadmill":"treadmill","elliptical":"treadmill","rowing machine":"treadmill","rower":"treadmill",
    "home gym":"treadmill","punching bag":"treadmill","heavy bag":"treadmill","stair climber":"treadmill",
    # larger dorm / household extras
    "bed frame":"bed frame","bedframe":"bed frame","bed frames":"bed frame","metal bed frame":"bed frame",
    "wooden bed frame":"bed frame","bed rails":"bed frame","platform bed":"bed frame",
    "ironing board":"ironing board","step stool":"step stool","stepstool":"step stool",
    "step ladder":"step stool","ladder":"step stool","stool":"ottoman","bar stool":"swivel/arm chair",
    "barstool":"swivel/arm chair","drying rack":"rolling cart","milk crate":"crate","storage cube":"plastic container",
    # kitchen — packed into a box/bin for the summer
    "cooler":"plastic container","dishes":"utrucking box","dish set":"utrucking box",
    "pots and pans":"utrucking box","pots":"utrucking box","pans":"utrucking box",
    "cookware":"utrucking box","kitchenware":"utrucking box","dish rack":"plastic container",
    # clothing / soft goods — boxed
    "clothes":"utrucking box","clothing":"utrucking box","winter clothes":"utrucking box",
    "coats":"utrucking box","jackets":"utrucking box","shoes":"utrucking box","boots":"utrucking box",
    "wardrobe box":"utrucking box","garment bag":"camp duffel","laundry bag":"hamper/laundry basket",
    "towels":"bedding","curtains":"bedding",
    # decor / wall items
    "tapestry":"rug","whiteboard":"framed art","cork board":"framed art","corkboard":"framed art",
    "bulletin board":"framed art","picture frame":"framed art","picture frames":"framed art",
    "floor lamp":"lamp","desk lamp":"lamp",
    # storage / organization & folding furniture
    "shelving unit":"bookshelf","shelving":"bookshelf","shelf unit":"bookshelf",
    "closet organizer":"wardrobe","shoe organizer":"shoe rack","under bed storage":"plastic container",
    "under-bed storage":"plastic container","folding chair":"swivel/arm chair","folding table":"table",
    "card table":"table","storage bench":"ottoman","papasan chair":"swivel/arm chair",
    # small electricals / appliances a student boxes up
    "sewing machine":"small appliance","clothes steamer":"small appliance","steamer":"small appliance",
    "paper shredder":"small appliance","shredder":"small appliance","lockbox":"toolbox","file box":"filing cabinet",
    "router":"small appliance","modem":"small appliance","wifi router":"small appliance","power strip":"small appliance",
    # compound-noun guards: phrases whose MODIFIER is itself an item alias (golf, ski, book, bean bag...)
    # must resolve as ONE item, or the parser would emit the modifier AND the head noun as two lines.
    # Footwear -> boxed like any shoes; the rest map to their real single item.
    "golf shoes":"utrucking box","golf cleats":"utrucking box","ski boots":"utrucking box",
    "basketball shoes":"utrucking box","baseball cleats":"utrucking box","baseball shoes":"utrucking box",
    "football cleats":"utrucking box","soccer cleats":"utrucking box","volleyball shoes":"utrucking box",
    "tennis shoes":"utrucking box","running shoes":"utrucking box","hiking boots":"utrucking box",
    "snow boots":"utrucking box","winter boots":"utrucking box","rain boots":"utrucking box",
    "book shelf":"bookshelf","book case":"bookshelf","bean bag chair":"beanbag chair",
    "bike helmet":"sports equipment","ski helmet":"sports equipment","football helmet":"sports equipment",
    "bike pump":"sports equipment","bike lock":"sports equipment","bike rack":"sports equipment",
    "floor mirror":"mirror","full length mirror":"mirror","storage ottoman":"ottoman","storage drawers":"dresser",
    # more common household compounds whose head noun (lamp/fan/table/chair/mirror/hamper) is an alias
    "table lamp":"lamp","bedside lamp":"lamp","reading lamp":"lamp",
    "desk fan":"fan","ceiling fan":"fan","tower fan":"fan","floor fan":"fan","oscillating fan":"fan",
    "night table":"nightstand","picnic table":"table",
    "rocking chair":"swivel/arm chair","accent chair":"swivel/arm chair","lounge chair":"swivel/arm chair","dining chair":"swivel/arm chair",
    "wall mirror":"mirror","standing mirror":"mirror","vanity mirror":"mirror",
    "ski jacket":"utrucking box","winter jacket":"utrucking box","rain jacket":"utrucking box","letterman jacket":"utrucking box",
    "shoe box":"utrucking box","book bag":"utrucking box","backpack":"utrucking box","gym bag":"camp duffel","tote bag":"plastic container",
    "clothes hamper":"hamper/laundry basket","trash can":"plastic container","garbage can":"plastic container",
    "waste basket":"plastic container","recycling bin":"plastic container",
}

# Things that show up in photos / descriptions but aren't stored items we price — never match these.
NON_STORAGE = {
    "tape","packing tape","masking tape","duct tape","moving strap","strap","straps","dolly",
    "hand truck","moving blanket","moving blankets","bubble wrap","stretch wrap","shrink wrap",
    "plastic wrap","label","labels","marker","sharpie","scissors","box cutter","rope","twine",
    "person","people","hand","wall","floor","ceiling","door","window","room","truck","van","car",
}

def resolve_item_ex(name, price_book, approx_floor=0.84):
    # approx_floor 0.84: catches real spelling variants ("microwave oven" 0.87) but leaves
    # semantically-different lookalikes ("toaster"~"poster" 0.77, "coffee maker"~"coffee table")
    # to the AI mapping pass, which understands what the object actually is.
    """Resolve a spoken/typed item name to a catalog key.
    Returns (key, kind) with kind in {'exact','alias','approx','none'}:
      exact/alias — a confident match; approx — the nearest priced item (caller shows the mapping so
      the user sees 'you said X -> we priced it as Y'); none — nothing close / a non-storage object."""
    key = _canon(name)
    if not key or key in NON_STORAGE:
        return (None, "none")
    sing = key[:-1] if key.endswith("s") else key
    for k in (key, sing):
        if k in price_book: return (k, "exact")
        if k in ALIASES and ALIASES[k] in price_book: return (ALIASES[k], "alias")
    # tight fuzzy — an obvious typo of a real catalog name or alias
    m = difflib.get_close_matches(key, list(price_book), n=1, cutoff=0.82)
    if m: return (m[0], "exact")
    ma = difflib.get_close_matches(key, list(ALIASES), n=1, cutoff=0.82)
    if ma and ALIASES[ma[0]] in price_book: return (ALIASES[ma[0]], "alias")
    # word containment — the catalog item is literally named inside the phrase
    # ("microwave oven" contains "microwave", "storage ottoman" contains "ottoman")
    words = set(key.split())
    best = None
    for cand in list(price_book) + [a for a in ALIASES if ALIASES[a] in price_book]:
        if all(w in words for w in cand.split()) and (best is None or len(cand) > len(best)):
            best = cand
    if best:
        return (ALIASES.get(best, best), "approx")
    # loose fallback — nearest priced item, flagged approximate so the mapping is shown to the user
    pool = list(price_book) + [a for a in ALIASES if ALIASES[a] in price_book]
    m2 = difflib.get_close_matches(key, pool, n=1, cutoff=approx_floor)
    if m2:
        return (ALIASES.get(m2[0], m2[0]), "approx")
    return (None, "none")

def resolve_item(name, price_book):
    """Back-compat: the resolved key only (or None)."""
    return resolve_item_ex(name, price_book)[0]

MAX_QTY = 200  # per-line sanity cap: beyond a dorm-floor's worth, route to a human bulk quote

def price_items(items, price_book):
    """items: list of (name, qty). Resolves each name (alias -> typo -> closest priced item), clamps
    qty to [1, MAX_QTY], and aggregates by resolved item. A name that only matched approximately is
    flagged with `matched_from` and summarised in `matched` so the user sees the exact breakdown;
    names with nothing close land in `unmatched`."""
    order, bykey, unmatched, unmatched_items, capped = [], {}, [], [], False
    for name, qty in items:
        try: qty = int(qty)
        except Exception: qty = 1
        if qty < 1: qty = 1
        if qty > MAX_QTY: qty = MAX_QTY; capped = True
        key, kind = resolve_item_ex(name, price_book)
        if key is None:
            unmatched.append(str(name)); unmatched_items.append((str(name), qty)); continue
        if key not in bykey:
            price = price_book[key]
            line = {"item": key.title(), "qty": 0, "unit_price": round(price, 2), "amount": 0.0}
            if kind == "approx":
                line["matched_from"] = str(name)
            bykey[key] = line; order.append(key)
        line = bykey[key]
        if kind != "approx":
            line.pop("matched_from", None)     # an exact/alias hit for the same item wins
        line["qty"] += qty
        line["amount"] = round(line["unit_price"] * line["qty"], 2)
    lines = [bykey[k] for k in order]
    total = round(sum(l["amount"] for l in lines), 2)
    res = {"line_items": lines, "total": total, "unmatched": unmatched,
           "summary": "Estimated total ${:.2f} for {} item(s).".format(total, sum(l["qty"] for l in lines))}
    matched = [{"from": l["matched_from"], "to": l["item"]} for l in lines if l.get("matched_from")]
    if matched: res["matched"] = matched
    if unmatched_items: res["unmatched_items"] = unmatched_items   # (name, qty) — lets an AI pass re-price them
    if capped: res["capped"] = MAX_QTY
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
             "things thing stuff item items lot lots bunch other others "
             # request/filler verbs & adjectives so they're never mistaken for an item
             "quote quotes estimate price prices priced pricing cost costs need needs want wants have has had "
             "get got give gives like would could can cant will there here that this these those got gonna "
             "big small large heavy light old new full empty red blue black white green tall short mini "
             "them they got total how much many put").split())

def _item_vocab(price_book):
    """All individual words that appear in known item names / aliases — the spell-check dictionary."""
    v = set()
    for ph in list(ALIASES.keys()) + list(price_book.keys()):
        for w in ph.replace("/", " ").split():
            if len(w) >= 3:
                v.add(w)
    return v

def _fix_spelling(text, price_book):
    """Light domain spell-fix so a misspelled item is still understood: map an obvious typo to the
    nearest catalog word (e.g. 'utrucing'->'utrucking', 'matress'->'mattress'). Numbers, stop-words,
    quantity-words and already-known words are left untouched; only close matches (>=0.82) are changed,
    so unrelated words ('llama', 'spaceship', 'cable') are left alone and surfaced as unmatched."""
    vocab = _item_vocab(price_book)
    toks = re.findall(r"[a-z]+|\d+|[^a-z\d]+", (text or "").lower())
    for i, tok in enumerate(toks):
        if (tok.isalpha() and len(tok) >= 4 and tok not in vocab
                and tok not in _STOP and tok not in _WORDS):
            m = difflib.get_close_matches(tok, vocab, n=1, cutoff=0.82)
            if m:
                toks[i] = m[0]
    return "".join(toks)

_SEPS_RE = re.compile(r'[,;/&]|\band\b|\bplus\b|\+|\n')

def parse_freetext_ex(text, price_book):
    """Parse free text into a list of (name, qty). A quantity binds to the item it *precedes*, else the
    item it *follows*, within the same comma/'and' segment — and prefers a known item over a stray word.
    So '6 utrucing box', 'box 6', '6 red box', and '6x box' all read as 6 boxes. Multi-word items that
    are aliased (e.g. 'desk lamp', 'golf shoes') resolve as one item; two un-aliased adjacent knowns
    (e.g. 'dresser mirror') stay separate. Runs a domain spell-fix first so typos still resolve."""
    # split a quantity glued to an item so it isn't lost: "3bed"->"3 bed", "4u"->"4 u", "2boxes"->"2 boxes".
    # Only digit->letter (a leading number is almost always a count); letter->digit is left intact so
    # model names like "ps5"/"mp3" and the "x3" qty form survive.
    pre = re.sub(r'(?<=\d)(?=[a-zA-Z])', ' ', text or "")
    low = " " + _fix_spelling(pre, price_book).lower() + " "
    occ = [False] * len(low)
    seps = [m.start() for m in _SEPS_RE.finditer(low)]
    hits = []   # [start, end, key_or_None, original_text]
    for ph in sorted(set(list(ALIASES.keys()) + list(price_book.keys())), key=len, reverse=True):
        k = resolve_item(ph, price_book)
        if not k: continue
        for m in re.finditer(r'\b' + re.escape(ph) + r's?\b', low):
            s, e = m.start(), m.end()
            if any(occ[s:e]): continue
            for i in range(s, e): occ[i] = True
            hits.append([s, e, k, ph])
    # leftover nouns = possible unknown items; ADJACENT unknown words in the same segment
    # merge into one phrase ("baseball bat" stays one item, never "baseball" + a dropped "bat")
    runs = []
    for m in re.finditer(r'\b([a-z][a-z0-9]{2,})\b', low):   # alphanumeric so "ps5"/"mp3 player" count
        s, e, w = m.start(), m.end(), m.group(1)
        if any(occ[s:e]) or w in _STOP or w in _WORDS: continue
        if runs and s - runs[-1][1] <= 1 and not any(runs[-1][1] <= p < s for p in seps):
            runs[-1][1] = e; runs[-1][2] += " " + w
        else:
            runs.append([s, e, w])
    for s, e, w in runs:
        for i in range(s, e): occ[i] = True
        hits.append([s, e, None, w])
    hits.sort(key=lambda h: h[0])
    # quantity tokens: number-words / digits, plus "x3" and "3x" forms
    qtys = []
    for m in re.finditer(r'\b(' + _QTY + r')\b', low):
        if not any(occ[m.start():m.end()]):
            qtys.append([m.start(), m.end(), _word_to_int(m.group(1))])
    for m in re.finditer(r'\bx\s*(\d+)\b|\b(\d+)\s*x\b', low):
        qtys.append([m.start(), m.end(), int(m.group(1) or m.group(2))])
    def crosses(a, b):
        lo, hi = min(a, b), max(a, b)
        return any(lo <= p < hi for p in seps)
    qty_of = {}
    for q in sorted(qtys, key=lambda z: z[0]):
        after  = [h for h in hits if h[0] >= q[1] and id(h) not in qty_of and not crosses(q[1], h[0])]
        before = [h for h in hits if h[1] <= q[0] and id(h) not in qty_of and not crosses(h[1], q[0])]
        # prefer the item the qty precedes; prefer a KNOWN item; then the nearest
        target = None
        if after:  target = min(after,  key=lambda h: (h[2] is None, h[0] - q[1]))
        elif before: target = min(before, key=lambda h: (h[2] is None, q[0] - h[1]))
        if target is not None:
            qty_of[id(target)] = q[2]
    items = []
    # quote context = the text names a known item OR attaches a quantity to anything;
    # in that context bare unknown phrases are kept (qty 1) so "1 toaster, skis" keeps the skis.
    # Outside it ("hello there") stray words are filler and are dropped.
    has_context = any(h[2] is not None for h in hits) or bool(qty_of)
    for h in hits:
        qty = qty_of.get(id(h))
        if h[2] is None and qty is None and not has_context:
            continue
        items.append((h[2] if h[2] is not None else h[3], qty if qty is not None else 1))
    return items

def parse_freetext(text, price_book):
    """Back-compat wrapper."""
    return parse_freetext_ex(text, price_book)

def quote(items_or_text, price_book):
    if isinstance(items_or_text, list):
        return price_items(items_or_text, price_book)
    return price_items(parse_freetext_ex(items_or_text, price_book), price_book)

def merge_photo_text(detected, text, price_book):
    """Combine AI photo detections with the customer's typed clarification into one item list.
    The customer's own words are authoritative: if both mention the same item, the TYPED quantity
    wins (they can see their stuff better than the AI); items only in the text are added; items only
    in the photo are kept. Returns (items, source_by_key) — items is [(name, qty)] ready for
    price_items (original names kept so approx matches still show their mapping), and source_by_key
    maps each resolved catalog key -> 'photo' | 'you' | 'photo+you' for a transparent breakdown."""
    def bucket(pairs):
        out, passthru = {}, []
        for name, qty in pairs:
            try: qty = int(qty)
            except Exception: qty = 1
            key, kind = resolve_item_ex(name, price_book)
            if key is None:
                passthru.append((str(name), qty))       # unknown → let price_items surface it
            else:
                q, nm = out.get(key, (0, None))
                # keep an original name only while the match is approximate (so the mapping shows)
                out[key] = (q + qty, nm if nm is not None and kind != "approx" else
                            (str(name) if kind == "approx" else key))
        return out, passthru
    photo_q, photo_un = bucket(detected)
    text_q, text_un = bucket(parse_freetext_ex(text or "", price_book))
    items, source_by_key = [], {}
    for key in list(photo_q) + [k for k in text_q if k not in photo_q]:
        in_p, in_t = key in photo_q, key in text_q
        qty, name = text_q[key] if in_t else photo_q[key]     # typed count wins
        items.append((name, qty))
        source_by_key[key] = "photo+you" if (in_p and in_t) else ("you" if in_t else "photo")
    return items + photo_un + text_un, source_by_key

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

def _room_key(s):
    """Natural sort key for a room string so 2 < 10 < 12A (a sensible walking order in a building).
    Each chunk is a (type_rank, number, text) tuple so int and str chunks never compare directly."""
    out = []
    for t in re.findall(r'\d+|\D+', (s or "").strip()):
        if t.isdigit():
            out.append((0, int(t), ""))
        else:
            out.append((1, 0, t.lower()))
    return out


def dispatch_plan(dispatch_rows, date):
    """B-ops: cluster a day's pickups by building and suggest crew split (route optimizer core).
    Stops inside a building are sequenced by room order so a crew walks it in one pass."""
    d = date if isinstance(date, datetime.date) else _parse_date(date)
    stops = defaultdict(list)
    for r in dispatch_rows:
        if _parse_date(r.get("Date", "")) == d:
            b = (r.get("Building", "") or "").strip() or "Unknown"
            stops[b].append({"student": r.get("Student", ""), "room": r.get("Room", ""),
                             "order_id": r.get("ID", ""), "service": r.get("Service", "")})
    for b in stops:                                   # sequence each building's stops by room
        stops[b].sort(key=lambda o: _room_key(o.get("room", "")))
        for i, o in enumerate(stops[b], 1):
            o["seq"] = i
    clusters = sorted(stops.items(), key=lambda kv: -len(kv[1]))
    total = sum(len(v) for v in stops.values())
    crews = crews_for(d) if d else 2
    # greedy crew split: biggest building cluster goes to the least-loaded crew,
    # so each crew works a compact set of buildings with a balanced stop count
    bins = [{"crew": i + 1, "stops": 0, "buildings": []} for i in range(max(crews, 1))]
    for b, v in clusters:
        tgt = min(bins, key=lambda x: x["stops"])
        tgt["buildings"].append(b)
        tgt["stops"] += len(v)
    capacity = crews * JOBS_PER_CREW
    return {"date": str(d) if d else None, "total_stops": total, "buildings": len(stops),
            "crews_available": crews, "avg_stops_per_crew": round(total / max(crews, 1), 1),
            "capacity": capacity, "jobs_per_crew": JOBS_PER_CREW,
            "utilization_pct": round(100 * total / capacity, 0) if capacity else 0,
            "crew_plan": bins,
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
