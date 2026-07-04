"""Offline unit tests for analytics.compute_metrics — safety on empty/malformed + forecast shape."""
import analytics


def _dispatch(day, building, n, svc="Summer Storage"):
    return [{"Student": "N%s%d" % (day, i), "Building": building, "Date": day,
             "ID": "#%s%d" % (day.replace('/', ''), i), "Service": svc,
             "Status": "Scheduled", "Dispatch Status": "Dispatched", "Phone": "3145550%03d" % i}
            for i in range(n)]


def _service(day, building, n, price=87.0, svc="Summer Storage"):
    return [{"Student Name": "N%s%d" % (day, i), "Order#:": "%s%d" % (day.replace('/', ''), i),
             "Service Type": svc, "Building": building, "Invoice ID": "INV%s%d" % (day, i), "Date": day,
             "Summer Storage Item List": "Mattress (Amount: %.2f USD, Quantity: 1); Total: $%.2f" % (price, price)}
            for i in range(n)]


def _season():
    D, S = [], []
    for day, bld, n in [("5/4/2026", "Umrath", 10), ("5/5/2026", "Eliot", 14),
                        ("5/6/2026", "Umrath", 22), ("5/7/2026", "Lien", 9), ("5/12/2026", "Park", 6)]:
        D += _dispatch(day, bld, n)
        S += _service(day, bld, n)
    D += _dispatch("8/20/2026", "Umrath", 8, svc="Return Delivery")
    S += _service("8/20/2026", "Umrath", 8, price=40.0, svc="Return Delivery")
    return D, S


def test_empty_inputs_do_not_crash():
    m = analytics.compute_metrics([], [])
    assert isinstance(m, dict)
    assert isinstance(m.get("forecast"), dict)
    assert m["overview"]["dispatch_orders"] == 0


def test_malformed_rows_do_not_crash():
    m = analytics.compute_metrics([{"foo": "bar"}], [{"baz": "qux"}])
    assert isinstance(m, dict)


def test_overview_and_forecast_shape():
    D, S = _season()
    m = analytics.compute_metrics(D, S)
    assert m["overview"]["dispatch_orders"] == 10 + 14 + 22 + 9 + 6 + 8
    fc = m["forecast"]
    assert isinstance(fc["peak_window"], list) and fc["peak_window"]
    assert fc["peak_window"][0]["orders"] == 22                # the peak day
    assert fc["peak_window"][0]["crews_needed"] >= 1
    assert fc["return_season"]["orders"] == 8                  # the August return rows


def test_revenue_forecast_and_building_timing_present():
    D, S = _season()
    fc = analytics.compute_metrics(D, S)["forecast"]
    rv = fc["revenue_forecast"]
    assert rv["peak_day_revenue"] > 0
    assert rv["move_out_window_revenue"] >= rv["peak_day_revenue"]
    assert fc["building_peak_timing"]                          # at least one building timed


def test_pricing_levers_present():
    D, S = _season()
    m = analytics.compute_metrics(D, S)
    assert m["pricing"]
    top = m["pricing"][0]
    assert top["unit_price"] > 0 and top["units_sold"] > 0


def test_ask_brief_leaks_no_pii():
    import main
    D, S = _season()
    m = analytics.compute_metrics(D, S)
    brief = main._metrics_brief(m)
    import re
    assert not re.search(r"\d{10}", brief)                     # no raw phone numbers
