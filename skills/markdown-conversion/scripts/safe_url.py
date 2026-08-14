"""Bounded remote input downloader with DNS/IP pinning and redirect revalidation."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import mimetypes
import socket
import ssl
import time
import urllib.parse


MAX_REMOTE_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 10.0
TOTAL_TIMEOUT = 30.0
_REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class RemoteDownload:
    payload: bytes
    locator: str
    media_type: str | None
    suffix: str


def redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or "invalid-host"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return urllib.parse.urlunsplit((parsed.scheme.lower(), f"{host}{port}", parsed.path or "/", "", ""))


def _endpoint(value: str) -> tuple[urllib.parse.SplitResult, str]:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("Remote input must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Remote input credentials in URLs are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise RuntimeError("Remote input has an invalid port") from exc
    try:
        candidates = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise RuntimeError(f"Remote input host could not be resolved: {parsed.hostname}") from exc
    public: list[str] = []
    for candidate in candidates:
        if not ipaddress.ip_address(candidate).is_global:
            raise RuntimeError("Remote input resolved to a non-public network address")
        public.append(candidate)
    if not public:
        raise RuntimeError("Remote input host did not resolve to a public address")
    return parsed, sorted(public)[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _request(value: str, deadline: float) -> tuple[int, dict[str, str], bytes]:
    parsed, ip = _endpoint(value)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Remote input exceeded the total timeout")
    timeout = min(CONNECT_TIMEOUT, remaining)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    connection_type = _PinnedHTTPSConnection if parsed.scheme.lower() == "https" else _PinnedHTTPConnection
    connection = connection_type(parsed.hostname or "", ip, port, timeout)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = parsed.hostname or ""
    if parsed.port:
        host_header = f"{host_header}:{parsed.port}"
    try:
        connection.request("GET", target, headers={
            "Host": host_header,
            "User-Agent": "file-processing-markdown-conversion/6.5",
            "Accept-Encoding": "identity",
        })
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_REMOTE_BYTES:
                    raise RuntimeError(f"Remote input exceeds {MAX_REMOTE_BYTES} bytes")
            except ValueError as exc:
                raise RuntimeError("Remote input has an invalid Content-Length") from exc
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Remote input exceeded the total timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(CONNECT_TIMEOUT, remaining))
            chunk = response.read(min(1024 * 1024, MAX_REMOTE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_REMOTE_BYTES:
                raise RuntimeError(f"Remote input exceeds {MAX_REMOTE_BYTES} bytes")
        return response.status, headers, b"".join(chunks)
    except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as exc:
        raise RuntimeError(f"Remote input request failed: {type(exc).__name__}") from exc
    finally:
        connection.close()


def download_url(value: str) -> RemoteDownload:
    deadline = time.monotonic() + TOTAL_TIMEOUT
    current = value
    for redirect_index in range(MAX_REDIRECTS + 1):
        status, headers, payload = _request(current, deadline)
        if status in _REDIRECTS:
            if redirect_index >= MAX_REDIRECTS:
                raise RuntimeError(f"Remote input exceeded {MAX_REDIRECTS} redirects")
            location = headers.get("location")
            if not location:
                raise RuntimeError("Remote input redirect lacked a Location header")
            current = urllib.parse.urljoin(current, location)
            _endpoint(current)
            continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"Remote input returned HTTP {status}")
        media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
        filename = urllib.parse.urlsplit(current).path.rsplit("/", 1)[-1]
        suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        if not suffix or len(suffix) > 10:
            suffix = mimetypes.guess_extension(media_type or "") or ".html"
        return RemoteDownload(payload, redact_url(current), media_type, suffix)
    raise RuntimeError("Remote input redirect processing failed")
