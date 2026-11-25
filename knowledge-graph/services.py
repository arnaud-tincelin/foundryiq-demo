"""Use keyless Azure endpoints for the demo."""

import json
import re
import time
from email.message import Message
from email.utils import formatdate
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from records import DemoError, PRODUCTION_FILES, QUALITY_FILES, Record, WAREHOUSE_FILES
from records import production_record, quality_record, warehouse_record

STORAGE_SCOPE = "https://storage.azure.com/.default"
SEARCH_SCOPE = "https://search.azure.com/.default"
FOUNDRY_SCOPE = "https://ai.azure.com/.default"
ARM_SCOPE = "https://management.azure.com/.default"
SOURCE_FILE_LIMIT = 16_384


def setting(config: dict, key: str, pattern: str = r".+") -> str:
    value = config.get(key)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise DemoError(f"Missing or invalid configuration value: {key}.")
    return value


def uuid_setting(config: dict, key: str) -> str:
    try:
        return str(UUID(setting(config, key)))
    except ValueError as exc:
        raise DemoError(f"{key} must be an Entra UUID.") from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpError(DemoError):
    def __init__(self, operation: str, status: int, details: str = ""):
        self.status = status
        super().__init__(f"{operation}: HTTP {status}{details}")


class Http:
    def __init__(self, credential):
        self.credential = credential
        self.opener = build_opener(NoRedirect())
        self._tokens = {}

    def token(self, scope: str) -> str:
        token = self._tokens.get(scope)
        if token is None or token.expires_on <= time.time() + 120:
            token = self.credential.get_token(scope)
            self._tokens[scope] = token
        return token.token

    def read(self, url: str, operation: str, scope: str, **kwargs) -> bytes:
        data, _ = self.read_with_headers(url, operation, scope, **kwargs)
        return data

    def read_with_headers(self, url: str, operation: str, scope: str, *, method: str = "GET",
                          body: dict | bytes | None = None, headers: dict | None = None,
                          limit: int = 4_000_000, timeout: int = 30) -> tuple[bytes, Message]:
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {self.token(scope)}"
        if isinstance(body, dict):
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        request = Request(url, body, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = response.read(limit + 1)
                response_headers = response.headers
        except HTTPError as exc:
            with exc:
                details = ""
                if scope in {SEARCH_SCOPE, FOUNDRY_SCOPE, ARM_SCOPE}:
                    details = service_error_details(exc.read(16_384), exc.headers.get("x-ms-error-code"))
                raise HttpError(operation, exc.code, details) from None
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            raise DemoError(f"{operation}: network or TLS request failed.") from None
        if len(data) > limit:
            raise DemoError(f"{operation}: response exceeds the demo size limit.")
        return data, response_headers

    def json(self, url: str, operation: str, scope: str, **kwargs) -> dict:
        value = decode_json(self.read(url, operation, scope, **kwargs), operation)
        if not isinstance(value, dict):
            raise DemoError(f"{operation}: expected a JSON object.")
        return value


def decode_json(data: bytes, operation: str):
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        raise DemoError(f"{operation}: response was not valid JSON.") from None


def service_error_details(body: bytes, error_code: str | None) -> str:
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        value = {}
    root = value if isinstance(value, dict) else {}
    error = root.get("error")
    error = error if isinstance(error, dict) else {}
    raw_diagnostics = root.get("@search.graphDiagnostics")
    diagnostics = tuple(d for d in raw_diagnostics if isinstance(d, dict)) if isinstance(raw_diagnostics, list) else ()
    messages = [error.get("code", error_code), error.get("message")]
    messages.extend(f"{d.get('code')}: {d.get('message')}" for d in diagnostics)
    text = "; ".join(m for m in messages if isinstance(m, str))
    return f" ({text[:600]})" if text else ""


def container_url(config: dict, key: str) -> str:
    return setting(config, key,
                   r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/?").rstrip("/")


def storage_headers(**kwargs) -> dict:
    return {"x-ms-version": "2023-11-03", "x-ms-date": formatdate(usegmt=True), **kwargs}


def write_blob(http: Http, url: str, filename: str, data: bytes, mime: str, *, etag: str | None = None) -> None:
    condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
    operation = "Update" if etag else "Create"
    http.read(f"{url}/{quote(filename)}", f"{operation} {filename}", STORAGE_SCOPE, method="PUT", body=data,
              headers=storage_headers(**condition, **{"Content-Type": mime, "x-ms-blob-type": "BlockBlob"}))


def read_blob_records(http: Http, config: dict) -> list[Record]:
    production = container_url(config, "productionContainer")
    warehouse = container_url(config, "warehouseContainer")
    if urlsplit(production).hostname == urlsplit(warehouse).hostname:
        raise DemoError("Use separate production and warehouse Blob accounts.")
    records = []
    for url, files, parse in [(production, PRODUCTION_FILES, production_record),
                              (warehouse, WAREHOUSE_FILES, warehouse_record),
                              (warehouse, QUALITY_FILES, quality_record)]:
        for filename in files:
            locator = f"{url}/{quote(filename)}"
            fields = http.json(locator, f"Read {filename}", STORAGE_SCOPE, headers=storage_headers(),
                               limit=SOURCE_FILE_LIMIT)
            records.append(parse(filename, fields, locator))
    return records
