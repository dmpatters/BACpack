"""Stand-ins for site details in a diagnostics file.

One `StandIns` table is filled while the config is replaced field by field
(`standin_config`), then `scan` replaces the same real values, and any IPv4
address the table hasn't seen, in free text such as log lines.

The scan costs, per token of the text, at most the token count of the longest
value in the table; it does not grow with the number of values. The text is
split into tokens and each start position walks a trie of case-folded tokens.
No database access.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from bacpypes3.basetypes import ObjectType, PropertyIdentifier

from . import bacpypes3_compat
from .log_names import unescape_name

# Words log lines are made of; a name equal to one is matched as a whole
# token in its own case only, so replacing it doesn't mangle the sentence.
LOG_KEYWORDS = frozenset(
    {
        "bacnet", "device", "object", "read", "write", "error", "warning", "info", "started",
        "stopped", "request", "response", "debug", "critical",
    }
)

# Texts shorter than this ("On", "Off") aren't identifying and are left alone
# in log lines.
MIN_SCANNED_TEXT = 4


def _bacnet_words() -> frozenset:
    words: set = set()
    for enum_class in (ObjectType, PropertyIdentifier):
        words.update(bacpypes3_compat.enum_name_maps(enum_class)[2].values())
    return frozenset(words)


# A name is never replaced inside one of these (`analog-value:1`, `present-value`).
BACNET_WORDS = _bacnet_words()

_IPV4 = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})(/\d+)?(:\d+)?")
_IPV4_IN_TEXT = re.compile(r"(?<![0-9A-Za-z])(?<![0-9A-Za-z]\.)\d{1,3}(?:\.\d{1,3}){3}(?![0-9A-Za-z]|\.[0-9A-Za-z])")
_NETWORK_PREFIX = re.compile(r"(\d+):(.+)")
_HEX = re.compile(r"0x([0-9a-fA-F]+)")
_HOST_PORT = re.compile(r"(.+?)(:\d+)?")
_URL_HOST_PORT = re.compile(r"(\[[^\]]*\]|[^:]*)(?::(\d*))?")
_TOKEN = re.compile(r"[^\W_]+|[^\w\s]|_")
_BACNET_WORD = re.compile(r"(?<![\w-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?::\d+)?(?![\w-])")
_QUOTED_REF = re.compile(
    r'(?:[a-z][a-z0-9]*(?:-[a-z0-9]+)*:\d+|device \d+|connector \d+) (?P<q>"(?:[^"\\]|\\.)*")'
)
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://[^:@/?#]*):[^@/?#]*@")
_SECRET_QUERY_NAME = re.compile(r"(?:.*(?:key|token|secret|passw|pwd)|pass|sig|signature|auth|authorization)")


# --- masking of credentials in URLs (both modes) ---


def mask_url_secrets(url: str) -> str:
    """The URL with a userinfo password and the values of secret-looking query
    parameters (key, token, secret, password, sig, ...) replaced by `***`."""
    if not url:
        return url
    url = _USERINFO.sub(r"\1:***@", url)
    base, mark, rest = url.partition("?")
    if not mark:
        return url
    query, hash_mark, fragment = rest.partition("#")
    parts = []
    for part in query.split("&"):
        name, equals, _value = part.partition("=")
        if equals and _SECRET_QUERY_NAME.fullmatch(re.sub(r"[-_]", "", name).lower()):
            part = f"{name}=***"
        parts.append(part)
    return f"{base}?{'&'.join(parts)}{hash_mark}{fragment}"


def mask_config_urls(document: dict) -> dict:
    """A copy of a config export with every connector URL's credentials masked."""
    out = copy.deepcopy(document)
    for connector in out.get("connectors", []):
        if isinstance(connector.get("url"), str):
            connector["url"] = mask_url_secrets(connector["url"])
    return out


# --- the table ---


def _path_pattern(directory: str) -> str:
    parts = re.split(r"[\\/]+", directory.rstrip("\\/"))
    # `+`: a repr or an OSError text doubles each backslash.
    return r"[\\/]+".join(re.escape(p) for p in parts)


def _usable_directory(directory: str) -> bool:
    """False for a directory that would match everywhere: empty, `/`, a drive root."""
    parts = [p for p in re.split(r"[\\/]+", directory) if p]
    return bool(parts) and not (len(parts) == 1 and re.fullmatch(r"[A-Za-z]:", parts[0]))


def _short_path(path: str) -> Optional[str]:
    """The Windows 8.3 form of an existing path (C:\\Users\\USERNA~1), else None."""
    if os.name != "nt" or not os.path.exists(path):
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))  # pyright: ignore[reportAttributeAccessIssue] -- windll exists on Windows only
    return buffer.value if 0 < length < len(buffer) else None


@dataclass(slots=True)
class _Entry:
    standin: str
    raw: tuple  # the value's tokens in their own case
    free_text: bool  # description, state text, ...: a plain word is matched strictly


