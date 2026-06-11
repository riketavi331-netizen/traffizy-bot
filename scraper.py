"""
Traffizy → Telegram  |  Customer Operator API + STATISTIC_TOKEN
Hourly report per brand + conversion drop alerts
"""
import os
import asyncio
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import httpx

STATISTIC_TOKEN = os.environ["STATISTIC_TOKEN"]
TG_TOKEN        = os.environ["TELEGRAM_TOKEN"]
TG_CHAT         = os.environ["TELEGRAM_CHAT_ID"]
# {"192": "Orowin", "193": "Sikkaro", ...} — добавляйте новые бренды сюда
BRAND_NAMES: dict = json.loads(os.environ.get("BRAND_NAMES", "{}"))

HOST       = "affiliate.traffizy.partners"
BASE_URL   = f"https://{HOST}"
STATE_FILE = Path("state.json")
ALERT_DROP_PP = 3.0

HEADERS = {
    "Accept":        "application/json",
    "Content-Type":  "application/json",
    "Host":          HOST,
    "Authorization": STATISTIC_TOKEN,
}

COLUMNS = [
    "visits_count",
    "registrations_count",
    "first_deposits_count",
    "first_deposits_sum",
    "deposits_count",
    "deposits_sum",
    "cashouts_count",
    "ngr",
]


# ── Telegram ──────────────────────────────────────────────────────────────────

async def tg(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


# ── API ───────────────────────────────────────────────────────────────────────

async def api_get_with_retry(client: httpx.AsyncClient,
                             path: str, params: list) -> httpx.Response:
    """GET with automatic retry on 429 (wait 10s and try once more)."""
    for attempt in range(2):
        r = await client.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params,
            timeout=30,
        )
        print(f"GET {path} → {r.status_code}")
        if r.status_code == 429 and attempt == 0:
            print("Rate limited, waiting 10s...")
            await asyncio.sleep(10)
            continue
        return r
    return r


async def fetch_report(client: httpx.AsyncClient,
                       d_from: str, d_to: str) -> list[dict]:
    """
    GET /api/customer/v1/casino/report
    group_by[]=brand  →  one row per brand
    """
    params = [
        ("from",               d_from),
        ("to",                 d_to),
        ("async",              "false"),
        ("exchange_rates_date","2024-01-01"),
    ]
    for col in COLUMNS:
        params.append(("columns[]", col))
    params.append(("group_by[]", "brand"))

    r = await api_get_with_retry(client, "/api/customer/v1/casino/report", params)
    print(f"report body: {r.text[:600]}")
    r.raise_for_status()

    data = r.json()
    if isinstance(data, list):
        return data
    rows = data.get("rows", {}).get("data", [])
    if rows:
        return rows
    totals = data.get("totals", {}).get("data", [])
    return totals if totals else []


async def fetch_hourly_breakdown(client: httpx.AsyncClient,
                                  d_from: str, d_to: str,
                                  brand_id=None) -> list[dict]:
    """
    GET /api/customer/v1/casino/report
    group_by[]=hour  →  one row per hour
    """
    params = [
        ("from",               d_from),
        ("to",                 d_to),
        ("async",              "false"),
        ("exchange_rates_date","2024-01-01"),
    ]
    for col in COLUMNS:
        params.append(("columns[]", col))
    params.append(("group_by[]", "hour"))
    if brand_id is not None:
        params.append(("brand_id[]", str(brand_id)))

    try:
        r = await api_get_with_retry(client, "/api/customer/v1/casino/report", params)
        print(f"hourly body: {r.text[:600]}")
        if r.status_code != 200:
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        rows = data.get("rows", {}).get("data", [])
        return rows if rows else []
    except Exception as e:
        print(f"hourly fetch error: {e}")
        return []


# ── Parse row format ─────────────────────────────────────────────────────────
# API returns rows as list of {name, value, type} objects, not plain dicts.
# Money values: {"currency":"USD","amount":"55.31","amount_cents":"5531.0"}

def parse_row(row: list) -> dict:
    """Convert [{name, value, type}, ...] → flat dict with numeric values."""
    out = {}
    for item in row:
        key = item["name"]
        val = item["value"]
        if isinstance(val, dict) and "amount" in val:
            try:
                out[key] = float(val["amount"])
            except (ValueError, TypeError):
                out[key] = 0.0
        elif isinstance(val, (int, float)):
            out[key] = val
        elif isinstance(val, str):
            out[key] = val
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────

def row_to_metrics(d: dict) -> dict:
    clicks = d.get("visits_count") or 0
    unique = d.get("unique_visits_count") or 0
    regs   = d.get("registrations_count") or 0
    ftd    = d.get("first_deposits_count") or 0
    c2r = round(regs / clicks * 100, 2) if clicks else 0.0
    c2d = round(ftd  / clicks * 100, 2) if clicks else 0.0
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0
    return {"clicks": clicks, "unique": unique, "regs": regs,
            "ftd": ftd, "c2r": c2r, "c2d": c2d, "r2d": r2d}


def brand_name(d: dict, brand_map: dict) -> str:
    bid = d.get("brand_id")
    # Check env BRAND_NAMES first, then API-fetched map
    if bid:
        name = BRAND_NAMES.get(str(bid)) or brand_map.get(bid)
        if name:
            return name
    return f"Brand {bid}" if bid else "Traffizy"


def extract_hour_label(d: dict) -> str:
    """Extract HH:00 string from a row dict. Tries common field names."""
    for field in ("hour", "date", "created_at", "time", "datetime"):
        val = d.get(field)
        if not val:
            continue
        s = str(val)
        # "2024-01-01T12:00:00" or "2024-01-01 12:00:00"
        if "T" in s:
            s = s.split("T")[1]
        elif " " in s and len(s) > 10:
            s = s.split(" ")[1]
        # "12:00:00" → "12:00"
        if ":" in s:
            return s[:5]
        # plain integer hour "12"
        if s.isdigit():
            return f"{int(s):02d}:00"
    return ""


