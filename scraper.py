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
# {"192": "Orowin", "193": "Sikkaro", ...}
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


def _extract_rows(data) -> list:
    if isinstance(data, list):
        return data
    rows = data.get("rows", {}).get("data", [])
    if rows:
        return rows
    return data.get("totals", {}).get("data", [])


async def fetch_report(client: httpx.AsyncClient,
                       d_from: str, d_to: str) -> list:
    """group_by[]=brand — one row per brand."""
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
    return _extract_rows(r.json())


async def fetch_daily_breakdown(client: httpx.AsyncClient,
                               d_from: str, d_to: str) -> list:
    """group_by[]=day&group_by[]=brand — one row per day per brand."""
    params = [
        ("from",               d_from),
        ("to",                 d_to),
        ("async",              "false"),
        ("exchange_rates_date","2024-01-01"),
    ]
    for col in COLUMNS:
        params.append(("columns[]", col))
    params.append(("group_by[]", "day"))
    params.append(("group_by[]", "brand"))

    try:
        r = await api_get_with_retry(client, "/api/customer/v1/casino/report", params)
        print(f"daily {r.status_code}: {r.text[:400]}")
        if r.status_code != 200:
            return []
        return _extract_rows(r.json())
    except Exception as e:
        print(f"daily fetch error: {e}")
        return []


async def fetch_affiliate_breakdown(client: httpx.AsyncClient,
                                    d_from: str, d_to: str,
                                    brand_id=None) -> list:
    """group_by[]=partner — one row per webmaster."""
    params = [
        ("from",               d_from),
        ("to",                 d_to),
        ("async",              "false"),
        ("exchange_rates_date","2024-01-01"),
    ]
    for col in COLUMNS:
        params.append(("columns[]", col))
    params.append(("group_by[]", "brand"))
    params.append(("group_by[]", "partner"))

    try:
        r = await api_get_with_retry(client, "/api/customer/v1/casino/report", params)
        print(f"partner {r.status_code}: {r.text[:600]}")
        if r.status_code != 200:
            return []
        return _extract_rows(r.json())
    except Exception as e:
        print(f"partner fetch error: {e}")
        return []


# ── Parse row format ─────────────────────────────────────────────────────────

def parse_row(row: list) -> dict:
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


def brand_name(d: dict) -> str:
    bid = d.get("brand_id")
    if bid:
        name = BRAND_NAMES.get(str(bid))
        if name:
            return name
    return f"Brand {bid}" if bid else "Traffizy"


def affiliate_label(d: dict) -> str:
    for field in ("partner_name", "partner", "affiliate_name", "affiliate", "name", "login", "email"):
        val = d.get(field)
        if val and isinstance(val, str):
            return val
    aid = d.get("partner_id") or d.get("affiliate_id") or d.get("id")
    return f"#{aid}" if aid else "—"


# ── Format ────────────────────────────────────────────────────────────────────

def fmt_affiliates(rows: list, filter_brand_id=None) -> str:
    lines = []
    for row in rows:
        d = parse_row(row) if isinstance(row, list) else row
        if filter_brand_id is not None and d.get("brand_id") != filter_brand_id:
            continue
        m = row_to_metrics(d)
        if not (m["clicks"] or m["regs"] or m["ftd"]):
            continue
        label = affiliate_label(d)
        lines.append(
            f"<b>{label}</b>  "
            f"👆{m['clicks']} 📝{m['regs']} 💰{m['ftd']}  "
            f"C2R {m['c2r']:.1f}% R2D {m['r2d']:.1f}% C2D {m['c2d']:.1f}%"
        )
    if not lines:
        return ""
    return "👥 <b>По вебам:</b>\n" + "\n".join(lines)


