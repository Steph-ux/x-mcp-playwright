"""
x-mcp-playwright — Comprehensive Twitter/X MCP server.

Uses patchright (stealth Playwright) with a persistent Chrome profile.
Browser context is shared across tool calls for high performance.

Tools (9 grouped — all 33 actions preserved, zero functionality lost):
  x_post      — post, thread, post_media, reply, quote, delete
  x_engage    — like, retweet, bookmark, pin (each with on:bool toggle)
  x_user      — profile, timeline, connections, follow, block, mute
  x_search    — simple + advanced search (from/since/lang/filters)
  x_feed      — home, bookmarks, notifications, trending
  x_tweet     — details, replies, analytics, engagers
  x_list      — list listing + list tweets
  x_dm        — send, conversations, messages
  x_session   — check login, screenshot

Resources:    x://session/status, x://version, x://tools/list, x://tools/categories
Prompts:      analyze_tweet, draft_reply
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from utils import (
    USER_DATA_DIR,
    HEADLESS,
    DEFAULT_TIMEOUT_MS,
    SCREENSHOT_DIR,
    _log,
    find_chrome,
)

# ---------------------------------------------------------------------------
# Config / logging
# ---------------------------------------------------------------------------

mcp = FastMCP("Twitter-Playwright")

print = _log  # type: ignore[assignment]

__version__ = "1.2.0"

# ---------------------------------------------------------------------------
# Shared browser context + page pool (single instance, lazy-init)
# ---------------------------------------------------------------------------

POOL_SIZE = 3  # max pages in the pool — concurrent-safe via asyncio.Queue


class _BrowserManager:
    """Persistent context + page pool. Pages are reused across tool calls."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._pool: asyncio.Queue[Page] = asyncio.Queue(maxsize=POOL_SIZE)
        self._pool_size = 0
        self._lock = asyncio.Lock()

    async def get_context(self) -> BrowserContext:
        async with self._lock:
            if self._context is not None:
                try:
                    _ = self._context.pages
                    return self._context
                except Exception:
                    _log("[browser] stale context, recreating")
                    self._context = None

            self._playwright = await async_playwright().start()
            chrome_path = find_chrome()
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": USER_DATA_DIR,
                "headless": HEADLESS,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            }
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path

            _log(f"[browser] launching chrome (headless={HEADLESS}) at {chrome_path or 'bundled'}")
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
            self._context.set_default_timeout(DEFAULT_TIMEOUT_MS)
            return self._context

    async def _new_page(self) -> Page:
        ctx = await self.get_context()
        return await ctx.new_page()

    async def acquire_page(self) -> Page:
        """Get a page from the pool, or create one if the pool is empty."""
        try:
            page = self._pool.get_nowait()
            try:
                _ = page.url  # quick liveness check
                return page
            except Exception:
                self._pool_size -= 1
        except asyncio.QueueEmpty:
            pass
        return await self._new_page()

    async def release_page(self, page: Page) -> None:
        """Return a page to the pool for reuse. Drop if pool is full."""
        try:
            if self._pool_size < POOL_SIZE:
                self._pool.put_nowait(page)
                self._pool_size += 1
                return
        except asyncio.QueueFull:
            pass
        try:
            await page.close()
        except Exception:
            pass

    async def shutdown(self) -> None:
        async with self._lock:
            # drain pool
            while not self._pool.empty():
                try:
                    p = self._pool.get_nowait()
                    await p.close()
                except Exception:
                    pass
            self._pool_size = 0
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as e:
                    _log(f"[browser] context close error: {e}")
                self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    _log(f"[browser] playwright stop error: {e}")
                self._playwright = None


BROWSER = _BrowserManager()


@asynccontextmanager
async def use_page():
    """Context manager that reuses a page from the pool."""
    page = await BROWSER.acquire_page()
    try:
        yield page
    finally:
        await BROWSER.release_page(page)


# ---------------------------------------------------------------------------
# Pydantic models — typed structured outputs
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class Tweet(BaseModel):
    text: str = ""
    author_handle: Optional[str] = None
    author_name: Optional[str] = None
    tweet_url: Optional[str] = None
    tweet_id: Optional[str] = None
    timestamp: Optional[str] = None
    reply_count: Optional[str] = None
    retweet_count: Optional[str] = None
    like_count: Optional[str] = None
    view_count: Optional[str] = None


class UserProfile(BaseModel):
    handle: str
    url: str
    display_name: Optional[str] = None
    bio: Optional[str] = None
    followers_count: Optional[str] = None
    following_count: Optional[str] = None
    joined: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    verified: Optional[bool] = None


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def with_retry(max_attempts: int = 3, base_delay: float = 1.0,
               catch: tuple = (Exception,)):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except catch as e:
                    last_exc = e
                    if attempt == max_attempts:
                        _log(f"[retry] {func.__name__} failed after {attempt} attempts: {e}")
                        raise
                    delay = base_delay * (2 ** (attempt - 1))
                    _log(f"[retry] {func.__name__} attempt {attempt}/{max_attempts} failed ({e}); "
                         f"sleeping {delay}s")
                    await asyncio.sleep(delay)
            if last_exc:
                raise last_exc
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Rate limiter — token bucket for write actions (anti-LLM-mistake)
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple async-safe token bucket. Refills at `rate` tokens/sec, max `capacity`."""

    def __init__(self, capacity: int, rate: float) -> None:
        self._capacity = capacity
        self._rate = rate
        self._tokens = float(capacity)
        self._last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last_refill = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    async def wait(self, timeout: float = 30.0) -> bool:
        """Wait up to `timeout` seconds for a token."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if await self.acquire():
                return True
            await asyncio.sleep(0.5)
        return False


# 1 write/min for post/thread/delete, 2 writes/min for engage
WRITE_BUCKET = _TokenBucket(capacity=1, rate=1 / 60)
ENGAGE_BUCKET = _TokenBucket(capacity=2, rate=2 / 60)

WRITE_ACTIONS = {"post", "thread", "post_media", "reply", "quote", "delete", "send"}


async def check_rate_limit(action: str) -> Optional[str]:
    """Check rate limit. Returns error message if denied, None if allowed."""
    if action in WRITE_ACTIONS:
        bucket = WRITE_BUCKET
    elif action in {"like", "retweet", "bookmark", "pin", "follow", "block", "mute"}:
        bucket = ENGAGE_BUCKET
    else:
        return None  # read actions are unlimited

    if not await bucket.acquire():
        return (
            f"Rate limit: {action} is capped to prevent accidental spam. "
            "Wait a moment and try again."
        )
    return None


# ---------------------------------------------------------------------------
# Session keepalive — periodic ping to detect expiration early
# ---------------------------------------------------------------------------

async def _keepalive_loop() -> None:
    """Background loop that pings /home every 30 min to keep session alive."""
    while True:
        await asyncio.sleep(1800)  # 30 minutes
        try:
            async with use_page() as page:
                await goto_x(page, "/home", wait_until="domcontentloaded")
                url = page.url
                if "/home" not in url:
                    _log("[keepalive] session expired! Redirected to:", url)
                else:
                    _log("[keepalive] session OK")
        except Exception as e:
            _log(f"[keepalive] ping failed: {e}")


_keepalive_task: Optional[asyncio.Task] = None


