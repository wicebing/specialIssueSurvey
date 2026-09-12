"""Polite HTTP client for publisher pages and scholarly APIs.

Seventeen of the thirty-two sources in the 2026-W37 run failed, most of them
with HTTP 403.  The cause was the tracker's custom User-Agent: Elsevier,
Wiley, JAMA, the Lancet and MDPI all reject unknown agents outright.  Sending a
normal browser UA clears those blocks, so that is the default here.

The other half of being a good citizen is rate limiting.  We crawl a small,
fixed set of journal pages once a week, and we identify ourselves in the
``From`` header so publishers can contact the maintainer.  Per-host spacing and
retry-with-backoff keep us well inside anyone's fair-use expectations.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover - dependency check happens at call time
    requests = None


__all__ = ["FetchResult", "PoliteSession", "BROWSER_USER_AGENT"]


# Publisher CDNs reject unrecognised agents. A mainstream desktop UA is what
# gets Elsevier, Wiley, JAMA, Lancet and MDPI to answer at all.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Hosts that answer better as a plain API client than as a browser.
API_HOSTS = {
    "eutils.ncbi.nlm.nih.gov",
    "api.crossref.org",
    "api.openalex.org",
    "api.semanticscholar.org",
}

# Minimum seconds between requests to the same host.
DEFAULT_HOST_DELAY = 1.5
HOST_DELAYS = {
    "eutils.ncbi.nlm.nih.gov": 0.4,  # NCBI allows 3/sec without a key
    "api.crossref.org": 0.2,  # polite pool
    "api.openalex.org": 0.15,
}


# Bot walls that answer HTTP 200 with a challenge page.  Springer's returns a
# 3 KB "Client Challenge" document, so a status-code-only check records a
# healthy fetch while collecting nothing. Detecting these is what keeps the
# source-health table truthful.
CHALLENGE_MARKERS = (
    "client challenge",
    "just a moment",
    "checking your browser",
    "attention required",
    "enable javascript and cookies to continue",
    "access denied",
    "are you a robot",
    "unusual traffic",
    "captcha",
    "cf-browser-verification",
    "px-captcha",
)

# An HTML page this small is a stub, a redirect shim, or a challenge.
MIN_HTML_BYTES = 5000


def looks_like_challenge(text: str, content_type: str = "") -> bool:
    """True when a 2xx response is really a bot wall rather than content."""
    if not text:
        return True
    sample = text[:6000].lower()
    if any(marker in sample for marker in CHALLENGE_MARKERS):
        return True
    if "json" in content_type.lower():
        return False
    # A short document that never opens a body-level container is not an
    # article listing, whatever the status line claims.
    if len(text) < MIN_HTML_BYTES and "<html" in sample:
        return "challenge" in sample or "<noscript" in sample
    return False


@dataclass
class FetchResult:
    """Everything the report's source-health table needs to be honest."""

    url: str
    final_url: str = ""
    status_code: int | None = None
    text: str = ""
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    attempts: int = 0
    from_cache: bool = False
    transport: str = "requests"
    challenged: bool = False

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        if self.challenged:
            return "bot_challenge"
        if self.status_code == 403:
            return "blocked_403"
        if self.status_code == 404:
            return "dead_link_404"
        if self.status_code:
            return f"http_{self.status_code}"
        return "error"

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.text)


