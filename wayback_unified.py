#!/usr/bin/env python3
"""
wayback-unified — A high-coverage Wayback Machine URL harvester.

Combines techniques from:
  - waymore   (xnl-h4ck3r)     : pagination, filters, collapse, matchType, resumeKey
  - waybackurls (tomnomnom)     : wildcard subdomains, urlkey collapse, versions
  - waybackpy  (akamhy)         : resumeKey pagination, CDX field selection, date ranges

Usage:
  python wayback_unified.py -d example.com
  python wayback_unified.py -d example.com --subs --threads 10
  python wayback_unified.py -d example.com --from 20200101 --to 20231231
  python wayback_unified.py -d example.com --filter-status 200 --filter-mime text/html
  python wayback_unified.py -d example.com -o results.txt
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import urllib.request
import urllib.error
import json
import re


CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
DEFAULT_LIMIT = 50000
DEFAULT_THREADS = 5
MAX_RETRIES = 3
RETRY_DELAY = 5

VERSION = "1.0.0"


def make_request(url: str, retries: int = MAX_RETRIES) -> str:
    """
    Perform an HTTP GET request with retry logic.
    Returns the response body as text, or empty string on failure.
    """
    headers = {
        "User-Agent": f"wayback-unified/{VERSION} (https://github.com/xhackingz/wayback-unified)"
    }
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_DELAY * attempt * 2
                _log(f"[WARN] Rate limited (429). Waiting {wait}s before retry {attempt}/{retries}...")
                time.sleep(wait)
            elif e.code == 503:
                wait = RETRY_DELAY * attempt
                _log(f"[WARN] Service unavailable (503). Waiting {wait}s before retry {attempt}/{retries}...")
                time.sleep(wait)
            else:
                _log(f"[WARN] HTTP {e.code} for URL: {url}")
                if attempt == retries:
                    return ""
        except urllib.error.URLError as e:
            wait = RETRY_DELAY * attempt
            _log(f"[WARN] URL error: {e.reason}. Waiting {wait}s before retry {attempt}/{retries}...")
            time.sleep(wait)
        except Exception as e:
            _log(f"[WARN] Request failed: {e}")
            if attempt == retries:
                return ""
    return ""


def build_cdx_url(target: str, params: dict) -> str:
    """Build a CDX API URL with the given parameters."""
    from urllib.parse import urlencode
    query = urlencode(params)
    return f"{CDX_ENDPOINT}?{query}"


def get_total_pages(target: str, params: dict) -> int:
    """
    Technique from waymore: query showNumPages=true to get total page count
    before spawning concurrent page fetches.
    """
    check_params = dict(params)
    check_params["showNumPages"] = "true"
    check_params.pop("page", None)
    check_params.pop("output", None)

    url = build_cdx_url(target, check_params)
    _log(f"[INFO] Fetching page count from CDX API...")
    response = make_request(url)

    try:
        pages = int(response.strip())
        return max(1, pages)
    except (ValueError, AttributeError):
        return 1


def fetch_page(target: str, params: dict, page: int) -> set:
    """
    Fetch a single page from the CDX API.
    Returns a set of URLs found on that page.
    """
    page_params = dict(params)
    page_params["page"] = str(page)
    url = build_cdx_url(target, page_params)
    response = make_request(url)
    return parse_cdx_response(response)


def fetch_with_resume_key(target: str, params: dict, limit: int = DEFAULT_LIMIT) -> set:
    """
    Technique from waybackpy: use showResumeKey + resumeKey for paginated fetching.
    More reliable than page-based for very large result sets.
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

        url = build_cdx_url(target, fetch_params)
        response = make_request(url)

        if not response or response.isspace():
            break

        lines = response.strip().splitlines()
        more = False

        if len(lines) >= 3:
            second_last = lines[-2]
            if len(second_last) == 0:
                resume_key = lines[-1].strip()
                response = "\n".join(lines[:-2])
                more = True

        page_results = parse_cdx_response(response)
        results.update(page_results)
        page_num += 1
        _log(f"[INFO] Resume-key page {page_num}: {len(page_results)} URLs (total: {len(results)})")

        if not more:
            break

    return results


