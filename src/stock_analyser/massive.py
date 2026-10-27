"""Massive boundary: bounded transport, one local allowance, and allowlisted evidence.

No provider URLs, headers, error bodies, or opaque cursors are retained. Inject the
transport and clock for deterministic failure tests without spending live quota.
"""

import http.client
import io
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from stock_analyser.collection import RawPriceResponse, encode
from stock_analyser.contracts import Instrument, MarketContext, PriceBar


class MassiveError(ValueError):
    """Safe public error; never includes upstream content or request details."""


class ProviderCooldownError(MassiveError):
    """The shared allowance stopped this run to honor a provider cooldown."""


class MassiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_minute: int = Field(default=5, ge=1, le=1000)
    timeout_seconds: float = Field(default=20, gt=0, le=120, allow_inf_nan=False)
    retries: int = Field(default=2, ge=0, le=5)
    max_requests: int = Field(default=100, ge=1, le=10000)
    max_retry_delay_seconds: float = Field(default=120, ge=1, le=3600, allow_inf_nan=False)
    page_size: int = Field(default=50000, ge=1, le=50000)
    news_page_size: int = Field(default=100, ge=1, le=1000)
    news_max_pages: int = Field(default=3, ge=1, le=100)
    news_lookback_days: int = Field(default=7, ge=1, le=365)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def __call__(self, target: str, key: SecretStr, timeout: float) -> HttpResponse: ...


