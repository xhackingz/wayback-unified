#!/usr/bin/env python3
"""
wayback-unified — A high-coverage Wayback Machine URL harvester.

Combines techniques from:
  - waymore   (xnl-h4ck3r)     : pagination, filters, collapse, matchType, resumeKey
  - waybackurls (tomnomnom)     : wildcard subdomains, urlkey collapse, JSON output
  - waybackpy  (akamhy)         : resumeKey pagination, CDX field selection, date ranges

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
MAX_RETRIES = 3
RETRY_DELAY = 5

VERSION = "1.3.0"

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
    tag  = f"{_C.DIM}Maximum Wayback Machine URL harvester{_C.RESET}"
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
# HTTP helpers
# ---------------------------------------------------------------------------

def make_request(url: str, retries: int = MAX_RETRIES) -> str:
    """
    Perform an HTTP GET request with retry/back-off logic.
    Returns the response body as text, or empty string on failure.
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
            with urllib.request.urlopen(req, timeout=60) as resp:
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
# CDX query builders
# ---------------------------------------------------------------------------

def _cdx_params_for_target(domain: str, include_subs: bool) -> dict:
    """
    Build correct CDX `url` + `matchType` parameters.
    Uses matchType=prefix for subdomain targets to avoid SURT key leakage.
    """
    is_sub = is_subdomain_target(domain)

    if not is_sub and include_subs:
        return {"url": f"*.{domain}/*", "matchType": "domain"}
    elif not is_sub and not include_subs:
        return {"url": f"{domain}/*", "matchType": "prefix"}
    elif is_sub and include_subs:
        return {"url": f"*.{domain}/*", "matchType": "prefix"}
    else:
        return {"url": f"{domain}/*", "matchType": "prefix"}


# ---------------------------------------------------------------------------
# CDX pagination helpers
# ---------------------------------------------------------------------------

def get_total_pages(params: dict) -> int:
    """Query showNumPages=true to get total CDX page count (waymore technique)."""
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
    Fetch a single CDX page. Calls on_batch immediately when parsed so
    results can stream to the caller without waiting for all pages.
    """
    page_params = dict(params)
    page_params["page"] = str(page)
    url = build_cdx_url(page_params)
    response = make_request(url)
    results = parse_cdx_response(response)
    if on_batch and results:
        on_batch(results)
    return results


def fetch_with_resume_key(params: dict, limit: int = DEFAULT_LIMIT,
                          on_batch=None) -> set:
    """
    Resume-key cursor pagination (waybackpy technique).
    Calls on_batch after each cursor page for real-time streaming.
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
        response = make_request(url)

        if not response or response.isspace():
            break

        lines = response.strip().splitlines()
        more = False

        if len(lines) >= 3 and len(lines[-2]) == 0:
            resume_key = lines[-1].strip()
            response = "\n".join(lines[:-2])
            more = True

        page_results = parse_cdx_response(response)
        results.update(page_results)
        page_num += 1
        _log(
            f"[INFO] Resume-key page {page_num}: "
            f"{len(page_results)} URLs (running total: {len(results)})"
        )

        if on_batch and page_results:
            on_batch(page_results)

        if not more:
            break

    return results


# ---------------------------------------------------------------------------
# CDX response parser
# ---------------------------------------------------------------------------

