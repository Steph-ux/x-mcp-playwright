"""Smoke test: prove the persistent session works by running a search.

Usage:
    python fetch_tweets.py [query] [limit]
"""

from __future__ import annotations

import asyncio
import sys
import urllib.parse

from patchright.async_api import async_playwright
from utils import USER_DATA_DIR, find_chrome


async def fetch_tweets(query: str, limit: int) -> int:
    encoded = urllib.parse.quote(query)
    search_url = f"https://x.com/search?q={encoded}&src=typed_query"
    chrome_path = find_chrome()

    async with async_playwright() as p:
        launch_kwargs: dict = {
            "user_data_dir": USER_DATA_DIR,
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path

        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        page = await context.new_page()

        try:
            print(f"[search] Query: {query!r}")
            await page.goto(search_url, wait_until="domcontentloaded")

            try:
                await page.wait_for_selector("[data-testid='tweet']", timeout=15000)
                await page.wait_for_timeout(2000)
            except Exception:
                print("[search] No tweets found / timeout.")
                return 1

            tweets = await page.query_selector_all("[data-testid='tweet']")
            if not tweets:
                print("[search] No tweets in DOM.")
                return 1

            print(f"[search] Found {len(tweets)} tweets, printing first {limit}\n")
            for i, element in enumerate(tweets[:limit]):
                text = await element.inner_text()
                clean = "\n".join(line for line in text.split("\n") if line.strip())
                print(f"=== TWEET {i + 1} ===")
                print(clean)
                print("=" * 16, "\n")
            return 0
        finally:
            await context.close()


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "claude opus"
    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    except ValueError:
        limit = 8
    return asyncio.run(fetch_tweets(query, limit))


if __name__ == "__main__":
    sys.exit(main())
