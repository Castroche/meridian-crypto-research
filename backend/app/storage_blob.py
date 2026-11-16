from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import httpx

from .storage import DocumentReportStore, StorageUnavailable


class BlobReportStore(DocumentReportStore):
    """Vercel Blob-backed report store with no network work during construction."""

    def __init__(self, token: str | None, access: str = "private"):
        self.token = token
        self.access = access if access in {"private", "public"} else "private"

    def _sdk(self) -> Any:
        try:
            from vercel import blob
        except ImportError as exc:
            raise StorageUnavailable("Vercel Blob SDK is unavailable") from exc
        return blob

    def _oidc_credentials(self) -> tuple[str, str]:
        store_id = os.getenv("BLOB_STORE_ID", "").removeprefix("store_")
        if not store_id:
            raise StorageUnavailable(
                "Vercel Blob credentials are not configured (expected BLOB_STORE_ID or BLOB_READ_WRITE_TOKEN)"
            )
        try:
            from vercel.oidc import get_vercel_oidc_token_sync

            return get_vercel_oidc_token_sync(), store_id
        except Exception as exc:
            raise StorageUnavailable(f"Vercel Blob OIDC authentication is unavailable: {exc}") from exc

    def _oidc_api_request(
        self,
        method: str,
        endpoint: str = "",
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        json: Any = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            token, store_id = self._oidc_credentials()
            request_headers = {
                "authorization": f"Bearer {token}",
                "x-vercel-blob-store-id": store_id,
                "x-api-version": "12",
                "x-api-blob-request-id": f"{store_id}:{int(time.time() * 1000)}:{attempt}",
                **(headers or {}),
            }
            try:
                response = httpx.request(
                    method,
                    f"https://vercel.com/api/blob{endpoint}",
                    params=params,
                    headers=request_headers,
                    content=content,
                    json=json,
                    timeout=30.0,
                )
                if response.status_code == 403 and "store is blocked" in response.text.lower():
                    raise StorageUnavailable(
                        "Vercel Blob store is blocked, usually because the Hobby usage allowance was exhausted"
                    )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if attempt == 2 or (status is not None and status != 429 and status < 500):
                    raise
                time.sleep(0.25 * (2 ** attempt))
        raise last_error or StorageUnavailable("Vercel Blob request failed")

    def _oidc_blob_url(self, path: str) -> str:
        _token, store_id = self._oidc_credentials()
        encoded_path = "/".join(quote(part, safe="") for part in path.lstrip("/").split("/"))
        return f"https://{store_id}.{self.access}.blob.vercel-storage.com/{encoded_path}"

    def _put_bytes(
        self, path: str, body: bytes, *, content_type: str = "application/json", overwrite: bool = True
    ) -> None:
        try:
            if self.token:
                self._sdk().put(
                    path,
                    body,
                    access=self.access,
                    content_type=content_type,
                    add_random_suffix=False,
                    overwrite=overwrite,
                    token=self.token,
                )
            else:
                self._oidc_api_request(
                    "PUT",
                    params={"pathname": path},
                    headers={
                        "x-vercel-blob-access": self.access,
                        "x-content-type": content_type,
                        "x-add-random-suffix": "0",
                        "x-allow-overwrite": "1" if overwrite else "0",
                    },
                    content=body,
                )
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable(f"Vercel Blob write failed for {path}: {exc}") from exc

    def _get_bytes(self, path: str) -> bytes | None:
        try:
            if self.token:
                result = self._sdk().get(path, access=self.access, token=self.token, use_cache=False)
                return bytes(result) if result is not None else None
            token, store_id = self._oidc_credentials()
            encoded_path = "/".join(quote(part, safe="") for part in path.lstrip("/").split("/"))
            response = httpx.get(
                f"https://{store_id}.{self.access}.blob.vercel-storage.com/{encoded_path}",
                params={"t": str(int(time.time() * 1000))},
                headers={
                    "authorization": f"Bearer {token}",
                    "x-vercel-blob-store-id": store_id,
                },
                follow_redirects=True,
                timeout=30.0,
            )
            if response.status_code == 404:
                return None
            if response.status_code == 403 and "store is blocked" in response.text.lower():
                raise StorageUnavailable(
                    "Vercel Blob store is blocked, usually because the Hobby usage allowance was exhausted"
                )
            response.raise_for_status()
            return response.content
        except StorageUnavailable:
            raise
        except Exception as exc:
            if type(exc).__name__ == "BlobNotFoundError" or "not found" in str(exc).lower():
                return None
            raise StorageUnavailable(f"Vercel Blob read failed for {path}: {exc}") from exc

    def _list_paths(self, prefix: str) -> list[str]:
        try:
            paths: list[str] = []
            cursor = None
            while True:
                if self.token:
                    page = self._sdk().list_objects(
                        prefix=prefix, cursor=cursor, limit=1000, token=self.token
                    )
                    paths.extend(item.pathname for item in page.blobs)
                    if not page.has_more:
                        return paths
                    cursor = page.cursor
                else:
                    params: dict[str, Any] = {"prefix": prefix, "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    payload = self._oidc_api_request("GET", params=params).json()
                    paths.extend(item["pathname"] for item in payload.get("blobs", []))
                    cursor = payload.get("cursor")
                    if not payload.get("hasMore") or not cursor:
                        return paths
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable(f"Vercel Blob listing failed for {prefix}: {exc}") from exc

    def _delete_path(self, path: str) -> bool:
        existed = self._get_bytes(path) is not None
        if not existed:
            return False
        try:
            if self.token:
                self._sdk().delete(path, token=self.token)
            else:
                self._oidc_api_request(
                    "POST",
                    "/delete",
                    headers={"content-type": "application/json"},
                    json={"urls": [self._oidc_blob_url(path)]},
                )
            return True
        except StorageUnavailable:
            raise
        except Exception as exc:
            raise StorageUnavailable(f"Vercel Blob delete failed for {path}: {exc}") from exc