def parse_cdx_response(response: str) -> set:
    """Parse CDX API response into a set of original URLs."""
    urls = set()
    if not response or not response.strip():
        return urls

    stripped = response.strip()

    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
            for row in data:
                if isinstance(row, list) and len(row) >= 3:
                    candidate = row[2]
                    if candidate and candidate != "original" and candidate.startswith("http"):
                        urls.add(normalize_url(candidate))
                elif isinstance(row, str) and row.startswith("http"):
                    urls.add(normalize_url(row))
            return urls
        except json.JSONDecodeError:
            pass

    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http"):
            urls.add(normalize_url(line))
        else:
            for part in line.split(" "):
                if part.startswith("http"):
                    urls.add(normalize_url(part))
                    break

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
# Three fetch methods
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
    """Method 1 — Page-based concurrent pagination (waymore technique)."""
    base = _cdx_params_for_target(domain, include_subs)
    params = {
        **base,
        "output": "text",
        "fl": "original",
        "collapse": "urlkey",
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
            spinner.update("Fetching single page...")
        url = build_cdx_url(params)
        page_results = parse_cdx_response(make_request(url))
        results.update(page_results)
        if on_batch and page_results:
            on_batch(page_results)
        _log(f"[INFO] Page-based (single request): {len(results)} raw URLs")
        return results

    if spinner:
        spinner.update(f"Fetching {total_pages} pages concurrently...")

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
    """Method 2 — Resume-key cursor pagination (waybackpy technique)."""
    base = _cdx_params_for_target(domain, include_subs)
    params = {
        **base,
        "output": "text",
        "fl": "original",
        "collapse": "urlkey",
        "gzip": "false",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if filters:
        for i, f in enumerate(filters):
            params[f"filter{i}"] = f

    _log("[INFO] CDX resume-key pagination starting...")
    if spinner:
        spinner.update("Resume-key pagination running...")
    return fetch_with_resume_key(params, on_batch=on_batch)


def get_wayback_urls_exact(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
    on_batch=None,
    spinner: Spinner = None,
) -> set:
    """Method 3 — Exact prefix query with JSON output (waybackurls technique)."""
    params = {
        "url": f"{domain}/*",
        "output": "json",
        "collapse": "urlkey",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date

    _log("[INFO] CDX exact prefix query (waybackurls technique)...")
    if spinner:
        spinner.update("Running exact prefix query...")
    response = make_request(build_cdx_url(params))

    results = set()
    if not response or not response.strip():
        return results

    try:
        data = json.loads(response.strip())
        skip_first = True
        for row in data:
            if skip_first:
                skip_first = False
                continue
            if isinstance(row, list) and len(row) >= 3:
                candidate = row[2]
                if candidate and candidate.startswith("http"):
                    results.add(normalize_url(candidate))
    except (json.JSONDecodeError, TypeError):
        results.update(parse_cdx_response(response))

    _log(f"[INFO] Exact prefix query: {len(results)} raw URLs")
    if on_batch and results:
        on_batch(results)
    return results


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
    Harvest archived URLs using all three CDX methods with streaming output.

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
    _log(f"[INFO] Is subdomain  : {is_sub}")
    _log(f"[INFO] Include subs  : {include_subs}")
    if from_date:
        _log(f"[INFO] Date from     : {from_date}")
    if to_date:
        _log(f"[INFO] Date to       : {to_date}")
    if filters:
        _log(f"[INFO] CDX filters   : {filters}")
    _log(f"[INFO] Threads       : {threads}")
    if stream:
        _log(f"[INFO] Mode          : {_C.CYAN}streaming{_C.RESET} (results print as found)")
    _log("")

    spinner = Spinner()
    spinner.start("Initializing...")

    # --- Method 1 -----------------------------------------------------------
    _log(f"[METHOD 1/3] Page-based concurrent pagination (waymore technique)...")
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
        _log("[METHOD 2/3] Resume-key cursor pagination (waybackpy technique)...")
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
    _log("[METHOD 3/3] Exact prefix query (waybackurls technique)...")
    before = len(accepted_urls)
    try:
        exact_urls = get_wayback_urls_exact(
            domain,
            include_subs=include_subs,
            from_date=from_date,
            to_date=to_date,
            on_batch=on_batch,
            spinner=spinner,
        )
        new_count = len(accepted_urls) - before
        _log(
            f"[METHOD 3/3] Raw results: {len(exact_urls)} URLs "
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
        description="wayback-unified — Maximum coverage Wayback Machine URL harvester",
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
