"""
UTrucking analytics engine — pure functions over already-fetched sheet rows.
Powers ideas #2-#7: master orders join, data-quality, completion funnel,
basket/upsell, per-building demand, repeat customers. compute_metrics() returns
one JSON-safe dict used by /insights (display) and /ask (grounding for the copilot).
"""
from collections import Counter, defaultdict
import engines


def _f(x):
    return " ".join((x or "").split())


_UNKNOWN_BLD = {"", "unknown", "n/a", "na", "tbd", "none", "null", "xxx", "?", "-"}

def _is_unknown_building(b):
    """A building value that is really a placeholder (blank, 'unknown', 'DON'T KNOW YET…', 'xxx')."""
    b = _f(b).lower()
    if b in _UNKNOWN_BLD:
        return True
    return ("don't know" in b or "dont know" in b or "do not know" in b
            or "unknown" in b or "xxx" in b or b.startswith("?"))

def _norm_building(b):
    return "Unknown" if _is_unknown_building(b) else _f(b)


def _order_id(row, *keys):
    for k in keys:
        v = _f(row.get(k, ""))
        if v:
            return v
    return ""


def master_orders(dispatch, service):
    """Join DISPATCH + SERVICE on Order#/ID (fallback: cleaned name). Returns unified records."""
    svc_by_id, svc_by_name = {}, {}
    for r in service:
        oid = _order_id(r, "Order#:", "Order #", "Order#")
        if oid:
            svc_by_id[oid.lstrip("#").strip()] = r
        n = engines._canon(r.get("Student Name", ""))
        if n:
            svc_by_name.setdefault(n, r)
    out = []
    for r in dispatch:
        did = _order_id(r, "ID").lstrip("#").strip()
        name = engines._canon(r.get("Student", ""))
        s = svc_by_id.get(did) or svc_by_name.get(name) or {}
        out.append({
            "order_id": did or _order_id(s, "Order#:"),
            "name": _f(r.get("Student", "")) or _f(s.get("Student Name", "")),
            "building": _f(r.get("Building", "")) or _f(s.get("Building", "")),
            "status": _f(r.get("Status", "")),
            "dispatch_status": _f(r.get("Dispatch Status", "")),
            "phone": _f(r.get("Phone", "")),
            "total": engines._order_total(s) if s else None,
            "invoice": _f(s.get("Invoice ID", "")) if s else "",
            "completed": bool(_f(s.get("Date of completion", ""))) if s else False,
        })
    return out


