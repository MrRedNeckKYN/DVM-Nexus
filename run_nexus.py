#!/usr/bin/env python3
"""
DVM Nexus - Runner Script
Starts the DVM Nexus web management service.
"""

import sys
import argparse
import uvicorn
from dvm_nexus.core.config import settings


def main():
    parser = argparse.ArgumentParser(description="DVM Nexus - Web Management Interface for DVMHost")
    parser.add_argument("--host", default=settings.web_host, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=settings.web_port, help="Bind port (default: 8080)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    print("=" * 60)
    print("  DVM Nexus - Modern DVMHost Web Management Interface")
    print(f"  Version: {settings.app_version}")
    print(f"  Config Dir: {settings.dvm_dir}")
    print(f"  Web UI: http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port}")
    print("=" * 60)

    uvicorn.run(
        "dvm_nexus.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
