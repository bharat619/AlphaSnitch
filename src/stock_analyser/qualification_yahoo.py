"""Bounded Yahoo inventory/profile acquisition, with no SDK or automatic consent.

Only the explicit Yahoo endpoints used by the locked yfinance source are used.
Authentication, retries and failed HTTP attempts all consume the shared budget.
Cookies/crumbs live only in memory. No network occurs during construction.
"""

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from http.cookiejar import CookieJar
from urllib.parse import quote, urlencode
from urllib.request import Request

from stock_analyser.massive import ProviderAllowance, _DeadlineHTTPSConnection
from stock_analyser.qualification_budget import QualificationStop

ADAPTER_VERSION = "yahoo-qualified-http-1"
ALLOWED_HOSTS = {"fc.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"}


@dataclass(frozen=True)
class YahooResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class YahooHTTPTransport:
    """One HTTP request per call: no redirects, implicit retries, proxies or SDK."""

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.cookies = CookieJar()
        self.closed = False

    def close(self):
        self.cookies.clear()
        self.closed = True

    def __call__(self, host, method, path, params, body, timeout, *, deadline=None):
        if self.closed or host not in ALLOWED_HOSTS or method not in ("GET", "POST"):
            raise QualificationStop("YAHOO_UNSAFE_REQUEST")
        deadline = (
            min(self.clock() + timeout, deadline)
            if deadline is not None
            else self.clock() + timeout
        )

        def remaining():
            value = deadline - self.clock()
            if value <= 0:
                raise QualificationStop("DEADLINE")
            return value

        target = path + ("?" + urlencode(params) if params else "")
        request = Request("https://" + host + target)
        self.cookies.add_cookie_header(request)
        headers = {"Accept": "application/json", "User-Agent": "stock-analyser/0.1"}
        headers.update(request.header_items())
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection = _DeadlineHTTPSConnection(host, remaining)
        try:
            connection.request(method, target, body=payload, headers=headers)
            response = connection.getresponse()
            message = Message()
            response_headers = tuple(response.getheaders())
            for name, value in response_headers:
                message.add_header(name, value)

            class CookieResponse:
                def info(self):
                    return message

            self.cookies.extract_cookies(CookieResponse(), request)
            parts, size = [], 0
            while True:
                remaining()
                chunk = response.read1(65536)
                remaining()
                if not chunk:
                    break
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise QualificationStop("YAHOO_RESPONSE_LIMIT")
                parts.append(chunk)
            return YahooResponse(response.status, response_headers, b"".join(parts))
        finally:
            connection.close()