# ── Format ────────────────────────────────────────────────────────────────────

def fmt_hourly(rows: list) -> str:
    """Format hourly breakdown as compact table. Returns empty string if no data."""
    lines = []
    for row in rows:
        d = parse_row(row) if isinstance(row, list) else row
        hour = extract_hour_label(d)
        m = row_to_metrics(d)
        if not (m["clicks"] or m["regs"] or m["ftd"]):
            continue
        label = f"{hour}  " if hour else ""
        lines.append(
            f"{label}C2R <b>{m['c2r']:.1f}%</b>  "
            f"R2D <b>{m['r2d']:.1f}%</b>  "
            f"C2D <b>{m['c2d']:.1f}%</b>"
        )
    if not lines:
        return ""
    return "📈 <b>По часам (UTC):</b>\n" + "\n".join(lines)


def fmt_report(m: dict, name: str, label: str, hourly_str: str = "") -> str:
    lines = [f"📊 <b>{name} — {label}</b>", ""]
    lines.append(f"👆 All clicks: <b>{m['clicks']:,}</b>")
    if m["unique"]:
        lines.append(f"🎯 Unique clicks: <b>{m['unique']:,}</b>")
    lines.append(f"📝 Registrations: <b>{m['regs']:,}</b>")
    lines.append(f"🆕 FTDs: <b>{m['ftd']:,}</b>")
    lines.append("──────────────")
    lines.append(f"C2R: <b>{m['c2r']:.1f}%</b>  (click→reg)")
    lines.append(f"C2D: <b>{m['c2d']:.1f}%</b>  (click→FTD)")
    lines.append(f"R2D: <b>{m['r2d']:.1f}%</b>  (reg→FTD)")
    if not m["clicks"] and not m["regs"]:
        lines.append("\n<i>Нет данных за период</i>")
    if hourly_str:
        lines.append("")
        lines.append(hourly_str)
    return "\n".join(lines)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s))


# ── Main ──────────────────────────────────────────────────────────────────────

async def fetch_traffic_report(client: httpx.AsyncClient,
                               d_from: str, d_to: str) -> dict:
    params = [("from", d_from), ("to", d_to)]
    try:
        r = await client.get(
            f"{BASE_URL}/api/customer/v1/casino/traffic_report",
            headers=HEADERS, params=params, timeout=30,
        )
        print(f"traffic_report → {r.status_code}: {r.text[:500]}")
        if r.status_code != 200:
            return {}
        data = r.json()
        rows = data.get("rows", {}).get("data", []) if isinstance(data, dict) else data
        if not rows:
            return {}
        row = rows[0]
        return parse_row(row) if isinstance(row, list) else row
    except Exception as e:
        print(f"traffic_report error: {e}")
        return {}


async def get_brand_map(client: httpx.AsyncClient) -> dict:
    """Returns {brand_id: brand_name} mapping."""
    try:
        r = await client.get(
            f"{BASE_URL}/api/client/partner/brands",
            headers=HEADERS, timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            return {b["id"]: b["name"] for b in items if "id" in b and "name" in b}
    except Exception as e:
        print(f"brand_map error: {e}")
    return {}


async def main():
    now_utc = datetime.now(timezone.utc)
    label   = f"сегодня ({now_utc.strftime('%d.%m, %H:%M')} UTC)"
    today   = date.today().isoformat()

    prev_state = load_state()
    new_state: dict = dict(prev_state)

    last_report_hour = prev_state.get("__last_report_hour__")
    send_report = (last_report_hour != now_utc.strftime("%Y-%m-%d %H"))

    async with httpx.AsyncClient() as client:
        try:
            rows = await fetch_report(client, today, today)
        except httpx.HTTPStatusError as e:
            await tg(client,
                f"❌ <b>Traffizy API error {e.response.status_code}</b>\n"
                f"<code>{e.response.text[:300]}</code>")
            return

        print(f"Rows received: {len(rows)}")

        if not rows:
            if send_report:
                await tg(client, f"📊 <b>Traffizy — {label}</b>\n\n<i>Нет данных за сегодня</i>")
            return

        for row in rows:
            d    = parse_row(row) if isinstance(row, list) else row
            bid  = d.get("brand_id")
            name = brand_name(d, {})
            m    = row_to_metrics(d)
            key  = str(bid or name)

            print(f"{name}: {m}")

            if send_report:
                hourly_rows = await fetch_hourly_breakdown(client, today, today, brand_id=bid)
                hourly_str  = fmt_hourly(hourly_rows)
                await tg(client, fmt_report(m, name, label, hourly_str))

            prev = prev_state.get(key, {})
            if prev:
                alerts = []
                for metric, lbl in (("c2r", "C2R"), ("c2d", "C2D"), ("r2d", "R2D")):
                    drop = prev.get(metric, 0) - m.get(metric, 0)
                    if prev.get(metric, 0) > 0 and drop >= ALERT_DROP_PP:
                        alerts.append(
                            f"📉 <b>{lbl}</b>: {prev[metric]:.1f}% → "
                            f"{m[metric]:.1f}% (−{drop:.1f}pp)")
                if alerts:
                    await tg(client,
                        f"⚠️ <b>{name} — падение конверсии!</b>\n\n"
                        + "\n".join(alerts))

            new_state[key] = m

        if send_report:
            new_state["__last_report_hour__"] = now_utc.strftime("%Y-%m-%d %H")

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
