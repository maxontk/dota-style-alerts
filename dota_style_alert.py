#!/usr/bin/env python3
"""Steam Dota 2 unlocked-style alerts to Telegram. Python 3.10+, no pip."""
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

ITEMS = [
    ("Cloak of the Battleranger", "Chaotic", 3, 11),
    ("Wrap of the Urushin Huntsman", "Chaotic", 14, 20),
    ("Axolotl", "Red", 20, 100),
    ("Talons of the Urushin Huntsman", "Chaotic", 3, 6),
    ("Sentinels of the Urushin Huntsman", "Chaotic", 3, 7),
]

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
INTERVAL = max(180, int(os.environ.get("CHECK_INTERVAL", "300")))
MAX_PAGES = max(1, int(os.environ.get("MAX_PAGES", "10")))
PAGE_DELAY = max(1.0, float(os.environ.get("PAGE_DELAY", "2")))
DATABASE = Path(os.environ.get("DB_PATH", "dota_alerts.sqlite3"))
PROXY = os.environ.get("HTTPS_PROXY", "") or os.environ.get("https_proxy", "")
OPENER = build_opener(ProxyHandler({"https": PROXY} if PROXY else {}))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DotaStyleWatcher/1.0)",
           "Accept-Language": "en-US,en;q=0.9"}


def fetch(url, *, data=None):
    payload = None if data is None else urlencode(data).encode("utf-8")
    req = Request(url, data=payload, headers=HEADERS)
    with OPENER.open(req, timeout=30) as response:
        return response.read().decode("utf-8")


def send_message(message):
    if not TOKEN or not CHAT_ID:
        raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    # Telegram requests must not use the Steam proxy when it is blocked there.
    tg_opener = build_opener(ProxyHandler({}))
    data = urlencode({"chat_id": CHAT_ID, "text": message,
                      "disable_web_page_preview": "true"}).encode()
    with tg_opener.open(Request(url, data=data), timeout=20) as response:
        answer = json.load(response)
        if not answer.get("ok"):
            raise RuntimeError("Telegram did not accept message")


def listings(name, start):
    # Steam's 2026 Market UI embeds the individual sell listings in SSR JSON.
    url = "https://steamcommunity.com/market/listings/570/" + quote(name, safe="")
    page = fetch(url + "?" + urlencode({"start": start, "l": "english"}))
    match = re.search(r'<script\b[^>]*id="valve-ssr-data"[^>]*>(.*?)</script>',
                      page, flags=re.S)
    if not match:
        raise RuntimeError("Steam page changed or returned a challenge")
    content = json.loads(html.unescape(match.group(1)))
    data = json.loads(content["renderContext"]["queryData"])
    for query in data["queries"]:
        state = query.get("state", {}).get("data")
        if not isinstance(state, dict):
            continue
        for p in state.get("pages", []):
            if isinstance(p, dict) and isinstance(p.get("listings"), list):
                return p["listings"], bool(p.get("more", False))
    raise RuntimeError("No individual listing data in Steam response")


def unlocked(description, style):
    # Check exact style label; avoid generic text advertising the unlock item.
    for line in description.get("descriptions", []):
        value = re.sub(r"<[^>]+>", " ", html.unescape(line.get("value", "")))
        if re.fullmatch(r"\s*" + re.escape(style) + r"\s*", value, flags=re.I):
            return True
    return False


def rub_rate():
    # Compare the SAME Steam item in USD and RUB. This is an estimate; Steam
    # rounds low-priced items, and buyer region may affect the actual amount.
    base = "https://steamcommunity.com/market/priceoverview/?"
    values = []
    for currency in (1, 5):
        raw = fetch(base + urlencode({"appid": 570, "currency": currency,
                                     "market_hash_name": "Axolotl"}))
        data = json.loads(raw)
        amount = re.search(r"\d[\d\s.,]*", data["lowest_price"])
        if not amount:
            raise ValueError("Could not parse Steam price overview")
        # Russian comma decimal / US dot decimal.
        n = amount.group().replace(" ", "").replace("\xa0", "")
        values.append(Decimal(n.replace(",", ".")))
        time.sleep(PAGE_DELAY)
    if values[0] <= 0:
        raise ValueError("Steam USD price is zero")
    return values[1] / values[0]