class StandIns:
    def __init__(self, *, data_dir: str | None = None, home: str | None = None):
        self._names: dict[tuple[str, str], str] = {}
        self._current: dict[str, str] = {}  # real name -> stand-in, for `id "name"` log fields
        self._current_folded: dict[str, str] = {}
        self._kind_counts: dict[str, int] = {}
        self._texts: dict[str, str] = {}
        self._ips: dict[str, str] = {}
        self._hosts: dict[str, str] = {}
        self._paths: dict[str, str] = {}
        self._queries: dict[str, str] = {}
        self._hex: dict[str, str] = {}
        # A trie of case-folded tokens: token -> child node; a node's None key holds the
        # entry of the value that ends there.
        self._trie: dict = {}
        self._keywords = set(LOG_KEYWORDS)
        self._dirs: list = []
        for directory, label in ((data_dir, "<data-dir>"), (home, "<home>")):
            if not directory or not _usable_directory(directory):
                continue
            for form in (directory, _short_path(directory)):
                if form and _usable_directory(form):
                    self._dirs.append((re.compile(f"(?i:{_path_pattern(form)})"), label, len(form)))
        self._dirs.sort(key=lambda d: -d[2])

    def _register(self, real: str, standin: str, *, free_text: bool = False) -> None:
        if not real or real == standin:
            return
        raw = tuple(_TOKEN.findall(real))
        if not any(token[0].isalnum() for token in raw):
            return
        key = tuple(token.casefold() for token in raw)
        node = self._trie
        for token in key:
            node = node.setdefault(token, {})
        node.setdefault(None, _Entry(standin, raw, free_text))

    def name(self, kind: str, real: str) -> str:
        key = (kind, real)
        if key not in self._names:
            n = self._kind_counts[kind] = self._kind_counts.get(kind, 0) + 1
            standin = self._names[key] = f"{kind}-{n}"
            self._keywords.add(kind)
            self._current.setdefault(real, standin)
            self._current_folded.setdefault(real.casefold(), standin)
            self._register(real, standin)
        return self._names[key]

    def text(self, real: str) -> str:
        if real not in self._texts:
            self._texts[real] = f"text-{len(self._texts) + 1}"
            if len(real.strip()) >= MIN_SCANNED_TEXT:
                self._register(real, self._texts[real], free_text=True)
        return self._texts[real]

    def ipv4(self, real: str) -> str:
        if real not in self._ips:
            n = len(self._ips)
            self._ips[real] = f"10.0.{n // 254}.{n % 254 + 1}"
        return self._ips[real]

    def _host(self, real: str) -> str:
        if _IPV4.fullmatch(real):
            return self.ipv4(real)
        if real not in self._hosts:
            self._hosts[real] = f"host-{len(self._hosts) + 1}.example"
            self._register(real, self._hosts[real])
        return self._hosts[real]

    def _hex_address(self, digits: str) -> str:
        if digits not in self._hex:
            n = len(self._hex) + 1
            while (candidate := f"{n:0{len(digits)}x}"[-len(digits):]) == digits.lower():
                n += 1
            self._hex[digits] = candidate
        return self._hex[digits]

    def address(self, real: Optional[str]) -> Optional[str]:
        if not real or real == "*":
            return real
        prefix = ""
        rest = real
        network = _NETWORK_PREFIX.fullmatch(real)
        if network is not None and not _IPV4.fullmatch(real):
            prefix, rest = f"{network.group(1)}:", network.group(2)
        if rest == "*" or rest.isdigit():
            return real
        ip = _IPV4.fullmatch(rest)
        if ip is not None:
            return f"{prefix}{self.ipv4(ip.group(1))}{ip.group(2) or ''}{ip.group(3) or ''}"
        hex_match = _HEX.fullmatch(rest)
        if hex_match is not None:
            standin = f"{prefix}0x{self._hex_address(hex_match.group(1))}"
            self._register(real, standin)
            return standin
        host_port = _HOST_PORT.fullmatch(rest)
        host, port = (host_port.group(1), host_port.group(2) or "") if host_port else (rest, "")
        return f"{prefix}{self._host(host)}{port}"

    def url(self, real: Optional[str]) -> Optional[str]:
        if not real:
            return real
        try:
            parts = urlsplit(real)
        except ValueError:
            return self.text(real)
        host_port = _URL_HOST_PORT.fullmatch(parts.netloc.rsplit("@", 1)[-1])
        if not parts.hostname or host_port is None or not host_port.group(1):
            return self.text(real)
        host = self._host(host_port.group(1))
        port = f":{host_port.group(2)}" if host_port.group(2) else ""
        # Path segments get stand-ins but aren't scanned for: a segment like
        # "device" or "1" would mangle every log line.
        segments = []
        for segment in parts.path.split("/"):
            if segment and segment not in self._paths:
                self._paths[segment] = f"path-{len(self._paths) + 1}"
            segments.append(self._paths[segment] if segment else segment)
        out = f"{parts.scheme}://{host}{port}{'/'.join(segments)}"
        if parts.query:
            if parts.query not in self._queries:
                self._queries[parts.query] = f"q-{len(self._queries) + 1}"
                self._register(parts.query, self._queries[parts.query])
            out += f"?{self._queries[parts.query]}"
        self._register(real, out)
        return out

    # --- text scan ---

    def scan(self, text: str) -> str:
        if not text:
            return text
        spans: list = []
        if '"' in text:
            for match in _QUOTED_REF.finditer(text):
                spans.append((match.start("q"), match.end("q"), self._quoted_replacement(match.group("q"))))
        if self._trie:
            spans.extend(self._known_spans(text))
        for match in _IPV4_IN_TEXT.finditer(text):
            if all(int(octet) <= 255 for octet in match.group().split(".")):
                spans.append((match.start(), match.end(), self.ipv4(match.group())))
        for pattern, label, _length in self._dirs:
            for match in pattern.finditer(text):
                spans.append((match.start(), match.end(), label))
        if not spans:
            return text
        spans.sort(key=lambda s: (s[0], s[0] - s[1]))
        out = []
        position = 0
        for start, end, replacement in spans:
            if start < position:
                continue
            out.append(text[position:start])
            out.append(replacement)
            position = end
        out.append(text[position:])
        return "".join(out)

    def _quoted_replacement(self, quoted: str) -> str:
        name = unescape_name(quoted[1:-1])
        standin = self._current.get(name) or self._current_folded.get(name.casefold())
        return f'"{standin}"' if standin else '"<name>"'

    def _known_spans(self, text: str) -> list:
        tokens = [(m.start(), m.end(), m.group().casefold()) for m in _TOKEN.finditer(text)]
        protected = [
            (m.start(), m.end())
            for m in _BACNET_WORD.finditer(text)
            if m.group().partition(":")[0] in BACNET_WORDS
        ]
        spans = []
        i = 0
        count = len(tokens)
        while i < count:
            # Every value that starts here, found in one walk down the trie.
            ends = []
            node = self._trie.get(tokens[i][2])
            j = i
            while node is not None:
                entry = node.get(None)
                if entry is not None:
                    ends.append((j - i + 1, entry))
                j += 1
                node = node.get(tokens[j][2]) if j < count else None
            for length, entry in reversed(ends):
                start, end = tokens[i][0], tokens[i + length - 1][1]
                if self._accepted(text, start, end, length, entry, protected):
                    spans.append((start, end, entry.standin))
                    i += length - 1
                    break
            i += 1
        return spans

    def _accepted(self, text: str, start: int, end: int, length: int, entry: _Entry, protected: list) -> bool:
        if length == 1:
            word = entry.raw[0]
            exact = len(word) < 3 or word.casefold() in self._keywords
            if exact and text[start:end] != word:
                return False
            if (exact or (entry.free_text and word.isalpha())) and (
                (start > 0 and text[start - 1] in "-_")
                or text[end : end + 1] in ("-", "_")
                or (text[end : end + 1] == ":" and text[end + 1 : end + 2].isdigit())
            ):
                return False
        for p_start, p_end in protected:
            if p_start < end and start < p_end:
                contains = start <= p_start and p_end <= end and (end - start) > (p_end - p_start)
                if not contains:
                    return False
        return True


