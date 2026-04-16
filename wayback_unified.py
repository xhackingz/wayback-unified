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

VERSION = "1.2.0"

# Lock so concurrent page threads don't interleave their stderr log lines
_log_lock = threading.Lock()


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

    A root domain has exactly one dot separating label from public suffix.
    We use a simple heuristic: split by '.' and count labels.
    Two labels  -> root domain   (example.com)
    Three+      -> subdomain     (api.example.com, sub.api.example.com)
    """
    parts = domain.lower().strip().split(".")
    return len(parts) > 2


def filter_by_scope(urls: set, target: str, include_subs: bool) -> set:
    """
    Strict hostname-level post-filter — applied after every CDX fetch to
    guarantee that CDX scope leakage (SURT key expansion) never pollutes output.

    Rules:
      include_subs=True  -> keep URLs where hostname == target
                            OR hostname ends with ".<target>"
                            (i.e. direct subdomains of target only)
      include_subs=False -> keep URLs where hostname == target exactly

    This means:
      target=api.example.com, include_subs=True  -> api.example.com + *.api.example.com
      target=api.example.com, include_subs=False -> api.example.com only
      target=example.com,     include_subs=True  -> example.com + *.example.com
      target=example.com,     include_subs=False -> example.com only
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
# CDX query builders — correct matchType per target type
# ---------------------------------------------------------------------------

def _cdx_params_for_target(domain: str, include_subs: bool) -> dict:
    """
    Build the correct CDX `url` + `matchType` parameters for a given target.

    The CDX API's `matchType=domain` is designed for root domains and can
    produce SURT key collisions when applied to subdomains, causing scope
    leakage back to the parent domain.

    Safe strategy:
      - Root domain  + include_subs  -> url=*.domain/*,       matchType=domain
      - Root domain  + no subs       -> url=domain/*,         matchType=prefix
      - Subdomain    + include_subs  -> url=*.subdomain/*,    matchType=prefix
                                        (no matchType=domain — avoids leakage)
      - Subdomain    + no subs       -> url=subdomain/*,      matchType=prefix

    The post-filter in filter_by_scope() is the final guarantee regardless.
    """
    is_sub = is_subdomain_target(domain)

    if not is_sub and include_subs:
        return {
            "url": f"*.{domain}/*",
            "matchType": "domain",
        }
    elif not is_sub and not include_subs:
        return {
            "url": f"{domain}/*",
            "matchType": "prefix",
        }
    elif is_sub and include_subs:
        return {
            "url": f"*.{domain}/*",
            "matchType": "prefix",
        }
    else:  # is_sub and not include_subs
        return {
            "url": f"{domain}/*",
            "matchType": "prefix",
        }


# ---------------------------------------------------------------------------
# CDX pagination helpers
# ---------------------------------------------------------------------------

def get_total_pages(params: dict) -> int:
    """
    Technique from waymore: query showNumPages=true to get total page count
    before spawning concurrent page fetches.
    """
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
    Fetch a single CDX page and return a set of URLs.
    If on_batch is provided, call it immediately with the parsed results
    so the caller can stream URLs as soon as this page finishes.
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
    Technique from waybackpy: use showResumeKey + resumeKey cursor for
    paginated fetching. More reliable than page-based for very large sets.
    on_batch is called after each cursor page so results stream immediately.
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
    """
    Parse a CDX API response into a set of original URLs.
    Handles both plain text (one URL per line) and JSON array formats.
    """
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
    """
    Normalize a URL: remove default ports (80/443), clean whitespace.
    Technique from waymore's linksFoundAdd().
    """
    try:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc
        if parsed.port in (80, 443):
            netloc = parsed.hostname or netloc
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Three fetch methods (waymore / waybackurls / waybackpy techniques)
# ---------------------------------------------------------------------------