def parse_cdx_response(response: str) -> set:
    """
    Parse CDX API response text into a set of original URLs.
    Handles both plain text (one URL per line) and JSON formats.
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
                    url = row[2]
                    if url and url != "original" and url.startswith("http"):
                        urls.add(normalize_url(url))
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
            parts = line.split(" ")
            for part in parts:
                if part.startswith("http"):
                    urls.add(normalize_url(part))
                    break

    return urls


def normalize_url(url: str) -> str:
    """
    Normalize a URL: remove default ports (80, 443), strip trailing slash variations.
    Technique from waymore's linksFoundAdd().
    """
    try:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc
        if parsed.port == 80 or parsed.port == 443:
            netloc = parsed.hostname or netloc
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return url


def is_subdomain(url: str, domain: str) -> bool:
    """
    Check if a URL belongs to a subdomain (not the root domain).
    From waybackurls' isSubdomain().
    """
    try:
        parsed = urlparse(url)
        return parsed.hostname.lower() != domain.lower()
    except Exception:
        return False


def get_wayback_urls_pagination(
    domain: str,
    include_subs: bool = True,
    filters: list = None,
    from_date: str = None,
    to_date: str = None,
    threads: int = DEFAULT_THREADS,
) -> set:
    """
    Main CDX fetcher using PAGE-BASED pagination (technique from waymore).
    Spawns concurrent requests for all pages.
    """
    results = set()

    target = f"*.{domain}/*" if include_subs else f"{domain}/*"

    params = {
        "url": target,
        "output": "text",
        "fl": "original",
        "collapse": "urlkey",
        "matchType": "domain" if include_subs else "prefix",
    }

    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    if filters:
        for i, f in enumerate(filters):
            params[f"filter{i}"] = f

    total_pages = get_total_pages(domain, params)
    _log(f"[INFO] CDX page-based: {total_pages} page(s) to fetch")

    if total_pages == 1:
        url = build_cdx_url(domain, params)
        response = make_request(url)
        results.update(parse_cdx_response(response))
        _log(f"[INFO] Page-based single request: {len(results)} URLs")
        return results

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(fetch_page, domain, params, page): page
            for page in range(total_pages)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_urls = future.result()
                results.update(page_urls)
                _log(f"[INFO] Page {page + 1}/{total_pages}: {len(page_urls)} URLs (total: {len(results)})")
            except Exception as e:
                _log(f"[WARN] Page {page} failed: {e}")

    return results


def get_wayback_urls_resumekey(
    domain: str,
    include_subs: bool = True,
    filters: list = None,
    from_date: str = None,
    to_date: str = None,
) -> set:
    """
    Secondary CDX fetcher using RESUME KEY pagination (technique from waybackpy).
    Catches any results missed by page-based pagination.
    """
    target = f"*.{domain}/*" if include_subs else f"{domain}/*"

    params = {
        "url": target,
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

    _log(f"[INFO] CDX resume-key pagination starting...")
    return fetch_with_resume_key(domain, params)


def get_wayback_urls_exact(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
) -> set:
    """
    Exact CDX query without subdomain wildcard (technique from waybackurls).
    Uses collapse=urlkey and JSON output for robust parsing.
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

    url = build_cdx_url(domain, params)
    _log(f"[INFO] CDX exact domain query (no wildcard)...")
    response = make_request(url)

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
                url_val = row[2]
                if url_val and url_val.startswith("http"):
                    results.add(normalize_url(url_val))
    except (json.JSONDecodeError, TypeError):
        results.update(parse_cdx_response(response))

    _log(f"[INFO] Exact query: {len(results)} URLs")
    return results


def get_wayback_versions(url_input: str) -> set:
    """
    Get all archived versions of a specific URL (technique from waybackurls --get-versions).
    Deduplicates by content digest (same content = same version).
    """
    params = {
        "url": url_input,
        "output": "json",
    }

    api_url = build_cdx_url(url_input, params)
    _log(f"[INFO] Fetching all archived versions of: {url_input}")
    response = make_request(api_url)

    versions = set()
    if not response or not response.strip():
        return versions

    try:
        data = json.loads(response.strip())
        seen_digests = set()
        skip_first = True
        for row in data:
            if skip_first:
                skip_first = False
                continue
            if isinstance(row, list) and len(row) >= 7:
                timestamp = row[1]
                original = row[2]
                digest = row[5]
                if digest not in seen_digests:
                    seen_digests.add(digest)
                    versions.add(f"https://web.archive.org/web/{timestamp}if_/{original}")
    except (json.JSONDecodeError, TypeError):
        pass

    return versions


def build_filters(status_code: str = None, mime_type: str = None) -> list:
    """Build CDX filter strings from user arguments."""
    filters = []
    if status_code:
        filters.append(f"statuscode:{status_code}")
    if mime_type:
        safe_mime = mime_type.replace("+", r"\+")
        filters.append(f"mimetype:{safe_mime}")
    return filters


