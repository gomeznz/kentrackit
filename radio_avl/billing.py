"""
Charges for companies on the hosted service.

A company's plan can have any mix of:
  flat    a monthly fee
  radio   per active radio: any radio heard that month (a position report or a call)
  user    per user: the most sign-in accounts the company had at once during the month
  phone   per phone: phones that shared their location that month

Each per-item charge is a list of tiers, e.g. [{"upto": 10, "price": 20}, {"upto": None, "price": 15}]:
radios 1-10 cost 20 each and every radio after that 15 each (graduated, like tax brackets).
One tier with "upto": None is a plain price per item. An empty list means that item isn't charged.

Plans have a starting month. A new plan applies from its month on; earlier months keep the plan
they had. A plan of None from a month means "no charge from then on".

Money is worked in cents so the totals always add up.
"""
import datetime
import re

ITEMS = (("radio", "Radios", "radio"), ("user", "Users", "user"), ("phone", "Phones", "phone"))
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
MAX_PRICE = 100000


class Invalid(ValueError):
    pass


# ---- months ------------------------------------------------------------------------------------
def month_of(t):
    """'2026-11' for a time, in the server's time zone (set it to Pacific/Auckland)."""
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m")


def month_range(month):
    """-> (start, end) times of a month, end exclusive."""
    m = MONTH_RE.match(month or "")
    if not m:
        raise Invalid("Month should look like 2026-11")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime.datetime(y, mo, 1)
    end = datetime.datetime(y + (mo == 12), mo % 12 + 1, 1)
    return start.timestamp(), end.timestamp()


def add_months(month, n):
    y, mo = map(int, month.split("-"))
    k = y * 12 + (mo - 1) + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def check_month(month):
    month_range(month)
    return month


# ---- plans -----------------------------------------------------------------------------------------
def _money(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise Invalid(f"{what} must be an amount")
    if not 0 <= x <= MAX_PRICE:
        raise Invalid(f"{what} must be between 0 and {MAX_PRICE}")
    return round(x, 2)


def _tiers(raw, label):
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list) or len(raw) > 10:
        raise Invalid(f"{label}: up to 10 price tiers")
    out, last = [], 0
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise Invalid(f"{label}: each tier needs an 'up to' and a price")
        price = _money(t.get("price"), f"{label} price")
        upto = t.get("upto")
        if i == len(raw) - 1:
            upto = None                     # the last tier covers everything above the one before
        elif upto in (None, ""):
            raise Invalid(f"{label}: only the last tier can be 'and above'")
        if upto is not None:
            try:
                upto = int(upto)
            except (TypeError, ValueError):
                raise Invalid(f"{label}: 'up to' must be a whole number")
            if upto <= last:
                raise Invalid(f"{label}: each tier must go higher than the one before")
            last = upto
        out.append({"upto": upto, "price": price})
    return out


def validate_plan(p):
    """A plan as sent from the page -> a clean plan. Raises Invalid."""
    if p is None:
        return None
    if not isinstance(p, dict):
        raise Invalid("Bad plan")
    plan = {"flat": _money(p.get("flat") or 0, "Monthly fee")}
    for key, label, _ in ITEMS:
        plan[key] = _tiers(p.get(key), label)
    plan["note"] = str(p.get("note") or "")[:200]
    if not plan["flat"] and not any(plan[k] for k, _, _ in ITEMS):
        raise Invalid("Set a monthly fee or a price for radios, users or phones (or choose no charge)")
    return plan


def describe(plan, currency="NZD"):
    """'NZD 50.00 a month + radios: 1-10 at 20.00, 11+ at 15.00' for lists and CSVs."""
    if not plan:
        return "No charge"
    parts = []
    if plan["flat"]:
        parts.append(f"{currency} {plan['flat']:.2f} a month")
    for key, label, _ in ITEMS:
        tiers = plan[key]
        if not tiers:
            continue
        if len(tiers) == 1:
            parts.append(f"{tiers[0]['price']:.2f} per {key}")
            continue
        lo, bits = 1, []
        for t in tiers:
            rng = f"{lo}-{t['upto']}" if t["upto"] else f"{lo}+"
            bits.append(f"{rng} at {t['price']:.2f}")
            lo = (t["upto"] or 0) + 1
        parts.append(f"{label.lower()}: " + ", ".join(bits))
    return " + ".join(parts)


# ---- statements -------------------------------------------------------------------------------------
def _cents(x):
    return int(round(x * 100))


def graduated(qty, tiers):
    """-> lines for qty items over graduated tiers: [{from, to, qty, price, amount_c}]."""
    lines, lo = [], 1
    for t in tiers:
        if qty < lo:
            break
        hi = qty if t["upto"] is None else min(qty, t["upto"])
        n = hi - lo + 1
        if n > 0:
            lines.append({"from": lo, "to": hi, "qty": n, "price": t["price"], "amount_c": n * _cents(t["price"])})
        if t["upto"] is None:
            break
        lo = t["upto"] + 1
    return lines


def statement(plan, usage, gst_pct=15.0):
    """What a month costs. usage: {"radios": n, "users": n, "phones": n}."""
    lines = []
    if plan:
        if plan["flat"]:
            lines.append({"item": "Monthly fee", "detail": "", "qty": 1, "price": plan["flat"], "amount_c": _cents(plan["flat"])})
        for key, label, unit in ITEMS:
            if not plan[key]:
                continue
            qty = int(usage.get(key + "s", 0) or 0)
            parts = graduated(qty, plan[key])
            if not parts:
                lines.append({"item": label, "detail": "none", "qty": 0, "price": plan[key][0]["price"], "amount_c": 0})
            for g in parts:
                detail = "" if len(plan[key]) == 1 else (f"{g['from']}-{g['to']}" if g["to"] != g["from"] else f"no. {g['from']}")
                lines.append({"item": label, "detail": detail, "qty": g["qty"], "price": g["price"], "amount_c": g["amount_c"]})
    sub = sum(x["amount_c"] for x in lines)
    gst = int(round(sub * float(gst_pct) / 100))
    for x in lines:
        x["amount"] = x.pop("amount_c") / 100
    return {"lines": lines, "subtotal": sub / 100, "gst": gst / 100, "total": (sub + gst) / 100}


# ---- usage from a company's own data ------------------------------------------------------------------
def usage_from(app, month):
    """Radios and phones heard in the month, from the company's database."""
    t0, t1 = month_range(month)
    with app.store.lock:
        db = app.store.db
        pos = [r[0] for r in db.execute(
            "SELECT DISTINCT unit_id FROM positions WHERE rx_time >= ? AND rx_time < ?", (t0, t1))]
        calls = [r[0] for r in db.execute(
            "SELECT DISTINCT unit_id FROM transmissions WHERE start >= ? AND start < ? AND unit_id IS NOT NULL "
            "AND kind IN ('data', 'voice')", (t0, t1))]
    radios = {u for u in pos + calls if u and not u.startswith("PH")}
    phones = {u for u in pos if u and u.startswith("PH")}
    return radios, phones
