"""US economic calendar from FRED (St. Louis Fed) - free API key.

Stores one item per scheduled release in the ECONCAL partition:
  pk = "ECONCAL", sk = "2026-10-15#CPI"
with the release time, importance, and the latest published values
(e.g. CPI m/m and y/y, payrolls change, unemployment rate).

FRED gives release *dates* (including future ones) and the data itself.
It does not give consensus forecasts.
"""
import calendar
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger()

FRED = "https://api.stlouisfed.org/fred"

# FRED release name (lower case) -> (key, label, ET time, importance)
RELEASES = {
    "consumer price index": ("CPI", "CPI inflation", "08:30", "high"),
    "employment situation": ("NFP", "Nonfarm payrolls & unemployment rate", "08:30", "high"),
    "personal income and outlays": ("PCE", "PCE inflation (Fed's preferred gauge)", "08:30", "high"),
    "gross domestic product": ("GDP", "GDP", "08:30", "high"),
    "producer price index": ("PPI", "PPI (producer prices)", "08:30", "medium"),
    "advance monthly sales for retail and food services": ("RETAIL", "Retail sales", "08:30", "medium"),
    "job openings and labor turnover survey": ("JOLTS", "Job openings (JOLTS)", "10:00", "medium"),
    "unemployment insurance weekly claims report": ("CLAIMS", "Initial jobless claims", "08:30", "low"),
}
# Known IDs as a fallback in case FRED renames a release.
RELEASE_IDS = {10: "consumer price index", 50: "employment situation",
               9: "advance monthly sales for retail and food services"}

# key -> list of (series id, how to describe it, label)
SERIES = {
    "CPI": [("CPIAUCSL", "mom_yoy", "Headline CPI"), ("CPILFESL", "mom_yoy", "Core CPI")],
    "NFP": [("PAYEMS", "change_k", "Payrolls"), ("UNRATE", "pct", "Unemployment rate")],
    "PCE": [("PCEPI", "mom_yoy", "PCE prices"), ("PCEPILFE", "mom_yoy", "Core PCE")],
    "GDP": [("A191RL1Q225SBEA", "pct_q", "Real GDP, annualized q/q")],
    "PPI": [("PPIFIS", "mom_yoy", "PPI final demand")],
    "RETAIL": [("RSAFS", "mom", "Retail sales")],
    "JOLTS": [("JTSJOL", "level_k", "Job openings")],
    "CLAIMS": [("ICSA", "level_count", "Initial claims")],
    "FOMC": [("DFEDTARL", "pct", "Target range low"), ("DFEDTARU", "pct", "Target range high")],
}

# FOMC rate decisions (statement day, 2:00 pm ET). 2027 dates are the Fed's tentative schedule.
FOMC_DATES = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
    "2027-10-27", "2027-12-08",
]


def match_release(rel):
    name = (rel.get("release_name") or "").strip().lower()
    if name in RELEASES:
        return RELEASES[name]
    if rel.get("release_id") in RELEASE_IDS:
        return RELEASES[RELEASE_IDS[rel["release_id"]]]
    return None


def release_calendar(get_json, api_key, start, end):
    """All matching release dates between start and end (inclusive), including future ones."""
    found, offset = {}, 0
    while True:
        data = get_json(f"{FRED}/releases/dates", {
            "api_key": api_key, "file_type": "json",
            "realtime_start": start.isoformat(), "realtime_end": end.isoformat(),
            "include_release_dates_with_no_data": "true",
            "order_by": "release_date", "sort_order": "asc", "limit": 1000, "offset": offset,
        }) or {}
        rows = data.get("release_dates", [])
        for r in rows:
            m = match_release(r)
            if m:
                found[(r["date"], m[0])] = (r, m)
        offset += len(rows)
        if not rows or offset >= int(data.get("count", 0)):
            break
    for d in FOMC_DATES:
        if start.isoformat() <= d <= end.isoformat():
            found[(d, "FOMC")] = ({"release_name": "FOMC statement", "date": d},
                                  ("FOMC", "Fed interest-rate decision", "14:00", "high"))
    return found


# ------------------------------------------------------------ values
def _obs(get_json, api_key, series_id):
    data = get_json(f"{FRED}/series/observations", {
        "api_key": api_key, "file_type": "json", "series_id": series_id,
        "sort_order": "desc", "limit": 14,
    }) or {}
    out = []
    for o in data.get("observations", []):
        try:
            out.append((o["date"], float(o["value"])))
        except (KeyError, ValueError):
            continue  # "." = missing
    return out