class _DeadlineReader(io.RawIOBase):
    """Apply the same deadline to every low-level header/body read, including dribbles."""

    def __init__(self, raw, sock, remaining):
        self.raw, self.sock, self.remaining = raw, sock, remaining

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(self.remaining())
        result = self.raw.readinto(buffer)
        self.remaining()
        return result

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, remaining):
        self.sock, self.remaining = sock, remaining

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data, *args):
        self.sock.settimeout(self.remaining())
        self.sock.sendall(data, *args)
        self.remaining()

    def makefile(self, mode, buffering=None):
        if mode != "rb":
            raise ValueError("Unsupported HTTP response stream mode")
        raw = self.sock.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineReader(raw, self.sock, self.remaining))


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, remaining):
        self.remaining = remaining
        super().__init__(host, timeout=remaining())
        self._create_connection = self._connect_socket

    def _connect_socket(self, address, timeout, source_address=None):
        # A C-library DNS lookup cannot be interrupted by a socket timeout.
        # Isolate only resolution; subprocess.run kills and reaps on timeout or
        # interruption, and the child never receives the API key as an argument.
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import json,socket,sys; print(json.dumps(socket.getaddrinfo("
                    "sys.argv[1],int(sys.argv[2]),0,socket.SOCK_STREAM)))",
                    address[0],
                    str(address[1]),
                ],
                capture_output=True,
                timeout=self.remaining(),
                check=True,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError("Massive DNS deadline exhausted") from None
        except subprocess.SubprocessError:
            raise OSError("Massive DNS resolution failed") from None
        self.remaining()
        try:
            if len(result.stdout) > 65536:
                raise ValueError
            addresses = json.loads(result.stdout)
            if not addresses or len(addresses) > 128:
                raise ValueError
        except (ValueError, TypeError):
            raise OSError("Massive DNS resolution returned invalid addresses") from None
        last_error = None
        for family, kind, protocol, _, sockaddr in addresses:
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(self.remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(tuple(sockaddr))
                self.remaining()
                return sock
            except OSError as exc:
                sock.close()
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("No address available for provider")

    def connect(self):
        http.client.HTTPConnection.connect(self)
        # TCP and TLS share the original deadline; TLS gets only what remains.
        self.sock.settimeout(self.remaining())
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        self.remaining()
        self.sock = _DeadlineSocket(self.sock, self.remaining)


def https_transport(
    target: str, key: SecretStr, timeout: float, *, deadline=None, clock=None
) -> HttpResponse:
    # Start before connect/TLS, never after headers. Buffered HTTP parsing uses
    # a deadline-aware raw reader, so slow header lines cannot reset the budget.
    clock = clock or time.monotonic
    deadline = min(clock() + timeout, deadline) if deadline is not None else clock() + timeout

    def remaining():
        value = deadline - clock()
        if value <= 0:
            raise TimeoutError("Massive absolute transport deadline exhausted")
        return value

    connection = _DeadlineHTTPSConnection("api.massive.com", remaining)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Authorization": f"Bearer {key.get_secret_value()}",
                "Accept": "application/json",
            },
        )
        remaining()
        response = connection.getresponse()
        remaining()
        parts, size = [], 0
        while True:
            remaining()
            chunk = response.read1(65536)
            remaining()
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            if size > 16 * 1024 * 1024:
                raise MassiveError("Massive response exceeded the size bound")
        return HttpResponse(response.status, dict(response.getheaders()), b"".join(parts))
    finally:
        connection.close()


def _lock_file(stream: TextIO) -> None:
    """Acquire a nonblocking OS lease without importing platform APIs at startup."""
    if sys.platform == "win32":
        import msvcrt

        # Lock the same byte even when pacing state already exists. Windows
        # permits a region beyond EOF, so an empty new file needs no sentinel.
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


class ProviderAllowance:
    """Exclusive process lease plus thread-safe pacing, including across restarts.

    The lock file is never unlinked: replacing the file defeats OS lock exclusion.
    Every client/worker in a run must share this allowance. A single fixed path is
    used by the CLI regardless of its report/data directory.
    """

    def __init__(self, path: Path, requests_per_minute: int, *, clock=time.time, sleep=time.sleep):
        self.path = path
        self.interval = 60 / requests_per_minute
        self.clock = clock
        self.sleep = sleep
        self._mutex = threading.Lock()
        self._file = None
        self._stopped = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="ascii")
        try:
            _lock_file(stream)
        except OSError:
            stream.close()
            raise MassiveError("Another local Massive collection holds the provider lock") from None
        self._file = stream
        # Always wait one complete configured interval after acquisition. This
        # also protects a restart with a smaller allowance than the previous run.
        self._next = self.clock() + self.interval
        stream.seek(0)
        try:
            saved = float(stream.read() or "0")
            if not math.isfinite(saved):
                raise ValueError
            self._next = max(self._next, saved)
        except ValueError:
            stream.close()
            self._file = None
            raise MassiveError("Invalid provider pacing state") from None
        return self

    def __exit__(self, *_):
        if self._file is not None:
            self._file.close()
            self._file = None

    def _save(self):
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(self._next))
        self._file.flush()
        os.fsync(self._file.fileno())

    def acquire(self, *, max_wait_seconds: float | None = None):
        with self._mutex:
            if self._file is None:
                raise MassiveError("Provider allowance requires an active process lease")
            wait = max(0, self._next - self.clock())
            if self._stopped or (max_wait_seconds is not None and wait > max_wait_seconds):
                self._stopped = True
                raise ProviderCooldownError("Massive cooldown exceeds the configured wait bound")
            self.sleep(wait)
            self._next = self.clock() + self.interval
            self._save()

    def defer(self, seconds: float, *, stop: bool = False):
        with self._mutex:
            self._next = max(self._next, self.clock() + seconds)
            self._stopped = self._stopped or stop
            self._save()


