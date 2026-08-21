# x-mcp-playwright

A **Twitter/X MCP server** powered by [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) (stealth Playwright fork) and a persistent Chrome user profile.

**9 grouped tools — 33 actions preserved, zero functionality lost.**

---

## 1. Install

```bash
git clone <this-repo> x-mcp-playwright
cd x-mcp-playwright

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e .
patchright install chromium
```

### One-time login

```bash
x-mcp-login
# or: python login.py
```

Opens a real browser window. Log in to X normally, then close the window. Session saved in `~/.x-mcp-playwright/profile/`.

---

## 2. Register in your MCP client

### Claude Code

```bash
claude mcp add --scope user x-twitter x-mcp-server
```

### Claude Desktop

```json
{
  "mcpServers": {
    "x-twitter": {
      "command": "x-mcp-server",
      "env": { "X_MCP_HEADLESS": "1" }
    }
  }
}
```

If `x-mcp-server` isn't on PATH, use:

```json
{
  "mcpServers": {
    "x-twitter": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "x_mcp_server"]
    }
  }
}
```

### Docker (headless VPS)

```bash
docker build -t x-mcp-playwright .
docker run -i --rm \
  -v ~/.x-mcp-playwright/profile:/root/.x-mcp-playwright/profile \
  x-mcp-playwright
```

---

## 3. Tool catalog (9 tools, 33 actions)

### `x_post` — Publish tweets (6 actions)

| action | Required args | Description |
|--------|---------------|-------------|
| `post` | `text` | Post a single tweet |
| `thread` | `tweets` (newline-separated) | Post a connected thread |
| `post_media` | `text`, `media_paths` (comma-separated) | Up to 4 images or 1 video |
| `reply` | `tweet_url`, `reply_text` | Reply to a tweet |
| `quote` | `tweet_url`, `comment` | Quote-tweet with comment |
| `delete` | `tweet_url` | Delete your tweet |

### `x_engage` — Interact with tweets (4 actions)

| action | Required args | `on` param |
|--------|---------------|------------|
| `like` | `tweet_url` | `on=True` to like, `False` to unlike |
| `retweet` | `tweet_url` | `on=True` to RT, `False` to undo |
| `bookmark` | `tweet_url` | `on=True` to save, `False` to remove |
| `pin` | `tweet_url` | `on=True` to pin, `False` to unpin |

### `x_user` — User operations (6 actions)

| action | Required args | Optional |
|--------|---------------|----------|
| `profile` | `handle` | — |
| `timeline` | `handle` | `tab="tweets"\|"likes"\|"media"`, `limit` |
| `connections` | `handle` | `kind="followers"\|"following"`, `limit` |
| `follow` | `handle` | `on=True` to follow, `False` to unfollow |
| `block` | `handle` | — |
| `mute` | `handle` | — |

### `x_search` — Search tweets (simple + advanced)

| arg | Default | Description |
|-----|---------|-------------|
| `query` | — | Keywords to search |
| `filter` | `"top"` | `"latest"`, `"people"`, `"media"` |
| `from_user` | — | Filter by author |
| `to_user` | — | Filter by reply-to |
| `since` / `until` | — | Date range (YYYY-MM-DD) |
| `min_likes` | 0 | Minimum likes |
| `min_retweets` | 0 | Minimum retweets |
| `lang` | — | Language code |
| `limit` | 10 | Max results |

### `x_feed` — Browse feeds (4 actions)

| action | Description |
|--------|-------------|
| `home` | Your home timeline. Optional: `tab="for_you"\|"following"` |
| `bookmarks` | Your saved bookmarks |
| `notifications` | Mentions, likes, follows |
| `trending` | What's happening |

### `x_tweet` — Tweet details (4 actions)

| action | Required args | Description |
|--------|---------------|-------------|
| `details` | `tweet_url` | Full tweet with metrics |
| `replies` | `tweet_url` | Replies under a tweet |
| `analytics` | `tweet_url` | Impressions/engagements (own tweets) |
| `engagers` | `tweet_url` | Who liked/retweeted (`kind="likes"\|"retweets"`) |

### `x_list` — Lists

| Scenario | Args |
|----------|------|
| Your lists | No args |
| User's lists | `handle` |
| List tweets | `list_id` |

### `x_dm` — Direct messages (3 actions)

| action | Required args | Description |
|--------|---------------|-------------|
| `send` | `handle`, `message` | Send a DM |
| `conversations` | — | List recent DM threads |
| `messages` | `handle` | Read messages with a user |

### `x_session` — Session management (2 actions)

| action | Description |
|--------|-------------|
| `check` | Returns `{logged_in, current_url, user_handle}` |
| `screenshot` | Capture any X page. Optional: `url`, `full_page` |

---

## 4. Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `X_MCP_HEADLESS` | `1` | `0` to see the browser window |
| `X_MCP_TIMEOUT_MS` | `20000` | Default Playwright timeout (ms) |
| `X_MCP_SCREENSHOT_DIR` | `~/.x-mcp-playwright/screenshots` | Debug screenshots |
| `X_MCP_USE_SYSTEM_CHROME` | `0` | `1` to use system Chrome |

---

## 5. Safety

- **Rate limiter**: Write actions (post, delete, DM, reply) capped at 1/min. Engage actions (like, RT, follow) capped at 2/min. Read actions unlimited.
- **Session keepalive**: Background task pings X every 30 min. Logs warning if session expires.
- **Pinned to your real account.** Anything the LLM does, *you did*.

---

## 6. MCP resources

- `x://session/status` — Login state
- `x://version` — Server version
- `x://tools/list` — All tools
- `x://tools/categories` — Grouped taxonomy

## 7. MCP prompts

- `analyze_tweet(tweet_url)` — Fetch & analyze a tweet (sentiment, engagement, response tone)
- `draft_reply(tweet_url, intent)` — Draft 3 reply variants under 280 chars

---

## 7. Development

```bash
make install     # deps + chromium
make login       # interactive X login
make test        # unit tests (no browser)
make smoke       # live search test
make server      # run MCP server
make lint        # compile-check
make clean       # remove pycache, screenshots
```

---

## 8. Safety

- **Pinned to your real account.** Anything the LLM does, *you did*.
- Cap: <= 1-2 publish/reply actions per minute.
- Gate destructive tools (`delete`, `block`, `send_dm`) behind human approval on VPS.

---

## License

MIT