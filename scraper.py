"""
Traffizy → Telegram reporter
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

BASE_URL = "https://affiliate.traffizy.partners"
STATE_FILE = Path("/tmp/prev_stats.json")

HEADERS = {
    "Accept":        "application/json",
    "Authorization": STATISTIC_TOKEN,
}


async def tg_send(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


async def try_get(client: httpx.AsyncClient, url: str, params: dict) -> tuple[int, str]:
    """Returns (status_code, response_text)."""
    try:
        r = await client.get(url, headers=HEADERS, params=params, timeout=30)
        print(f"GET {url} {params} → {r.status_code}: {r.text[:300]}")
        return r.status_code, r.text
    except Exception as e:
        print(f"GET {url} → Exception: {e}")
        return 0, str(e)


async def fetch_data(client: httpx.AsyncClient, date_from: str, date_to: str) -> dict | list | None:
    """
    Tries multiple parameter combinations to get traffic/report data.
    Returns the first successful JSON response.
    """
    base = f"{BASE_URL}/api/customer/v1/casino/traffic_report"

    attempts = [
        {"date_from": date_from, "date_to": date_to, "group_by": "total"},
        {"date_from": date_from, "date_to": date_to, "group_by": "day"},
        {"date_from": date_from, "date_to": date_to},
        {"from": date_from, "to": date_to},
        {"start_date": date_from, "end_date": date_to},
    ]

    for params in attempts:
        status, body = await try_get(client, base, params)
        if status == 200:
            try:
                return json.loads(body)
            except Exception:
                pass

    # Also try casino/report endpoint
    report_base = f"{BASE_URL}/api/customer/v1/casino/report"
    for params in attempts:
        status, body = await try_get(client, report_base, params)
        if status == 200:
            try:
                return json.loads(body)
            except Exception:
                pass

    return None


async def fetch_filters(client: httpx.AsyncClient) -> str:
    """Fetches available filters to understand required params."""
    status, body = await try_get(
        client,
        f"{BASE_URL}/api/customer/v1/casino/traffic_report/filters",
        {}
    )
    return body[:800] if status == 200 else f"filters: {status}"


def flatten(data: dict | list) -> dict:
    stats: dict = {}
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + v
    return stats


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()

def today() -> str:
    return date.today().isoformat()


LABELS = {
    "visits":                  ("👆", "Visits"),
    "registrations":           ("📝", "Registrations"),
    "depositing_players_count":("👤", "Depositors"),
    "first_deposits_count":    ("🆕", "FTD count"),
    "first_deposits_sum":      ("💵", "FTD sum"),
    "deposits_count":          ("💳", "Deposits"),
    "deposits_sum":            ("💰", "Deposits sum"),
    "cashouts_count":          ("🔄", "Cashouts"),
    "ngr":                     ("📊", "NGR"),
}
MONEY = {"first_deposits_sum", "deposits_sum", "ngr", "ggr"}


def format_stats(stats: dict, label: str) -> str:
    lines = [f"📊 <b>Traffizy — {label}</b>", ""]
    found = False
    for key, (emoji, name) in LABELS.items():
        val = stats.get(key)
        if val is not None:
            v = f"${val:,.2f}" if key in MONEY else f"{val:,.0f}"
            lines.append(f"{emoji} <b>{name}</b>: {v}")
            found = True
    if not found:
        lines.append("⚠️ Нет распознанных данных")
    return "\n".join(lines)


def load_prev() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_prev(stats: dict):
    STATE_FILE.write_text(json.dumps(stats))


async def main():
    mode = os.environ.get("RUN_MODE", "daily")
    d_from = yesterday() if mode == "daily" else today()
    d_to   = d_from
    label  = f"вчера ({d_from})" if mode == "daily" else f"сегодня ({d_from})"

    async with httpx.AsyncClient() as client:
        # Diagnostic: fetch filters first
        filters_info = await fetch_filters(client)
        print(f"FILTERS: {filters_info}")

        data = await fetch_data(client, d_from, d_to)

        if data is None:
            # Send diagnostic to Telegram
            await tg_send(client,
                f"❌ <b>Все попытки API вернули ошибку</b>\n\n"
                f"<b>Filters endpoint:</b>\n<code>{filters_info[:400]}</code>"
            )
            return

        stats = flatten(data)
        print(f"STATS: {stats}")

        if mode == "daily":
            await tg_send(client, format_stats(stats, label))
        elif mode == "check":
            prev  = load_prev()
            alert_lines = []
            for key in ("visits", "registrations", "first_deposits_count"):
                old, new = prev.get(key), stats.get(key)
                if old and new and old != 0:
                    delta = (new - old) / old
                    if abs(delta) >= 0.30:
                        arrow = "📈" if delta > 0 else "📉"
                        alert_lines.append(f"{arrow} <b>{key}</b>: {old:,.0f}→{new:,.0f} ({delta:+.0%})")
            if alert_lines:
                await tg_send(client, "⚡️ <b>Traffizy — изменение</b>\n\n" + "\n".join(alert_lines))

        save_prev(stats)


if __name__ == "__main__":
    asyncio.run(main())
