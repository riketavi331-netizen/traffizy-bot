"""
Traffizy → Telegram reporter
Uses the Customer Operator REST API (no browser scraping).
"""
import os
import asyncio
import json
from pathlib import Path
from datetime import date, timedelta
import httpx

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATISTIC_TOKEN  = os.environ["STATISTIC_TOKEN"]

BASE_URL   = "https://affiliate.traffizy.partners"
STATE_FILE = Path("/tmp/prev_stats.json")

HEADERS = {
    "Accept":        "application/json",
    "Content-Type":  "application/json",
    "Host":          "affiliate.traffizy.partners",
    "Authorization": STATISTIC_TOKEN,
}


# ── Traffizy API ──────────────────────────────────────────────────────────────

async def fetch_traffic(client: httpx.AsyncClient, date_from: str, date_to: str) -> dict:
    """
    GET /api/customer/v1/casino/traffic_report
    Returns visits, registrations, etc.
    """
    r = await client.get(
        f"{BASE_URL}/api/customer/v1/casino/traffic_report",
        headers=HEADERS,
        params={"date_from": date_from, "date_to": date_to},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


async def fetch_report(client: httpx.AsyncClient, date_from: str, date_to: str) -> dict:
    """
    GET /api/customer/v1/casino/report
    Returns deposits, FTD, NGR, etc.
    Requests specific columns to keep the response compact.
    """
    columns = [
        "first_deposits_count",
        "first_deposits_sum",
        "deposits_count",
        "deposits_sum",
        "cashouts_count",
        "cashouts_sum",
        "depositing_players_count",
        "ngr",
        "ggr",
    ]
    r = await client.get(
        f"{BASE_URL}/api/customer/v1/casino/report",
        headers=HEADERS,
        params={
            "date_from": date_from,
            "date_to":   date_to,
            "columns[]": columns,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def yesterday_range() -> tuple[str, str]:
    d = (date.today() - timedelta(days=1)).isoformat()
    return d, d


def today_range() -> tuple[str, str]:
    d = date.today().isoformat()
    return d, d


# ── Merge API responses into one flat dict ────────────────────────────────────

def flatten(traffic: dict, report: dict) -> dict:
    stats: dict = {}

    # Traffic report — could be list of rows or a totals dict; grab totals
    t = traffic if isinstance(traffic, dict) else {}
    if isinstance(traffic, list) and traffic:
        # Sum across all rows if grouped
        for row in traffic:
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + v
    else:
        stats.update({k: v for k, v in t.items() if isinstance(v, (int, float))})

    # Casino report — same logic
    rr = report if isinstance(report, dict) else {}
    if isinstance(report, list) and report:
        for row in report:
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + v
    else:
        stats.update({k: v for k, v in rr.items() if isinstance(v, (int, float))})

    return stats


# ── Telegram ──────────────────────────────────────────────────────────────────

async def tg_send(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


# ── Change detection ──────────────────────────────────────────────────────────

ALERT_KEYS  = ("visits", "registrations", "first_deposits_count", "deposits_sum")
ALERT_THRESHOLD = 0.30  # 30 %


def load_prev() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_prev(stats: dict):
    STATE_FILE.write_text(json.dumps(stats))


def build_alert(prev: dict, curr: dict) -> str | None:
    lines = []
    for key in ALERT_KEYS:
        old = prev.get(key)
        new = curr.get(key)
        if old is None or new is None or old == 0:
            continue
        delta = (new - old) / old
        if abs(delta) >= ALERT_THRESHOLD:
            arrow = "📈" if delta > 0 else "📉"
            lines.append(f"{arrow} <b>{key}</b>: {old:,.0f} → {new:,.0f} ({delta:+.0%})")
    return "\n".join(lines) if lines else None


# ── Message formatting ────────────────────────────────────────────────────────

LABELS = {
    "visits":                  ("👆", "Visits"),
    "registrations":           ("📝", "Registrations"),
    "depositing_players_count":("👤", "Depositors"),
    "first_deposits_count":    ("🆕", "FTD count"),
    "first_deposits_sum":      ("💵", "FTD sum (USD)"),
    "deposits_count":          ("💳", "Deposits count"),
    "deposits_sum":            ("💰", "Deposits sum (USD)"),
    "cashouts_count":          ("🔄", "Cashouts count"),
    "cashouts_sum":            ("🔄", "Cashouts sum (USD)"),
    "ngr":                     ("📊", "NGR (USD)"),
    "ggr":                     ("📊", "GGR (USD)"),
}


def fmt_val(v: float, key: str) -> str:
    money_keys = {"first_deposits_sum", "deposits_sum", "cashouts_sum", "ngr", "ggr"}
    if key in money_keys:
        return f"${v:,.2f}"
    return f"{v:,.0f}"


def format_daily(stats: dict, period_label: str) -> str:
    lines = [f"📊 <b>Traffizy — {period_label}</b>", ""]
    found = False
    for key, (emoji, label) in LABELS.items():
        val = stats.get(key)
        if val is not None:
            lines.append(f"{emoji} <b>{label}</b>: {fmt_val(val, key)}")
            found = True
    if not found:
        lines.append("⚠️ Нет данных за период")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    mode = os.environ.get("RUN_MODE", "daily")  # daily | check

    async with httpx.AsyncClient() as client:
        try:
            if mode == "daily":
                date_from, date_to = yesterday_range()
                label = f"вчера ({date_from})"
            else:
                date_from, date_to = today_range()
                label = f"сегодня ({date_from})"

            traffic = await fetch_traffic(client, date_from, date_to)
            report  = await fetch_report(client, date_from, date_to)
            stats   = flatten(traffic, report)

        except httpx.HTTPStatusError as e:
            await tg_send(client, f"❌ <b>Traffizy API error {e.response.status_code}</b>\n<code>{e.response.text[:300]}</code>")
            raise
        except Exception as e:
            await tg_send(client, f"❌ <b>Ошибка</b>\n<code>{e}</code>")
            raise

        if mode == "daily":
            await tg_send(client, format_daily(stats, label))

        elif mode == "check":
            prev  = load_prev()
            alert = build_alert(prev, stats)
            if alert:
                await tg_send(client, f"⚡️ <b>Traffizy — изменение</b>\n\n{alert}")

        save_prev(stats)


if __name__ == "__main__":
    asyncio.run(main())
