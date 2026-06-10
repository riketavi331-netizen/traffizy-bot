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
    "unique_visits_count",
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

async def fetch_report(client: httpx.AsyncClient,
                       d_from: str, d_to: str) -> list[dict]:
    """
    GET /api/customer/v1/casino/report
    group_by[]=brand  →  one row per brand
    Returns list of row dicts.
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

    r = await client.get(
        f"{BASE_URL}/api/customer/v1/casino/report",
        headers=HEADERS,
        params=params,
        timeout=30,
    )
    print(f"report → {r.status_code}: {r.text[:600]}")
    r.raise_for_status()

    data = r.json()
    # Extract rows
    if isinstance(data, list):
        return data
    rows = data.get("rows", {}).get("data", [])
    if rows:
        return rows
    # Maybe totals only
    totals = data.get("totals", {}).get("data", [])
    return totals if totals else []


# ── Metrics ───────────────────────────────────────────────────────────────────

def row_to_metrics(row: dict) -> dict:
    clicks  = row.get("visits_count") or row.get("visits") or 0
    unique  = row.get("unique_visits_count") or row.get("unique_visits") or 0
    regs    = row.get("registrations_count") or row.get("registrations") or 0
    ftd     = row.get("first_deposits_count") or row.get("ftd_count") or 0
    c2r = round(regs / clicks * 100, 2) if clicks else 0.0
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0
    return {"clicks": clicks, "unique": unique, "regs": regs,
            "ftd": ftd, "c2r": c2r, "r2d": r2d}


def brand_name(row: dict) -> str:
    """Extract brand name from a grouped row."""
    for key in ("brand_name", "brand", "name", "title"):
        if isinstance(row.get(key), str) and row[key]:
            return row[key]
    if isinstance(row.get("brand"), dict):
        return row["brand"].get("name", "Unknown")
    return "All brands"


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

async def main():
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    label   = f"сегодня ({now_msk.strftime('%d.%m, %H:%M')} МСК)"
    today   = date.today().isoformat()

    prev_state = load_state()
    new_state: dict = {}

    async with httpx.AsyncClient() as client:
        try:
            rows = await fetch_report(client, today, today)
        except httpx.HTTPStatusError as e:
            await tg(client,
                f"❌ <b>Traffizy API error {e.response.status_code}</b>\n"
                f"<code>{e.response.text[:300]}</code>")
            return

        print(f"Rows received: {len(rows)}")
        for row in rows:
            print(f"  row: {row}")

        if not rows:
            await tg(client, f"📊 <b>Traffizy — {label}</b>\n\n<i>Нет данных за сегодня</i>")
            return

        for row in rows:
            name = brand_name(row)
            m    = row_to_metrics(row)
            key  = name.lower().replace(" ", "_")

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
                            f"{m[metric]:.1f}% (−{drop:.1f}pp)"
                        )
                if alerts:
                    await tg(client,
                        f"⚠️ <b>{name} — падение конверсии!</b>\n\n"
                        + "\n".join(alerts))

            new_state[key] = m

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
