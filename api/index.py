"""Vercel Serverless entrypoint for the Meridian FastAPI application."""

from __future__ import annotations

import os


if os.getenv("VERCEL"):
    os.environ.setdefault("MERIDIAN_SCHEDULER_ENABLED", "false")

from backend.app.main import app  # noqa: E402,F401
