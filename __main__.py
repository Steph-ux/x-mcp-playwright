"""Allow running as a module: python -m x_mcp_server"""

import asyncio
import sys

from x_mcp_server import mcp, BROWSER

if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(BROWSER.shutdown())
        except Exception:
            pass