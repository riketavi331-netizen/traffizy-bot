"""
Traffizy → Telegram  |  hourly stats + conversion alerts
"""
import os
import asyncio
import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import httpx

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATISTIC_TOKEN  = os.environ["STATISTIC_TOKEN"]

BASE_URL   = "https://affiliate.traffizy.partners"
STATE_FILE = Path("state.json")   # кешируется между запусками в GitHub Actions

HEADERS = {
    "Accept":        "application/json",
    "Authorization": STATISTIC_TOKEN,
}

# Порог алерта: падение конверсии на X процентных пунктов
ALERT_THRESHOLD_PP = 3.0


# ── Telegram ──────────────────────────────────────────────────────────────────

async def tg(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


# ── API calls ─────────────────────────────────────────────────────────────────

async def get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    r = await client.get(
        f"{BASE_URL}{path}",
        headers=HEADERS,
        params=params,
        timeout=30,
    )
    print(f"GET {path} {params} → {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


def parse_totals(resp: dict) -> dict:
    """Extract numeric totals from API response."""
    stats: dict = {}
    # Try totals.data[0] first
    td = resp.get("totals", {}).get("data", [])
    if td and isinstance(td[0], dict):
        for k, v in td[0].items():
            if isinstance(v, (int, float)):
                stats[k] = v
        return stats
    # Fallback: sum rows.data
    for row in resp.get("rows", {}).get("data", []):
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + v
    return stats


async def fetch_stats(client: httpx.AsyncClient, d_from: str, d_to: str) -> dict:
    stats: dict = {}

    # traffic_report → clicks, unique clicks, registrations
    try:
        data = await get(client, "/api/customer/v1/casino/traffic_report",
                         {"from": d_from, "to": d_to})
        stats.update(parse_totals(data))
    except httpx.HTTPStatusError as e:
        print(f"traffic_report error {e.response.status_code}")

    await asyncio.sleep(2)  # avoid rate limiting

    # casino/report → FTDs
    try:
        data = await get(client, "/api/customer/v1/casino/report",
                         {"from": d_from, "to": d_to})
        stats.update(parse_totals(data))
    except httpx.HTTPStatusError as e:
        print(f"report error {e.response.status_code}")

    return stats


# ── Metrics calculation ───────────────────────────────────────────────────────

def calc_metrics(stats: dict) -> dict:
    """
    Build the final metrics dict including calculated conversion rates.
    Field name mapping covers common variants returned by Traffizy.
    """
    clicks      = (stats.get("visits") or stats.get("clicks") or
                   stats.get("all_clicks") or 0)
    unique      = (stats.get("unique_visits") or stats.get("unique_clicks") or
                   stats.get("uniq_clicks") or 0)
    regs        = (stats.get("registrations") or stats.get("reg_count") or 0)
    ftd         = (stats.get("first_deposits_count") or stats.get("ftd_count") or
                   stats.get("ftd") or 0)

    c2r = round(regs / clicks * 100, 2) if clicks else 0.0
    r2d = round(ftd  / regs  * 100, 2) if regs  else 0.0

    return {
        "clicks":  clicks,
        "unique":  unique,
        "regs":    regs,
        "ftd":     ftd,
        "c2r":     c2r,
        "r2d":     r2d,
        "_raw":    stats,   # сохраняем raw для отладки
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_report(m: dict, label: str) -> str:
    lines = [f"📊 <b>Traffizy — {label}</b>", ""]
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


def check_alerts(prev: dict, curr: dict) -> str | None:
    lines = []
    for key, label in (("c2r", "C2R"), ("r2d", "R2D")):
        old = prev.get(key, 0)
        new = curr.get(key, 0)
        drop = old - new
        if old > 0 and drop >= ALERT_THRESHOLD_PP:
            lines.append(
                f"📉 <b>{label}</b>: {old:.1f}% → {new:.1f}% "
                f"(<b>−{drop:.1f}pp</b>)"
            )
    return "\n".join(lines) if lines else None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(m: dict):
    # Don't save raw API dump to state
    to_save = {k: v for k, v in m.items() if k != "_raw"}
    STATE_FILE.write_text(json.dumps(to_save))


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=3)
    label   = f"сегодня ({now_msk.strftime('%d.%m, %H:%M')} МСК)"

    today   = date.today().isoformat()
    # Also include yesterday in case today's data isn't live yet
    d_from  = (date.today() - timedelta(days=1)).isoformat()

    async with httpx.AsyncClient() as client:
        raw = await fetch_stats(client, d_from, today)
        print(f"RAW STATS: {raw}")

        m = calc_metrics(raw)
        print(f"METRICS: { {k:v for k,v in m.items() if k != '_raw'} }")

        prev = load_state()

        # Always send hourly report
        await tg(client, fmt_report(m, label))

        # Check conversion alerts
        if prev:
            alert = check_alerts(prev, m)
            if alert:
                await tg(client,
                    f"⚠️ <b>Traffizy — падение конверсии!</b>\n\n{alert}")

        save_state(m)


if __name__ == "__main__":
    asyncio.run(main())
