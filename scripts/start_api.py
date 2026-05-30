#!/usr/bin/env python3
"""
Start the StockSage REST API server.

Usage:
  python scripts/start_api.py                        # localhost:8000
  python scripts/start_api.py --host 0.0.0.0 --port 8080
  python scripts/start_api.py --reload               # dev mode
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import uvicorn
from rich.console import Console

_console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="StockSage REST API server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    args = parser.parse_args()

    _console.print(f"\n[bold blue]StockSage API[/] starting on http://{args.host}:{args.port}")
    _console.print(f"[dim]Docs: http://{args.host}:{args.port}/docs[/]")
    _console.print(f"[dim]WebSocket live feed: ws://{args.host}:{args.port}/ws/live[/]\n")

    uvicorn.run(
        "agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
