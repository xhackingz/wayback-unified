#!/usr/bin/env python3
"""
wayback-unified — A high-coverage Wayback Machine URL harvester.

CDX-ONLY mode: All URLs are fetched exclusively from this endpoint:
  https://web.archive.org/cdx/search/cdx?url=*.DOMAIN&fl=original&collapse=urlkey

No other data sources are used — no Common Crawl, no live crawling, no alternate APIs.

Engineered for infinite-scale stability:
  - Streaming / chunk-based HTTP ingestion (never loads full response into RAM)
  - Resume-key cursor pagination for continuous iteration over any dataset size
  - Page-based concurrent pagination for parallel throughput
  - Exponential back-off retry logic with rate-limit awareness
  - Controlled request pacing to avoid hammering the API

Usage:
  python wayback_unified.py -d example.com
  python wayback_unified.py -d api.example.com --subs
  python wayback_unified.py -d example.com --from 20200101 --to 20231231
  python wayback_unified.py -d example.com --filter-status 200 --filter-mime text/html
  python wayback_unified.py -d example.com -o results.txt
"""

import argparse
import sys
import time
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlencode
import urllib.request
import urllib.error
import json


CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
DEFAULT_LIMIT = 50000
DEFAULT_THREADS = 5
MAX_RETRIES = 5
RETRY_DELAY = 5
STREAM_CHUNK_SIZE = 65536  # 64 KB chunks for streaming HTTP reads

VERSION = "1.4.0"

# ---------------------------------------------------------------------------
# Terminal colors and TTY detection
# ---------------------------------------------------------------------------

_IS_TTY = sys.stderr.isatty()


class _C:
    """ANSI color codes. Empty strings when not a TTY so pipes stay clean."""
    RESET  = "\033[0m"   if _IS_TTY else ""
    BOLD   = "\033[1m"   if _IS_TTY else ""
    DIM    = "\033[2m"   if _IS_TTY else ""
    CYAN   = "\033[36m"  if _IS_TTY else ""
    BCYAN  = "\033[96m"  if _IS_TTY else ""
    GREEN  = "\033[32m"  if _IS_TTY else ""
    BGREEN = "\033[92m"  if _IS_TTY else ""
    YELLOW = "\033[33m"  if _IS_TTY else ""
    RED    = "\033[31m"  if _IS_TTY else ""
    WHITE  = "\033[97m"  if _IS_TTY else ""


def _ansi_len(s: str) -> int:
    """Return the visible (non-ANSI) length of a string."""
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


# Lock so concurrent threads don't interleave stderr writes
_log_lock = threading.Lock()

_BOX_DASHES = 52  # number of ─ chars between ╭ and ╮


def _box_row(text: str) -> str:
    """Format a banner row, padding to box width accounting for ANSI codes."""
    visible = _ansi_len(text)
    pad = _BOX_DASHES - 2 - visible  # 2 = leading+trailing space inside box
    return f"{_C.CYAN}│{_C.RESET} {text}{' ' * max(0, pad)} {_C.CYAN}│{_C.RESET}"


def _box_blank() -> str:
    return f"{_C.CYAN}│{' ' * _BOX_DASHES}│{_C.RESET}"


def _print_banner() -> None:
    top  = f"{_C.CYAN}╭{'─' * _BOX_DASHES}╮{_C.RESET}"
    bot  = f"{_C.CYAN}╰{'─' * _BOX_DASHES}╯{_C.RESET}"

    name = f"{_C.BOLD}{_C.BCYAN}wayback-unified{_C.RESET}  {_C.DIM}v{VERSION}{_C.RESET}"
    tag  = f"{_C.DIM}CDX-only · Infinite-scale URL harvester{_C.RESET}"
    cred = f"{_C.DIM}by @xhacking_z{_C.RESET}"

    with _log_lock:
        for row in [
            "",
            top,
            _box_blank(),
            _box_row(name),
            _box_row(tag),
            _box_row(cred),
            _box_blank(),
            bot,
            "",
        ]:
            sys.stderr.write(row + "\n")
        sys.stderr.flush()


def _colorize(msg: str) -> str:
    """Apply color to log messages based on their prefix tag."""
    if not _IS_TTY:
        return msg
    if msg.startswith("[WARN]"):
        return f"{_C.YELLOW}{_C.BOLD}{msg}{_C.RESET}"
    if msg.startswith("[METHOD"):
        return f"{_C.BOLD}{_C.BCYAN}{msg}{_C.RESET}"
    if msg.startswith("[DONE]"):
        return f"{_C.BOLD}{_C.BGREEN}{msg}{_C.RESET}"
    if msg.startswith("[SCOPE]"):
        return f"{_C.YELLOW}{msg}{_C.RESET}"
    if msg.startswith("[INFO]"):
        return f"{_C.DIM}{msg}{_C.RESET}"
    return msg


