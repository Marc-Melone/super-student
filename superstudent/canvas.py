"""Read-only Canvas REST client: pagination, rate limits, retries, safe downloads.

It only ever sends GET requests, and it only sends your token to your Canvas host. A file is
downloaded from Canvas with your token; Canvas then redirects to its file storage with a short-lived
signed link, and the token is not sent there (requests drops it when the host changes). If that
fails, it asks Canvas for the file's signed public link and downloads that without the token.
"""

from __future__ import annotations

import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from . import __version__
from .util import mark_downloaded

USER_AGENT = f"SuperStudent/{__version__} (+read-only study sync)"
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
# Signs that a web page is Canvas's (or a school's single sign-on) login page rather than a course file.
_LOGIN_MARKERS = re.compile(
    rb"(name=[\"']?pseudonym_session|id=[\"']?login_form|action=[\"'][^\"']*/login/(?:canvas|saml|cas|ldap)|"
    rb"name=[\"']?SAMLRequest|Log In to Canvas)",
    re.I,
)
_LOGIN_PATH = re.compile(r"(^|/)(login|saml2?|idp|sso|shibboleth|cas)(/|$)", re.I)
_NETWORK_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                   requests.exceptions.ContentDecodingError)


class CanvasError(Exception):
    def __init__(self, status: int, url: str, message: str = ""):
        self.status = status
        self.url = url
        self.message = message
        super().__init__(f"HTTP {status} for {_redact(url)}{': ' + message if message else ''}")


class AuthError(CanvasError):
    """401 with Canvas rejecting the token itself: missing, expired or revoked."""


class ForbiddenError(CanvasError):
    """403 (not a rate limit), or a 401 that only means "students can't see this"."""


class NotFoundError(CanvasError):
    """404: doesn't exist, or hidden from students."""


class DownloadError(Exception):
    pass


def _redact(url: str) -> str:
    return re.sub(r"(verifier|access_token|ks|sig|signature|token)=[^&]+", r"\1=…", url, flags=re.I)


def _origin(url: str) -> Tuple[str, str, int]:
    """Normalized transport origin; credentials must never cross a scheme or port boundary."""
    parts = urlparse(url)
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None:
        raise ValueError("Canvas addresses must be HTTP(S) URLs without embedded credentials")
    return scheme, host, parts.port if parts.port is not None else (443 if scheme == "https" else 80)


class _CanvasSession(requests.Session):
    def should_strip_auth(self, old_url: str, new_url: str) -> bool:
        try:
            return _origin(old_url) != _origin(new_url) or super().should_strip_auth(old_url, new_url)
        except ValueError:
            return True


