"""Interactive one-time login to create the persistent X session profile.

Launches Chrome (headed) and waits for the user to complete login (incl. 2FA).
Detects success by waiting for the /home URL, then closes the browser to flush
cookies to disk. The profile is reused by `x_mcp_server.py` afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys

from patchright.async_api import async_playwright
from utils import USER_DATA_DIR, find_chrome

LOGIN_TIMEOUT_SECONDS = int(os.environ.get("X_MCP_LOGIN_TIMEOUT", "300"))


async def login() -> int:
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    chrome_path = find_chrome()
    if chrome_path:
        print(f"[login] Chrome détecté : {chrome_path}")
    else:
        print("[login] Chrome introuvable — Chromium par défaut utilisé.")

    async with async_playwright() as p:
        launch_kwargs: dict = {
            "user_data_dir": USER_DATA_DIR,
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path

        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        page = await context.new_page()

        print("[login] Navigation vers https://x.com/login ...")
        await page.goto("https://x.com/login")

        print("\n" + "=" * 60)
        print("Connectez-vous dans la fenêtre du navigateur (login + 2FA si besoin).")
        print("La détection est automatique dès que x.com/home apparaît.")
        print(f"Délai max : {LOGIN_TIMEOUT_SECONDS} s.")
        print("=" * 60 + "\n")

        success = False
        for _ in range(LOGIN_TIMEOUT_SECONDS):
            await asyncio.sleep(1)
            if "x.com/home" in page.url or "twitter.com/home" in page.url:
                print(f"[login] Connecté ! URL : {page.url}")
                print("[login] Stabilisation de la session (5 s)...")
                await asyncio.sleep(5)
                success = True
                break

        await context.close()

        if success:
            print(f"[login] OK. Session sauvegardée dans : {USER_DATA_DIR}")
            return 0
        print("[login] Timeout — pas de connexion détectée.")
        return 1


def main() -> int:
    return asyncio.run(login())


if __name__ == "__main__":
    sys.exit(main())
