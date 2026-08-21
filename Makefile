.PHONY: help install login test smoke server clean introspect lint format

PYTHON ?= python
VENV_ACTIVATE_WIN := .venv\Scripts\activate
VENV_ACTIVATE_NIX := .venv/bin/activate

help:
	@echo "x-mcp-playwright — Make targets"
	@echo "  make install     Install deps and patchright chromium"
	@echo "  make login       Run interactive one-time X.com login"
	@echo "  make test        Run unit smoke tests (no browser)"
	@echo "  make smoke       Run live X search smoke test (needs login)"
	@echo "  make server      Run MCP server (stdio)"
	@echo "  make introspect  List all registered tools / resources / prompts"
	@echo "  make lint        Compile-check every Python file"
	@echo "  make clean       Remove pycache, screenshots, *.pyc"

install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m patchright install chromium

login:
	$(PYTHON) login.py

test:
	$(PYTHON) -m unittest tests.test_smoke -v

smoke:
	$(PYTHON) fetch_tweets.py "claude opus" 5

server:
	$(PYTHON) x_mcp_server.py

introspect:
	@$(PYTHON) -c "import x_mcp_server as m; \
ts=list(m.mcp._tool_manager._tools.keys()); \
print(f'TOOLS ({len(ts)}):'); \
[print('  -', t) for t in ts]; \
print('RESOURCES:', list(m.mcp._resource_manager._resources.keys())); \
print('PROMPTS:', list(m.mcp._prompt_manager._prompts.keys()))"

lint:
	$(PYTHON) -m py_compile x_mcp_server.py login.py fetch_tweets.py tests/test_smoke.py
	@echo "ALL_OK"

clean:
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type f -name "*.png" -not -path "./.venv/*" -delete 2>/dev/null || true
	@echo "Cleaned."
