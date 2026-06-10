"""
Traffizy → Telegram  |  Customer Operator API + STATISTIC_TOKEN
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

HOST     = "affiliate.traffizy.partners"
BASE_URL = f"https://{HOST}"
STATE_FILE = Path("state.json")
ALERT_DROP_PP = 3.0

HEADERS = {
    "Accept":        "application/json",
    "Host":          HOST,
    "Authorization": STATISTIC_TOKEN,
}

# Колонки для /casino/report
REPORT_COLS = [
    "first_deposits_count",
    "first_deposits_sum",
    "deposits_count",
    "deposits_sum",
    "cashouts_count",
    "depositing_players_count",
    "ngr",
]


async def tg(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


# ── API ───────────────────────────────────────────────────────────────────────

async def api_get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(
        f"{BASE_URL}{path}",
        headers=HEADERS,
        params=params,
        timeout=30,
    )
    print(f"GET {path} {params} → {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


async def get_brands(client: httpx.AsyncClient) -> list[dict]:
    """Fetch brand list. Tries both Customer Operator and Partner endpoints."""
    for path in ["/api/customer/v1/casino/brands",
                 "/api/client/partner/brands"]:
        try:
            data = await api_get(client, path, {})
            items = data if isinstance(data, list) else (
                data.get("data") or data.get("brands") or []
            )
            if items:
                return [{"id": b["id"], "name": b["name"]} for b in items]
        except Exception as e:
            print(f"{path} failed: {e}")
    return []


async def fetch_traffic(client: httpx.AsyncClient,
                        d_from: str, d_to: str,
                        brand_id: int | None = None) -> dict:
    params: dict = {"from": d_from, "to": d_to, "group_by": "total"}
    if brand_id:
        params["brand_id"] = brand_id
    return await api_get(client, "/api/customer/v1/casino/traffic_report", params)


async def fetch_report(client: httpx.AsyncClient,
                       d_from: str, d_to: str,
                       brand_id: int | None = None) -> dict:
    params: dict = {
        "from": d_from,
        "to":   d_to,
        "group_by": "total",
    }
    for col in REPORT_COLS:
        params.setdefault("columns[]", [])
    # httpx handles list params correctly
    params["columns[]"] = REPORT_COLS
    if brand_id:
        params["brand_id"] = brand_id
    return await api_get(client, "/api/customer/v1/casino/report", params)


# ── Parse ─────────────────────────────────────────────────────────────────────

def parse_totals(resp) -> dict:
    out: dict = {}
    if isinstance(resp, list):
        for row in resp:
            if isinstance(row, dict):
                for k, v in row.items():
                    if isinstance(v, (int, float)):
                        out[k] = out.get(k, 0) + v
        return out
    td = resp.get("totals", {}).get("data", [])
    if td and isinstance(td[0], dict):
        return {k: v for k, v in td[0].items() if isinstance(v, (int, float))}
    for row in resp.get("rows", {}).get("data", []):
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    out[k] = out.get(k, 0) + v
    return out


def calc(traffic: dict, report: dict) -> dict:
    raw = {**traffic, **report}
    clicks = raw.get("visits") or raw.get("clicks") or raw.get("click_count") or 0
    unique = raw.get("unique_visits") or raw.get("unique_clicks") or raw.get("uniq_clicks") or 0
    regs   = raw.get("registrations") or raw.get("reg_count") or 0
    ftd    = raw.get("first_deposits_count") or raw.get("ftd_count") or raw.get("ftd") or 0
    c2r = round(regs / clicks * 100, 2) if clicks else 0.0
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0
    return {"clicks": clicks, "unique": unique, "regs": regs,
            "ftd": ftd, "c2r": c2r, "r2d": r2d}


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


# ── Per brand ─────────────────────────────────────────────────────────────────

async def process_brand(client, brand, d_from, d_to, label, prev_state) -> dict:
    bid, name = brand["id"], brand["name"]
    t_raw, r_raw = {}, {}

    try:
        t_raw = parse_totals(await fetch_traffic(client, d_from, d_to, bid))
    except httpx.HTTPStatusError as e:
        print(f"traffic error {name}: {e.response.status_code}")

    await asyncio.sleep(2)

    try:
        r_raw = parse_totals(await fetch_report(client, d_from, d_to, bid))
    except httpx.HTTPStatusError as e:
        print(f"report error {name}: {e.response.status_code}")

    m = calc(t_raw, r_raw)
    print(f"{name}: {m}")
    await tg(client, fmt_report(m, name, label))

    prev = prev_state.get(str(bid), {})
    if prev:
        alerts = []
        for key, lbl in (("c2r", "C2R"), ("r2d", "R2D")):
            drop = prev.get(key, 0) - m.get(key, 0)
            if prev.get(key, 0) > 0 and drop >= ALERT_DROP_PP:
                alerts.append(
                    f"📉 <b>{lbl}</b>: {prev[key]:.1f}% → {m[key]:.1f}% (−{drop:.1f}pp)")
        if alerts:
            await tg(client, f"⚠️ <b>{name} — падение конверсии!</b>\n\n" + "\n".join(alerts))
    return m


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    label   = f"сегодня ({now_msk.strftime('%d.%m, %H:%M')} МСК)"
    today   = date.today().isoformat()
    d_from  = (date.today() - timedelta(days=1)).isoformat()

    prev_state = load_state()
    new_state: dict = {}

    async with httpx.AsyncClient() as client:
        # Try to get brand list
        brands = await get_brands(client)
        print(f"Brands found: {brands}")

        if not brands:
            # No brand list → send single combined report
            await asyncio.sleep(1)
            t_raw = parse_totals(await fetch_traffic(client, d_from, today))
            await asyncio.sleep(2)
            r_raw = parse_totals(await fetch_report(client, d_from, today))
            m = calc(t_raw, r_raw)
            await tg(client, fmt_report(m, "Traffizy", label))
            new_state["all"] = m
        else:
            for brand in brands:
                await asyncio.sleep(2)
                m = await process_brand(client, brand, d_from, today,
                                        label, prev_state)
                new_state[str(brand["id"])] = m

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