class YahooQualificationClient:
    def __init__(
        self,
        budget,
        *,
        http_cap=8,
        retries=1,
        requests_per_minute=60,
        timeout_seconds=20,
        max_cooldown_seconds=30,
        transport=None,
        sleep=time.sleep,
        lease_path=None,
    ):
        if (
            not 1 <= http_cap <= 100
            or not 0 <= retries <= 5
            or not 1 <= requests_per_minute <= 60
            or not 0 < timeout_seconds <= 120
            or not 0 <= max_cooldown_seconds <= 120
        ):
            raise ValueError("Invalid Yahoo controls")
        self.budget, self.http_cap, self.retries = budget, http_cap, retries
        self.interval, self.timeout = 60 / requests_per_minute, timeout_seconds
        self.max_cooldown, self.sleep = max_cooldown_seconds, sleep
        self.transport = transport or YahooHTTPTransport(clock=budget.clock)
        self.next_request = budget.clock()
        self.crumb = None
        self.auth_attempts = 0
        self.refreshes = 0
        self.operation_attempts = 0
        self.operation_lock = threading.Lock()
        self.logical_calls = 0
        self.observations = []
        self.closed = False
        self.allowance = None
        if lease_path is not None:
            self.allowance = ProviderAllowance(lease_path, requests_per_minute, sleep=sleep)
            self.allowance.__enter__()

    def close(self):
        if self.allowance is not None:
            self.allowance.__exit__()
            self.allowance = None
        close = getattr(self.transport, "close", None)
        if close:
            close()
        self.crumb = None
        self.closed = True

    def _attempt(self, host, method, path, params=None, body=None):
        if self.closed or host not in ALLOWED_HOSTS:
            raise QualificationStop("YAHOO_UNSAFE_REQUEST")
        if self.operation_attempts >= self.http_cap:
            raise QualificationStop("YAHOO_OPERATION_LIMIT")
        wait = max(0, self.next_request - self.budget.clock())
        if wait >= self.budget.remaining():
            raise QualificationStop("DEADLINE")
        self.sleep(wait)
        if self.allowance is not None:
            self.allowance.acquire(
                max_wait_seconds=min(max(self.interval, self.max_cooldown), self.budget.remaining())
            )
        self.budget.dispatch("yfinance")
        self.operation_attempts += 1
        self.next_request = self.budget.clock() + self.interval
        try:
            result = self.transport(
                host, method, path, params or {}, body, min(self.timeout, self.budget.remaining())
            )
        except QualificationStop:
            raise
        except Exception:
            self.budget.remaining()
            raise QualificationStop("YAHOO_TRANSPORT_FAILURE") from None
        self.budget.remaining()
        self.observations.append(
            {"http_status": result.status, "retrieved_at": datetime.now(UTC).isoformat()}
        )
        if 300 <= result.status < 400:
            raise QualificationStop("YAHOO_REDIRECT_REQUIRES_REVIEW")
        if result.status == 429 or 500 <= result.status < 600:
            delay = next((v for k, v in result.headers if k.lower() == "retry-after"), "1")
            try:
                delay = float(delay)
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise QualificationStop("YAHOO_INVALID_COOLDOWN") from None
            self.budget.record_cooldown(delay)
            if self.allowance is not None:
                self.allowance.defer(delay, stop=delay > self.max_cooldown)
            self.next_request = max(self.next_request, self.budget.clock() + delay)
            if delay > self.max_cooldown or delay >= self.budget.remaining():
                raise QualificationStop("YAHOO_COOLDOWN_LIMIT")
        return result

    def _authenticate(self):
        if self.auth_attempts >= 2:
            raise QualificationStop("YAHOO_AUTHENTICATION_LIMIT")
        self.auth_attempts += 1
        # No CSRF consent submission, hidden fallback, persistent cookie store or
        # redirect following. If basic auth is insufficient the observation fails.
        cookie = self._attempt("fc.yahoo.com", "GET", "/")
        if cookie.status not in (200, 404):
            raise QualificationStop("YAHOO_AUTHENTICATION_FAILED")
        response = self._attempt("query1.finance.yahoo.com", "GET", "/v1/test/getcrumb")
        if response.status != 200:
            raise QualificationStop("YAHOO_AUTHENTICATION_FAILED")
        try:
            crumb = response.body.decode("utf-8").strip()
            if not crumb or len(crumb) > 256 or any(c in crumb for c in "\r\n<>"):
                raise ValueError
        except (ValueError, UnicodeError):
            raise QualificationStop("YAHOO_AUTHENTICATION_FAILED") from None
        self.crumb = crumb

    def _json(self, method, path, *, body=None, params=None, host="query1.finance.yahoo.com"):
        if not self.operation_lock.acquire(blocking=False):
            raise QualificationStop("YAHOO_CONCURRENCY_NOT_SUPPORTED")
        try:
            return self._perform_json(method, path, body=body, params=params, host=host)
        finally:
            self.operation_lock.release()

    def _perform_json(
        self, method, path, *, body=None, params=None, host="query1.finance.yahoo.com"
    ):
        self.logical_calls += 1
        self.operation_attempts = 0
        if self.crumb is None:
            self._authenticate()
        for attempt in range(self.retries + 1):
            response = self._attempt(
                host,
                method,
                path,
                {
                    **(params or {}),
                    "crumb": self.crumb,
                    "formatted": "false",
                    "lang": "en-US",
                    "region": "US",
                },
                body,
            )
            if response.status in (401, 403) and self.refreshes < 1:
                self.refreshes += 1
                self.budget.record_retry()
                self.crumb = None
                self._authenticate()
                response = self._attempt(
                    host,
                    method,
                    path,
                    {**(params or {}), "crumb": self.crumb, "formatted": "false"},
                    body,
                )
            if response.status == 429 or 500 <= response.status < 600:
                if attempt < self.retries:
                    self.budget.record_retry()
                    continue
            if response.status != 200:
                raise QualificationStop("YAHOO_HTTP_FAILURE")
            try:
                result = json.loads(response.body)
                if not isinstance(result, dict):
                    raise ValueError
                return result
            except (ValueError, TypeError):
                raise QualificationStop("YAHOO_INVALID_RESPONSE") from None
        raise AssertionError("Unreachable Yahoo retry state")

    def screen(self, offset, size):
        if type(offset) is not int or offset < 0 or not 1 <= size <= 250:
            raise ValueError("Invalid screener bounds")
        result = self._json(
            "POST",
            "/v1/finance/screener",
            body={
                "offset": offset,
                "size": size,
                "sortField": "ticker",
                "sortType": "ASC",
                "userId": "",
                "userIdType": "guid",
                "quoteType": "EQUITY",
                "query": {
                    "operator": "AND",
                    "operands": [
                        {"operator": "EQ", "operands": ["region", "us"]},
                        {"operator": "EQ", "operands": ["sector", "Technology"]},
                    ],
                },
            },
        )
        try:
            finance = result["finance"]
            if finance.get("error") or len(finance["result"]) != 1:
                raise ValueError
            return finance["result"][0]
        except (KeyError, TypeError, ValueError):
            raise QualificationStop("YAHOO_INVALID_RESPONSE") from None

    def profile(self, symbol):
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 64:
            raise ValueError("Invalid provider symbol")
        result = self._json(
            "GET",
            "/v10/finance/quoteSummary/" + quote(symbol, safe=""),
            params={"modules": "assetProfile,quoteType,price"},
            host="query2.finance.yahoo.com",
        )
        try:
            summary = result["quoteSummary"]
            if summary.get("error") or len(summary["result"]) != 1:
                raise ValueError
            row = summary["result"][0]
            identity, classification, price = (
                row["quoteType"],
                row.get("assetProfile", {}),
                row.get("price", {}),
            )
            actual = identity["symbol"]
            if actual != symbol:
                raise ValueError
            projected = {
                "symbol": actual,
                "sector": classification.get("sector"),
                "industry": classification.get("industry"),
                "exchange": identity.get("exchange"),
                "quoteType": identity.get("quoteType"),
                "currency": price.get("currency"),
            }
            if any(
                v is not None and (not isinstance(v, str) or len(v) > 256)
                for v in projected.values()
            ):
                raise ValueError
            return projected
        except (KeyError, TypeError, ValueError):
            raise QualificationStop("YAHOO_PROFILE_IDENTITY_OR_SCHEMA_FAILURE") from None
