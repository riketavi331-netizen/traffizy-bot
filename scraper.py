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
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0
    return {"clicks": clicks, "unique": unique, "regs": regs,
            "ftd": ftd, "c2r": c2r, "r2d": r2d}


def brand_name(d: dict, brand_map: dict) -> str:
    bid = d.get("brand_id")
    # Check env BRAND_NAMES first, then API-fetched map
    if bid:
        name = BRAND_NAMES.get(str(bid)) or brand_map.get(bid)
        if name:
            return name
    return f"Brand {bid}" if bid else "Traffizy"


# ── Format ────────────────────────────────────────────────────────────────────

def fmt_report(m: dict, name: str, label: str) -> str:
    lines = [f"📊 <b>{name} — {label}</b>", ""]
    lines.append(f"👆 All clicks: <b>{m['clicks']:,}</b>")
    if m["unique"]:
        lines.append(f"🎯 Unique clicks: <b>{m['unique']:,}</b>")
    lines.append(f"📝 Registrations: <b>{m['regs']:,}</b>")
    lines.append(f"🆕 FTDs: <b>{m['ftd']:,}</b>")
    lines.append("──────────────")
    lines.append(f"C2R: <b>{m['c2r']:.1f}%</b>  (click→reg)")
    lines.append(f"R2D: <b>{m['r2d']:.1f}%</b>  (reg→FTD)")
    if not m["clicks"] and not m["regs"]:
        lines.append("\n<i>Нет данных за период</i>")
    return "\n".join(lines)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s))


# ── Main ──────────────────────────────────────────────────────────────────────

async def fetch_traffic_report(client: httpx.AsyncClient,
                               d_from: str, d_to: str) -> dict:
    """
    GET /api/customer/v1/casino/traffic_report  group_by[]=brand
    Returns {brand_id: unique_clicks} mapping.
    """
    params = [
        ("from",       d_from),
        ("to",         d_to),
        ("group_by[]", "brand"),
    ]
    try:
        r = await client.get(
            f"{BASE_URL}/api/customer/v1/casino/traffic_report",
            headers=HEADERS, params=params, timeout=30,
        )
        print(f"traffic_report → {r.status_code}: {r.text[:400]}")
        if r.status_code != 200:
            return {}
        data = r.json()
        rows = data if isinstance(data, list) else (
            data.get("rows", {}).get("data", []) or
            data.get("totals", {}).get("data", [])
        )
        result = {}
        for row in rows:
            d = parse_row(row) if isinstance(row, list) else row
            bid = d.get("brand_id")
            if bid:
                result[bid] = d.get("unique_visits_count") or d.get("unique_clicks") or 0
        return result
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
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    label   = f"сегодня ({now_msk.strftime('%d.%m, %H:%M')} МСК)"
    today   = date.today().isoformat()

    prev_state = load_state()
    new_state: dict = {}

    async with httpx.AsyncClient() as client:
        # Get unique clicks per brand (best effort, skip on 429)
        unique_map = await fetch_traffic_report(client, today, today)
        print(f"Unique clicks map: {unique_map}")

        await asyncio.sleep(3)

        try:
            rows = await fetch_report(client, today, today)
        except httpx.HTTPStatusError as e:
            await tg(client,
                f"❌ <b>Traffizy API error {e.response.status_code}</b>\n"
                f"<code>{e.response.text[:300]}</code>")
            return

        print(f"Rows received: {len(rows)}")

        if not rows:
            await tg(client, f"📊 <b>Traffizy — {label}</b>\n\n<i>Нет данных за сегодня</i>")
            return

        for row in rows:
            # Row is a list of {name, value, type} — convert to dict first
            d    = parse_row(row) if isinstance(row, list) else row
            bid  = d.get("brand_id")
            name = brand_name(d, brand_map)
            # Inject unique clicks from traffic_report
            d["unique_visits_count"] = unique_map.get(bid, 0)
            m    = row_to_metrics(d)
            key  = str(bid or name)

            print(f"{name}: {m}")
            await tg(client, fmt_report(m, name, label))

            # Conversion alert
            prev = prev_state.get(key, {})
            if prev:
                alerts = []
                for metric, lbl in (("c2r", "C2R"), ("r2d", "R2D")):
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

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
