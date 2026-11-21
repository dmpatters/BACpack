"""Pull connectors: poll external HTTP endpoints on an interval, extract
mapped fields, and feed them into local objects at FEED_PRIORITY, like the
feed API.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import ssl
from typing import Any, Dict, List, Optional

import httpx
from bacpypes3.primitivedata import Boolean

from . import held_tasks, timeutil
from .connector_config import missing_credentials, missing_credentials_message, unreadable_credentials
from .models import BacnetObject, Connector, ConnectorMapping, Device
from .point_config import finite_float

logger = logging.getLogger(__name__)

SCHEDULER_TICK_SECONDS = 1.0
# Cap on exponential backoff for a connector whose host is unreachable.
MAX_BACKOFF_SECONDS = 300.0
# Largest response body a poll or preview reads, so a streaming endpoint
# can't grow memory without limit.
MAX_RESPONSE_BYTES = 1024 * 1024


class FetchError(Exception):
    """A response arrived but can't be used: not valid JSON, or larger than
    MAX_RESPONSE_BYTES. A content problem, not an outage: relinquish, but
    no backoff."""


CERTIFICATE_HINT = (
    ": the server's certificate couldn't be verified; if this is a "
    "self-signed lab endpoint, turn off Verify TLS certificate"
)


class _NoCookies(http.cookiejar.CookiePolicy):
    """A cookie jar that refuses every cookie. httpx keeps one jar per client
    and ConnectorPoller shares a client between every connector, so a session
    cookie one server sets would otherwise go out on another connector's
    requests to the same host and answer it with the first one's data. A
    connector's own Cookie header is a custom header and is kept."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


def _without_cookies(client: httpx.AsyncClient) -> httpx.AsyncClient:
    client.cookies.jar.set_policy(_NoCookies())
    return client


def _is_certificate_failure(exc: BaseException) -> bool:
    """True if a connect error was a TLS certificate check failing.
    httpx wraps the ssl error, so walk the chain, and fall back on OpenSSL's
    reason code in the message."""
    seen = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        seen = seen.__cause__ or seen.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _keeps_credentials(url: httpx.URL, location: httpx.URL) -> bool:
    """True if a redirect from `url` to `location` keeps credentials: the same
    origin, or a plain http -> https upgrade on the same host. The same rule
    httpx applies to Authorization, used here for a connector's custom headers."""

    def port(u: httpx.URL) -> Optional[int]:
        return u.port if u.port is not None else {"http": 80, "https": 443}.get(u.scheme)

    if url.host != location.host:
        return False
    if url.scheme == location.scheme:
        return port(url) == port(location)
    return (url.scheme, port(url), location.scheme, port(location)) == ("http", 80, "https", 443)


def extract_json_path(data: Any, path: str) -> Any:
    """Minimal dot-path into a parsed JSON document, e.g. "data.temperature".
    No array indexing or wildcards. Raises KeyError if any segment is
    missing or isn't a dict, a parse-level failure to callers.
    """
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def flatten_json_fields(data: dict, *, _prefix: str = "") -> List[dict]:
    """Every field extract_json_path can reach, depth-first, for the JSON
    path picker. A list is
    listed with is_array=True but not recursed into: there's no array
    indexing to select an element with.
    """
    fields: List[dict] = []
    for key, value in data.items():
        path = f"{_prefix}.{key}" if _prefix else key
        if isinstance(value, dict):
            fields.extend(flatten_json_fields(value, _prefix=path))
        elif isinstance(value, list):
            fields.append({"path": path, "value": None, "is_array": True})
        else:
            fields.append({"path": path, "value": value, "is_array": False})
    return fields


def release_http_mappings(mappings) -> None:
    """Queue a relinquish of the feed slot (priority 10) for each mapping's
    object whose device is running, in the caller's session, when an
    http-poll mapping is deleted or its connector disabled or deleted. A
    device that isn't running needs nothing: priority arrays are runtime-only."""
    for mapping in mappings:
        obj = mapping.object
        if obj is not None and obj.device is not None and obj.device.runtime.process_status == "running":
            obj.queue_feed_write(None)


def _missing(connector: Connector) -> List[str]:
    return missing_credentials(
        auth_type=connector.auth_type,
        auth_password=connector.auth_password,
        auth_token=connector.auth_token,
        headers=connector.headers,
    )


def _unreadable(connector: Connector) -> List[str]:
    return unreadable_credentials(
        auth_password=connector.auth_password, auth_token=connector.auth_token, headers=connector.headers
    )


