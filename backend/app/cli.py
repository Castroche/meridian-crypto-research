from __future__ import annotations

import argparse
import asyncio
import sys

from .config import settings
from .service import ResearchService
from .storage_factory import create_report_store


async def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Meridian report operations")
    parser.add_argument("command", choices=["generate", "latest"])
    args = parser.parse_args()
    store = create_report_store(settings)
    if args.command == "generate":
        report = await ResearchService(settings, store).generate()
    else:
        report = store.latest()
        if report is None:
            raise SystemExit("No report generated yet")
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
