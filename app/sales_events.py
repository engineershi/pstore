# -*- coding: utf-8 -*-
"""pstore hot-sale finder: know which Amazon / retail sale events are active or
incoming so every marketing layer (drop emails, the Telegram digest, social
kits, the deal banner) can borrow urgency from the calendar — and when a real
price drop is also found, the event becomes the leverage that makes the drop
unmissable ("Black Friday is live AND this just dropped 30%").

Every recurring event is a date *anchor* (month, day) plus a window in days
before/after, so the code never has to resolve "which year" or handle
cross-month wraparound. One-offs (operator-added: "my launch Jul 14-16",
"Prime Day 2026", ...) use absolute YYYY-MM-DD dates.
"""

import datetime

# ------------------------------------------------------------------ registry
# anchor: (month, day) in a *reference* non-leap year (2020-01-01 == day 1).
# The window (before/after, in days) is measured around the anchor so
# "incoming" urgency shows well before the event launches.
SALE_EVENTS = [
    {"id": "newyear", "name": "New Year Deals", "emoji": "🎇",
     "anchor": (1, 8), "before": 7, "after": 7,
     "hashtags": ("#NewYearDeals",)},
    {"id": "valentine", "name": "Valentine's Day Deals", "emoji": "💘",
     "anchor": (2, 14), "before": 7, "after": 7,
     "hashtags": ("#ValentinesDay",)},
    {"id": "spring", "name": "Spring Sale", "emoji": "🌱",
     "anchor": (3, 25), "before": 10, "after": 16,
     "hashtags": ("#SpringSale",)},
    {"id": "mother", "name": "Mother's Day Gifting", "emoji": "🌷",
     "anchor": (5, 10), "before": 10, "after": 7,
     "hashtags": ("#MothersDay",)},
    {"id": "father", "name": "Father's Day Deals", "emoji": "👔",
     "anchor": (6, 20), "before": 10, "after": 7,
     "hashtags": ("#FathersDay",)},
    {"id": "primeday", "name": "Amazon Prime Day", "emoji": "⚡",
     "anchor": (7, 16), "before": 14, "after": 7,
     "hashtags": ("#PrimeDay",)},
    {"id": "backtoschool", "name": "Back-to-School Savings", "emoji": "🎒",
     "anchor": (8, 8), "before": 14, "after": 7,
     "hashtags": ("#BackToSchool",)},
    {"id": "bigdealdays", "name": "Amazon Big Deal Days", "emoji": "🚀",
     "anchor": (10, 10), "before": 14, "after": 7,
     "hashtags": ("#BigDealDays",)},
    {"id": "blackfriday", "name": "Black Friday", "emoji": "🖤",
     "anchor": (11, 28), "before": 17, "after": 3,
     "hashtags": ("#BlackFriday #BlackFridayDeals",)},
    {"id": "cybermonday", "name": "Cyber Monday", "emoji": "💻",
     "anchor": (12, 1), "before": 2, "after": 2,
     "hashtags": ("#CyberMonday",)},
    {"id": "december", "name": "December Holiday Deals", "emoji": "🎄",
     "anchor": (12, 20), "before": 6, "after": 6,
     "hashtags": ("#HolidayDeals #GiftGuide",)},
]

_UPCOMING_HORIZON = 60          # days; past that an event is just "off" noise

# days before the 1st of each month in a plain (non-leap) year — event math is
# anchored to this table so recurring windows are year-agnostic
_DAYS_BEFORE = {1: 0, 2: 31, 3: 59, 4: 90, 5: 120, 6: 151, 7: 181,
                8: 212, 9: 243, 10: 273, 11: 304, 12: 334}


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _year_day(month, day):
    """0-based day-of-year (0..364) on the non-leap reference calendar."""
    return _DAYS_BEFORE.get(month, 0) + _int(day, 1) - 1


def _as_date(value, default=None):
    """Best-effort YYYY-MM-DD / MM-DD / datetime.date -> date or default."""
    if value is None or value == "":
        return default
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d"):
        try:
            return datetime.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return default


def _doy(d):
    """0-based day-of-year on the non-leap reference calendar."""
    return _year_day(d.month, d.day)


def _anchor_doy(event):
    return _year_day(*event["anchor"])


def _event_days(event, now):
    """Return (days_until, status) for a recurring event relative to `now`.
    days_until == 0 means the window is active today; otherwise it is the
    distance to the next window start (1..364). status is "active", "upcoming"
    (next start within _UPCOMING_HORIZON) or "off"."""
    today = _doy(now)
    anchor = _anchor_doy(event)
    before = _int(event.get("before"), 0)
    after = _int(event.get("after"), 0)
    start = (anchor - before) % 365
    end = (anchor + after) % 365
    if after == 0 and before == 0:
        days_until = (anchor - today) % 365
    elif start <= end:
        if start <= today <= end:          # inside this year's window
            return 0, "active"
        days_until = (start - today) % 365
        if days_until == 0:
            days_until = 365
    else:
        # window wraps the new year: [start..364] U [0..end]
        if today >= start or today <= end:
            return 0, "active"
        days_until = (start - today) % 365 or 365
    if days_until <= _UPCOMING_HORIZON:
        return days_until, "upcoming"
    return days_until, "off"


