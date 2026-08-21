# ---------------------------------------------------------------------------
# x-mcp-playwright — Docker image for headless VPS deployment
#
# Build:
#   docker build -t x-mcp-playwright .
#
# Run (mount your session profile):
#   docker run -i --rm \
#     -v ~/.x-mcp-playwright/profile:/root/.x-mcp-playwright/profile \
#     x-mcp-playwright
# ---------------------------------------------------------------------------

FROM python:3.12-slim

# Install system deps for Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && patchright install chromium

COPY *.py ./

# Headless by default, 20s timeout
ENV X_MCP_HEADLESS=1
ENV X_MCP_TIMEOUT_MS=20000

# Profile directory — mount your local profile here
RUN mkdir -p /root/.x-mcp-playwright/profile

ENTRYPOINT ["python", "-m", "x_mcp_server"]