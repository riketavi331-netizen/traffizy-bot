"""
Traffizy → Telegram  |  Frontend Partner API
- Hourly stats per brand (Sikkaro, Orowin, + new ones automatically)
- Conversion alert when C2R or R2D drops ≥ 3 percentage points
"""
import os
import asyncio
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import httpx

EMAIL    = os.environ["TRAFFIZY_EMAIL"]
PASSWORD = os.environ["TRAFFIZY_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]

HOST     = "affiliate.traffizy.partners"
BASE_URL = f"https://{HOST}"
STATE_FILE = Path("state.json")

ALERT_DROP_PP = 3.0   # alert when C2R or R2D drops by this many percentage points

HEADERS = {
    "Accept":       "application/json",
    "Content-Type": "application/json",
    "Host":         HOST,
}


# ── Telegram ──────────────────────────────────────────────────────────────────

async def tg(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

async def sign_in(client: httpx.AsyncClient) -> None:
    """Login and store session cookie in client.cookies."""
    r = await client.post(
        f"{BASE_URL}/api/client/partner/sign_in",
        headers=HEADERS,
        json={"partner_user": {"email": EMAIL, "password": PASSWORD, "otp_attempt": None}},
        timeout=30,
    )
    print(f"sign_in → {r.status_code}")
    r.raise_for_status()


# ── Brands ────────────────────────────────────────────────────────────────────

async def get_brands(client: httpx.AsyncClient) -> list[dict]:
    """Returns list of {id, name} for all brands available to this partner."""
    r = await client.get(
        f"{BASE_URL}/api/client/partner/brands",
        headers=HEADERS,
        timeout=30,
    )
    print(f"brands → {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    data = r.json()
    # Accept both list and wrapped response
    if isinstance(data, list):
        return [{"id": b["id"], "name": b["name"]} for b in data]
    if isinstance(data, dict):
        items = data.get("data") or data.get("brands") or []
        return [{"id": b["id"], "name": b["name"]} for b in items]
    return []


# ── Reports ───────────────────────────────────────────────────────────────────

async def fetch_traffic(client: httpx.AsyncClient, d_from: str, d_to: str,
                        brand_id: int) -> dict:
    params = {"from": d_from, "to": d_to, "brand_id": brand_id}
    r = await client.get(
        f"{BASE_URL}/api/client/partner/traffic_report",
        headers=HEADERS, params=params, timeout=30,
    )
    print(f"traffic brand={brand_id} → {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


async def fetch_report(client: httpx.AsyncClient, d_from: str, d_to: str,
                       brand_id: int) -> dict:
    params = {"from": d_from, "to": d_to, "brand_id": brand_id}
    r = await client.get(
        f"{BASE_URL}/api/client/partner/report",
        headers=HEADERS, params=params, timeout=30,
    )
    print(f"report brand={brand_id} → {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


# ── Parse response ────────────────────────────────────────────────────────────

def parse_totals(resp) -> dict:
    """Extract numeric values from totals.data[0] or by summing rows.data."""
    out: dict = {}
    if isinstance(resp, list):
        # Some endpoints return a plain list
        for row in resp:
            if isinstance(row, dict):
                for k, v in row.items():
                    if isinstance(v, (int, float)):
                        out[k] = out.get(k, 0) + v
        return out

    # Wrapped response: {totals: {data: [...]}, rows: {data: [...]}}
    td = resp.get("totals", {}).get("data", [])
    if td and isinstance(td[0], dict):
        for k, v in td[0].items():
            if isinstance(v, (int, float)):
                out[k] = v
        return out

    for row in resp.get("rows", {}).get("data", []):
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    out[k] = out.get(k, 0) + v
    return out


# ── Metrics ───────────────────────────────────────────────────────────────────

def calc(traffic: dict, report: dict) -> dict:
    raw = {**traffic, **report}

    clicks = (raw.get("visits") or raw.get("clicks") or
              raw.get("all_clicks") or raw.get("click_count") or 0)
    unique = (raw.get("unique_visits") or raw.get("unique_clicks") or
              raw.get("uniq_clicks") or raw.get("unique_click_count") or 0)
    regs   = (raw.get("registrations") or raw.get("reg_count") or
              raw.get("registration_count") or 0)
    ftd    = (raw.get("first_deposits_count") or raw.get("ftd_count") or
              raw.get("ftd") or raw.get("first_deposit_count") or 0)

    c2r = round(regs / clicks * 100, 2) if clicks else 0.0
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0

    return {"clicks": clicks, "unique": unique, "regs": regs,
            "ftd": ftd, "c2r": c2r, "r2d": r2d}


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_report(m: dict, brand_name: str, label: str) -> str:
    lines = [f"📊 <b>{brand_name} — {label}</b>", ""]
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

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


# ── Per-brand logic ───────────────────────────────────────────────────────────

async def process_brand(client: httpx.AsyncClient, brand: dict,
                        d_from: str, d_to: str, label: str,
                        prev_state: dict) -> dict:
    bid  = brand["id"]
    name = brand["name"]

    traffic_raw, report_raw = {}, {}

    try:
        traffic_raw = parse_totals(await fetch_traffic(client, d_from, d_to, bid))
    except httpx.HTTPStatusError as e:
        print(f"traffic error for {name}: {e.response.status_code}")

    await asyncio.sleep(1)

    try:
        report_raw = parse_totals(await fetch_report(client, d_from, d_to, bid))
    except httpx.HTTPStatusError as e:
        print(f"report error for {name}: {e.response.status_code}")

    m = calc(traffic_raw, report_raw)
    print(f"{name} metrics: {m}")

    await tg(client, fmt_report(m, name, label))

    # Conversion alert
    prev = prev_state.get(str(bid), {})
    if prev:
        alerts = []
        for key, lbl in (("c2r", "C2R"), ("r2d", "R2D")):
            old, new = prev.get(key, 0), m.get(key, 0)
            drop = old - new
            if old > 0 and drop >= ALERT_DROP_PP:
                alerts.append(
                    f"📉 <b>{lbl}</b>: {old:.1f}% → {new:.1f}% (−{drop:.1f}pp)"
                )
        if alerts:
            await tg(client,
                f"⚠️ <b>{name} — падение конверсии!</b>\n\n" + "\n".join(alerts))

    return m


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    label   = f"сегодня ({now_msk.strftime('%d.%m, %H:%M')} МСК)"
    today   = date.today().isoformat()
    d_from  = (date.today() - timedelta(days=1)).isoformat()  # yesterday + today

    prev_state = load_state()
    new_state: dict = {}

    async with httpx.AsyncClient() as client:
        # 1. Login
        try:
            await sign_in(client)
        except httpx.HTTPStatusError as e:
            await tg(client, f"❌ <b>Ошибка входа в Traffizy</b>\n<code>{e.response.text[:200]}</code>")
            return

        # 2. Get brands
        try:
            brands = await get_brands(client)
        except httpx.HTTPStatusError as e:
            await tg(client, f"❌ <b>Ошибка получения брендов</b>\n<code>{e.response.text[:200]}</code>")
            return

        print(f"Brands: {brands}")

        if not brands:
            await tg(client, "⚠️ Нет доступных брендов в аккаунте")
            return

        # 3. Process each brand
        for brand in brands:
            await asyncio.sleep(2)  # rate limit between brands
            m = await process_brand(client, brand, d_from, today,
                                    label, prev_state)
            new_state[str(brand["id"])] = m

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