def _log(msg: str) -> None:
    """Print a log message to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr)


def deduplicate(urls: set) -> list:
    """
    Final deduplication pass: normalize and sort all collected URLs.
    Removes duplicates that differ only in trailing slashes or port numbers.
    """
    normalized = set()
    for url in urls:
        clean = url.rstrip("/")
        normalized.add(clean)
    return sorted(normalized)


def harvest(
    domain: str,
    include_subs: bool = True,
    from_date: str = None,
    to_date: str = None,
    filter_status: str = None,
    filter_mime: str = None,
    threads: int = DEFAULT_THREADS,
    skip_resume_key: bool = False,
) -> list:
    """
    Main harvest function combining all three techniques for maximum coverage.

    Strategy:
    1. Page-based pagination with wildcard + matchType=domain (waymore)
    2. Resume-key pagination (waybackpy) — catches any missed entries
    3. Exact domain query without wildcard (waybackurls) — additional coverage
    4. Final in-memory deduplication
    """
    all_urls = set()
    filters = build_filters(filter_status, filter_mime)

    _log(f"\n[wayback-unified v{VERSION}]")
    _log(f"[INFO] Target: {domain}")
    _log(f"[INFO] Include subdomains: {include_subs}")
    if from_date:
        _log(f"[INFO] Date from: {from_date}")
    if to_date:
        _log(f"[INFO] Date to: {to_date}")
    if filters:
        _log(f"[INFO] Filters: {filters}")
    _log(f"[INFO] Threads: {threads}")
    _log("")

    _log("[METHOD 1/3] Page-based pagination (waymore technique)...")
    try:
        page_urls = get_wayback_urls_pagination(
            domain,
            include_subs=include_subs,
            filters=filters,
            from_date=from_date,
            to_date=to_date,
            threads=threads,
        )
        all_urls.update(page_urls)
        _log(f"[METHOD 1/3] Found: {len(page_urls)} URLs\n")
    except Exception as e:
        _log(f"[WARN] Method 1 failed: {e}\n")

    if not skip_resume_key:
        _log("[METHOD 2/3] Resume-key pagination (waybackpy technique)...")
        try:
            resume_urls = get_wayback_urls_resumekey(
                domain,
                include_subs=include_subs,
                filters=filters,
                from_date=from_date,
                to_date=to_date,
            )
            new_urls = resume_urls - all_urls
            all_urls.update(resume_urls)
            _log(f"[METHOD 2/3] Found: {len(resume_urls)} URLs ({len(new_urls)} new)\n")
        except Exception as e:
            _log(f"[WARN] Method 2 failed: {e}\n")

    _log("[METHOD 3/3] Exact domain query (waybackurls technique)...")
    try:
        exact_urls = get_wayback_urls_exact(
            domain,
            include_subs=include_subs,
            from_date=from_date,
            to_date=to_date,
        )
        new_urls = exact_urls - all_urls
        all_urls.update(exact_urls)
        _log(f"[METHOD 3/3] Found: {len(exact_urls)} URLs ({len(new_urls)} new)\n")
    except Exception as e:
        _log(f"[WARN] Method 3 failed: {e}\n")

    if not include_subs:
        all_urls = {u for u in all_urls if not is_subdomain(u, domain)}

    final = deduplicate(all_urls)
    _log(f"[DONE] Total unique URLs after deduplication: {len(final)}")
    return final


def main():
    parser = argparse.ArgumentParser(
        description="wayback-unified — Maximum coverage Wayback Machine URL harvester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python wayback_unified.py -d example.com
  python wayback_unified.py -d example.com --subs
  python wayback_unified.py -d example.com --from 20200101 --to 20231231
  python wayback_unified.py -d example.com --filter-status 200
  python wayback_unified.py -d example.com --filter-mime text/html
  python wayback_unified.py -d example.com -o output.txt --threads 10
  python wayback_unified.py -d example.com --versions
        """,
    )

    parser.add_argument(
        "-d", "--domain",
        required=True,
        help="Target domain (e.g. example.com)"
    )
    parser.add_argument(
        "--subs",
        action="store_true",
        default=True,
        help="Include subdomains (default: True)"
    )
    parser.add_argument(
        "--no-subs",
        action="store_true",
        help="Exclude subdomains"
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        metavar="YYYYMMDD",
        help="Start date for filtering (e.g. 20200101)"
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        metavar="YYYYMMDD",
        help="End date for filtering (e.g. 20231231)"
    )
    parser.add_argument(
        "--filter-status",
        metavar="CODE",
        help="Filter by HTTP status code (e.g. 200)"
    )
    parser.add_argument(
        "--filter-mime",
        metavar="TYPE",
        help="Filter by MIME type (e.g. text/html)"
    )
    parser.add_argument(
        "-o", "--output",
        metavar="FILE",
        help="Output file (default: stdout)"
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Number of concurrent threads (default: {DEFAULT_THREADS})"
    )
    parser.add_argument(
        "--skip-resume-key",
        action="store_true",
        help="Skip the resume-key pagination method (faster but less coverage)"
    )
    parser.add_argument(
        "--versions",
        action="store_true",
        help="Fetch all archived versions of the input (use -d as the full URL)"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"wayback-unified {VERSION}"
    )

    args = parser.parse_args()

    include_subs = not args.no_subs

    if args.versions:
        versions = get_wayback_versions(args.domain)
        final = sorted(versions)
        _log(f"[DONE] Total unique versions: {len(final)}")
    else:
        final = harvest(
            domain=args.domain,
            include_subs=include_subs,
            from_date=args.from_date,
            to_date=args.to_date,
            filter_status=args.filter_status,
            filter_mime=args.filter_mime,
            threads=args.threads,
            skip_resume_key=args.skip_resume_key,
        )

    output_text = "\n".join(final)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_text + "\n")
        _log(f"[INFO] Results written to: {args.output}")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