def compute_metrics(dispatch, service):
    m = {}
    # ---- revenue + baskets (from SERVICE item lists) ----
    totals, rev_by_building, item_counter, baskets = [], defaultdict(float), Counter(), []
    unit_price_ctr, item_revenue = defaultdict(Counter), defaultdict(float)
    for r in service:
        t = engines._order_total(r)
        b = _norm_building(r.get("Building", ""))
        if t:
            totals.append(t); rev_by_building[b] += t
        parsed = engines._ITEM_RE.findall(r.get("Summer Storage Item List", "") or "")
        items = [(engines._canon(n), int(q)) for n, a, q in parsed]
        for n, q in items:
            item_counter[n] += q
        for n, a, q in parsed:                       # per-item unit price + revenue (pricing levers)
            cn = engines._canon(n)
            unit_price_ctr[cn][float(a)] += int(q)
            item_revenue[cn] += float(a) * int(q)
        names = sorted({n for n, _ in items})
        if names:
            baskets.append(names)
    revenue = round(sum(totals), 2)
    m["overview"] = {
        "service_orders": len(service),
        "dispatch_orders": len(dispatch),
        "orders_with_revenue": len(totals),
        "revenue": revenue,
        "avg_order": round(revenue / len(totals), 2) if totals else 0.0,
        "median_order": round(sorted(totals)[len(totals) // 2], 2) if totals else 0.0,
    }
    m["revenue_by_building"] = [{"building": b, "revenue": round(v, 2)}
                               for b, v in sorted(rev_by_building.items(), key=lambda kv: -kv[1])[:12]]
    # ---- basket / upsell (idea #5) ----
    pair = Counter()
    for names in baskets:
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pair[(names[i], names[j])] += 1
    m["top_items"] = [{"item": k.title(), "count": c} for k, c in item_counter.most_common(12)]
    m["top_pairs"] = [{"a": a.title(), "b": b.title(), "count": c} for (a, b), c in pair.most_common(8)]
    # ---- pricing levers: current unit price, units sold, and $/season per +$1 (idea: price optimization) ----
    pricing = []
    for k, units in item_counter.most_common(10):
        price = unit_price_ctr[k].most_common(1)[0][0] if unit_price_ctr[k] else 0.0
        pricing.append({
            "item": k.title(),
            "unit_price": round(price, 2),
            "units_sold": units,
            "revenue": round(item_revenue[k], 2),
            "extra_per_$1_increase": round(units, 2),      # +$1 on this item ≈ this much more per season
            "revenue_share_pct": round(100 * item_revenue[k] / revenue, 1) if revenue else 0.0,
        })
    m["pricing"] = pricing
    m["avg_items_per_order"] = round(sum(sum(q for _, q in
        [(engines._canon(n), int(qq)) for n, a, qq in engines._ITEM_RE.findall(r.get("Summer Storage Item List", "") or "")])
        for r in service) / max(len(baskets), 1), 1)
    # ---- completion funnel (idea #4) ----
    status_c = Counter()
    for r in dispatch:
        status_c[_f(r.get("Status", "")).title() or "(blank)"] += 1
    completed = sum(v for k, v in status_c.items() if "complete" in k.lower())
    dispatched = sum(1 for r in dispatch
                     if _f(r.get("Dispatch Status", "")) and "not" not in _f(r.get("Dispatch Status", "")).lower())
    audit = engines.billing_audit(service)
    m["funnel"] = {
        "orders": len(dispatch),
        "dispatched": dispatched,
        "completed": completed,
        "invoiced": sum(1 for r in service if _f(r.get("Invoice ID", ""))),
        "flagged_billing": audit["count"],
    }
    m["status_breakdown"] = [{"status": k, "count": v} for k, v in status_c.most_common(8)]
    m["billing_flags"] = audit["summary"]
    # ---- per-building demand + calendar (idea #6) ----
    load = engines.day_load(dispatch)
    by_month = Counter()
    for d, c in load.items():
        by_month["%04d-%02d" % (d.year, d.month)] += c
    bld = Counter(_norm_building(r.get("Building", "")) for r in dispatch)
    peak = engines.peak_date(dispatch)
    top_days = sorted(load.items(), key=lambda kv: -kv[1])[:5]
    m["demand"] = {
        "by_month": [{"month": k, "orders": v} for k, v in sorted(by_month.items())],
        "top_buildings": [{"building": b, "orders": c} for b, c in bld.most_common(12)],
        "peak_date": str(peak) if peak else None,
        "busiest_days": [{"date": str(d), "orders": c} for d, c in top_days],
    }
    # ---- repeat customers / LTV (idea #7) ----
    name_orders, name_rev = Counter(), defaultdict(float)
    for r in service:
        n = engines._canon(r.get("Student Name", ""))
        if n:
            name_orders[n] += 1
            t = engines._order_total(r)
            if t:
                name_rev[n] += t
    repeats = {n: c for n, c in name_orders.items() if c > 1}
    m["repeat"] = {
        "unique_customers": len(name_orders),
        "repeat_customers": len(repeats),
        "repeat_rate_pct": round(100 * len(repeats) / max(len(name_orders), 1), 1),
    }
    # ---- data-quality scorecard (idea #3) ----
    unknown_b = sum(1 for r in dispatch if _is_unknown_building(r.get("Building", "")))
    missing_phone = sum(1 for r in dispatch if not _f(r.get("Phone", "")))
    dup_names = sum(1 for n, c in Counter(engines._canon(r.get("Student", "")) for r in dispatch if _f(r.get("Student", ""))).items() if c > 1)
    nd = len(dispatch) or 1
    m["data_quality"] = {
        "unknown_building": unknown_b,
        "unknown_building_pct": round(100 * unknown_b / nd, 1),
        "missing_phone": missing_phone,
        "missing_phone_pct": round(100 * missing_phone / nd, 1),
        "duplicate_named_customers": dup_names,
        "missing_invoice": audit["summary"].get("missing_invoice", 0),
        "zero_or_missing_total": audit["summary"].get("zero_or_missing_total", 0),
    }
    return m
