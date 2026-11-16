from __future__ import annotations

import os

from .config import Settings
from .storage import ReportStore
from .storage_blob import BlobReportStore
from .storage_local import LocalFileReportStore


def create_report_store(settings: Settings) -> ReportStore:
    """Select persistence without contacting Blob or touching the filesystem."""
    if os.getenv("VERCEL"):
        return BlobReportStore(settings.blob_read_write_token, settings.blob_access)
    return LocalFileReportStore(settings.data_dir)
