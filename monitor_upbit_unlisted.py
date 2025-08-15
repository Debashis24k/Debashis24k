#!/usr/bin/env python3

import asyncio
import json
import os
import re
import sys
import time
import contextlib
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Response

ARKHAM_UPBIT_URL = "https://intel.arkm.com/explorer/entity/upbit"
UPBIT_MARKETS_API = "https://api.upbit.com/v1/market/all"
DEFAULT_SCAN_INTERVAL_SECONDS = 10
DEFAULT_HEADLESS = True

# Token shape constraints
TOKEN_PATTERN = re.compile(r"^[A-Z0-9]{2,12}$")

# Words to ignore when scanning generic uppercase tokens from the page text
COMMON_WORDS_TO_IGNORE: Set[str] = {
    # brands / UI
    "UPBIT", "ARKM", "ARKHAM", "INTEL", "EXPLORER", "ENTITY",
    # generic words
    "MORE", "LESS", "COPY", "OPEN", "CLOSE", "LOGIN", "SIGN", "NEXT", "PREV", "SHOW", "HIDE",
    # time / dates
    "AM", "PM", "UTC",
}


def log(message: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}")


def fetch_upbit_listed_tokens() -> Set[str]:
    """Fetch current set of listed token symbols from Upbit public API."""
    response = requests.get(UPBIT_MARKETS_API, params={"isDetails": "false"}, timeout=30)
    response.raise_for_status()
    markets = response.json()
    listed_symbols: Set[str] = set()
    for item in markets:
        market: str = item.get("market", "")
        if "-" in market:
            try:
                _base, quote = market.split("-", 1)
                listed_symbols.add(quote.upper())
            except Exception:
                continue
    return listed_symbols


def normalize_candidate_symbol(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    if not candidate:
        return None
    # remove separators and odd punctuation
    candidate = candidate.replace("·", "").replace("—", "-").replace("–", "-")
    candidate = re.sub(r"[^A-Z0-9\-]", "", candidate)
    if candidate.endswith("-MAINNET"):
        candidate = candidate.replace("-MAINNET", "")
    # Simple validity check
    if TOKEN_PATTERN.match(candidate) is None:
        return None
    if candidate in COMMON_WORDS_TO_IGNORE:
        return None
    return candidate


def collect_tokens_from_json(obj: Any) -> Set[str]:
    """Recursively scan JSON and pull out plausible token symbols."""
    keys_of_interest: Set[str] = {
        "symbol", "assetSymbol", "tokenSymbol", "ticker", "asset", "token", "currency", "currencySymbol",
    }
    collected: Set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value)
                else:
                    if isinstance(value, str):
                        if key in keys_of_interest or key.lower().endswith("symbol") or key.lower().endswith("ticker"):
                            norm = normalize_candidate_symbol(value)
                            if norm:
                                collected.add(norm)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return collected


async def extract_tokens_from_dom(page: Page) -> Set[str]:
    """Scrape the page DOM for capitalized token-like strings with liberal heuristics."""
    tokens: Set[str] = set()
    try:
        # First try: elements with likely classnames
        locator = page.locator("css=[class*='Asset'], [class*='asset'], [class*='Token'], [class*='token'], [data-testid*='asset'], [data-testid*='token']")
        texts: List[str] = await locator.all_text_contents()
        for text in texts:
            for part in re.findall(r"[A-Z0-9]{2,12}", text.upper()):
                norm = normalize_candidate_symbol(part)
                if norm:
                    tokens.add(norm)
    except Exception:
        pass

    try:
        # Fallback: entire page text scan
        full_text: str = await page.evaluate("document.body ? document.body.innerText : ''")
        for part in re.findall(r"\b[A-Z0-9]{2,12}\b", full_text.upper()):
            norm = normalize_candidate_symbol(part)
            if norm:
                tokens.add(norm)
    except Exception:
        pass

    return tokens


async def send_telegram_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception:
        pass


async def monitor_unlisted_tokens(
    headless: bool = DEFAULT_HEADLESS,
    scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
) -> None:
    listed_tokens = fetch_upbit_listed_tokens()
    log(f"Loaded {len(listed_tokens)} currently listed Upbit symbols")

    seen_unlisted: Set[str] = set()

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page: Page = await context.new_page()

        # Queue to unify sources
        queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()

        async def handle_response(response: Response) -> None:
            try:
                url = response.url
                if not ("arkm" in url or "arkham" in url or "intel" in url):
                    return
                req = response.request
                if req.resource_type not in {"xhr", "fetch"}:
                    return
                # Try parse JSON payloads
                ctype = response.headers.get("content-type", "")
                if "application/json" not in ctype:
                    return
                data = await response.json()
                for token in collect_tokens_from_json(data):
                    await queue.put(("network", token))
            except Exception:
                # Ignore parse errors
                pass

        page.on("response", handle_response)

        log("Navigating to Arkham Upbit entity page...")
        await page.goto(ARKHAM_UPBIT_URL, wait_until="networkidle")

        # Try to accept cookie banners if present
        for button_text in ("Accept", "I agree", "Agree", "OK", "Got it"):
            try:
                await page.get_by_role("button", name=re.compile(button_text, re.I)).click(timeout=1500)
                break
            except Exception:
                pass

        # Try to switch to a likely transfers/activity tab and filter to inbound
        for label in ("Transfers", "Activity", "Transactions"):
            try:
                await page.locator(f"text={label}").first.click(timeout=1500)
                break
            except Exception:
                pass
        for label in ("Received", "Inflows", "Inbound", "To Upbit", "Deposits"):
            try:
                await page.locator(f"text={label}").first.click(timeout=1500)
                break
            except Exception:
                pass

        async def dom_scanner_task() -> None:
            while True:
                try:
                    tokens = await extract_tokens_from_dom(page)
                    for token in tokens:
                        await queue.put(("dom", token))
                except Exception:
                    pass
                await asyncio.sleep(scan_interval_seconds)

        scanner = asyncio.create_task(dom_scanner_task())

        try:
            while True:
                source, token = await queue.get()
                if token in listed_tokens:
                    continue
                if token in seen_unlisted:
                    continue
                seen_unlisted.add(token)
                msg = f"Potential unlisted token deposit mention on Upbit entity ({source}): {token}"
                log(msg)
                await send_telegram_message(msg)
        finally:
            scanner.cancel()
            with contextlib.suppress(Exception):
                await scanner
            await context.close()
            await browser.close()


def parse_args(argv: List[str]) -> Tuple[bool, int]:
    headless = DEFAULT_HEADLESS
    interval = DEFAULT_SCAN_INTERVAL_SECONDS
    for arg in argv:
        if arg == "--headed":
            headless = False
        elif arg.startswith("--interval="):
            try:
                interval = int(arg.split("=", 1)[1])
            except Exception:
                pass
    return headless, interval


if __name__ == "__main__":
    try:
        headless_mode, scan_interval = parse_args(sys.argv[1:])
        asyncio.run(monitor_unlisted_tokens(headless=headless_mode, scan_interval_seconds=scan_interval))
    except KeyboardInterrupt:
        log("Stopped by user")
    except Exception as e:
        log(f"Fatal error: {e}")
        raise