def _start_keepalive() -> None:
    global _keepalive_task
    if _keepalive_task is None or _keepalive_task.done():
        _keepalive_task = asyncio.create_task(_keepalive_loop())
        _log("[keepalive] started (every 30 min)")


def _stop_keepalive() -> None:
    global _keepalive_task
    if _keepalive_task and not _keepalive_task.done():
        _keepalive_task.cancel()
        _log("[keepalive] stopped")


# ---------------------------------------------------------------------------
# Helpers — parsing, navigation, validation
# ---------------------------------------------------------------------------

_TWEET_ID_RE = re.compile(r"/status/(\d+)")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_LIST_ID_RE = re.compile(r"^\d+$")
_X_HOST_RE = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/")
TWEET_MAX_CHARS_FREE = 280


class ValidationError(ValueError):
    """Raised by input validators when the supplied argument is invalid."""


def extract_tweet_id(tweet_url: str) -> Optional[str]:
    m = _TWEET_ID_RE.search(tweet_url)
    return m.group(1) if m else None


def normalize_handle(handle: str) -> str:
    handle = handle.strip()
    if handle.startswith("http"):
        m = re.search(r"x\.com/([^/?#]+)", handle) or re.search(r"twitter\.com/([^/?#]+)", handle)
        if m:
            handle = m.group(1)
    return handle.lstrip("@")


def validate_handle(handle: str) -> str:
    if not handle or not handle.strip():
        raise ValidationError("handle is empty")
    norm = normalize_handle(handle)
    if not _HANDLE_RE.match(norm):
        raise ValidationError(
            f"invalid handle: {handle!r} (must be 1–15 chars, alnum + underscore)"
        )
    return norm


def validate_tweet_url(url: str) -> str:
    if not url or not url.strip():
        raise ValidationError("tweet_url is empty")
    url = url.strip()
    if not _X_HOST_RE.match(url):
        raise ValidationError(f"not an x.com/twitter.com URL: {url!r}")
    if not extract_tweet_id(url):
        raise ValidationError(f"URL missing /status/<id>: {url!r}")
    return url


def validate_tweet_text(text: str, *, allow_premium: bool = False) -> str:
    if text is None:
        raise ValidationError("text is None")
    if not text.strip():
        raise ValidationError("text is empty")
    cap = 25000 if allow_premium else TWEET_MAX_CHARS_FREE
    if len(text) > cap:
        raise ValidationError(f"text length {len(text)} exceeds cap {cap}")
    return text


def validate_limit(limit: int, *, lo: int = 1, hi: int = 200) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValidationError(f"limit must be int, got {type(limit).__name__}")
    if limit < lo or limit > hi:
        raise ValidationError(f"limit must be in [{lo}, {hi}], got {limit}")
    return limit


def validate_list_id(list_id: str) -> str:
    if not list_id or not _LIST_ID_RE.match(str(list_id).strip()):
        raise ValidationError(f"invalid list_id: {list_id!r} (must be numeric)")
    return str(list_id).strip()


def err_result(message: str, **extra: Any) -> dict[str, Any]:
    base = {"success": False, "message": message}
    base.update(extra)
    return base


async def goto_x(page: Page, path: str, *, wait_until: str = "domcontentloaded") -> None:
    url = path if path.startswith("http") else f"https://x.com{path}"
    await page.goto(url, wait_until=wait_until)


async def click_data_testid(page: Page, testid: str, timeout: int = 5000) -> bool:
    try:
        loc = page.locator(f"[data-testid='{testid}']").first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.click()
        return True
    except Exception as e:
        _log(f"[click {testid}] failed: {e}")
        return False


async def wait_stable(page: Page, selector: str, *, timeout: int = DEFAULT_TIMEOUT_MS,
                      settle_ms: int = 500) -> bool:
    """Wait for a selector, then confirm DOM is stable (no mutations for settle_ms).

    Returns True if the element appeared and settled. Much faster than fixed sleeps
    because it exits as soon as the DOM stops mutating, with a short settle buffer.
    """
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        # Brief pause to let late-painted elements land
        await page.wait_for_timeout(settle_ms)
        return True
    except Exception:
        return False


async def _scroll_collect_tweets(page: Page, limit: int, max_scrolls: int = 10) -> list[dict[str, Any]]:
    tweets: list[dict[str, Any]] = []
    seen: set[str] = set()
    scrolls = 0
    stale_rounds = 0
    while len(tweets) < limit and scrolls < max_scrolls:
        before = len(tweets)
        elements = await page.query_selector_all("[data-testid='tweet']")
        for el in elements:
            try:
                data = await extract_tweet_data(el)
                tid = data.get("tweet_id") or data.get("tweet_url")
                if tid and tid in seen:
                    continue
                if tid:
                    seen.add(tid)
                if data.get("text") or data.get("tweet_url"):
                    tweets.append(data)
                if len(tweets) >= limit:
                    break
            except Exception:
                continue
        # Early exit: if no new tweets appeared, DOM is exhausted
        if len(tweets) == before:
            stale_rounds += 1
            if stale_rounds >= 2:
                break
        else:
            stale_rounds = 0
        if len(tweets) < limit:
            await page.mouse.wheel(0, 3000)
            # Short wait — just enough for lazy-loaded content to render
            await page.wait_for_timeout(800)
        scrolls += 1
    return tweets[:limit]


async def save_debug_screenshot(page: Page, label: str) -> str:
    ts = asyncio.get_event_loop().time()
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)[:40]
    out = SCREENSHOT_DIR / f"{safe}_{int(ts)}.png"
    try:
        await page.screenshot(path=str(out), full_page=False)
    except Exception as e:
        _log(f"[screenshot] failed: {e}")
        return ""
    return str(out)


async def extract_tweet_data(element) -> dict[str, Any]:
    data: dict[str, Any] = {
        "text": "",
        "author_handle": None,
        "author_name": None,
        "tweet_url": None,
        "tweet_id": None,
        "timestamp": None,
        "reply_count": None,
        "retweet_count": None,
        "like_count": None,
        "view_count": None,
    }
    try:
        text = await element.inner_text()
        data["text"] = "\n".join([line for line in text.split("\n") if line.strip()])
    except Exception:
        pass
    try:
        anchors = await element.query_selector_all("a[href*='/status/']")
        href = None
        for a in anchors:
            h = await a.get_attribute("href")
            if h and "/status/" in h and "/analytics" not in h and "/photo/" not in h:
                href = h
                break
        if href:
            if href.startswith("/"):
                href = "https://x.com" + href
            data["tweet_url"] = href
            data["tweet_id"] = extract_tweet_id(href)
            m = re.search(r"x\.com/([^/]+)/status/", href)
            if m:
                data["author_handle"] = m.group(1)
    except Exception:
        pass
    try:
        time_el = await element.query_selector("time")
        if time_el:
            data["timestamp"] = await time_el.get_attribute("datetime")
    except Exception:
        pass
    for key, testid in (
        ("reply_count", "reply"),
        ("retweet_count", "retweet"),
        ("like_count", "like"),
    ):
        try:
            btn = await element.query_selector(f"[data-testid='{testid}']")
            if btn:
                txt = await btn.inner_text()
                data[key] = txt.strip() or "0"
        except Exception:
            pass
    return data