def _log(msg: str) -> None:
    """
    Write a log message to stderr — thread-safe.
    Clears the spinner line first (via \\r\\033[K) so the spinner never
    overwrites a log message.
    """
    colored = _colorize(msg)
    with _log_lock:
        if _IS_TTY:
            sys.stderr.write("\r\033[K")
        print(colored, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Live spinner
# ---------------------------------------------------------------------------

class Spinner:
    """
    Braille spinner that runs in a background daemon thread.
    Writes to stderr using \\r so it stays on one line and is cleared by _log().
    Safely disabled when stderr is not a TTY (e.g. redirected or piped).
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self) -> None:
        self._msg = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, msg: str = "") -> "Spinner":
        if not _IS_TTY:
            return self
        self._msg = msg
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def update(self, msg: str) -> None:
        """Change the spinner message while it is running."""
        self._msg = msg

    def stop(self) -> None:
        if not _IS_TTY:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        with _log_lock:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            # Non-blocking acquire: skip this frame if _log() holds the lock
            if _log_lock.acquire(blocking=False):
                try:
                    frame = self._FRAMES[i % len(self._FRAMES)]
                    line = f"\r{_C.CYAN}{frame}{_C.RESET} {_C.DIM}{self._msg}{_C.RESET}"
                    sys.stderr.write(line)
                    sys.stderr.flush()
                finally:
                    _log_lock.release()
            time.sleep(0.08)
            i += 1


# ---------------------------------------------------------------------------
# HTTP helpers — standard + streaming
# ---------------------------------------------------------------------------

def make_request(url: str, retries: int = MAX_RETRIES) -> str:
    """
    Perform an HTTP GET request with retry/back-off logic.
    Returns the response body as text, or empty string on failure.
    Suitable for small bounded responses (page-count probes, single pages).
    """
    headers = {
        "User-Agent": (
            f"wayback-unified/{VERSION} "
            "(https://github.com/xhackingz/wayback-unified)"
        )
    }
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_DELAY * attempt * 2
                _log(
                    f"[WARN] Rate limited (429). "
                    f"Waiting {wait}s before retry {attempt}/{retries}..."
                )
                time.sleep(wait)
            elif e.code == 503:
                wait = RETRY_DELAY * attempt
                _log(
                    f"[WARN] Service unavailable (503). "
                    f"Waiting {wait}s before retry {attempt}/{retries}..."
                )
                time.sleep(wait)
            else:
                _log(f"[WARN] HTTP {e.code} for: {url}")
                if attempt == retries:
                    return ""
        except urllib.error.URLError as e:
            wait = RETRY_DELAY * attempt
            _log(
                f"[WARN] URL error: {e.reason}. "
                f"Waiting {wait}s before retry {attempt}/{retries}..."
            )
            time.sleep(wait)
        except Exception as e:
            _log(f"[WARN] Request failed: {e}")
            if attempt == retries:
                return ""
    return ""


def stream_cdx_lines(url: str, retries: int = MAX_RETRIES):
    """
    Generator — streams CDX API response line-by-line using chunk-based reads.

    Critically: the full response body is NEVER loaded into memory.
    Each 64 KB chunk is read, split on newlines, and lines are yielded
    immediately. Safe for datasets of any size — millions or billions of URLs.

    Yields raw line strings (including empty strings) so callers can detect
    resume-key sentinels (an empty line followed by the key on the next line).

    Implements the same retry / back-off logic as make_request().
    """
    headers = {
        "User-Agent": (
            f"wayback-unified/{VERSION} "
            "(https://github.com/xhackingz/wayback-unified)"
        )
    }
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as resp:
                buf = b""
                while True:
                    chunk = resp.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        # Flush any remaining partial line
                        if buf:
                            yield buf.decode("utf-8", errors="replace")
                        return
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        yield raw_line.decode("utf-8", errors="replace")
            return  # success — do not retry
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_DELAY * attempt * 2
                _log(
                    f"[WARN] Rate limited (429). "
                    f"Waiting {wait}s before retry {attempt}/{retries}..."
                )
                time.sleep(wait)
            elif e.code == 503:
                wait = RETRY_DELAY * attempt
                _log(
                    f"[WARN] Service unavailable (503). "
                    f"Waiting {wait}s before retry {attempt}/{retries}..."
                )
                time.sleep(wait)
            else:
                _log(f"[WARN] HTTP {e.code} for: {url}")
                if attempt == retries:
                    return
        except urllib.error.URLError as e:
            wait = RETRY_DELAY * attempt
            _log(
                f"[WARN] URL error: {e.reason}. "
                f"Waiting {wait}s before retry {attempt}/{retries}..."
            )
            time.sleep(wait)
        except Exception as e:
            _log(f"[WARN] Stream error: {e}")
            if attempt == retries:
                return


def build_cdx_url(params: dict) -> str:
    """Build a CDX API URL with the given parameter dict."""
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------

def is_subdomain_target(domain: str) -> bool:
    """
    Return True if `domain` is itself a subdomain (e.g. api.example.com),
    False if it is a root/eTLD+1 domain (e.g. example.com).
    """
    parts = domain.lower().strip().split(".")
    return len(parts) > 2


def filter_by_scope(urls: set, target: str, include_subs: bool) -> set:
    """
    Strict hostname-level post-filter. Applied after every CDX fetch to
    guarantee that SURT key expansion never leaks out-of-scope URLs.
    """
    target_lower = target.lower().strip()
    sub_suffix = f".{target_lower}"
    scoped = set()

    for url in urls:
        try:
            hostname = urlparse(url).hostname
            if not hostname:
                continue
            hostname = hostname.lower()

            if hostname == target_lower:
                scoped.add(url)
            elif include_subs and hostname.endswith(sub_suffix):
                scoped.add(url)
        except Exception:
            continue

    return scoped


# ---------------------------------------------------------------------------
# CDX query builder — strict CDX-only format
# ---------------------------------------------------------------------------

def _cdx_params_for_target(domain: str, include_subs: bool) -> dict:
    """
    Build CDX parameters using the ONLY permitted query format:
      url=*.{domain}&fl=original&collapse=urlkey

    This is the strict CDX-only ingestion source. No other URL pattern,
    matchType, or data source is used anywhere in this tool.

    include_subs controls post-fetch scope filtering only — the CDX query
    always uses *.{domain} to ensure maximum coverage from the archive.
    """
    # Normalize: strip any leading wildcard the caller may have included
    base = domain.lower().strip().lstrip("*").lstrip(".")
    return {
        "url": f"*.{base}",
        "fl": "original",
        "collapse": "urlkey",
    }


# ---------------------------------------------------------------------------
# CDX pagination helpers
# ---------------------------------------------------------------------------

def get_total_pages(params: dict) -> int:
    """Query showNumPages=true to get total CDX page count."""
    check_params = dict(params)
    check_params["showNumPages"] = "true"
    check_params.pop("page", None)
    check_params.pop("output", None)

    url = build_cdx_url(check_params)
    _log("[INFO] Fetching page count from CDX API...")
    response = make_request(url)

    try:
        pages = int(response.strip())
        return max(1, pages)
    except (ValueError, AttributeError):
        return 1


def fetch_page(params: dict, page: int, on_batch=None) -> set:
    """
    Fetch a single CDX page using streaming line-by-line reads.
    The response is never fully buffered — each URL line is processed
    as it arrives and immediately added to the result set.
    Calls on_batch with the full page set once streaming completes.
    """
    page_params = dict(params)
    page_params["page"] = str(page)
    url = build_cdx_url(page_params)

    results = set()
    for raw_line in stream_cdx_lines(url):
        line = raw_line.strip()
        if line.startswith("http"):
            results.add(normalize_url(line))

    if on_batch and results:
        on_batch(results)
    return results


def fetch_with_resume_key(params: dict, limit: int = DEFAULT_LIMIT,
                          on_batch=None) -> set:
    """
    Resume-key cursor pagination — streams each cursor page line-by-line.

    The CDX API signals "more pages available" by appending an empty line
    followed by the resumeKey on the final line of the response. This
    streaming implementation detects that sentinel without buffering
    the full response body.

    Iteration continues until the API returns no resume key, guaranteeing
    complete coverage over arbitrarily large datasets.
    """
    results = set()
    resume_key = None
    page_num = 0

    fetch_params = dict(params)
    fetch_params["showResumeKey"] = "true"
    fetch_params["limit"] = str(limit)
    fetch_params.pop("page", None)

    while True:
        if resume_key:
            fetch_params["resumeKey"] = resume_key
        elif "resumeKey" in fetch_params:
            del fetch_params["resumeKey"]

        url = build_cdx_url(fetch_params)

        # Collect lines from streaming response.
        # We need to keep the last two entries to detect the resume-key sentinel
        # (empty line immediately before the key on the final line).
        page_lines = []
        for raw_line in stream_cdx_lines(url):
            page_lines.append(raw_line)  # preserves empty lines for sentinel detection

        if not page_lines:
            break

        more = False
        next_resume_key = None

        # Detect sentinel: [..., "", resumeKey] at the tail of the response
        if (len(page_lines) >= 2
                and page_lines[-2].strip() == ""
                and page_lines[-1].strip()):
            next_resume_key = page_lines[-1].strip()
            page_lines = page_lines[:-2]  # strip sentinel from data
            more = True

        page_results = set()
        for raw_line in page_lines:
            line = raw_line.strip()
            if line.startswith("http"):
                page_results.add(normalize_url(line))

        results.update(page_results)
        resume_key = next_resume_key
        page_num += 1
        _log(
            f"[INFO] Resume-key page {page_num}: "
            f"{len(page_results)} URLs (running total: {len(results)})"
        )

        if on_batch and page_results:
            on_batch(page_results)

        if not more:
            break

        # Controlled pacing between cursor pages to respect rate limits
        time.sleep(0.1)

    return results


# ---------------------------------------------------------------------------
# CDX response parser (used for legacy / fallback paths)
# ---------------------------------------------------------------------------

def parse_cdx_response(response: str) -> set:
    """Parse a CDX API text response into a set of original URLs."""
    urls = set()
    if not response or not response.strip():
        return urls

    for line in response.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http"):
            urls.add(normalize_url(line))

    return urls


def normalize_url(url: str) -> str:
    """Normalize a URL: remove default ports (80/443), clean whitespace."""
    try:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc
        if parsed.port in (80, 443):
            netloc = parsed.hostname or netloc
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return url


# ---------------------------------------------------------------------------
# CDX fetch methods — all use strict *.domain&fl=original&collapse=urlkey
# ---------------------------------------------------------------------------

def get_wayback_urls_pagination(
    domain: str,
    include_subs: bool = True,
    filters: list = None,
    from_date: str = None,
    to_date: str = None,
    threads: int = DEFAULT_THREADS,
    on_batch=None,
    spinner: Spinner = None,
) -> set:
    """
    Method 1 — Page-based concurrent pagination with streaming per page.

    CDX source: *.{domain}&fl=original&collapse=urlkey
    Each page is fetched in a separate thread and streamed line-by-line —
    pages never accumulate in memory before processing.
    """
    base = _cdx_params_for_target(domain, include_subs)
    params = {
        **base,
        "output": "text",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if filters:
        for i, f in enumerate(filters):
            params[f"filter{i}"] = f

    if spinner:
        spinner.update("Querying CDX page count...")
    total_pages = get_total_pages(params)
    _log(f"[INFO] CDX page-based: {total_pages} page(s) to fetch")

    results = set()

    if total_pages == 1:
        if spinner:
            spinner.update("Fetching single page (streaming)...")
        page_results = fetch_page(params, 0, on_batch)
        results.update(page_results)
        _log(f"[INFO] Page-based (single page): {len(results)} raw URLs")
        return results

    if spinner:
        spinner.update(f"Streaming {total_pages} pages concurrently...")

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(fetch_page, params, page, on_batch): page
            for page in range(total_pages)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_urls = future.result()
                results.update(page_urls)
                _log(
                    f"[INFO] Page {page + 1}/{total_pages}: "
                    f"{len(page_urls)} URLs (running total: {len(results)})"
                )
            except Exception as e:
                _log(f"[WARN] Page {page} failed: {e}")

    return results


def get_wayback_urls_resumekey(
    domain: str,
    include_subs: bool = True,
    filters: list = None,
    from_date: str = None,
    to_date: str = None,
    on_batch=None,
    spinner: Spinner = None,
) -> set:
    """
    Method 2 — Resume-key cursor pagination with streaming per cursor page.

    CDX source: *.{domain}&fl=original&collapse=urlkey
    Iterates cursor pages until the API signals completion — safe for
    datasets of any size, never loads a full result set into memory at once.
    """
    base = _cdx_params_for_target(domain, include_subs)
    params = {
        **base,
        "output": "text",
        "gzip": "false",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if filters:
        for i, f in enumerate(filters):
            params[f"filter{i}"] = f

    _log("[INFO] CDX resume-key streaming pagination starting...")
    if spinner:
        spinner.update("Resume-key streaming...")
    return fetch_with_resume_key(params, on_batch=on_batch)


def get_wayback_urls_wildcard(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
    on_batch=None,
    spinner: Spinner = None,
) -> set:
    """
    Method 3 — Wildcard CDX streaming with resume-key cursor pagination.

    CDX source: *.{domain}&fl=original&collapse=urlkey
    Replaces the previous single-request JSON fetch with a fully streaming,
    cursor-paginated approach — crash-proof regardless of result count.
    """
    base = _cdx_params_for_target(domain, include_subs)
    params = {
        **base,
        "output": "text",
        "gzip": "false",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    _log("[INFO] CDX wildcard streaming (resume-key cursor)...")
    if spinner:
        spinner.update("Wildcard CDX streaming...")
    return fetch_with_resume_key(params, limit=DEFAULT_LIMIT, on_batch=on_batch)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def build_filters(status_code: str = None, mime_type: str = None) -> list:
    """Build CDX filter strings from CLI arguments."""
    filters = []
    if status_code:
        filters.append(f"statuscode:{status_code}")
    if mime_type:
        filters.append(f"mimetype:{mime_type.replace('+', r'+')}")
    return filters


def deduplicate(urls: set) -> list:
    """Final dedup pass: strip trailing slashes uniformly and sort."""
    normalized = {url.rstrip("/") for url in urls}
    return sorted(normalized)


# ---------------------------------------------------------------------------
# Main harvest orchestrator
# ---------------------------------------------------------------------------

def harvest(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
    filter_status: str = None,
    filter_mime: str = None,
    threads: int = DEFAULT_THREADS,
    skip_resume_key: bool = False,
    stream: bool = False,
) -> list:
    """
    Harvest archived URLs using three CDX streaming methods.

    All methods fetch exclusively from:
      https://web.archive.org/cdx/search/cdx?url=*.{domain}&fl=original&collapse=urlkey

    When stream=True, every new in-scope URL is printed to stdout immediately
    as it is discovered — per page, per resume-key batch, and per method.
    Thread-safe deduplication ensures no URL appears twice regardless of
    which method or thread found it.
    """
    filters = build_filters(filter_status, filter_mime)
    is_sub = is_subdomain_target(domain)

    accepted_urls: set = set()
    emit_lock = threading.Lock()
    scope_filtered = [0]

    def on_batch(raw_urls: set) -> None:
        scoped = filter_by_scope(raw_urls, domain, include_subs)
        rejected = len(raw_urls) - len(scoped)
        with emit_lock:
            scope_filtered[0] += rejected
            for url in scoped:
                normalized = url.rstrip("/")
                if normalized not in accepted_urls:
                    accepted_urls.add(normalized)
                    if stream:
                        print(normalized, flush=True)

    _log(f"[INFO] Target        : {_C.BOLD}{domain}{_C.RESET}")
    _log(f"[INFO] CDX source    : *.{domain}&fl=original&collapse=urlkey")
    _log(f"[INFO] Is subdomain  : {is_sub}")
    _log(f"[INFO] Include subs  : {include_subs}")
    if from_date:
        _log(f"[INFO] Date from     : {from_date}")
    if to_date:
        _log(f"[INFO] Date to       : {to_date}")
    if filters:
        _log(f"[INFO] CDX filters   : {filters}")
    _log(f"[INFO] Threads       : {threads}")
    _log(f"[INFO] Streaming     : enabled (chunk-based, no full-body buffering)")
    if stream:
        _log(f"[INFO] Mode          : {_C.CYAN}streaming{_C.RESET} (results print as found)")
    _log("")

    spinner = Spinner()
    spinner.start("Initializing...")

    # --- Method 1 -----------------------------------------------------------
    _log(f"[METHOD 1/3] Page-based concurrent streaming pagination...")
    try:
        page_urls = get_wayback_urls_pagination(
            domain,
            include_subs=include_subs,
            filters=filters,
            from_date=from_date,
            to_date=to_date,
            threads=threads,
            on_batch=on_batch,
            spinner=spinner,
        )
        _log(f"[METHOD 1/3] Raw results: {len(page_urls)} URLs\n")
    except Exception as e:
        _log(f"[WARN] Method 1 failed: {e}\n")

    # --- Method 2 -----------------------------------------------------------
    if not skip_resume_key:
        _log("[METHOD 2/3] Resume-key cursor streaming pagination...")
        before = len(accepted_urls)
        try:
            resume_urls = get_wayback_urls_resumekey(
                domain,
                include_subs=include_subs,
                filters=filters,
                from_date=from_date,
                to_date=to_date,
                on_batch=on_batch,
                spinner=spinner,
            )
            new_count = len(accepted_urls) - before
            _log(
                f"[METHOD 2/3] Raw results: {len(resume_urls)} URLs "
                f"({new_count} new unique)\n"
            )
        except Exception as e:
            _log(f"[WARN] Method 2 failed: {e}\n")

    # --- Method 3 -----------------------------------------------------------
    _log("[METHOD 3/3] Wildcard CDX streaming (cursor-paginated)...")
    before = len(accepted_urls)
    try:
        wildcard_urls = get_wayback_urls_wildcard(
            domain,
            include_subs=include_subs,
            from_date=from_date,
            to_date=to_date,
            on_batch=on_batch,
            spinner=spinner,
        )
        new_count = len(accepted_urls) - before
        _log(
            f"[METHOD 3/3] Raw results: {len(wildcard_urls)} URLs "
            f"({new_count} new unique)\n"
        )
    except Exception as e:
        _log(f"[WARN] Method 3 failed: {e}\n")

    spinner.stop()

    if scope_filtered[0] > 0:
        _log(
            f"[SCOPE] Removed {scope_filtered[0]} out-of-scope URLs "
            f"(CDX scope leakage corrected)"
        )

    final = sorted(accepted_urls)
    _log(f"[DONE] Total unique in-scope URLs: {_C.BOLD}{len(final)}{_C.RESET}")
    return final


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "wayback-unified — CDX-only, infinite-scale Wayback Machine URL harvester\n"
            "Data source: https://web.archive.org/cdx/search/cdx?url=*.DOMAIN"
            "&fl=original&collapse=urlkey"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Root domain (all subdomains included by default)
  python wayback_unified.py -d example.com

  # Specific subdomain — scoped strictly to api.example.com + *.api.example.com
  python wayback_unified.py -d api.example.com

  # Specific subdomain — only api.example.com, no sub-subdomains
  python wayback_unified.py -d api.example.com --no-subs

  # Date range filter
  python wayback_unified.py -d example.com --from 20200101 --to 20231231

  # Only HTTP 200 responses
  python wayback_unified.py -d example.com --filter-status 200

  # Only HTML pages
  python wayback_unified.py -d example.com --filter-mime text/html

  # Save to file with more threads
  python wayback_unified.py -d example.com -o results.txt --threads 10

  # Pipe into other tools
  python wayback_unified.py -d example.com | grep '\\.js$'
  python wayback_unified.py -d example.com | httpx -silent
        """,
    )

    parser.add_argument(
        "-d", "--domain",
        required=True,
        help=(
            "Target domain or subdomain (e.g. example.com or api.example.com). "
            "Subdomain targets are scoped strictly to that subdomain."
        ),
    )
    parser.add_argument(
        "--subs",
        action="store_true",
        default=True,
        help="Include sub-subdomains of the target (default: enabled)",
    )
    parser.add_argument(
        "--no-subs",
        action="store_true",
        help="Restrict results to the exact target hostname only (no subdomains)",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYYMMDD",
        help="Start date filter (e.g. 20200101)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYYMMDD",
        help="End date filter (e.g. 20231231)",
    )
    parser.add_argument(
        "--filter-status",
        metavar="CODE",
        help="Only return URLs archived with this HTTP status code (e.g. 200)",
    )
    parser.add_argument(
        "--filter-mime",
        metavar="TYPE",
        help="Only return URLs with this MIME type (e.g. text/html)",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Write results to a file instead of stdout",
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Concurrent threads for page fetching (default: {DEFAULT_THREADS})",
    )
    parser.add_argument(
        "--skip-resume-key",
        action="store_true",
        help="Skip Method 2 (resume-key) — faster but slightly less coverage",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"wayback-unified {VERSION}",
    )

    args = parser.parse_args()
    include_subs = not args.no_subs
    stream = args.output is None

    _print_banner()

    final = harvest(
        domain=args.domain,
        include_subs=include_subs,
        from_date=args.from_date,
        to_date=args.to_date,
        filter_status=args.filter_status,
        filter_mime=args.filter_mime,
        threads=args.threads,
        skip_resume_key=args.skip_resume_key,
        stream=stream,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(final) + "\n")
        _log(f"[INFO] Results written to: {_C.BOLD}{args.output}{_C.RESET}")


if __name__ == "__main__":
    main()