def total_usd(listing):
    # Steam's price + buyer fee, USD cents (currency 1 on anonymous pages).
    if int(listing.get("eCurrency", 1)) != 1:
        raise ValueError("Steam listing currency changed; price conversion unsafe")
    return Decimal(int(listing["unPrice"]) + int(listing["unFee"])) / 100


def run_once(connection, rate):
    failures = 0
    total_checked = 0
    for name, style, lower, upper in ITEMS:
        checked, found = 0, 0
        start = 0
        try:
            for page_number in range(MAX_PAGES):
                rows, more = listings(name, start)
                if not rows:
                    break
                checked += len(rows)
                total_checked += len(rows)
                too_expensive = False
                for listing in rows:
                    price_rub = total_usd(listing) * rate
                    if price_rub > Decimal(upper):
                        too_expensive = True
                        break  # Steam lists in ascending price order.
                    if price_rub < Decimal(lower):
                        continue
                    if not unlocked(listing.get("description", {}), style):
                        continue
                    identifier = str(listing["listingid"])
                    existing = connection.execute("SELECT 1 FROM sent WHERE id=?", (identifier,)).fetchone()
                    if existing:
                        continue
                    link = ("https://steamcommunity.com/market/listings/570/" +
                            quote(name, safe="") + "?listingid=" + quote(identifier))
                    msg = (f"🔥 ОТКРЫТ {style}!\n{name}\n"
                           f"≈ {price_rub:.2f} ₽ (в Steam: {listing.get('strSubtotal', '?')})\n"
                           f"Диапазон: {lower}–{upper} ₽\nЛот: {identifier}\n{link}")
                    send_message(msg)
                    connection.execute("INSERT OR IGNORE INTO sent(id) VALUES (?)", (identifier,))
                    connection.commit()
                    found += 1
                if too_expensive or not more:
                    break
                start += len(rows)
                time.sleep(PAGE_DELAY)
            else:
                logging.warning("%s: reached MAX_PAGES=%d; deeper listings not checked", name, MAX_PAGES)
            logging.info("%s: scanned %d, new unlocked listings %d", name, checked, found)
        except (HTTPError, URLError, KeyError, ValueError, RuntimeError, TypeError) as e:
            logging.error("Error for %s: %s", name, e)
            failures += 1
        time.sleep(PAGE_DELAY)
    if failures:
        raise RuntimeError(f"Steam checks failed for {failures}/{len(ITEMS)} items")
    if not total_checked:
        raise RuntimeError("Steam returned no individual listings to inspect")


def main():
    if not TOKEN or not CHAT_ID:
        raise SystemExit("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    connection = sqlite3.connect(DATABASE)
    connection.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY)")
    connection.commit()
    rate = None
    static_rate = os.environ.get("RUB_PER_USD", "").strip()
    if static_rate:
        try:
            rate = Decimal(static_rate)
            if rate <= 0:
                raise ValueError("must be positive")
        except ValueError as exc:
            raise SystemExit(f"Invalid RUB_PER_USD value: {exc}") from exc
        logging.warning("Using configured approximate RUB_PER_USD=%s; actual Steam prices can differ", rate)
    rate_updated = 0
    if "--test-telegram" in sys.argv:
        send_message("✅ Тест Dota Style Monitor: Telegram работает!")
        return
    once = "--once" in sys.argv
    if not once:
        send_message("✅ Мониторинг Dota 2 запущен. Проверяю 5 предметов.")
    while True:
        started = time.monotonic()
        if not static_rate and (rate is None or started - rate_updated > 6 * 3600):
            try:
                rate = rub_rate()
                rate_updated = started
                logging.info("Approximate Steam RUB per USD: %.2f", float(rate))
            except (HTTPError, URLError, ValueError, KeyError) as e:
                logging.error("Cannot refresh Steam RUB conversion: %s", e)
        if rate is not None:
            try:
                run_once(connection, rate)
            except RuntimeError as e:
                logging.error("Monitor incomplete: %s", e)
                if once:
                    raise SystemExit(1) from e
        if once:
            connection.close()
            return
        time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