def _ends_in(event, now):
    """Remaining days (1 when it ends later today) for an active event."""
    end = (_anchor_doy(event) + _int(event.get("after"), 0)) % 365
    days = (end - _doy(now)) % 365
    return max(days, 1)


def _render(event, status, days_until, ends_in):
    name = event["name"]
    emoji = event["emoji"]
    if status == "active":
        tagline = "%s %s is LIVE — ends in %dd" % (emoji, name, ends_in)
    elif status == "upcoming":
        tagline = "%s %s incoming — in %dd" % (emoji, name, days_until)
    else:
        tagline = "%s %s (next in %dd)" % (emoji, name, days_until)
    return {"id": event["id"], "name": name, "emoji": emoji,
            "custom": event.get("custom", False), "status": status,
            "days_until": days_until, "ends_in": ends_in,
            "tagline": tagline, "hashtags": event.get("hashtags") or (),
            "start": event.get("start"), "end": event.get("end")}


def window_status(now=None, custom=None):
    """Snapshot of every sale event relative to `now` (today UTC by default).

    custom: optional list of one-off events as dicts
      {"id","name","emoji","start","end","hashtags"} using YYYY-MM-DD dates.

    Returns a list (active first, then upcoming by soonest, then off), each:
      id, name, emoji, custom, status ("active"/"upcoming"/"off"), days_until,
      ends_in, tagline ("⚡ Prime Day is LIVE — ends in 3d"), hashtags.
    """
    now = now or datetime.datetime.utcnow().date()
    if isinstance(now, datetime.datetime):
        now = now.date()
    out = []
    for ev in SALE_EVENTS:
        days_until, status = _event_days(ev, now)
        ends_in = _ends_in(ev, now) if status == "active" else 0
        out.append(_render(dict(ev, custom=False), status, days_until, ends_in))
    for ev in (custom or []):
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("name") or "Deal event").strip()
        eid = str(ev.get("id") or name).strip() or None
        emoji = str(ev.get("emoji") or "🔥")[:2]
        start = _as_date(ev.get("start"))
        end = _as_date(ev.get("end"))
        tags = tuple((ev.get("hashtags") or "").split())
        rec = {"id": eid or "custom", "name": name, "emoji": emoji,
               "custom": True, "hashtags": tags, "start": ev.get("start"),
               "end": ev.get("end")}
        if start and end:
            if end < start:
                start, end = end, start
            if now < start:
                status, days_until, ends_in = "upcoming", (start - now).days, 0
            elif now > end:
                status, days_until, ends_in = "off", (start - now).days, 0
            else:
                status, days_until, ends_in = "active", 0, (end - now).days
        else:
            status, days_until, ends_in = "off", 0, 0
        out.append(_render(rec, status, days_until, ends_in))
    live = [e for e in out if e["status"] == "active"]
    soon = [e for e in out if e["status"] == "upcoming"]
    rest = [e for e in out if e["status"] == "off"]
    live.sort(key=lambda e: e["ends_in"])
    soon.sort(key=lambda e: e["days_until"])
    rest.sort(key=lambda e: e["days_until"])
    return live + soon + rest


def upcoming_summary(now=None, custom=None, horizon_days=28, limit=1):
    """Pick the most urgent leverage hook: the active event ending soonest,
    else the nearest upcoming event within `horizon_days`.

    Returns {} when nothing is on the radar, else
      {"active": bool, "event": {...}, "line": "...", "hashtags": (...)}."""
    events = window_status(now, custom)
    if not events:
        return {}
    active = [e for e in events if e["status"] == "active"]
    if active:
        ev = sorted(active, key=lambda e: e["ends_in"])[0]
        line = ("%s %s is LIVE — prices are at their seasonal lows, this is the "
                "moment to buy" % (ev["emoji"], ev["name"]))
        return {"active": True, "event": ev, "line": line,
                "hashtags": ev["hashtags"]}
    soon = [e for e in events if e["status"] == "upcoming"
            and e["days_until"] <= horizon_days]
    if soon:
        ev = soon[0]
        line = ("%s %s incoming — in %dd, prices drop across the board" %
                (ev["emoji"], ev["name"], ev["days_until"]))
        return {"active": False, "event": ev, "line": line,
                "hashtags": ev["hashtags"]}
    return {}


def event_hashtag(summary):
    """First clean event hashtag (#BlackFriday) or ''."""
    for h in (summary or {}).get("hashtags") or ():
        h = str(h).strip()
        for word in h.split():
            if word.startswith("#"):
                return word
    return ""