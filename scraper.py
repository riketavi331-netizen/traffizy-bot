"""
Traffizy → Telegram reporter
API: /api/customer/v1/casino/report  params: from, to
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
    "Authorization": STATISTIC_TOKEN,
}

MONEY_KEYS = {"first_deposits_sum", "deposits_sum", "cashouts_sum", "ngr", "ggr", "real_ngr", "real_ggr"}

LABELS = {
    "visits":                  ("👆", "Visits"),
    "registrations":           ("📝", "Registrations"),
    "depositing_players_count":("👤", "Depositors"),
    "first_deposits_count":    ("🆕", "FTD count"),
    "first_deposits_sum":      ("💵", "FTD sum"),
    "deposits_count":          ("💳", "Deposits count"),
    "deposits_sum":            ("💰", "Deposits sum"),
    "cashouts_count":          ("🔄", "Cashouts"),
    "ngr":                     ("📊", "NGR"),
    "ggr":                     ("📈", "GGR"),
}


async def tg_send(client: httpx.AsyncClient, text: str):
    await client.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )


async def fetch_report(client: httpx.AsyncClient, d_from: str, d_to: str) -> dict:
    """Fetch /casino/report totals for the given date range."""
    url = f"{BASE_URL}/api/customer/v1/casino/report"
    params = {"from": d_from, "to": d_to}
    r = await client.get(url, headers=HEADERS, params=params, timeout=30)
    print(f"report → {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


async def fetch_traffic(client: httpx.AsyncClient, d_from: str, d_to: str) -> dict:
    """Fetch /casino/traffic_report totals for the given date range."""
    url = f"{BASE_URL}/api/customer/v1/casino/traffic_report"
    params = {"from": d_from, "to": d_to}
    r = await client.get(url, headers=HEADERS, params=params, timeout=30)
    print(f"traffic → {r.status_code}: {r.text[:400]}")
    r.raise_for_status()
    return r.json()


def extract_totals(response: dict) -> dict:
    """
    API response structure:
      {"totals": {"data": [{"visits": 49, ...}]}, "rows": {"data": [...]}, ...}
    Returns flattened numeric dict from totals.data[0] if available,
    otherwise sums rows.data.
    """
    stats: dict = {}

    totals_data = response.get("totals", {}).get("data", [])
    if totals_data and isinstance(totals_data[0], dict):
        for k, v in totals_data[0].items():
            if isinstance(v, (int, float)):
                stats[k] = v
        return stats

    # Fallback: sum rows
    for row in response.get("rows", {}).get("data", []):
        if isinstance(row, dict):
            for k, v in row.items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + v

    return stats


def format_stats(stats: dict, label: str) -> str:
    lines = [f"📊 <b>Traffizy — {label}</b>", ""]
    found = False
    for key, (emoji, name) in LABELS.items():
        val = stats.get(key)
        if val is not None:
            v = f"${val:,.2f}" if key in MONEY_KEYS else f"{val:,.0f}"
            lines.append(f"{emoji} <b>{name}</b>: {v}")
            found = True
    if not found:
        lines.append("⚠️ Нет данных за период — возможно данные за вчера ещё не готовы")
    return "\n".join(lines)


def load_prev() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_prev(stats: dict):
    STATE_FILE.write_text(json.dumps(stats))


async def main():
    mode  = os.environ.get("RUN_MODE", "daily")
    d     = (date.today() - timedelta(days=1)).isoformat()  # всегда вчера
    label = f"вчера ({d})"

    async with httpx.AsyncClient() as client:
        stats: dict = {}

        # Fetch report (financial data)
        try:
            report_data = await fetch_report(client, d, d)
            stats.update(extract_totals(report_data))
        except httpx.HTTPStatusError as e:
            print(f"report error: {e.response.status_code} {e.response.text[:200]}")
            await tg_send(client, f"⚠️ report {e.response.status_code}: <code>{e.response.text[:200]}</code>")

        # Small delay to avoid rate limiting
        await asyncio.sleep(2)

        # Fetch traffic (visits, registrations)
        try:
            traffic_data = await fetch_traffic(client, d, d)
            stats.update(extract_totals(traffic_data))
        except httpx.HTTPStatusError as e:
            print(f"traffic error: {e.response.status_code} {e.response.text[:200]}")

        print(f"STATS: {stats}")

        if mode == "daily":
            await tg_send(client, format_stats(stats, label))

        elif mode == "check":
            prev = load_prev()
            if not prev:
                # Первый запуск — сохраняем baseline, ничего не отправляем
                print("No prev stats, saving baseline")
            else:
                lines = []
                for key in ("visits", "registrations", "first_deposits_count", "deposits_sum"):
                    old, new = prev.get(key), stats.get(key)
                    if old and new and old != 0:
                        delta = (new - old) / old
                        if abs(delta) >= 0.30:
                            arrow = "📈" if delta > 0 else "📉"
                            lines.append(f"{arrow} <b>{key}</b>: {old:,.0f}→{new:,.0f} ({delta:+.0%})")
                if lines:
                    await tg_send(client, "⚡️ <b>Traffizy — изменение трафика</b>\n\n" + "\n".join(lines))

        save_prev(stats)


if __name__ == "__main__":
    asyncio.run(main())