def _settings_key(connector: Connector) -> tuple:
    """What a poll depends on; a change clears the connector's backoff."""
    return (
        connector.url,
        connector.poll_interval_seconds,
        connector.timeout_seconds,
        connector.response_format,
        connector.auth_type,
        connector.auth_username,
        connector.auth_password,
        connector.auth_token,
        json.dumps(connector.headers or {}, sort_keys=True),
        connector.verify_tls,
    )


def coerce_mapped_value(
    raw: Any, *, scale: float, offset: float, value_map: Optional[dict]
) -> float:
    """Apply value_map (string -> number translation, for binary/multi-state
    objects), then scale/offset. Raises ValueError/TypeError on anything
    that doesn't coerce, a parse-level failure to the poller.
    """
    if value_map is not None:
        key = _value_map_key(raw, value_map)
        if key is None:
            raise ValueError(f"value {raw!r} not in value_map")
        raw = value_map[key]
    return finite_float(finite_float(raw) * scale + offset)


def _value_map_key(raw: Any, value_map: dict) -> Optional[str]:
    """The value_map key `raw` matches, or None. A boolean -- JSON true/false,
    or a BACnet Boolean, whose str() is "1"/"0" -- is looked up as "true"/
    "false", the way the JSON reads; str(raw) ("True") also matches."""
    candidates = [str(raw)]
    if isinstance(raw, (bool, Boolean)):
        candidates.insert(0, "true" if raw else "false")
    return next((key for key in candidates if key in value_map), None)


class ConnectorStatus:
    """Runtime-only per-connector status: last poll time/result, and the
    consecutive-failure count driving backoff. Not persisted, to avoid a DB
    write every poll.
    """

    def __init__(self) -> None:
        self.last_poll_at: Optional[str] = None
        self.last_success: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures: int = 0
        # mapping id -> error or None, the shape BACnet connectors report.
        self.read_errors: Dict[str, Optional[str]] = {}

    def to_dict(self) -> dict:
        return {
            "last_poll_at": self.last_poll_at,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "read_errors": dict(self.read_errors),
        }