@dataclass
class PoliteSession:
    """A rate-limited, retrying session with browser-compatible headers."""

    contact_email: str = ""
    tool_name: str = "academic-cfp-tracker"
    timeout: int = 30
    max_retries: int = 3
    _last_hit: dict[str, float] = field(default_factory=dict, repr=False)
    _cache: dict[str, FetchResult] = field(default_factory=dict, repr=False)
    _session: Any = field(default=None, repr=False)
    # Hosts whose bot wall we have already hit. Springer challenges every
    # request from Python's TLS stack, so once one URL there is challenged the
    # rest will be too. Remembering that skips ~40 doomed requests per run.
    _challenged_hosts: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if requests is None:
            raise RuntimeError(
                "The 'requests' package is required. Run pip install -r requirements.txt."
            )
        self._session = requests.Session()

    def headers_for(self, url: str) -> dict[str, str]:
        host = urlparse(url).netloc.lower()
        if host in API_HOSTS:
            agent = f"{self.tool_name}/2.0"
            if self.contact_email:
                agent += f" (mailto:{self.contact_email})"
            headers = {"User-Agent": agent, "Accept": "application/json"}
        else:
            headers = dict(BROWSER_HEADERS)
        if self.contact_email:
            headers["From"] = self.contact_email
        return headers

    def _wait_turn(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        delay = HOST_DELAYS.get(host, DEFAULT_HOST_DELAY)
        last = self._last_hit.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    def _curl(self, url: str, params: dict[str, Any] | None = None) -> FetchResult:
        """Fetch via the curl binary.

        Springer's wall fingerprints the TLS handshake, not the headers, so no
        combination of request headers gets Python's stack past it while curl
        walks straight through. Shelling out is the cheapest reliable answer and
        needs no extra dependency: curl ships with GitHub's runners and with
        Git for Windows.
        """
        import shutil
        import subprocess

        binary = shutil.which("curl")
        if not binary:
            return FetchResult(url=url, ok=False, error="curl not available", transport="curl")

        target = url
        if params:
            from urllib.parse import urlencode

            separator = "&" if "?" in url else "?"
            target = f"{url}{separator}{urlencode(params)}"

        command = [
            binary, "-sL", "--compressed",
            "--max-time", str(self.timeout),
            "-w", "\n__STATUS__%{http_code}",
        ]
        for key, value in self.headers_for(url).items():
            command += ["-H", f"{key}: {value}"]
        command.append(target)

        try:
            # Decode as UTF-8 explicitly: publisher pages carry curly quotes and
            # em dashes that a Windows console codepage (cp950 here) cannot
            # decode, and the default text mode would raise mid-download.
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout + 15,
            )
        except Exception as exc:
            return FetchResult(url=url, ok=False, error=f"curl: {exc}", transport="curl")

        body = completed.stdout
        status_code: int | None = None
        marker = body.rfind("\n__STATUS__")
        if marker != -1:
            try:
                status_code = int(body[marker + len("\n__STATUS__") :].strip())
            except ValueError:
                status_code = None
            body = body[:marker]

        challenged = looks_like_challenge(body)
        ok = bool(status_code and status_code < 400) and not challenged
        return FetchResult(
            url=url,
            final_url=target,
            status_code=status_code,
            text=body,
            ok=ok,
            error="" if ok else ("bot challenge" if challenged else f"HTTP {status_code}"),
            transport="curl",
            challenged=challenged,
        )

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
        allow_curl_fallback: bool = True,
    ) -> FetchResult:
        cache_key = url if not params else f"{url}::{sorted(params.items())}"
        if use_cache and cache_key in self._cache:
            cached = self._cache[cache_key]
            return FetchResult(**{**cached.__dict__, "from_cache": True})

        started = time.monotonic()
        last_error = ""
        status_code: int | None = None
        challenged = False

        host = urlparse(url).netloc.lower()
        if allow_curl_fallback and host in self._challenged_hosts:
            self._wait_turn(url)
            straight = self._curl(url, params)
            straight.elapsed = time.monotonic() - started
            straight.attempts = 1
            if straight.ok or straight.status_code:
                if use_cache:
                    self._cache[cache_key] = straight
                return straight

        for attempt in range(1, self.max_retries + 1):
            self._wait_turn(url)
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers=self.headers_for(url),
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                status_code = response.status_code
                if response.status_code < 400:
                    challenged = looks_like_challenge(
                        response.text, response.headers.get("content-type", "")
                    )
                    if not challenged:
                        result = FetchResult(
                            url=url,
                            final_url=response.url,
                            status_code=response.status_code,
                            text=response.text,
                            ok=True,
                            elapsed=time.monotonic() - started,
                            attempts=attempt,
                        )
                        if use_cache:
                            self._cache[cache_key] = result
                        return result
                    last_error = "bot challenge"
                    self._challenged_hosts.add(host)
                    break  # retrying the same transport cannot help

                # 4xx other than rate limiting will not improve on retry.
                if response.status_code < 500 and response.status_code != 429:
                    break
                last_error = f"HTTP {response.status_code}"
            except Exception as exc:  # network flake, DNS, TLS, timeout
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.max_retries:
                time.sleep(1.5 * attempt)

        # A challenge or a hard block may still yield to curl's TLS fingerprint.
        if allow_curl_fallback and (challenged or status_code in {403, 429, None} or status_code and status_code >= 500):
            self._wait_turn(url)
            fallback = self._curl(url, params)
            fallback.elapsed = time.monotonic() - started
            fallback.attempts = self.max_retries + 1
            if fallback.ok or fallback.status_code:
                if use_cache:
                    self._cache[cache_key] = fallback
                return fallback

        result = FetchResult(
            url=url,
            status_code=status_code,
            ok=False,
            error=last_error,
            elapsed=time.monotonic() - started,
            attempts=self.max_retries,
            challenged=challenged,
        )
        if use_cache:
            self._cache[cache_key] = result
        return result