def _period(d, kind):
    y, m, dd = (int(x) for x in d.split("-"))
    if kind == "pct_q":
        return f"Q{(m - 1) // 3 + 1} {y}"
    if kind in ("level_count",):
        return f"week of {calendar.month_abbr[m]} {dd}"
    if len(d) == 10 and kind == "pct" and dd != 1:
        return f"{calendar.month_abbr[m]} {dd}, {y}"
    return f"{calendar.month_abbr[m]} {y}"


def _k(v):
    return f"{v / 1000:.2f}M" if abs(v) >= 1000 else f"{v:,.0f}K"


def describe(kind, obs):
    """Return (latest text, prior text) for a list of (date, value) newest first."""
    v = [x[1] for x in obs]
    pct = lambda a, b: (a / b - 1) * 100
    if kind == "mom_yoy" and len(v) >= 13:
        latest = f"{pct(v[0], v[1]):+.1f}% m/m, {pct(v[0], v[12]):.1f}% y/y"
        prior = f"{pct(v[1], v[2]):+.1f}% m/m, {pct(v[1], v[13]):.1f}% y/y" if len(v) >= 14 else f"{pct(v[1], v[2]):+.1f}% m/m"
        return latest, prior
    if kind == "mom" and len(v) >= 3:
        return f"{pct(v[0], v[1]):+.1f}% m/m", f"{pct(v[1], v[2]):+.1f}% m/m"
    if kind == "change_k" and len(v) >= 3:
        return f"{v[0] - v[1]:+,.0f}K jobs", f"{v[1] - v[2]:+,.0f}K jobs"
    if kind == "pct" and len(v) >= 2:
        fmt = lambda x: f"{x:.2f}".rstrip("0").rstrip(".") + "%"
        return fmt(v[0]), fmt(v[1])
    if kind == "pct_q" and len(v) >= 2:
        return f"{v[0]:.1f}%", f"{v[1]:.1f}%"
    if kind == "level_k" and len(v) >= 2:
        return _k(v[0]), _k(v[1])
    if kind == "level_count" and len(v) >= 2:
        return f"{v[0] / 1000:,.0f}K", f"{v[1] / 1000:,.0f}K"
    return None, None


def latest_values(get_json, api_key, key):
    vals = []
    for series_id, kind, label in SERIES.get(key, []):
        try:
            obs = _obs(get_json, api_key, series_id)
        except Exception as e:  # one bad series shouldn't stop the rest
            log.warning("FRED series %s failed: %s", series_id, e)
            continue
        if not obs:
            continue
        latest, prior = describe(kind, obs)
        if latest:
            vals.append({"series": series_id, "label": label, "latest": latest,
                         "prior": prior, "period": _period(obs[0][0], kind)})
    if key == "FOMC" and len(vals) == 2:  # combine low/high into one range
        lo, hi = vals
        vals = [{"series": "DFEDTARL/DFEDTARU", "label": "Fed funds target range",
                 "latest": f"{lo['latest'].rstrip('%')}–{hi['latest']}",
                 "prior": None, "period": lo["period"]}]
    return vals


def summary_text(label, vals):
    if not vals:
        return label
    parts = [f"{v['label']} {v['latest']} ({v['period']})" for v in vals]
    return f"{label}. Latest: " + "; ".join(parts)


def collect(table, get_json, api_key, today, ttl_days=60, back_days=7, ahead_days=30):
    """Refresh ECONCAL for [today-back_days, today+ahead_days]. Returns number of items written."""
    start, end = today - timedelta(days=back_days), today + timedelta(days=ahead_days)
    cal = release_calendar(get_json, api_key, start, end)
    cache = {}
    expires = int(datetime.combine(end + timedelta(days=ttl_days), datetime.min.time()).timestamp())
    with table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as bw:
        for (d, key), (rel, (k, label, t, imp)) in sorted(cal.items()):
            if key not in cache:
                cache[key] = latest_values(get_json, api_key, key)
            vals = cache[key]
            bw.put_item(Item={
                "pk": "ECONCAL", "sk": f"{d}#{key}", "type": "economic",
                "date": d, "time_et": t, "key": key, "label": label, "importance": imp,
                "release_name": rel.get("release_name", label),
                "values": vals, "summary": summary_text(label, vals),
                "updated_at": datetime.now(timezone.utc).isoformat(), "ttl": expires,
            })
    log.info("econ calendar: %d releases (%s)", len(cal), ", ".join(sorted({k for _, k in cal})))
    return len(cal)