class ConnectorPoller:
    """Polls every enabled http-poll connector; its mappings can target any
    device. One instance in the parent for the process lifetime, started by
    web/app.py's lifespan.
    "bacnet" connectors run in their device's own process instead
    (bacnet_connectors.py).
    """

    def __init__(
        self,
        session_factory,
        *,
        client: Optional[httpx.AsyncClient] = None,
        insecure_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.session_factory = session_factory
        # Test injection point; made on first use otherwise: building one
        # loads the CA bundle, slow enough to notice at every app start.
        self._client = _without_cookies(client) if client is not None else None
        self._owns_client = client is None
        # httpx checks certificates per client, not per request, so connectors
        # with verify_tls off share this second client, made on first use.
        self._insecure_client = _without_cookies(insecure_client) if insecure_client is not None else None
        self._owns_insecure_client = insecure_client is None

        self._scheduler_task: Optional[asyncio.Task] = None
        # connector_id -> next-due loop time.
        self._due: Dict[int, float] = {}
        # connector_id -> its poll in flight. One task per poll, so a slow
        # upstream waiting out its timeout doesn't hold up the others.
        self._in_flight: Dict[int, asyncio.Task] = {}
        # connector_id -> ConnectorStatus, exposed for a status API.
        self.status: Dict[int, ConnectorStatus] = {}
        # connector_id -> _settings_key last seen.
        self._settings: Dict[int, tuple] = {}
        # Connectors waiting for a credential, whose values were released on
        # entering that state.
        self._awaiting_credentials: set = set()

    def start(self) -> None:
        self._track_new_connectors()

        # Started even with zero connectors: the per-tick
        # _track_new_connectors picks up connectors created later.
        self._scheduler_task = asyncio.ensure_future(self._scheduler_loop())

    def stop(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        for task in self._in_flight.values():
            task.cancel()
        self._in_flight.clear()

        if self._owns_client and self._client is not None:
            held_tasks.start(self._client.aclose())
        self._client = None
        if self._owns_insecure_client and self._insecure_client is not None:
            held_tasks.start(self._insecure_client.aclose())
        self._insecure_client = None

        self._due.clear()
        self._settings.clear()
        self._awaiting_credentials.clear()
        self.status.clear()

    def _client_for(self, verify_tls: bool) -> httpx.AsyncClient:
        if verify_tls:
            if self._client is None:
                self._client = _without_cookies(httpx.AsyncClient())
            return self._client
        if self._insecure_client is None:
            self._insecure_client = _without_cookies(httpx.AsyncClient(verify=False))
        return self._insecure_client

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
            try:
                self._track_new_connectors()
                now = asyncio.get_event_loop().time()
                for connector_id, due_at in list(self._due.items()):
                    if now < due_at or connector_id in self._in_flight:
                        continue
                    self._in_flight[connector_id] = asyncio.ensure_future(
                        self._run_poll(connector_id)
                    )
            except Exception:
                # Last-resort net: an escaped exception would end polling
                # for every connector until a restart.
                logger.exception("connector scheduler tick failed")

    async def _run_poll(self, connector_id: int) -> None:
        try:
            await self._poll_one(connector_id)
        except Exception:
            # Last-resort net; _poll_one handles ordinary failures itself.
            logger.exception("connector %d: poll failed", connector_id)
        finally:
            if self._in_flight.get(connector_id) is asyncio.current_task():
                del self._in_flight[connector_id]

    def _track_new_connectors(self) -> None:
        """Start tracking any newly enabled connector (first poll after one
        interval). An edited connector is due at once with its backoff
        cleared. Disabled/deleted ones are dropped by _poll_one.
        """
        loop = asyncio.get_event_loop()
        with self.session_factory() as session:
            connectors = session.query(Connector).filter_by(enabled=True, type="http-poll").all()
            rows = [(c.id, c.poll_interval_seconds, _settings_key(c)) for c in connectors]

        for connector_id, interval, settings in rows:
            if connector_id not in self._due:
                self.status[connector_id] = ConnectorStatus()
                self._due[connector_id] = loop.time() + interval
                self._settings[connector_id] = settings
            elif self._settings.get(connector_id) != settings:
                # The edit is usually the fix: don't wait out the backoff.
                self._settings[connector_id] = settings
                self.status.setdefault(connector_id, ConnectorStatus()).consecutive_failures = 0
                self._due[connector_id] = loop.time()

    async def _fetch(
        self,
        *,
        url: str,
        timeout: float,
        response_format: str,
        auth_type: Optional[str],
        auth_username: Optional[str],
        auth_password: Optional[str],
        auth_token: Optional[str],
        headers: dict,
        verify_tls: bool,
    ) -> Any:
        """GET + auth + parse, for _poll_one and preview(). Raises FetchError
        for a response that can't be used, anything else for a
        transport/HTTP failure."""
        auth = (auth_username or "", auth_password or "") if auth_type == "basic" else None
        request_headers = dict(headers)
        if auth_type == "bearer" and auth_token:
            request_headers["Authorization"] = f"Bearer {auth_token}"

        # One deadline for the whole request, body included: httpx's own
        # timeout is per read, so a trickling endpoint would never hit it.
        client = self._client_for(verify_tls)
        try:
            return await asyncio.wait_for(
                self._get(client, url, auth, request_headers, response_format, timeout), timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"no complete response within {timeout:g} s") from None
        except httpx.ConnectError as exc:
            if verify_tls and _is_certificate_failure(exc):
                raise httpx.ConnectError(f"{exc}{CERTIFICATE_HINT}") from exc
            raise

    async def _get(self, client, url, auth, headers: dict, response_format: str, timeout: float) -> Any:
        response = await self._send_following_redirects(client, url, auth, headers, timeout)
        try:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FetchError("response larger than 1 MiB")
            encoding = response.encoding or "utf-8"
        finally:
            await response.aclose()
        if response_format != "json":
            return bytes(body).decode(encoding, errors="replace")
        try:
            return json.loads(bytes(body))
        except ValueError as exc:
            raise FetchError(f"response isn't valid JSON: {exc}") from None

    async def _send_following_redirects(
        self, client: httpx.AsyncClient, url, auth, headers: dict, timeout: float
    ) -> httpx.Response:
        """GET `url`, following redirects, and return the final response
        still streaming; the caller closes it.

        Followed by hand, not follow_redirects=True: httpx strips only
        Authorization on a redirect away from the origin, so custom headers
        (where API keys usually go) would leak to the target. They're dropped
        wherever httpx drops Authorization (see _keeps_credentials) and stay
        dropped for the rest of the chain. One client for the whole chain, so
        a redirect never changes whether the certificate is checked."""
        custom_names = list(headers)
        request = client.build_request("GET", url, headers=headers, timeout=timeout)
        response = await client.send(request, auth=auth, stream=True)
        for _ in range(client.max_redirects):
            next_request = response.next_request
            if next_request is None:
                return response
            await response.aclose()
            if not _keeps_credentials(request.url, next_request.url):
                for name in custom_names:
                    next_request.headers.pop(name, None)
            request = next_request
            # auth=None: basic auth was applied to the first request only,
            # as with follow_redirects=True; its header travels (or not) with
            # the redirect request httpx built.
            response = await client.send(request, auth=None, stream=True)
        await response.aclose()
        raise httpx.TooManyRedirects("too many redirects", request=request)

    async def preview(self, connector_id: int) -> dict:
        """On-demand fetch for the JSON path picker.
        Never raises: a broken upstream or missing connector is a displayable
        ok:false, not a server error.
        """
        with self.session_factory() as session:
            connector = session.get(Connector, connector_id)
            if connector is None:
                return {"ok": False, "error": "connector not found"}
            url = connector.url
            timeout = connector.timeout_seconds
            response_format = connector.response_format
            auth_type = connector.auth_type
            auth_username = connector.auth_username
            auth_password = connector.auth_password
            auth_token = connector.auth_token
            headers = dict(connector.headers or {})
            verify_tls = connector.verify_tls
            missing = _missing(connector)
            unreadable = _unreadable(connector)
        if missing:
            return {"ok": False, "error": missing_credentials_message(missing, unreadable)}

        try:
            body = await self._fetch(
                url=url,
                timeout=timeout,
                response_format=response_format,
                auth_type=auth_type,
                auth_username=auth_username,
                auth_password=auth_password,
                auth_token=auth_token,
                headers=headers,
                verify_tls=verify_tls,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if response_format != "json":
            return {"ok": True, "response_format": response_format, "raw_text": body}
        if not isinstance(body, dict):
            return {"ok": False, "error": "top-level response is not a JSON object"}
        return {"ok": True, "response_format": "json", "fields": flatten_json_fields(body)}

    async def _poll_one(self, connector_id: int) -> None:
        with self.session_factory() as session:
            connector = session.get(Connector, connector_id)
            if connector is None or not connector.enabled or connector.type != "http-poll":
                self._due.pop(connector_id, None)
                self._settings.pop(connector_id, None)
                self._awaiting_credentials.discard(connector_id)
                self.status.pop(connector_id, None)
                return

            settings = _settings_key(connector)
            interval = connector.poll_interval_seconds
            url = connector.url
            timeout = connector.timeout_seconds
            response_format = connector.response_format
            auth_type = connector.auth_type
            auth_username = connector.auth_username
            auth_password = connector.auth_password
            auth_token = connector.auth_token
            headers = dict(connector.headers or {})
            verify_tls = connector.verify_tls
            missing = _missing(connector)
            unreadable = _unreadable(connector)

            mapping_rows: List[dict] = []
            for mapping in connector.mappings:
                obj = session.get(BacnetObject, mapping.object_id)
                if obj is None:
                    continue  # object deleted out from under a configured mapping
                mapping_rows.append(
                    {
                        "mapping_id": mapping.id,
                        "object_id": obj.id,
                        "device_id": obj.device_id,
                        "json_path": mapping.json_path,
                        "scale": mapping.scale,
                        "offset": mapping.offset,
                        "value_map": mapping.value_map,
                    }
                )

        status = self.status.setdefault(connector_id, ConnectorStatus())
        loop = asyncio.get_event_loop()
        status.last_poll_at = timeutil.iso_utc(timespec="seconds")

        if missing:
            # Not a failure to back off from: it changes only with a save.
            message = missing_credentials_message(missing, unreadable)
            status.last_success = False
            status.last_error = message
            status.read_errors = {str(row["mapping_id"]): message for row in mapping_rows}
            if connector_id not in self._awaiting_credentials:
                self._awaiting_credentials.add(connector_id)
                await self._relinquish_all(connector_id, mapping_rows)
            self._set_due(connector_id, settings, loop.time() + interval)
            return
        self._awaiting_credentials.discard(connector_id)

        try:
            body = await self._fetch(
                url=url,
                timeout=timeout,
                response_format=response_format,
                auth_type=auth_type,
                auth_username=auth_username,
                auth_password=auth_password,
                auth_token=auth_token,
                headers=headers,
                verify_tls=verify_tls,
            )
        except FetchError as exc:
            # The response arrived but can't be used: relinquish every
            # mapping, keep the normal interval.
            status.last_success = False
            status.last_error = str(exc)
            status.read_errors = {str(row["mapping_id"]): str(exc) for row in mapping_rows}
            await self._relinquish_all(connector_id, mapping_rows)
            self._set_due(connector_id, settings, loop.time() + interval)
            return
        except Exception as exc:
            # Transport-level failure: relinquish every mapping and back off
            # exponentially, capped.
            status.last_success = False
            status.last_error = str(exc)
            status.read_errors = {str(row["mapping_id"]): str(exc) for row in mapping_rows}
            if not self._edited_since(connector_id, settings):
                status.consecutive_failures += 1
            await self._relinquish_all(connector_id, mapping_rows)
            # Cap the exponent too: a long outage would otherwise make
            # 2**failures a slow bignum every poll.
            backoff = min(interval * (2 ** min(status.consecutive_failures, 20)), MAX_BACKOFF_SECONDS)
            self._set_due(connector_id, settings, loop.time() + backoff)
            return

        status.last_success = True
        status.last_error = None
        status.consecutive_failures = 0

        read_errors: Dict[str, Optional[str]] = {}
        for row in mapping_rows:
            try:
                raw = (
                    extract_json_path(body, row["json_path"])
                    if response_format == "json"
                    else body
                )
                value = coerce_mapped_value(
                    raw, scale=row["scale"], offset=row["offset"], value_map=row["value_map"]
                )
            except Exception as exc:
                # Parse-level failure: relinquish only this mapping, no
                # backoff -- likely a config mistake or a sometimes-absent
                # field, not an outage.
                logger.warning(
                    "connector %d mapping %d: %s",
                    connector_id,
                    row["mapping_id"],
                    exc,
                    extra={"device_id": row["device_id"]},
                )
                read_errors[str(row["mapping_id"])] = (
                    f"no value at {row['json_path']!r}" if isinstance(exc, KeyError) else str(exc)
                )
                await self._queue_pending_write(row["mapping_id"], row["object_id"], row["device_id"], None)
                continue

            read_errors[str(row["mapping_id"])] = None
            await self._queue_pending_write(row["mapping_id"], row["object_id"], row["device_id"], value)

        status.read_errors = read_errors
        self._set_due(connector_id, settings, loop.time() + interval)

    def _set_due(self, connector_id: int, settings: tuple, when: float) -> None:
        """Schedule the next poll, unless the connector was edited while this
        poll was in flight: then poll again at once, so the old poll's
        outcome doesn't put back a backoff the edit cleared."""
        if self._edited_since(connector_id, settings):
            self._due[connector_id] = asyncio.get_event_loop().time()
        else:
            self._due[connector_id] = when

    def _edited_since(self, connector_id: int, settings: tuple) -> bool:
        current = self._settings.get(connector_id)
        return current is not None and current != settings

    async def _relinquish_all(self, connector_id: int, mapping_rows: List[dict]) -> None:
        for row in mapping_rows:
            try:
                await self._queue_pending_write(row["mapping_id"], row["object_id"], row["device_id"], None)
            except Exception:
                # One bad mapping's relinquish failure mustn't take the rest
                # of this poll down with it.
                logger.exception(
                    "connector %d: failed to relinquish mapping %d",
                    connector_id,
                    row["mapping_id"],
                    extra={"device_id": row["device_id"]},
                )

    async def _queue_pending_write(self, mapping_id: int, object_id: int, device_id: int, value) -> None:
        """Queue a feed write on the mapped object's row;
        a no-op if the target device isn't running. The commit runs in a worker
        thread, since it can wait out another process's write lock.
        """
        await asyncio.to_thread(self._queue_pending_write_sync, mapping_id, object_id, device_id, value)

    def _queue_pending_write_sync(self, mapping_id: int, object_id: int, device_id: int, value) -> None:
        with self.session_factory() as session:
            # A poll in flight while its mapping was deleted, or its connector
            # disabled, writes nothing: the API already relinquished the point
            # (release_http_mappings) and a late value would undo that.
            mapping = session.get(ConnectorMapping, mapping_id)
            if mapping is None or not mapping.connector.enabled:
                return
            device = session.get(Device, device_id)
            if device is None or device.runtime.process_status != "running":
                return
            obj = session.get(BacnetObject, object_id)
            if obj is None:
                return  # object deleted out from under a still-configured mapping
            obj.queue_feed_write(value)
            session.commit()