# --- config documents ---


def standin_config(document: dict, table: StandIns) -> dict:
    """A copy of a config export with the site's names, addresses, URLs and
    free text replaced field by field. Everything else (ids, instances, types,
    units, ports, settings) stays real."""
    out = copy.deepcopy(document)
    for device in out.get("devices", []):
        _standin_device(device, table)
    for connector in out.get("connectors", []):
        _standin_connector(connector, table)
    return out


def _replace(row: dict, field: str, replacement) -> None:
    if row.get(field) is not None:
        row[field] = replacement(row[field])


def _standin_device(device: dict, table: StandIns) -> None:
    _replace(device, "name", lambda v: table.name("device", v))
    for field in ("description", "location", "vendor_name"):
        _replace(device, field, table.text)
    _replace(device, "bind_address", table.address)
    _replace(device, "foreign_bbmd", table.address)
    _replace(device, "bbmd_peers", lambda peers: [table.address(p) for p in peers])
    for obj in device.get("objects", []):
        _standin_object(obj, table)


def _standin_object(obj: dict, table: StandIns) -> None:
    _replace(obj, "name", lambda v: table.name(obj["object_type"], v))
    _replace(obj, "trend_log_name", lambda v: table.name("trend-log", v))
    for field in ("description", "active_text", "inactive_text"):
        _replace(obj, field, table.text)
    _replace(obj, "state_text", lambda texts: [table.text(t) for t in texts])
    for entry in obj.get("notification_recipient_list") or []:
        recipient = entry.get("recipient") or {}
        _replace(recipient, "address", table.address)


def _standin_connector(connector: dict, table: StandIns) -> None:
    _replace(connector, "name", lambda v: table.name("connector", v))
    _replace(connector, "url", table.url)
    _replace(connector, "auth_username", table.text)
    _replace(connector, "remote_address", table.address)
    _replace(connector, "headers", lambda headers: {table.name("header", k): v for k, v in headers.items()})
    for key in ("mappings", "write_mappings"):
        for mapping in connector.get(key) or []:
            _replace(mapping, "json_path", table.text)
            _replace(mapping, "value_map", lambda m: {table.text(k): v for k, v in m.items()})