class MassiveClient:
    def __init__(
        self,
        key: SecretStr,
        allowance: ProviderAllowance,
        settings: MassiveSettings,
        transport: Transport = https_transport,
        *,
        budget=None,
    ):
        if not key.get_secret_value().strip():
            raise MassiveError("Set MASSIVE_API_KEY before live collection")
        self._key = key
        self.allowance = allowance
        self.settings = settings
        self.transport = transport
        self.budget = budget
        self._requests = 0
        self._mutex = threading.Lock()
        self.observations: list[dict] = []

    def _delay(self, response: HttpResponse, attempt: int) -> float:
        value = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
        delay = float(2**attempt)
        if value is not None:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - self.allowance.clock()
                except (ValueError, TypeError, OverflowError):
                    raise MassiveError("Massive returned an invalid retry delay") from None
        if not math.isfinite(delay):
            raise MassiveError("Massive returned an invalid retry delay")
        return max(0, delay)

    def request(self, target: str) -> dict:
        for attempt in range(self.settings.retries + 1):
            with self._mutex:
                if self._requests >= self.settings.max_requests:
                    raise MassiveError("Massive request budget exhausted")
                self._requests += 1
            wait_bound = max(self.allowance.interval, self.settings.max_retry_delay_seconds)
            if self.budget is not None:
                wait_bound = min(wait_bound, self.budget.remaining())
            self.allowance.acquire(max_wait_seconds=wait_bound)
            timeout = self.settings.timeout_seconds
            if self.budget is not None:
                timeout = min(timeout, self.budget.remaining())
                self.budget.dispatch("massive")
                if attempt:
                    self.budget.record_retry()
            try:
                if self.budget is not None and self.transport is https_transport:
                    response = https_transport(
                        target,
                        self._key,
                        timeout,
                        deadline=self.budget.started + self.budget.seconds,
                        clock=self.budget.clock,
                    )
                else:
                    response = self.transport(target, self._key, timeout)
                if self.budget is not None:
                    self.budget.remaining()
            except (OSError, http.client.HTTPException):
                if self.budget is not None:
                    self.budget.remaining()
                if attempt == self.settings.retries:
                    raise MassiveError("Massive transport failed within the retry bound") from None
                if self.budget is not None:
                    self.budget.record_cooldown(2**attempt)
                self.allowance.defer(2**attempt)
                continue
            self.observations.append(
                {
                    "http_status": response.status,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "attempt": attempt + 1,
                }
            )
            if response.status == 429 or 500 <= response.status <= 599:
                # Persist the cooldown even when the final attempt failed.
                delay = self._delay(response, attempt)
                exceeds_wait = delay > self.settings.max_retry_delay_seconds
                if self.budget is not None:
                    self.budget.record_cooldown(delay)
                self.allowance.defer(delay, stop=exceeds_wait)
                if exceeds_wait:
                    raise ProviderCooldownError(
                        "Massive cooldown exceeds the configured wait bound"
                    )
                if attempt == self.settings.retries:
                    raise MassiveError("Massive retryable HTTP failure exhausted retries")
                continue
            if response.status != 200:
                raise MassiveError(f"Massive HTTP failure ({response.status})")
            try:
                payload = json.loads(response.body, parse_float=Decimal)
                if not isinstance(payload, dict) or payload.get("status", "OK") not in (
                    "OK",
                    "DELAYED",
                ):
                    raise ValueError
                if "error" in payload or "message" in payload:
                    raise ValueError
                return payload
            except (ValueError, TypeError):
                raise MassiveError("Massive returned an invalid or error payload") from None
        raise AssertionError("Unreachable retry state")

    def pages(self, path: str, query: dict) -> Iterator[dict]:
        target = path + "?" + urlencode(query)
        seen = set()
        # Aggregate next pages may change the start/end portion of the path.
        prefix = path.split("/range/")[0] + "/range/1/day/" if "/range/" in path else None
        while True:
            if target in seen:
                raise MassiveError("Massive pagination cycle")
            seen.add(target)
            payload = self.request(target)
            yield payload
            next_url = payload.get("next_url")
            if next_url is None:
                return
            try:
                url = urlsplit(next_url)
                if (
                    url.scheme != "https"
                    or url.netloc != "api.massive.com"
                    or url.fragment
                    or url.username
                    or url.password
                ):
                    raise ValueError
                if prefix:
                    if not url.path.startswith(prefix):
                        raise ValueError
                    dates = url.path[len(prefix) :].split("/")
                    if len(dates) != 2 or any(
                        not (d.isdigit() or len(d) == 10 and date.fromisoformat(d)) for d in dates
                    ):
                        raise ValueError
                elif url.path != path:
                    raise ValueError
                params = dict(parse_qsl(url.query, strict_parsing=True))
                if set(params) - {"cursor", "apiKey", *query}:
                    raise ValueError
                params.pop("apiKey", None)
                # Explicit invariants are restored on every page.
                params.update(query)
                target = url.path + "?" + urlencode(params)
            except (ValueError, TypeError, AttributeError):
                raise MassiveError("Massive returned an unsafe pagination destination") from None