async def _scroll_collect_users(page: Page, limit: int, max_scrolls: int = 10) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    seen: set[str] = set()
    scrolls = 0
    stale_rounds = 0
    while len(users) < limit and scrolls < max_scrolls:
        before = len(users)
        cells = await page.query_selector_all("[data-testid='UserCell']")
        for cell in cells:
            try:
                handle = None
                try:
                    link = await cell.query_selector("a[role='link']")
                    if link:
                        href = await link.get_attribute("href")
                        if href and href.startswith("/"):
                            handle = href.lstrip("/").split("/")[0]
                except Exception:
                    pass
                if not handle or handle in seen:
                    continue
                seen.add(handle)
                txt = await cell.inner_text()
                lines = [l for l in txt.split("\n") if l.strip()]
                users.append({
                    "handle": handle,
                    "url": f"https://x.com/{handle}",
                    "display_name": lines[0] if lines else None,
                    "bio_snippet": lines[2] if len(lines) > 2 else None,
                })
                if len(users) >= limit:
                    break
            except Exception:
                continue
        if len(users) == before:
            stale_rounds += 1
            if stale_rounds >= 2:
                break
        else:
            stale_rounds = 0
        if len(users) < limit:
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(800)
        scrolls += 1
    return users[:limit]


async def _open_tweet_menu(page: Page, label_regex: str) -> bool:
    try:
        caret = page.locator("[data-testid='caret']").first
        await caret.click(timeout=4000)
        await page.wait_for_timeout(400)
        item = page.locator(f"[role='menuitem']:has-text(/{label_regex}/i)").first
        await item.click(timeout=4000)
        await page.wait_for_timeout(800)
        return True
    except Exception as e:
        _log(f"[menu {label_regex}] failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Internal implementations (private — called by the 9 gateway tools)
# ---------------------------------------------------------------------------

async def _do_post_tweet(text: str) -> dict[str, Any]:
    try:
        text = validate_tweet_text(text, allow_premium=True)
    except ValidationError as ve:
        return err_result(str(ve), tweet_text=text)
    async with use_page() as page:
        try:
            await goto_x(page, "/compose/post", wait_until="domcontentloaded")
            textbox = page.locator("[data-testid='tweetTextarea_0']").first
            await textbox.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await textbox.fill(text)
            posted = await click_data_testid(page, "tweetButton", timeout=5000)
            if not posted:
                shot = await save_debug_screenshot(page, "post_tweet_fail")
                return {"success": False, "message": "Post button not clickable",
                        "tweet_text": text, "screenshot": shot}
            await page.wait_for_timeout(1000)
            return {"success": True, "message": "Tweet posted",
                    "tweet_text": text, "screenshot": ""}
        except Exception as e:
            shot = await save_debug_screenshot(page, "post_tweet_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_text": text}


async def _do_post_thread(tweets: list[str]) -> dict[str, Any]:
    if not tweets:
        return err_result("tweets list is empty")
    errors: list[str] = []
    posted_texts: list[str] = []
    for i, text in enumerate(tweets):
        try:
            text = validate_tweet_text(text, allow_premium=True)
        except ValidationError as ve:
            errors.append(f"Tweet {i + 1}: {ve}")
            continue
        async with use_page() as page:
            try:
                await goto_x(page, "/compose/post", wait_until="domcontentloaded")
                textbox = page.locator("[data-testid='tweetTextarea_0']").first
                await textbox.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
                await textbox.fill(text)
                await page.wait_for_timeout(500)
                posted = await click_data_testid(page, "tweetButton", timeout=5000)
                if not posted:
                    errors.append(f"Tweet {i + 1}: button not clickable")
                    continue
                await page.wait_for_timeout(2500)
                posted_texts.append(text)
            except Exception as e:
                errors.append(f"Tweet {i + 1}: {e}")
    return {
        "success": len(errors) == 0,
        "posted_count": len(posted_texts),
        "posted": posted_texts,
        "errors": errors,
    }


async def _do_post_media(text: str, media_paths: list[str]) -> dict[str, Any]:
    try:
        text = validate_tweet_text(text, allow_premium=True)
    except ValidationError as ve:
        return err_result(str(ve), tweet_text=text)
    if not media_paths:
        return err_result("media_paths is empty", tweet_text=text)
    resolved: list[str] = []
    for p in media_paths:
        rp = os.path.abspath(os.path.expanduser(p))
        if os.path.isfile(rp):
            resolved.append(rp)
        else:
            return err_result(f"file not found: {p}", tweet_text=text,
                              media_count=len(media_paths))
    async with use_page() as page:
        try:
            await goto_x(page, "/compose/post", wait_until="domcontentloaded")
            textbox = page.locator("[data-testid='tweetTextarea_0']").first
            await textbox.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await textbox.fill(text)
            await page.wait_for_timeout(500)
            for rp in resolved:
                try:
                    chooser = page.locator("[data-testid='fileInput']").first
                    await chooser.set_input_files(rp)
                    await page.wait_for_timeout(1000)
                except Exception as e:
                    _log(f"[media] upload failed for {rp}: {e}")
            posted = await click_data_testid(page, "tweetButton", timeout=8000)
            if not posted:
                shot = await save_debug_screenshot(page, "post_media_fail")
                return {"success": False, "message": f"Submit failed; screenshot={shot}",
                        "tweet_text": text, "media_count": len(paths)}
            await page.wait_for_timeout(4000)
            return {"success": True, "message": "Tweet with media posted",
                    "tweet_text": text, "media_count": len(paths)}
        except Exception as e:
            shot = await save_debug_screenshot(page, "post_media_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_text": text, "media_count": len(paths)}


async def _do_reply(tweet_url: str, reply_text: str) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
        reply_text = validate_tweet_text(reply_text, allow_premium=True)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            textbox = page.locator("[data-testid='tweetTextarea_0']").first
            await textbox.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await textbox.click()
            await textbox.fill(reply_text)
            await page.wait_for_timeout(400)
            posted = await click_data_testid(page, "tweetButtonInline", timeout=5000)
            if not posted:
                posted = await click_data_testid(page, "tweetButton", timeout=3000)
            if not posted:
                shot = await save_debug_screenshot(page, "reply_fail")
                return {"success": False, "message": f"Reply button not found; screenshot={shot}",
                        "tweet_url": tweet_url}
            await page.wait_for_timeout(2500)
            return {"success": True, "message": "Reply posted", "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, "reply_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_url": tweet_url}


async def _do_quote(tweet_url: str, comment: str) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
        comment = validate_tweet_text(comment, allow_premium=True)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            rt = page.locator("[data-testid='retweet']").first
            await rt.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await rt.click()
            await page.wait_for_timeout(500)
            quote_opt = page.locator("[role='menuitem']").filter(
                has_text=re.compile(r"Quote|Citer", re.I)
            ).first
            await quote_opt.click(timeout=5000)
            await page.wait_for_timeout(1000)
            textbox = page.locator("[data-testid='tweetTextarea_0']").first
            await textbox.wait_for(state="visible", timeout=8000)
            await textbox.fill(comment)
            await page.wait_for_timeout(400)
            posted = await click_data_testid(page, "tweetButton", timeout=5000)
            if not posted:
                shot = await save_debug_screenshot(page, "quote_fail")
                return {"success": False, "message": f"Quote post failed; screenshot={shot}",
                        "tweet_url": tweet_url}
            await page.wait_for_timeout(2500)
            return {"success": True, "message": "Quote tweet posted", "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, "quote_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_url": tweet_url}


async def _do_delete(tweet_url: str) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            menu = page.locator("[data-testid='caret']").first
            await menu.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await menu.click()
            await page.wait_for_timeout(500)
            delete_opt = page.locator("[role='menuitem']").filter(
                has_text=re.compile(r"Delete|Supprimer", re.I)
            ).first
            await delete_opt.click(timeout=5000)
            await page.wait_for_timeout(800)
            confirm = page.locator("[data-testid='confirmationSheetConfirm']").first
            await confirm.click(timeout=5000)
            await page.wait_for_timeout(1000)
            return {"success": True, "message": "Tweet deleted", "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, "delete_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_url": tweet_url}


async def _do_engage_simple(action: str, tweet_url: str, testid: str,
                            confirm_testid: Optional[str] = None) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        return err_result(str(ve), action=action, tweet_url=tweet_url)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            btn = page.locator(f"[data-testid='{testid}']").first
            await btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await btn.click()
            await page.wait_for_timeout(500)
            if confirm_testid:
                try:
                    confirm = page.locator(f"[data-testid='{confirm_testid}']").first
                    await confirm.click(timeout=3000)
                except Exception:
                    pass
            await page.wait_for_timeout(800)
            return {"success": True, "action": action, "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, f"{action}_err")
            return {"success": False, "action": action, "tweet_url": tweet_url,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_engage_retweet(tweet_url: str, on: bool) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        action = "retweet" if on else "unretweet"
        return err_result(str(ve), action=action, tweet_url=tweet_url)
    action = "retweet" if on else "unretweet"
    btn_testid = "retweet" if on else "unretweet"
    confirm_testid = "retweetConfirm" if on else "unretweetConfirm"
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            btn = page.locator(f"[data-testid='{btn_testid}']").first
            await btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await btn.click()
            await page.wait_for_timeout(500)
            confirm = page.locator(f"[data-testid='{confirm_testid}']").first
            await confirm.click(timeout=5000)
            await page.wait_for_timeout(800)
            return {"success": True, "action": action, "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, f"{action}_err")
            return {"success": False, "action": action, "tweet_url": tweet_url,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_pin(tweet_url: str, on: bool) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url)
    action = "pin" if on else "unpin"
    label_regex = ("Pin to your profile|Épingler" if on
                   else "Unpin from your profile|Désépingler")
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            ok = await _open_tweet_menu(page, label_regex)
            if not ok:
                shot = await save_debug_screenshot(page, f"{action}_tweet_fail")
                return {"success": False, "message": f"{action.capitalize()} menu item not found",
                        "tweet_url": tweet_url, "screenshot": shot}
            await page.wait_for_timeout(800)
            return {"success": True, "message": f"Tweet {action}ned", "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, f"{action}_tweet_err")
            return {"success": False, "message": f"Error: {e}; screenshot={shot}",
                    "tweet_url": tweet_url}


async def _do_profile(handle: str) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle)
    async with use_page() as page:
        try:
            await goto_x(page, f"/{h}", wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='UserName']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(800)
            profile: dict[str, Any] = {"handle": h, "url": f"https://x.com/{h}"}
            try:
                name = await page.locator("[data-testid='UserName']").first.inner_text()
                lines = [l for l in name.split("\n") if l.strip()]
                profile["display_name"] = lines[0] if lines else None
            except Exception:
                pass
            try:
                bio = await page.locator("[data-testid='UserDescription']").first.inner_text(timeout=2000)
                profile["bio"] = bio.strip()
            except Exception:
                profile["bio"] = None
            try:
                links = page.locator(f"a[href$='/verified_followers'], a[href$='/followers'], a[href$='/following']")
                count = await links.count()
                for i in range(count):
                    href = await links.nth(i).get_attribute("href")
                    text = (await links.nth(i).inner_text()).strip()
                    if not href:
                        continue
                    if href.endswith("/following"):
                        profile["following_count"] = text.split("\n")[0]
                    elif "followers" in href:
                        profile["followers_count"] = text.split("\n")[0]
            except Exception:
                pass
            try:
                join = await page.locator("[data-testid='UserJoinDate']").first.inner_text(timeout=1500)
                profile["joined"] = join.strip()
            except Exception:
                pass
            try:
                loc = await page.locator("[data-testid='UserLocation']").first.inner_text(timeout=1000)
                profile["location"] = loc.strip()
            except Exception:
                pass
            try:
                url_link = await page.locator("[data-testid='UserUrl']").first.inner_text(timeout=1000)
                profile["website"] = url_link.strip()
            except Exception:
                pass
            return {"success": True, "profile": profile}
        except Exception as e:
            shot = await save_debug_screenshot(page, "profile_err")
            return {"success": False, "handle": h,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_timeline(handle: str, tab: str, limit: int) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
        limit = validate_limit(limit, lo=1, hi=50)
        if tab not in {"tweets", "likes", "media"}:
            raise ValidationError(f"tab must be tweets/likes/media, got {tab!r}")
    except ValidationError as ve:
        return err_result(str(ve), handle=handle, tab=tab, tweets=[], count=0)
    path = f"/{h}" if tab == "tweets" else f"/{h}/{tab}"
    empty_msg = {"tweets": "No tweets found",
                 "likes": "Likes not visible or empty",
                 "media": "No media"}[tab]
    async with use_page() as page:
        try:
            await goto_x(page, path, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                return {"success": True, "handle": h, "tab": tab, "count": 0,
                        "tweets": [], "message": empty_msg}
            tweets = await _scroll_collect_tweets(page, limit)
            return {"success": True, "handle": h, "tab": tab,
                    "count": len(tweets), "tweets": tweets}
        except Exception as e:
            shot = await save_debug_screenshot(page, "timeline_err")
            return {"success": False, "handle": h, "tab": tab, "tweets": [],
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_connections(handle: str, kind: str, limit: int) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
        limit = validate_limit(limit, lo=1, hi=100)
        if kind not in {"followers", "following"}:
            raise ValidationError(f"kind must be followers/following, got {kind!r}")
    except ValidationError as ve:
        return err_result(str(ve), handle=handle, kind=kind, users=[])
    async with use_page() as page:
        try:
            if kind == "followers":
                await goto_x(page, f"/{h}/verified_followers", wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector("[data-testid='UserCell']", timeout=8000)
                except Exception:
                    await goto_x(page, f"/{h}/followers", wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector("[data-testid='UserCell']", timeout=8000)
                    except Exception:
                        return {"success": True, "handle": h, "kind": kind, "count": 0,
                                "users": [], "message": "No followers visible"}
            else:
                await goto_x(page, f"/{h}/following", wait_until="domcontentloaded")
                try:
                    await page.wait_for_selector("[data-testid='UserCell']", timeout=10000)
                except Exception:
                    return {"success": True, "handle": h, "kind": kind, "count": 0,
                            "users": [], "message": "No following visible"}
            users = await _scroll_collect_users(page, limit)
            return {"success": True, "handle": h, "kind": kind,
                    "count": len(users), "users": users}
        except Exception as e:
            return {"success": False, "handle": h, "kind": kind,
                    "users": [], "message": f"Error: {e}"}


async def _do_follow(handle: str, on: bool) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle)
    action = "follow" if on else "unfollow"
    async with use_page() as page:
        try:
            await goto_x(page, f"/{h}", wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='UserName']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(1000)
            if on:
                btn_testid = "follow"
                confirm_testid = None
            else:
                btn_testid = "unfollow"
                confirm_testid = "unfollowConfirm"
            btn = page.locator(f"[data-testid='{btn_testid}']").first
            await btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await btn.click()
            await page.wait_for_timeout(500)
            if confirm_testid:
                try:
                    confirm = page.locator(f"[data-testid='{confirm_testid}']").first
                    await confirm.click(timeout=3000)
                except Exception:
                    pass
            await page.wait_for_timeout(800)
            return {"success": True, "action": action, "handle": h}
        except Exception as e:
            shot = await save_debug_screenshot(page, f"{action}_err")
            return {"success": False, "action": action, "handle": h,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_block(handle: str) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle)
    async with use_page() as page:
        try:
            await goto_x(page, f"/{h}", wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='UserName']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(1000)
            more = page.locator("[data-testid='userActions']").first
            await more.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await more.click()
            await page.wait_for_timeout(500)
            block_item = page.locator("[role='menuitem']").filter(
                has_text=re.compile(r"Block|Bloquer", re.I)
            ).first
            await block_item.click(timeout=5000)
            await page.wait_for_timeout(800)
            confirm = page.locator("[data-testid='confirmationSheetConfirm']").first
            await confirm.click(timeout=5000)
            await page.wait_for_timeout(800)
            return {"success": True, "message": f"Blocked @{h}", "handle": h}
        except Exception as e:
            shot = await save_debug_screenshot(page, "block_err")
            return {"success": False, "handle": h,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_mute(handle: str) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle)
    async with use_page() as page:
        try:
            await goto_x(page, f"/{h}", wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='UserName']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(1000)
            more = page.locator("[data-testid='userActions']").first
            await more.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await more.click()
            await page.wait_for_timeout(500)
            mute_item = page.locator("[role='menuitem']").filter(
                has_text=re.compile(r"Mute|Mettre en sourdine", re.I)
            ).first
            await mute_item.click(timeout=5000)
            await page.wait_for_timeout(800)
            return {"success": True, "message": f"Muted @{h}", "handle": h}
        except Exception as e:
            shot = await save_debug_screenshot(page, "mute_err")
            return {"success": False, "handle": h,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_search(query: str, limit: int, filter: str) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), tweets=[], count=0)
    encoded = urllib.parse.quote(query)
    search_url = f"https://x.com/search?q={encoded}&src=typed_query"
    if filter in {"top", "latest", "people", "media"}:
        search_url += f"&f={filter}"
    async with use_page() as page:
        try:
            await goto_x(page, search_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                return {"success": True, "count": 0, "tweets": [],
                        "message": "No tweets found", "query": query}
            tweets = await _scroll_collect_tweets(page, limit)
            return {"success": True, "count": len(tweets), "tweets": tweets, "query": query}
        except Exception as e:
            return {"success": False, "tweets": [], "message": f"Error: {e}", "query": query}


async def _do_home_timeline(limit: int, tab: str) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), tab=tab, tweets=[], count=0)
    if tab.lower() not in {"for_you", "following"}:
        return err_result(f"tab must be 'for_you' or 'following', got {tab!r}",
                          tab=tab, tweets=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, "/home", wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            if tab.lower() == "following":
                try:
                    following = page.locator("[role='tab']").filter(
                        has_text=re.compile(r"Following|Abonnements", re.I)
                    ).first
                    await following.click(timeout=3000)
                    await page.wait_for_timeout(800)
                except Exception:
                    pass
            tweets = await _scroll_collect_tweets(page, limit)
            return {"success": True, "tab": tab, "count": len(tweets), "tweets": tweets}
        except Exception as e:
            return {"success": False, "tab": tab, "tweets": [], "message": f"Error: {e}"}


async def _do_bookmarks(limit: int) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), tweets=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, "/i/bookmarks", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='tweet']", timeout=10000)
            except Exception:
                return {"success": True, "count": 0, "tweets": [], "message": "No bookmarks"}
            tweets = await _scroll_collect_tweets(page, limit)
            return {"success": True, "count": len(tweets), "tweets": tweets}
        except Exception as e:
            return {"success": False, "tweets": [], "message": f"Error: {e}"}


async def _do_notifications(limit: int) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=100)
    except ValidationError as ve:
        return err_result(str(ve), notifications=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, "/notifications", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            cells = await page.query_selector_all("[data-testid='cellInnerDiv']")
            notifs: list[dict[str, Any]] = []
            for cell in cells[:limit]:
                try:
                    text = await cell.inner_text()
                    clean = "\n".join([l for l in text.split("\n") if l.strip()])
                    if clean:
                        notifs.append({"text": clean})
                except Exception:
                    continue
            return {"success": True, "count": len(notifs), "notifications": notifs}
        except Exception as e:
            return {"success": False, "notifications": [], "message": f"Error: {e}"}


async def _do_trending(limit: int) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=30)
    except ValidationError as ve:
        return err_result(str(ve), trends=[])
    async with use_page() as page:
        try:
            await goto_x(page, "/explore/tabs/trending", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='trend']", timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                await goto_x(page, "/explore", wait_until="domcontentloaded")
                await page.wait_for_selector("[data-testid='trend']", timeout=10000)
            await page.wait_for_timeout(1000)
            elements = await page.query_selector_all("[data-testid='trend']")
            trends: list[dict[str, Any]] = []
            for el in elements[:limit]:
                try:
                    text = await el.inner_text()
                    lines = [l for l in text.split("\n") if l.strip()]
                    # DOM: ['1', '·', 'Category · Trending', 'TrendName']
                    # The rank number and separator are always first two lines
                    name = None
                    category = None
                    if len(lines) >= 4:
                        category = lines[2]
                        name = lines[3]
                    elif len(lines) >= 3:
                        category = lines[1] if lines[1] != "·" else lines[2] if len(lines) > 2 else None
                        name = lines[-1]
                    elif len(lines) >= 1:
                        name = lines[-1]
                    trends.append({
                        "category": category,
                        "name": name,
                        "raw": " | ".join(lines),
                    })
                except Exception:
                    continue
            return {"success": True, "count": len(trends), "trends": trends}
        except Exception as e:
            return {"success": False, "trends": [], "message": f"Error: {e}"}


async def _do_tweet_details(tweet_url: str) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(1000)
            element = page.locator("[data-testid='tweet']").first
            data = await extract_tweet_data(await element.element_handle())
            data["tweet_url"] = tweet_url
            data["tweet_id"] = extract_tweet_id(tweet_url)
            return {"success": True, "tweet": data}
        except Exception as e:
            return {"success": False, "tweet_url": tweet_url, "message": f"Error: {e}"}


async def _do_tweet_replies(tweet_url: str, limit: int) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), tweet_url=tweet_url, replies=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(800)
            replies = await _scroll_collect_tweets(page, limit + 1)
            return {"success": True, "tweet_url": tweet_url,
                    "count": max(0, len(replies) - 1), "replies": replies[1:]}
        except Exception as e:
            return {"success": False, "tweet_url": tweet_url, "replies": [],
                    "message": f"Error: {e}"}


async def _do_tweet_analytics(tweet_url: str) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
    except ValidationError as ve:
        return err_result(str(ve), metrics={}, raw_text="")
    tid = extract_tweet_id(tweet_url)
    analytics_url = f"https://x.com/i/status/{tid}/analytics" if tid else f"{tweet_url}/analytics"
    async with use_page() as page:
        try:
            await goto_x(page, analytics_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            try:
                raw = await page.locator("main").first.inner_text(timeout=5000)
            except Exception:
                raw = await page.inner_text("body")
            return {"success": True, "metrics": {}, "raw_text": raw[:4000],
                    "tweet_url": tweet_url}
        except Exception as e:
            shot = await save_debug_screenshot(page, "analytics_err")
            return {"success": False, "message": f"Error: {e}",
                    "metrics": {}, "raw_text": "", "screenshot": shot}


async def _do_tweet_engagers(tweet_url: str, kind: str, limit: int) -> dict[str, Any]:
    try:
        tweet_url = validate_tweet_url(tweet_url)
        limit = validate_limit(limit, lo=1, hi=200)
        if kind not in {"likes", "retweets"}:
            raise ValidationError(f"kind must be likes/retweets, got {kind!r}")
    except ValidationError as ve:
        return err_result(str(ve), users=[], count=0, kind=kind)
    err_label = "engagers"
    async with use_page() as page:
        try:
            await goto_x(page, tweet_url, wait_until="domcontentloaded")
            await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            await page.wait_for_timeout(1000)
            if kind == "likes":
                btn = page.locator("[data-testid='like']").first
            else:
                btn = page.locator("[data-testid='retweet']").first
            try:
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1000)
            except Exception:
                return {"success": True, "users": [], "count": 0, "kind": kind,
                        "message": f"Could not open {kind} list"}
            try:
                await page.wait_for_selector("[data-testid='UserCell']", timeout=8000)
            except Exception:
                empty_msg = f"No {kind} visible"
                shot = await save_debug_screenshot(page, f"{err_label}_empty")
                return {"success": True, "users": [], "count": 0, "kind": kind,
                        "message": empty_msg, "screenshot": shot}
            users = await _scroll_collect_users(page, limit)
            return {"success": True, "users": users, "count": len(users), "kind": kind}
        except Exception as e:
            shot = await save_debug_screenshot(page, f"{err_label}_err")
            return {"success": False, "message": f"Error: {e}",
                    "users": [], "count": 0, "kind": kind, "screenshot": shot}


async def _do_get_lists(handle: Optional[str], limit: int) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=50)
        if handle:
            handle = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), lists=[], count=0)
    h = handle or "i"
    async with use_page() as page:
        try:
            await goto_x(page, f"/{h}/lists", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='cellInnerDiv']", timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                return {"success": True, "count": 0, "lists": [],
                        "message": "No lists found"}
            cells = await page.query_selector_all("[data-testid='cellInnerDiv']")
            lists: list[dict[str, Any]] = []
            for cell in cells[:limit]:
                try:
                    text = await cell.inner_text()
                    lines = [l for l in text.split("\n") if l.strip()]
                    if lines:
                        lists.append({
                            "name": lines[0],
                            "member_count": lines[1] if len(lines) > 1 else None,
                        })
                except Exception:
                    continue
            return {"success": True, "count": len(lists), "lists": lists}
        except Exception as e:
            return {"success": False, "lists": [], "message": f"Error: {e}"}


async def _do_get_list_tweets(list_id: str, limit: int) -> dict[str, Any]:
    try:
        list_id = validate_list_id(list_id)
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), tweets=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, f"/i/lists/{list_id}", wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("[data-testid='tweet']", timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                return {"success": True, "count": 0, "tweets": [],
                        "message": "No tweets in list"}
            tweets = await _scroll_collect_tweets(page, limit)
            return {"success": True, "count": len(tweets), "tweets": tweets}
        except Exception as e:
            return {"success": False, "tweets": [], "message": f"Error: {e}"}


async def _do_send_dm(handle: str, message: str) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle)
    if not message or not message.strip():
        return err_result("message is empty", handle=h)
    async with use_page() as page:
        try:
            await goto_x(page, f"/messages/compose?recipient_id={h}", wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            try:
                search = page.locator("[data-testid='searchPeople']").first
                await search.fill(h, timeout=3000)
                await page.wait_for_timeout(1200)
                first_result = page.locator("[data-testid^='TypeaheadUser']").first
                await first_result.click(timeout=3000)
                await page.wait_for_timeout(500)
                next_btn = page.locator("[data-testid='nextButton']").first
                await next_btn.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            textbox = page.locator("[data-testid='dmComposerTextInput']").first
            await textbox.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
            await textbox.fill(message)
            await page.wait_for_timeout(400)
            send = page.locator("[data-testid='dmComposerSendButton']").first
            await send.click(timeout=5000)
            await page.wait_for_timeout(800)
            return {"success": True, "handle": h, "message": "DM sent"}
        except Exception as e:
            shot = await save_debug_screenshot(page, "dm_err")
            return {"success": False, "handle": h,
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_get_conversations(limit: int) -> dict[str, Any]:
    try:
        limit = validate_limit(limit, lo=1, hi=30)
    except ValidationError as ve:
        return err_result(str(ve), conversations=[], count=0)
    async with use_page() as page:
        try:
            await goto_x(page, "/messages", wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            cells = await page.query_selector_all("[data-testid='conversationEntry']")
            convos: list[dict[str, Any]] = []
            for cell in cells[:limit]:
                try:
                    txt = await cell.inner_text()
                    clean = "\n".join(l for l in txt.split("\n") if l.strip())
                    if clean:
                        convos.append({"text": clean})
                except Exception:
                    continue
            return {"success": True, "count": len(convos), "conversations": convos}
        except Exception as e:
            return {"success": False, "conversations": [], "message": f"Error: {e}"}


async def _do_get_messages(handle: str, limit: int) -> dict[str, Any]:
    try:
        h = validate_handle(handle)
        limit = validate_limit(limit, lo=1, hi=50)
    except ValidationError as ve:
        return err_result(str(ve), handle=handle, messages=[])
    async with use_page() as page:
        try:
            await goto_x(page, "/messages", wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            try:
                link = page.locator(f"a[href*='/messages/{h}']").first
                await link.click(timeout=4000)
            except Exception:
                await goto_x(page, f"/messages/compose?recipient_id={h}", wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            cells = await page.query_selector_all("[data-testid='messageEntry']")
            if not cells:
                cells = await page.query_selector_all("[data-testid='conversationEntry']")
            msgs: list[dict[str, Any]] = []
            for cell in cells[-limit:]:
                try:
                    txt = await cell.inner_text()
                    clean = "\n".join(l for l in txt.split("\n") if l.strip())
                    if clean:
                        msgs.append({"text": clean})
                except Exception:
                    continue
            return {"success": True, "handle": h, "count": len(msgs), "messages": msgs}
        except Exception as e:
            shot = await save_debug_screenshot(page, "msgs_err")
            return {"success": False, "handle": h, "messages": [],
                    "message": f"Error: {e}; screenshot={shot}"}


async def _do_check_session() -> dict[str, Any]:
    async with use_page() as page:
        try:
            await goto_x(page, "/home", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            url = page.url
            logged_in = "/home" in url
            handle = None
            if logged_in:
                try:
                    side_link = page.locator("[data-testid='AppTabBar_Profile_Link']").first
                    href = await side_link.get_attribute("href", timeout=3000)
                    if href:
                        handle = href.lstrip("/")
                except Exception:
                    pass
            return {"logged_in": logged_in, "current_url": url, "user_handle": handle}
        except Exception as e:
            return {"logged_in": False, "current_url": "", "user_handle": None,
                    "message": f"Error: {e}"}


async def _do_screenshot(url: str, full_page: bool) -> dict[str, Any]:
    async with use_page() as page:
        try:
            await goto_x(page, url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
            path = await save_debug_screenshot(page, "manual")
            if full_page and path:
                try:
                    await page.screenshot(path=path, full_page=True)
                except Exception:
                    pass
            return {"success": True, "screenshot_path": path, "url": page.url}
        except Exception as e:
            return {"success": False, "screenshot_path": "", "message": f"Error: {e}"}


# ---------------------------------------------------------------------------
# TOOL TAXONOMY (9 grouped tools)
# ---------------------------------------------------------------------------

TOOLS_BY_CATEGORY: dict[str, list[str]] = {
    "posting": ["x_post"],
    "engagement": ["x_engage"],
    "users": ["x_user"],
    "search": ["x_search"],
    "feeds": ["x_feed"],
    "tweet": ["x_tweet"],
    "lists": ["x_list"],
    "messaging": ["x_dm"],
    "session": ["x_session"],
}


def category_for(tool_name: str) -> Optional[str]:
    for cat, names in TOOLS_BY_CATEGORY.items():
        if tool_name in names:
            return cat
    return None


# ---------------------------------------------------------------------------
# 9 GROUPED GATEWAY TOOLS
# ---------------------------------------------------------------------------

@mcp.tool()
async def x_post(
    action: str,
    text: str = "",
    tweets: str = "",
    media_paths: str = "",
    tweet_url: str = "",
    reply_text: str = "",
    comment: str = "",
) -> dict[str, Any]:
    """Post, reply, quote, thread, post with media, or delete tweets.

    Args:
        action: What to do. One of:
            - "post": Post a single tweet. Requires: text.
            - "thread": Post a thread. Requires: tweets (newline-separated list of tweet texts).
            - "post_media": Post with images/video. Requires: text, media_paths (comma-separated file paths).
            - "reply": Reply to a tweet. Requires: tweet_url, reply_text.
            - "quote": Quote-tweet. Requires: tweet_url, comment.
            - "delete": Delete your tweet. Requires: tweet_url.
        text: Tweet text (for post, post_media). Max 280 chars free / 25000 premium.
        tweets: Newline-separated tweet texts for a thread (for thread).
        media_paths: Comma-separated local file paths for media upload (for post_media). Up to 4 images or 1 video.
        tweet_url: Full tweet URL like https://x.com/user/status/123 (for reply, quote, delete).
        reply_text: Reply body text (for reply).
        comment: Your comment for a quote-tweet (for quote).
    """
    rate_err = await check_rate_limit(action)
    if rate_err:
        return err_result(rate_err)
    if action == "post":
        return await _do_post_tweet(text)
    elif action == "thread":
        tweet_list = [t.strip() for t in tweets.split("\n") if t.strip()]
        return await _do_post_thread(tweet_list)
    elif action == "post_media":
        paths = [p.strip() for p in media_paths.split(",") if p.strip()]
        return await _do_post_media(text, paths)
    elif action == "reply":
        return await _do_reply(tweet_url, reply_text)
    elif action == "quote":
        return await _do_quote(tweet_url, comment)
    elif action == "delete":
        return await _do_delete(tweet_url)
    else:
        return err_result(f"Unknown action: {action!r}. Use post/thread/post_media/reply/quote/delete.")


@mcp.tool()
async def x_engage(
    action: str,
    tweet_url: str,
    on: bool = True,
) -> dict[str, Any]:
    """Like, retweet, bookmark, or pin/unpin a tweet.

    Args:
        action: One of "like", "retweet", "bookmark", "pin".
        tweet_url: Full tweet URL like https://x.com/user/status/123.
        on: True to do the action, False to undo (default True). E.g. on=False with action="like" unlikes.
    """
    rate_err = await check_rate_limit(action)
    if rate_err:
        return err_result(rate_err)
    if action == "like":
        return await _do_engage_simple("like" if on else "unlike", tweet_url, "like" if on else "unlike")
    elif action == "retweet":
        return await _do_engage_retweet(tweet_url, on)
    elif action == "bookmark":
        return await _do_engage_simple("bookmark" if on else "unbookmark", tweet_url,
                                        "bookmark" if on else "removeBookmark")
    elif action == "pin":
        return await _do_pin(tweet_url, on)
    else:
        return err_result(f"Unknown action: {action!r}. Use like/retweet/bookmark/pin.")


@mcp.tool()
async def x_user(
    action: str,
    handle: str,
    tab: str = "tweets",
    kind: str = "followers",
    limit: int = 20,
    on: bool = True,
) -> dict[str, Any]:
    """Get user info, timeline, connections, follow/unfollow, block, or mute.

    Args:
        action: One of "profile", "timeline", "connections", "follow", "block", "mute".
        handle: @username, username, or profile URL.
        tab: For timeline: "tweets" (default), "likes", or "media".
        kind: For connections: "followers" (default) or "following".
        limit: Max items to return (default 20, max 100).
        on: For follow: True to follow (default), False to unfollow.
    """
    rate_err = await check_rate_limit(action)
    if rate_err:
        return err_result(rate_err)
    if action == "profile":
        return await _do_profile(handle)
    elif action == "timeline":
        return await _do_timeline(handle, tab, limit)
    elif action == "connections":
        return await _do_connections(handle, kind, limit)
    elif action == "follow":
        return await _do_follow(handle, on)
    elif action == "block":
        return await _do_block(handle)
    elif action == "mute":
        return await _do_mute(handle)
    else:
        return err_result(f"Unknown action: {action!r}. Use profile/timeline/connections/follow/block/mute.")


@mcp.tool()
async def x_search(
    query: str,
    limit: int = 10,
    filter: str = "top",
    from_user: str = "",
    to_user: str = "",
    since: str = "",
    until: str = "",
    min_likes: int = 0,
    min_retweets: int = 0,
    lang: str = "",
) -> dict[str, Any]:
    """Search tweets with simple or advanced filters.

    Args:
        query: Base keywords to search for.
        limit: Max tweets to return (default 10, max 50).
        filter: One of "top" (default), "latest", "people", "media".
        from_user: Filter tweets from this @user (advanced).
        to_user: Filter tweets replying to this @user (advanced).
        since: Only tweets after this date, YYYY-MM-DD (advanced).
        until: Only tweets before this date, YYYY-MM-DD (advanced).
        min_likes: Minimum likes filter (advanced, 0 = disabled).
        min_retweets: Minimum retweets filter (advanced, 0 = disabled).
        lang: Language code like "en", "fr" (advanced, empty = any).
    """
    _ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    try:
        limit = validate_limit(limit, lo=1, hi=50)
        if filter not in {"latest", "top", "people", "media"}:
            raise ValidationError(f"filter must be one of latest/top/people/media, got {filter!r}")
        if from_user:
            from_user = validate_handle(from_user)
        if to_user:
            to_user = validate_handle(to_user)
        for label, val in (("since", since), ("until", until)):
            if val and not _ISO_DATE.match(val):
                raise ValidationError(f"{label} must be YYYY-MM-DD, got {val!r}")
        if lang and not re.match(r"^[a-z]{2,3}$", lang):
            raise ValidationError(f"lang must be 2-3 letter ISO code, got {lang!r}")
    except ValidationError as ve:
        return err_result(str(ve), tweets=[])

    parts = [query] if query else []
    if from_user:
        parts.append(f"from:{from_user}")
    if to_user:
        parts.append(f"to:{to_user}")
    if since:
        parts.append(f"since:{since}")
    if until:
        parts.append(f"until:{until}")
    if min_likes > 0:
        parts.append(f"min_faves:{min_likes}")
    if min_retweets > 0:
        parts.append(f"min_retweets:{min_retweets}")
    if lang:
        parts.append(f"lang:{lang}")
    full_query = " ".join(parts).strip()
    if not full_query:
        return err_result("Empty query", tweets=[])
    return await _do_search(full_query, limit=limit, filter=filter)


@mcp.tool()
async def x_feed(
    kind: str,
    limit: int = 10,
    tab: str = "for_you",
) -> dict[str, Any]:
    """Browse your home timeline, bookmarks, notifications, or trending topics.

    Args:
        kind: One of "home", "bookmarks", "notifications", "trending".
        limit: Max items (default 10, max 100).
        tab: For home feed only: "for_you" (default) or "following".
    """
    if kind == "home":
        return await _do_home_timeline(limit, tab)
    elif kind == "bookmarks":
        return await _do_bookmarks(limit)
    elif kind == "notifications":
        return await _do_notifications(limit)
    elif kind == "trending":
        return await _do_trending(limit)
    else:
        return err_result(f"Unknown kind: {kind!r}. Use home/bookmarks/notifications/trending.")


@mcp.tool()
async def x_tweet(
    action: str,
    tweet_url: str,
    limit: int = 10,
    kind: str = "likes",
) -> dict[str, Any]:
    """Get tweet details, replies, analytics, or who engaged (liked/retweeted).

    Args:
        action: One of "details", "replies", "analytics", "engagers".
        tweet_url: Full tweet URL like https://x.com/user/status/123.
        limit: For replies/engagers: max items (default 10, max 200).
        kind: For engagers only: "likes" (default) or "retweets".
    """
    if action == "details":
        return await _do_tweet_details(tweet_url)
    elif action == "replies":
        return await _do_tweet_replies(tweet_url, limit)
    elif action == "analytics":
        return await _do_tweet_analytics(tweet_url)
    elif action == "engagers":
        return await _do_tweet_engagers(tweet_url, kind, limit)
    else:
        return err_result(f"Unknown action: {action!r}. Use details/replies/analytics/engagers.")


@mcp.tool()
async def x_list(
    list_id: str = "",
    handle: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """List your X Lists, or get tweets from a specific list.

    Args:
        list_id: Numeric list ID. If provided, returns tweets from that list.
        handle: @username to get their lists. If empty and no list_id, gets your own lists.
        limit: Max items (default 20, max 50).
    """
    if list_id:
        return await _do_get_list_tweets(list_id, limit)
    return await _do_get_lists(handle or None, limit)


@mcp.tool()
async def x_dm(
    action: str,
    handle: str = "",
    message: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Send, read DMs, or list conversations.

    Args:
        action: One of "send", "conversations", "messages".
        handle: @username for send/messages.
        message: Message text (for send).
        limit: Max items for conversations/messages (default 20).
    """
    if action == "send":
        rate_err = await check_rate_limit("send")
        if rate_err:
            return err_result(rate_err)
        return await _do_send_dm(handle, message)
    elif action == "conversations":
        return await _do_get_conversations(limit)
    elif action == "messages":
        return await _do_get_messages(handle, limit)
    else:
        return err_result(f"Unknown action: {action!r}. Use send/conversations/messages.")


@mcp.tool()
async def x_session(
    action: str,
    url: str = "/home",
    full_page: bool = False,
) -> dict[str, Any]:
    """Check login status or take a screenshot of any X page.

    Args:
        action: One of "check" (verify session), "screenshot" (capture a page).
        url: x.com URL or path to screenshot (for screenshot). Default "/home".
        full_page: If True, capture the full scrollable page (for screenshot).
    """
    if action == "check":
        return await _do_check_session()
    elif action == "screenshot":
        return await _do_screenshot(url, full_page)
    else:
        return err_result(f"Unknown action: {action!r}. Use check/screenshot.")


# ---------------------------------------------------------------------------
# MCP RESOURCES & PROMPTS
# ---------------------------------------------------------------------------

@mcp.resource("x://session/status")
async def session_status_resource() -> str:
    info = await _do_check_session()
    return (
        f"Logged in: {info.get('logged_in')}\n"
        f"Handle: {info.get('user_handle') or 'unknown'}\n"
        f"URL: {info.get('current_url') or '-'}\n"
    )


@mcp.resource("x://version")
async def version_resource() -> str:
    return f"x-mcp-playwright {__version__}"


@mcp.resource("x://tools/list")
async def tools_list_resource() -> str:
    tools = sorted(mcp._tool_manager._tools.keys())
    resources = sorted(mcp._resource_manager._resources.keys())
    prompts = sorted(mcp._prompt_manager._prompts.keys())
    lines = [f"version: {__version__}", "",
             f"tools ({len(tools)}):"]
    lines += [f"  - {t}" for t in tools]
    lines += ["", f"resources ({len(resources)}):"]
    lines += [f"  - {r}" for r in resources]
    lines += ["", f"prompts ({len(prompts)}):"]
    lines += [f"  - {p}" for p in prompts]
    return "\n".join(lines)


@mcp.resource("x://tools/categories")
async def tools_categories_resource() -> str:
    registered = set(mcp._tool_manager._tools.keys())
    categorized: set[str] = set()
    lines = [f"version: {__version__}", ""]
    for cat in sorted(TOOLS_BY_CATEGORY):
        names = TOOLS_BY_CATEGORY[cat]
        lines.append(f"{cat} ({len(names)}):")
        for n in names:
            mark = "" if n in registered else "  [MISSING]"
            lines.append(f"  - {n}{mark}")
            categorized.add(n)
        lines.append("")
    leftover = sorted(registered - categorized)
    if leftover:
        lines.append(f"uncategorized ({len(leftover)}):")
        lines += [f"  - {n}" for n in leftover]
    return "\n".join(lines).rstrip() + "\n"


@mcp.prompt()
def analyze_tweet(tweet_url: str) -> str:
    return (
        f"Use x_tweet with action='details' to fetch {tweet_url}. Then provide:\n"
        "1. Summary of the content\n"
        "2. Sentiment (positive / neutral / negative)\n"
        "3. Engagement metrics interpretation\n"
        "4. Suggested response tone if you were to reply\n"
    )


@mcp.prompt()
def draft_reply(tweet_url: str, intent: str = "agree warmly") -> str:
    return (
        f"Use x_tweet with action='details' on {tweet_url} to read the tweet.\n"
        f"Then draft a reply with this intent: {intent}.\n"
        "Keep under 280 chars. Avoid emojis unless the original used them.\n"
        "Present 3 variants for me to choose from."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _start_keepalive()
    try:
        mcp.run()
    finally:
        _stop_keepalive()
        try:
            asyncio.get_event_loop().run_until_complete(BROWSER.shutdown())
        except Exception:
            pass