class Canvas:
    def __init__(self, base_url: str, token: str, log: Optional[Callable[[str], None]] = None,
                 timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.origin = _origin(self.base)
        self.host = self.origin[1]
        if self.origin[0] != "https" and self.host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Canvas requires HTTPS; HTTP is allowed only for local test servers")
        self._token = token
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        self._local = threading.local()
        self.calls = 0
        self._calls_lock = threading.Lock()
        self.auth_failed: Optional[AuthError] = None   # set when Canvas rejects the token itself

    # -- sessions (one per thread; requests.Session is not guaranteed thread-safe)
    def _session(self, with_token: bool) -> requests.Session:
        name = "api" if with_token else "plain"
        sess = getattr(self._local, name, None)
        if sess is None:
            sess = _CanvasSession()
            sess.headers["User-Agent"] = USER_AGENT
            if with_token:
                sess.headers["Accept"] = "application/json"
            setattr(self._local, name, sess)
        return sess

    def _is_canvas_host(self, url: str) -> bool:
        try:
            return _origin(url) == self.origin
        except ValueError:
            return False

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base + ("" if path.startswith("/") else "/") + path

    # -- core request with retries
    def _get(self, url: str, params: Optional[Dict[str, Any]] = None, *, stream: bool = False,
             with_token: bool = True, retries: int = 5) -> requests.Response:
        if with_token and not self._is_canvas_host(url):
            raise ValueError("refusing to send the Canvas token outside the configured secure Canvas address")
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self._token}"} if with_token else {}
            try:
                resp = self._session(with_token).get(
                    url, params=params, headers=headers, timeout=self.timeout, stream=stream,
                    allow_redirects=True,
                )
            except _NETWORK_ERRORS as exc:
                if attempt >= retries:
                    raise CanvasError(0, url, f"network error: {exc.__class__.__name__}") from exc
                self._sleep(attempt, None)
                continue
            with self._calls_lock:
                self.calls += 1
            status = resp.status_code
            if status < 400:
                self._throttle(resp)
                return resp
            body = ""
            if not stream:
                body = resp.text[:500]
            else:
                try:
                    body = resp.raw.read(500, decode_content=True).decode("utf-8", "replace")
                except Exception:
                    body = ""
                resp.close()
            rate_limited = status == 429 or (status == 403 and "rate limit" in body.lower())
            if (rate_limited or status >= 500) and attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                self.log(f"  Canvas said {'slow down' if rate_limited else f'error {status}'}; retrying…")
                self._sleep(attempt, retry_after, minimum=5.0 if rate_limited else 1.0)
                continue
            message = _clean_error(body)
            if status == 401:
                # Only a rejected token means "sign in again". Canvas also answers 401 for things a student
                # simply isn't allowed to see ("user not authorized to perform that action").
                api = urlparse(url).path.startswith("/api/")
                if with_token and api and (resp.headers.get("WWW-Authenticate") or "invalid access token" in body.lower()):
                    exc = AuthError(status, url, message or "Invalid access token")
                    self.auth_failed = exc
                    raise exc
                raise ForbiddenError(status, url, message or "not authorized")
            if status == 403:
                raise ForbiddenError(status, url, message)
            if status == 404:
                raise NotFoundError(status, url, message)
            raise CanvasError(status, url, message)

    @staticmethod
    def _sleep(attempt: int, retry_after: Optional[str], minimum: float = 1.0) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(60.0, minimum * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
        time.sleep(max(0.2, delay))

    @staticmethod
    def _throttle(resp: requests.Response) -> None:
        remaining = resp.headers.get("X-Rate-Limit-Remaining")
        try:
            if remaining is not None and float(remaining) < 100:
                time.sleep(1.5 if float(remaining) > 30 else 4.0)
        except ValueError:
            pass

    # -- JSON helpers
    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None, tries: int = 3) -> Tuple[Any, requests.Response]:
        """GET and decode JSON, retrying a response that arrives cut off or garbled."""
        attempt = 0
        while True:
            attempt += 1
            resp = self._get(url, params)
            if not resp.content:
                return None, resp
            try:
                return resp.json(), resp
            except ValueError as exc:
                if attempt >= tries:
                    raise CanvasError(0, url, "network error: Canvas sent an incomplete response") from exc
                self._sleep(attempt, None)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        data, _ = self._get_json(self.url(path), params)
        return data

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                 key: Optional[str] = None, max_pages: int = 200) -> Iterator[Any]:
        query: Optional[Dict[str, Any]] = {"per_page": 100}
        query.update(params or {})
        next_url: Optional[str] = self.url(path)
        pages = 0
        while next_url and pages < max_pages:
            data, resp = self._get_json(next_url, query)
            pages += 1
            data = data if data is not None else []
            if key and isinstance(data, dict):
                data = data.get(key) or []
            if isinstance(data, dict):
                yield data
                return
            for item in data or []:
                yield item
            match = _LINK_NEXT.search(resp.headers.get("Link", ""))
            next_url = match.group(1) if match else None
            query = None  # the next link already carries the query string
            if next_url and not self._is_canvas_host(next_url):
                break

    def get_all(self, path: str, params: Optional[Dict[str, Any]] = None, key: Optional[str] = None) -> List[Any]:
        return list(self.paginate(path, params, key=key))

    def get_text(self, url: str) -> str:
        """A small text resource on the Canvas host (e.g. a caption track), fetched with the token."""
        url = urljoin(self.base + "/", url)
        if not self._is_canvas_host(url):
            raise ValueError("only Canvas-hosted text is fetched with the token")
        resp = self._get(url)
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    # -- downloads
    def download(self, url: str, dest: Path, *, max_bytes: Optional[int] = None, expect_html: bool = False,
                 file_id: Optional[str] = None, submission_id: Optional[str] = None) -> Dict[str, Any]:
        """Download to `dest` atomically.

        Canvas file links are fetched from the Canvas host with the token (without the old `verifier`
        shortcut, which Canvas is retiring). If that fails and the file id is known, Canvas is asked for the
        file's signed public link, which is fetched without the token. Other hosts never get the token."""
        url = urljoin(self.base + "/", url) if url else ""
        plan: List[Tuple[str, bool]] = []
        if url and self._is_canvas_host(url):
            plan.append((_without_verifier(url), True))
        elif url:
            plan.append((url, False))
        if file_id:
            plan.append(("public_url", False))
        if url and self._is_canvas_host(url) and "verifier=" in url:
            plan.append((url, False))          # older Canvas: the pre-signed link on its own
        last_error: Optional[Exception] = None
        for target, with_token in plan:
            if target == "public_url":
                try:
                    params = {"submission_id": submission_id} if submission_id else None
                    data = self.get(f"/api/v1/files/{file_id}/public_url", params) or {}
                except CanvasError as exc:
                    last_error = last_error or exc
                    continue
                target = str(data.get("public_url") or "") if isinstance(data, dict) else ""
                if not target.startswith(("http://", "https://")):
                    continue
            for attempt in (1, 2):                     # one retry if the connection drops mid-file
                try:
                    return self._download_once(target, dest, max_bytes=max_bytes, expect_html=expect_html,
                                               with_token=with_token)
                except (DownloadError, CanvasError) as exc:
                    if isinstance(exc, DownloadError) and "too large" in str(exc):
                        raise
                    # keep the most telling reason: a sign-in page or a network drop beats a later "not found"
                    if last_error is None or isinstance(exc, DownloadError) or not isinstance(last_error, DownloadError):
                        last_error = exc
                    if "network error" in str(exc) and attempt == 1:
                        self._sleep(attempt, None)
                        continue
                    break
        if last_error is None:
            raise DownloadError("Canvas didn't provide a download link")
        if isinstance(last_error, DownloadError):
            raise last_error
        raise DownloadError(str(last_error))

    def _download_once(self, url: str, dest: Path, *, max_bytes: Optional[int], expect_html: bool,
                       with_token: bool) -> Dict[str, Any]:
        resp = self._get(url, stream=True, with_token=with_token, retries=3)
        short = dest.name.encode("utf-8")[:120].decode("utf-8", "ignore")   # names are limited in bytes, not letters
        tmp = None
        try:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length = resp.headers.get("Content-Length")
            if max_bytes and length and length.isdigit() and int(length) > max_bytes:
                raise DownloadError(f"too large ({int(length) // (1 << 20)} MB)")
            final = resp.url or url
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            head = b""
            fd, temp_name = tempfile.mkstemp(prefix=f".{short}.", suffix=".part", dir=str(dest.parent))
            tmp = Path(temp_name)
            with os.fdopen(fd, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    if len(head) < 65536:
                        head += chunk[: 65536 - len(head)]
                    size += len(chunk)
                    if max_bytes and size > max_bytes:
                        raise DownloadError(f"too large (over {max_bytes // (1 << 20)} MB)")
                    fh.write(chunk)
            looks_html = ctype in ("text/html", "application/xhtml+xml") or \
                head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))
            if looks_html:
                if _LOGIN_PATH.search(urlparse(final).path) or _LOGIN_MARKERS.search(head):
                    raise DownloadError("Canvas returned a sign-in page instead of the file")
                if not expect_html:
                    raise DownloadError("Canvas returned a web page instead of the file (usually a sign-in or error page)")
            os.replace(tmp, dest)
            mark_downloaded(dest)
            return {"content_type": ctype, "size": size, "final_url": final}
        except _NETWORK_ERRORS as exc:
            raise DownloadError(f"network error while downloading ({exc.__class__.__name__})") from exc
        finally:
            resp.close()
            try:
                if tmp is not None:
                    tmp.unlink()
            except OSError:
                pass


def _without_verifier(url: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "verifier"]
    return urlunparse(parts._replace(query=urlencode(query)))


def _clean_error(body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    match = re.search(r'"message"\s*:\s*"([^"]{1,200})"', body)
    if match:
        return match.group(1)
    if body.startswith("<"):
        return ""
    return body[:160]
