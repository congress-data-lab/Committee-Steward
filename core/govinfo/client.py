"""Small deterministic client for the GovInfo REST API."""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator
from typing import Any


DEFAULT_BASE_URL = "https://api.govinfo.gov"


class GovInfoApiError(RuntimeError):
    """Raised when GovInfo returns an invalid or unsuccessful response."""


class GovInfoClient:
    """GovInfo REST client with bounded retries and complete cursor pagination."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 60,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("GovInfo API key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _url(self, path_or_url: str, params: dict[str, str] | None = None) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"{self._base_url}{path_or_url}"
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        if params:
            query.update({key: value for key, value in params.items() if value is not None})
        query["api_key"] = self._api_key
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
        )

    @staticmethod
    def _safe_endpoint(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path_or_url, params)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(self._max_retries + 1):
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise GovInfoApiError(
                            f"GovInfo returned non-object JSON for {self._safe_endpoint(url)}"
                        )
                    return decoded
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 503} and attempt < self._max_retries:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else min(60, 2**attempt)
                    time.sleep(delay)
                    continue
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise GovInfoApiError(
                    f"GovInfo HTTP {exc.code} for {self._safe_endpoint(url)}: {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                if attempt < self._max_retries:
                    time.sleep(min(30, 2**attempt))
                    continue
                raise GovInfoApiError(
                    f"GovInfo request failed for {self._safe_endpoint(url)}: {exc.reason}"
                ) from exc

        raise GovInfoApiError(f"GovInfo request exhausted retries for {self._safe_endpoint(url)}")

    def download_package(self, package_id: str, rendition: str, destination: Any) -> None:
        path = f"/packages/{urllib.parse.quote(package_id, safe='')}/{rendition}"
        url = self._url(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")

        for attempt in range(self._max_retries + 1):
            request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                    with temporary.open("wb") as handle:
                        shutil.copyfileobj(response, handle)
                if rendition == "zip" and not zipfile.is_zipfile(temporary):
                    temporary.unlink(missing_ok=True)
                    if attempt < self._max_retries:
                        time.sleep(min(30, 2**attempt))
                        continue
                    raise GovInfoApiError(
                        f"GovInfo returned invalid ZIP for {package_id}/{rendition}"
                    )
                temporary.replace(destination)
                return
            except urllib.error.HTTPError as exc:
                temporary.unlink(missing_ok=True)
                if exc.code in {429, 503} and attempt < self._max_retries:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else min(60, 2**attempt)
                    time.sleep(delay)
                    continue
                raise GovInfoApiError(
                    f"GovInfo HTTP {exc.code} downloading {package_id}/{rendition}"
                ) from exc
            except urllib.error.URLError as exc:
                temporary.unlink(missing_ok=True)
                if attempt < self._max_retries:
                    time.sleep(min(30, 2**attempt))
                    continue
                raise GovInfoApiError(
                    f"GovInfo download failed for {package_id}/{rendition}: {exc.reason}"
                ) from exc

        raise GovInfoApiError(f"GovInfo download exhausted retries for {package_id}/{rendition}")

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, str],
        records_key: str,
    ) -> Iterator[dict[str, Any]]:
        current_path = path
        current_params = dict(params)
        seen_pages: set[str] = set()
        seen_cursors: set[str] = set()

        while True:
            page_signature = current_path + "?" + urllib.parse.urlencode(sorted(current_params.items()))
            if page_signature in seen_pages:
                raise GovInfoApiError(f"GovInfo pagination cycle detected for {path}")
            seen_pages.add(page_signature)

            page = self._request_json("GET", current_path, params=current_params)
            records = page.get(records_key, [])
            if not isinstance(records, list):
                raise GovInfoApiError(f"GovInfo response field {records_key!r} is not a list")
            for record in records:
                if isinstance(record, dict):
                    yield record

            next_page = page.get("nextPage")
            if isinstance(next_page, str) and next_page:
                current_path = next_page
                current_params = {}
                continue

            next_cursor = page.get("offsetMark")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            current_path = path
            current_params = dict(params)
            current_params["offsetMark"] = next_cursor

    def iter_published(
        self,
        start_date: str,
        end_date: str,
        *,
        collection: str,
        congress: int | None = None,
        doc_class: str | None = None,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        params = {
            "collection": collection,
            "offsetMark": "*",
            "pageSize": str(page_size),
        }
        if congress is not None:
            params["congress"] = str(congress)
        if doc_class:
            params["docClass"] = doc_class
        yield from self._paginate(
            f"/published/{start_date}/{end_date}", params=params, records_key="packages"
        )

    def iter_granules(
        self,
        package_id: str,
        *,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        yield from self._paginate(
            f"/packages/{urllib.parse.quote(package_id, safe='')}/granules",
            params={"offsetMark": "*", "pageSize": str(page_size)},
            records_key="granules",
        )

    def iter_search(
        self,
        query: str,
        *,
        page_size: int = 1000,
        result_level: str = "package",
        sort_field: str = "publishdate",
        sort_order: str = "ASC",
        historical: bool = False,
    ) -> Iterator[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query,
            "pageSize": str(page_size),
            "offsetMark": "*",
            "sorts": [{"field": sort_field, "sortOrder": sort_order}],
            "resultLevel": result_level,
            "historical": historical,
        }
        seen_cursors: set[str] = set()
        while True:
            page = self._request_json("POST", "/search", body=body)
            results = page.get("results", [])
            if not isinstance(results, list):
                raise GovInfoApiError("GovInfo response field 'results' is not a list")
            for result in results:
                if isinstance(result, dict):
                    yield result

            next_cursor = page.get("offsetMark")
            if not isinstance(next_cursor, str) or not next_cursor:
                next_page = page.get("nextPage")
                if isinstance(next_page, str):
                    parsed = urllib.parse.urlsplit(next_page)
                    next_cursor = dict(urllib.parse.parse_qsl(parsed.query)).get("offsetMark", "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            body["offsetMark"] = next_cursor

    def package_summary(self, package_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET", f"/packages/{urllib.parse.quote(package_id, safe='')}/summary"
        )

    def granule_summary(self, package_id: str, granule_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            "/packages/"
            f"{urllib.parse.quote(package_id, safe='')}/granules/"
            f"{urllib.parse.quote(granule_id, safe='')}/summary",
        )