def decimal_value(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise MassiveError("Massive returned an invalid numeric field")
    if not Decimal(value).is_finite():
        raise MassiveError("Massive returned a nonfinite numeric field")
    return str(value)


class MassivePriceProvider:
    def __init__(self, client: MassiveClient):
        self.client = client

    def fetch(self, instrument, market, resolution):
        market.validate_instrument(instrument)
        if (
            market.market_id != "US"
            or market.timezone != "America/New_York"
            or market.route("prices").provider != "massive"
            or not resolution.periods
            or resolution.market_id != market.market_id
            or resolution.venue != instrument.venue
            or resolution.market_configuration_version != market.configuration_version
        ):
            raise MassiveError("Massive requires a compatible resolved US history window")
        if resolution.requirement.adjustment == "total_return":
            raise MassiveError("Massive aggregates do not establish total-return adjustment")
        symbols = {
            instrument.provider_symbol("massive", p.session_start) for p in resolution.periods
        } | {instrument.provider_symbol("massive", p.session_end) for p in resolution.periods}
        if len(symbols) != 1:
            raise MassiveError("Massive history crosses a provider symbol change")
        symbol = symbols.pop()
        adjusted = resolution.requirement.adjustment == "split"
        path = (
            f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/"
            f"{resolution.history_start}/{resolution.session_date}"
        )
        rows, observations = [], []
        try:
            for page in self.client.pages(
                path,
                {
                    "adjusted": str(adjusted).lower(),
                    "sort": "asc",
                    "limit": self.client.settings.page_size,
                },
            ):
                if page.get("adjusted") is not adjusted or page.get("ticker") != symbol:
                    raise MassiveError("Massive adjustment or symbol metadata mismatch")
                results = page.get("results", [])
                if not isinstance(results, list):
                    raise ValueError
                observations.append({"status": page.get("status", "OK"), "rows": len(results)})
                for item in results:
                    stamp = item["t"]
                    if type(stamp) is not int:
                        raise ValueError
                    local_start = datetime.fromtimestamp(stamp / 1000, ZoneInfo(market.timezone))
                    if (
                        local_start.hour,
                        local_start.minute,
                        local_start.second,
                        local_start.microsecond,
                    ) != (0, 0, 0, 0):
                        raise ValueError
                    session = local_start.date()
                    values = {
                        name: decimal_value(item[key])
                        for name, key in (
                            ("open", "o"),
                            ("high", "h"),
                            ("low", "l"),
                            ("close", "c"),
                            ("volume", "v"),
                        )
                    }
                    bar = PriceBar(session=session, **values)
                    rows.append((bar, stamp))
            sessions = [b.session for b, _ in rows]
            if sessions != sorted(set(sessions)):
                raise MassiveError("Massive returned duplicate or unordered sessions")
            normalized = self._normalize(rows, market, resolution)
        except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
            if isinstance(exc, MassiveError):
                raise
            raise MassiveError("Massive price contract validation failed") from None
        return RawPriceResponse(
            provider="massive",
            provider_symbol=symbol,
            retrieved_at=datetime.now(UTC),
            quality_flags=("PUBLICATION_UNVERIFIED",),
            adjustment=resolution.requirement.adjustment,
            feed=market.route("prices").feed,
            payload=encode(
                {
                    "bars": normalized,
                    "provider": "massive",
                    "provider_symbol": symbol,
                    "adjusted": adjusted,
                    "feed": market.route("prices").feed.model_dump(mode="json"),
                    "pages": observations,
                    "daily_source": [
                        {**b.model_dump(mode="json"), "aggregate_start_ms": t} for b, t in rows
                    ],
                    "publication_timing": "unverified",
                    "timestamp_semantics": (
                        "normalized scheduled close; source starts retained separately"
                    ),
                }
            ),
        )

    @staticmethod
    def _normalize(rows, market, resolution):
        from stock_analyser.calendars import ExchangeCalendarAdapter

        bars = {b.session: b for b, _ in rows}
        allowed = set()
        normalized = []
        for period in resolution.periods:
            if resolution.requirement.timeframe == "daily":
                expected = {period.session_end}
            else:
                expected = {
                    s.session
                    for s in ExchangeCalendarAdapter().schedule(
                        market, resolution.venue, period.session_start, period.session_end
                    )
                }
            allowed.update(expected)
            # A missing constituent cannot be hidden by weekly aggregation.
            if not expected <= bars.keys():
                continue
            group = [bars[s] for s in sorted(expected)]
            normalized.append(
                {
                    "session": period.session_end.isoformat(),
                    "timestamp": period.closes_at.isoformat(),
                    "open": str(group[0].open),
                    "high": str(max(b.high for b in group)),
                    "low": str(min(b.low for b in group)),
                    "close": str(group[-1].close),
                    "volume": str(sum((b.volume for b in group), Decimal(0))),
                }
            )
        if bars.keys() - allowed:
            raise MassiveError("Massive returned sessions outside the resolved window")
        return normalized


class MassiveActionsProvider:
    """Allowlisted action values; no opaque response strings are retained."""

    def __init__(self, client: MassiveClient):
        self.client = client

    def fetch(self, instrument: Instrument, market: MarketContext, start: date, end: date) -> dict:
        market.validate_instrument(instrument)
        symbol = instrument.provider_symbol("massive", end)
        if instrument.provider_symbol("massive", start) != symbol:
            raise MassiveError("Corporate action window crosses a symbol change")
        actions = []
        try:
            for kind, field in (("splits", "execution_date"), ("dividends", "ex_dividend_date")):
                for page in self.client.pages(
                    f"/stocks/v1/{kind}",
                    {
                        "ticker": symbol,
                        f"{field}.gte": str(start),
                        f"{field}.lte": str(end),
                        "limit": min(1000, self.client.settings.page_size),
                        "sort": f"{field}.asc",
                    },
                ):
                    for row in page.get("results", []):
                        effective = date.fromisoformat(row[field])
                        if row["ticker"] != symbol or not start <= effective <= end:
                            raise ValueError
                        action = {"kind": kind, "effective_date": str(effective)}
                        for key in (
                            "split_from",
                            "split_to",
                            "cash_amount",
                            "split_adjusted_cash_amount",
                            "historical_adjustment_factor",
                        ):
                            if key in row:
                                action[key] = decimal_value(row[key])
                        if kind == "splits" and not {"split_from", "split_to"} <= action.keys():
                            raise ValueError
                        if kind == "dividends":
                            if row.get("currency") != instrument.quote_currency:
                                raise ValueError
                            action["currency"] = row["currency"]
                            if "cash_amount" not in action:
                                raise ValueError
                        actions.append(action)
        except (ValueError, TypeError, KeyError) as exc:
            if isinstance(exc, MassiveError):
                raise
            raise MassiveError("Massive corporate-action contract validation failed") from None
        return {
            "provider": "massive",
            "provider_symbol": symbol,
            "actions": actions,
            "session_start": str(start),
            "session_end": str(end),
            "retrieved_at": datetime.now(UTC).isoformat(),
        }


@contextmanager
def live_client(
    key: SecretStr, settings: MassiveSettings, lock_path: Path, *, budget=None
) -> Iterator[MassiveClient]:
    with ProviderAllowance(lock_path, settings.requests_per_minute) as allowance:
        yield MassiveClient(key, allowance, settings, budget=budget)


class MassiveEvidenceProvider:
    """Compose price and action evidence into the existing immutable snapshot path."""

    def __init__(self, client: MassiveClient):
        self.client = client
        self.prices = MassivePriceProvider(client)
        self.actions = MassiveActionsProvider(client)

    def fetch(self, instrument, market, resolution):
        first_observation = len(self.client.observations)
        response = self.prices.fetch(instrument, market, resolution)
        actions = self.actions.fetch(
            instrument, market, resolution.history_start, resolution.session_date
        )
        payload = json.loads(response.payload)
        payload["corporate_actions"] = actions
        payload["capability_observations"] = self.client.observations[first_observation:]
        return response.model_copy(
            update={
                "payload": encode(payload),
                "retrieved_at": datetime.now(UTC),
            }
        )