def fmt_daily(rows: list, filter_brand_id) -> str:
    """Format 7-day dynamics for one brand as a compact table."""
    lines = []
    for row in rows:
        d = parse_row(row) if isinstance(row, list) else row
        if d.get("brand_id") != filter_brand_id:
            continue
        # date field: "2026-06-12T00:00:00.000Z" or "2026-06-12"
        date_val = d.get("date") or d.get("day") or ""
        date_str = str(date_val)[:10]  # "2026-06-12"
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            date_label = dt.strftime("%d.%m")
        except ValueError:
            date_label = date_str
        m = row_to_metrics(d)
        if not (m["clicks"] or m["regs"] or m["ftd"]):
            continue
        lines.append(
            f"{date_label}  "
            f"👆{m['clicks']} 📝{m['regs']} 💰{m['ftd']}  "
            f"C2R {m['c2r']:.1f}%  R2D {m['r2d']:.1f}%  C2D {m['c2d']:.1f}%"
        )
    if not lines:
        return ""
    return "\n".join(lines)


def fmt_report(m: dict, name: str, label: str, affiliates_str: str = "") -> str:
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
    if affiliates_str:
        lines.append("")
        lines.append(affiliates_str)
    return "\n".join(lines)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s))


# ── Unused helpers kept for reference ─────────────────────────────────────────

async def get_brand_map(client: httpx.AsyncClient) -> dict:
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


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    now_utc = datetime.now(timezone.utc)
    label   = f"сегодня ({now_utc.strftime('%d.%m, %H:%M')} UTC)"
    today   = date.today().isoformat()

    prev_state = load_state()
    new_state: dict = dict(prev_state)

    last_report_hour = prev_state.get("__last_report_hour__")
    last_daily_report = prev_state.get("__last_daily_report__")
    force = os.environ.get("FORCE_REPORT", "").lower() == "true"
    send_report = force or (last_report_hour != now_utc.strftime("%Y-%m-%d %H"))
    # Daily dynamics sent once per day at 10:00 UTC
    send_daily = force or (now_utc.hour == 10 and last_daily_report != today)

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

        # Fetch partner breakdown every run (for alerts) + hourly report
        aff_rows = await fetch_affiliate_breakdown(client, today, today)

        # Fetch 7-day dynamics once per day
        if send_daily:
            week_from = (date.today() - timedelta(days=6)).isoformat()
            daily_rows = await fetch_daily_breakdown(client, week_from, today)
        else:
            daily_rows = []

        for row in rows:
            d    = parse_row(row) if isinstance(row, list) else row
            bid  = d.get("brand_id")
            name = brand_name(d)
            m    = row_to_metrics(d)
            key  = str(bid or name)

            print(f"{name}: {m}")

            if send_report:
                aff_str = fmt_affiliates(aff_rows, filter_brand_id=bid)
                await tg(client, fmt_report(m, name, label, aff_str))

            if send_daily and daily_rows:
                daily_str = fmt_daily(daily_rows, filter_brand_id=bid)
                if daily_str:
                    await tg(client,
                        f"📅 <b>{name} — динамика 7 дней</b>\n\n{daily_str}")

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

        # Per-partner alert check (every run)
        for row in aff_rows:
            d   = parse_row(row) if isinstance(row, list) else row
            bid = d.get("brand_id")
            pid = d.get("partner_id") or d.get("affiliate_id") or d.get("id")
            if not pid:
                continue
            pname  = affiliate_label(d)
            bname  = brand_name(d)
            m      = row_to_metrics(d)
            pkey   = f"partner_{bid}_{pid}"
            prev_p = prev_state.get(pkey, {})
            if prev_p:
                alerts = []
                for metric, lbl in (("c2r", "C2R"), ("c2d", "C2D"), ("r2d", "R2D")):
                    drop = prev_p.get(metric, 0) - m.get(metric, 0)
                    if prev_p.get(metric, 0) > 0 and drop >= ALERT_DROP_PP:
                        alerts.append(
                            f"📉 <b>{lbl}</b>: {prev_p[metric]:.1f}% → "
                            f"{m[metric]:.1f}% (−{drop:.1f}pp)")
                if alerts:
                    await tg(client,
                        f"⚠️ <b>{pname} ({bname}) — падение конверсии!</b>\n\n"
                        + "\n".join(alerts))
            new_state[pkey] = m

        if send_report:
            new_state["__last_report_hour__"] = now_utc.strftime("%Y-%m-%d %H")
        if send_daily:
            new_state["__last_daily_report__"] = today

    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
