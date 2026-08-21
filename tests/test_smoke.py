"""Smoke tests — verify the MCP server registers all 9 grouped tools and helpers work.

These tests do NOT touch the browser or X.com. They validate:
  * Server boots and registers every expected tool/resource/prompt
  * Pydantic models accept/reject expected shapes
  * `with_retry` actually retries and re-raises after exhaustion
  * `find_chrome` returns either None or a real path
  * Validators work correctly
  * Tool taxonomy stays in sync with the registry

Run with:
    python -m pytest tests -v
or:
    python -m unittest tests.test_smoke -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import x_mcp_server as srv  # noqa: E402


# 9 grouped tools — every action is preserved as a discriminator value
EXPECTED_TOOLS = {
    "x_post",
    "x_engage",
    "x_user",
    "x_search",
    "x_feed",
    "x_tweet",
    "x_list",
    "x_dm",
    "x_session",
}

EXPECTED_RESOURCES = {
    "x://session/status",
    "x://version",
    "x://tools/list",
    "x://tools/categories",
}


class RegistrationTest(unittest.TestCase):
    def test_all_tools_registered(self) -> None:
        actual = set(srv.mcp._tool_manager._tools.keys())
        missing = EXPECTED_TOOLS - actual
        self.assertFalse(missing, f"missing tools: {sorted(missing)}")

    def test_no_duplicate_tools(self) -> None:
        names = list(srv.mcp._tool_manager._tools.keys())
        self.assertEqual(len(names), len(set(names)), "duplicate tool names")

    def test_exactly_nine_tools(self) -> None:
        actual = set(srv.mcp._tool_manager._tools.keys())
        self.assertEqual(len(actual), 9, f"expected 9 tools, got {len(actual)}: {sorted(actual)}")

    def test_resources_registered(self) -> None:
        registered = set(srv.mcp._resource_manager._resources.keys())
        missing = EXPECTED_RESOURCES - registered
        self.assertFalse(missing, f"missing resources: {sorted(missing)}")

    def test_version_constant(self) -> None:
        self.assertRegex(srv.__version__, r"^\d+\.\d+\.\d+$")

    def test_prompts_registered(self) -> None:
        prompts = set(srv.mcp._prompt_manager._prompts.keys())
        self.assertIn("analyze_tweet", prompts)
        self.assertIn("draft_reply", prompts)


class RetryDecoratorTest(unittest.TestCase):
    def test_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        @srv.with_retry(max_attempts=3, base_delay=0.01)
        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "ok"

        result = asyncio.run(flaky())
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_exhausts_and_reraises(self) -> None:
        calls = {"n": 0}

        @srv.with_retry(max_attempts=2, base_delay=0.01)
        async def always_fails() -> None:
            calls["n"] += 1
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            asyncio.run(always_fails())
        self.assertEqual(calls["n"], 2)


class HelperTest(unittest.TestCase):
    def test_find_chrome_returns_path_or_none(self) -> None:
        path = srv.find_chrome() if hasattr(srv, "find_chrome") else None
        if path is not None:
            self.assertTrue(os.path.exists(path), f"chrome path not found: {path}")

    def test_extract_tweet_id(self) -> None:
        self.assertEqual(
            srv.extract_tweet_id("https://x.com/foo/status/1234567890"),
            "1234567890",
        )
        self.assertIsNone(srv.extract_tweet_id("https://x.com/foo"))

    def test_normalize_handle(self) -> None:
        self.assertEqual(srv.normalize_handle("@elonmusk"), "elonmusk")
        self.assertEqual(srv.normalize_handle("https://x.com/jack"), "jack")
        self.assertEqual(srv.normalize_handle("https://twitter.com/jack/status/1"), "jack")


class ValidatorTest(unittest.TestCase):
    def test_handle_ok(self) -> None:
        self.assertEqual(srv.validate_handle("@elonmusk"), "elonmusk")
        self.assertEqual(srv.validate_handle("jack"), "jack")

    def test_handle_rejects_empty(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_handle("")

    def test_handle_rejects_too_long(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_handle("a" * 16)

    def test_handle_rejects_bad_chars(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_handle("with spaces")
        with self.assertRaises(srv.ValidationError):
            srv.validate_handle("dash-no")

    def test_tweet_url_ok(self) -> None:
        url = srv.validate_tweet_url("https://x.com/foo/status/1234")
        self.assertEqual(url, "https://x.com/foo/status/1234")

    def test_tweet_url_rejects_non_x(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_tweet_url("https://google.com/foo/status/1")

    def test_tweet_url_rejects_missing_status(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_tweet_url("https://x.com/foo")

    def test_tweet_text_rejects_empty(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_tweet_text("")
        with self.assertRaises(srv.ValidationError):
            srv.validate_tweet_text("   ")

    def test_tweet_text_rejects_too_long(self) -> None:
        with self.assertRaises(srv.ValidationError):
            srv.validate_tweet_text("x" * 281)

    def test_tweet_text_premium_ok(self) -> None:
        self.assertEqual(
            len(srv.validate_tweet_text("x" * 1000, allow_premium=True)),
            1000,
        )

    def test_limit_bounds(self) -> None:
        self.assertEqual(srv.validate_limit(50), 50)
        with self.assertRaises(srv.ValidationError):
            srv.validate_limit(0)
        with self.assertRaises(srv.ValidationError):
            srv.validate_limit(201)
        with self.assertRaises(srv.ValidationError):
            srv.validate_limit(True)  # bool guard
        with self.assertRaises(srv.ValidationError):
            srv.validate_limit("10")  # type: ignore[arg-type]

    def test_list_id(self) -> None:
        self.assertEqual(srv.validate_list_id("12345"), "12345")
        with self.assertRaises(srv.ValidationError):
            srv.validate_list_id("abc")
        with self.assertRaises(srv.ValidationError):
            srv.validate_list_id("")


class CategoryRegistryTest(unittest.TestCase):
    def test_registry_matches_registered_tools(self) -> None:
        registered = set(srv.mcp._tool_manager._tools.keys())
        categorized = {n for names in srv.TOOLS_BY_CATEGORY.values() for n in names}
        uncategorized = registered - categorized
        ghosts = categorized - registered
        self.assertFalse(uncategorized,
                         f"tools registered but not categorized: {sorted(uncategorized)}")
        self.assertFalse(ghosts,
                         f"categories reference tools that don't exist: {sorted(ghosts)}")

    def test_no_tool_in_two_categories(self) -> None:
        seen: dict[str, str] = {}
        for cat, names in srv.TOOLS_BY_CATEGORY.items():
            for n in names:
                if n in seen:
                    self.fail(f"{n!r} appears in both {seen[n]!r} and {cat!r}")
                seen[n] = cat

    def test_category_for_helper(self) -> None:
        self.assertEqual(srv.category_for("x_post"), "posting")
        self.assertEqual(srv.category_for("x_session"), "session")
        self.assertIsNone(srv.category_for("definitely_not_a_tool"))


class ServerInstanceTest(unittest.TestCase):
    def test_mcp_name_set(self) -> None:
        self.assertTrue(srv.mcp.name)

    def test_mcp_name_is_twitter(self) -> None:
        self.assertEqual(srv.mcp.name, "Twitter-Playwright")


class RateLimiterTest(unittest.TestCase):
    def test_read_actions_not_limited(self) -> None:
        result = asyncio.run(srv.check_rate_limit("home"))
        self.assertIsNone(result)

    def test_write_actions_limited(self) -> None:
        # First call should succeed
        result = asyncio.run(srv.check_rate_limit("post"))
        self.assertIsNone(result)
        # Second immediate call should be rate-limited
        result = asyncio.run(srv.check_rate_limit("post"))
        self.assertIsNotNone(result)
        self.assertIn("Rate limit", result)

    def test_engage_actions_limited(self) -> None:
        result = asyncio.run(srv.check_rate_limit("like"))
        self.assertIsNone(result)
        # Bucket has capacity 2, so second call should still pass
        result = asyncio.run(srv.check_rate_limit("like"))
        self.assertIsNone(result)
        # Third immediate call should be rate-limited
        result = asyncio.run(srv.check_rate_limit("like"))
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)