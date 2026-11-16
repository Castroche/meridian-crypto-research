from __future__ import annotations

import os
import tempfile
from threading import RLock
from pathlib import Path

from .storage import DocumentReportStore, StorageUnavailable


class LocalFileReportStore(DocumentReportStore):
    """Local durable store rooted at ./data using the production object layout."""

    _lock_io = RLock()

    def acquire_lock(self, lock_key: str, ttl_seconds: int = 300) -> bool:
        with self._lock_io:
            return super().acquire_lock(lock_key, ttl_seconds)

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _resolve(self, path: str) -> Path:
        target = (self.root / Path(path.replace("/", os.sep))).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("Storage path escapes the configured data directory")
        return target

    def _put_bytes(
        self, path: str, body: bytes, *, content_type: str = "application/json", overwrite: bool = True
    ) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and target.exists():
            raise StorageUnavailable(f"Storage object already exists: {path}")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                handle.write(body)
                temporary_name = handle.name
            if overwrite:
                os.replace(temporary_name, target)
            else:
                try:
                    if os.name == "nt":
                        os.rename(temporary_name, target)
                    else:
                        os.link(temporary_name, target)
                except FileExistsError as exc:
                    raise StorageUnavailable(f"Storage object already exists: {path}") from exc
        except OSError as exc:
            raise StorageUnavailable(f"Local storage write failed for {path}: {exc}") from exc
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def _get_bytes(self, path: str) -> bytes | None:
        try:
            return self._resolve(path).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageUnavailable(f"Local storage read failed for {path}: {exc}") from exc

    def _list_paths(self, prefix: str) -> list[str]:
        prefix_path = self._resolve(prefix)
        try:
            if prefix_path.is_file():
                return [prefix_path.relative_to(self.root).as_posix()]
            if prefix.endswith("/") or prefix_path.is_dir():
                if not prefix_path.exists():
                    return []
                return sorted(
                    path.relative_to(self.root).as_posix() for path in prefix_path.rglob("*") if path.is_file()
                )
            parent = prefix_path.parent
            if not parent.exists():
                return []
            return sorted(
                path.relative_to(self.root).as_posix()
                for path in parent.iterdir()
                if path.is_file() and path.name.startswith(prefix_path.name)
            )
        except OSError as exc:
            raise StorageUnavailable(f"Local storage listing failed for {prefix}: {exc}") from exc

    def _delete_path(self, path: str) -> bool:
        target = self._resolve(path)
        try:
            existed = target.exists()
            target.unlink(missing_ok=True)
            return existed
        except OSError as exc:
            raise StorageUnavailable(f"Local storage delete failed for {path}: {exc}") from exc