def get_wayback_urls_pagination(
    domain: str,
    include_subs: bool = True,
    filters: list = None,
    from_date: str = None,
    to_date: str = None,
    threads: int = DEFAULT_THREADS,
    on_batch=None,
) -> set:
    """
    Method 1 — Page-based concurrent pagination (waymore technique).
    Fetches total page count first, then requests all pages in parallel.
    on_batch is called from each worker thread as pages complete, enabling
    real-time streaming without waiting for the full method to finish.
    """
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

    total_pages = get_total_pages(params)
    _log(f"[INFO] CDX page-based: {total_pages} page(s) to fetch")

    results = set()

    if total_pages == 1:
        url = build_cdx_url(params)
        page_results = parse_cdx_response(make_request(url))
        results.update(page_results)
        if on_batch and page_results:
            on_batch(page_results)
        _log(f"[INFO] Page-based (single request): {len(results)} raw URLs")
        return results

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
) -> set:
    """
    Method 2 — Resume-key cursor pagination (waybackpy technique).
    Catches entries that page-based pagination may miss on large datasets.
    on_batch is called after each cursor page for real-time streaming.
    """
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
    return fetch_with_resume_key(params, on_batch=on_batch)


def get_wayback_urls_exact(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
    on_batch=None,
) -> set:
    """
    Method 3 — Exact domain prefix query with JSON output (waybackurls technique).
    Uses collapse=urlkey and JSON parsing for additional coverage.
    on_batch is called once when the response is parsed.
    """
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


def _log(msg: str) -> None:
    """Write log messages to stderr — thread-safe, clean line output."""
    with _log_lock:
        print(msg, file=sys.stderr, flush=True)


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
    Harvest archived URLs using all three complementary CDX methods, then
    apply strict scope enforcement and deduplicate.

    When stream=True, URLs are printed to stdout as they are discovered —
    per page, per resume-key batch, and per method — rather than buffered
    until the end.  Results are still deduplicated globally so no URL
    appears twice in the output regardless of which method found it.

    Strategy:
      1. Page-based concurrent pagination          (waymore)
      2. Resume-key cursor pagination              (waybackpy)
      3. Exact prefix query with JSON output       (waybackurls)
      4. Inline scope filter + dedup on every batch (streaming)
      5. Final sorted list returned (for -o file output)
    """
    filters = build_filters(filter_status, filter_mime)
    is_sub = is_subdomain_target(domain)

    # Shared state across all methods and threads
    accepted_urls: set = set()       # in-scope, deduplicated
    emit_lock = threading.Lock()     # protects accepted_urls + stdout writes
    scope_filtered = [0]             # count of out-of-scope URLs removed

    def on_batch(raw_urls: set) -> None:
        """
        Called by every fetch method/page/batch as soon as results arrive.
        1. Applies scope filter inline (no CDX leakage reaches output).
        2. Deduplicates globally across all methods and threads.
        3. In stream mode: prints each new URL to stdout immediately.
        Thread-safe via emit_lock.
        """
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

    _log(f"\n[wayback-unified v{VERSION}]")
    _log(f"[INFO] Target        : {domain}")
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
        _log(f"[INFO] Mode          : streaming (results print as found)")
    _log("")

    # --- Method 1 -----------------------------------------------------------
    _log("[METHOD 1/3] Page-based concurrent pagination (waymore technique)...")
    try:
        page_urls = get_wayback_urls_pagination(
            domain,
            include_subs=include_subs,
            filters=filters,
            from_date=from_date,
            to_date=to_date,
            threads=threads,
            on_batch=on_batch,
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
        )
        new_count = len(accepted_urls) - before
        _log(
            f"[METHOD 3/3] Raw results: {len(exact_urls)} URLs "
            f"({new_count} new unique)\n"
        )
    except Exception as e:
        _log(f"[WARN] Method 3 failed: {e}\n")

    if scope_filtered[0] > 0:
        _log(
            f"[SCOPE] Removed {scope_filtered[0]} out-of-scope URLs "
            f"(CDX scope leakage corrected)"
        )

    final = sorted(accepted_urls)
    _log(f"[DONE] Total unique in-scope URLs: {len(final)}")
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

    # Stream to stdout in real-time when no output file is specified.
    # When -o is used, collect everything first then write sorted to the file.
    stream = args.output is None

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
        _log(f"[INFO] Results written to: {args.output}")


if __name__ == "__main__":
    main()
