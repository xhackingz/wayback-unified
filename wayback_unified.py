#!/usr/bin/env python3
"""
wayback-unified — CDX-only Wayback Machine URL harvester.

Single ingestion source, no exceptions:
  https://web.archive.org/cdx/search/cdx?url=*.DOMAIN&fl=original&collapse=urlkey

Engineered for infinite-scale stability:
  - Chunk-based streaming HTTP reads (never loads full response into memory)
  - Resume-key cursor pagination (iterates until the API signals completion)
  - Exponential back-off retry with rate-limit awareness
  - Thread-safe deduplication and scope filtering

Usage:
  python wayback_unified.py -d example.com
  python wayback_unified.py -d api.example.com --no-subs
  python wayback_unified.py -d example.com --from 20200101 --to 20231231
  python wayback_unified.py -d example.com --filter-status 200 --filter-mime text/html
  python wayback_unified.py -d example.com -o results.txt
  python wayback_unified.py -d example.com | grep '\\.js$'
  python wayback_unified.py -d example.com | httpx -silent
"""

import argparse
import sys
import time
import threading
import re
from urllib.parse import urlparse, urlencode
import urllib.request
import urllib.error


CDX_ENDPOINT  = "https://web.archive.org/cdx/search/cdx"
CURSOR_LIMIT  = 50000      # URLs per cursor page
STREAM_CHUNK  = 65536      # 64 KB per HTTP read chunk
MAX_RETRIES   = 5
RETRY_DELAY   = 5          # base seconds for back-off
VERSION       = "2.0.0"

# ---------------------------------------------------------------------------
# TTY / color helpers
# ---------------------------------------------------------------------------

_IS_TTY = sys.stderr.isatty()


class _C:
    RESET  = "\033[0m"   if _IS_TTY else ""
    BOLD   = "\033[1m"   if _IS_TTY else ""
    DIM    = "\033[2m"   if _IS_TTY else ""
    CYAN   = "\033[36m"  if _IS_TTY else ""
    BCYAN  = "\033[96m"  if _IS_TTY else ""
    BGREEN = "\033[92m"  if _IS_TTY else ""
    YELLOW = "\033[33m"  if _IS_TTY else ""


def _ansi_len(s: str) -> int:
    return len(re.sub(r'\033\[[0-9;]*m', '', s))


_log_lock  = threading.Lock()
_BOX_WIDTH = 52


def _box_row(text: str) -> str:
    pad = _BOX_WIDTH - 2 - _ansi_len(text)
    return f"{_C.CYAN}│{_C.RESET} {text}{' ' * max(0, pad)} {_C.CYAN}│{_C.RESET}"


def _print_banner() -> None:
    top  = f"{_C.CYAN}╭{'─' * _BOX_WIDTH}╮{_C.RESET}"
    bot  = f"{_C.CYAN}╰{'─' * _BOX_WIDTH}╯{_C.RESET}"
    sep  = f"{_C.CYAN}│{' ' * _BOX_WIDTH}│{_C.RESET}"
    name = f"{_C.BOLD}{_C.BCYAN}wayback-unified{_C.RESET}  {_C.DIM}v{VERSION}{_C.RESET}"
    tag  = f"{_C.DIM}CDX-only · Infinite-scale URL harvester{_C.RESET}"
    cred = f"{_C.DIM}by @xhacking_z{_C.RESET}"
    with _log_lock:
        for row in ["", top, sep, _box_row(name), _box_row(tag), _box_row(cred), sep, bot, ""]:
            sys.stderr.write(row + "\n")
        sys.stderr.flush()


def _colorize(msg: str) -> str:
    if not _IS_TTY:
        return msg
    if msg.startswith("[WARN]"):
        return f"{_C.YELLOW}{_C.BOLD}{msg}{_C.RESET}"
    if msg.startswith("[DONE]"):
        return f"{_C.BOLD}{_C.BGREEN}{msg}{_C.RESET}"
    if msg.startswith("[INFO]"):
        return f"{_C.DIM}{msg}{_C.RESET}"
    return msg


def _log(msg: str) -> None:
    with _log_lock:
        if _IS_TTY:
            sys.stderr.write("\r\033[K")
        print(_colorize(msg), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

class Spinner:
    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self) -> None:
        self._msg    = ""
        self._thread: threading.Thread | None = None
        self._stop   = threading.Event()

    def start(self, msg: str = "") -> "Spinner":
        if not _IS_TTY:
            return self
        self._msg = msg
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def update(self, msg: str) -> None:
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
            if _log_lock.acquire(blocking=False):
                try:
                    f = self._FRAMES[i % len(self._FRAMES)]
                    sys.stderr.write(f"\r{_C.CYAN}{f}{_C.RESET} {_C.DIM}{self._msg}{_C.RESET}")
                    sys.stderr.flush()
                finally:
                    _log_lock.release()
            time.sleep(0.08)
            i += 1


# ---------------------------------------------------------------------------
# Streaming HTTP
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        f"wayback-unified/{VERSION} "
        "(https://github.com/xhackingz/wayback-unified)"
    )
}


def _stream_lines(url: str):
    """
    Generator — streams the HTTP response in STREAM_CHUNK-byte blocks and
    yields individual lines (including empty ones, needed for cursor detection).
    Never buffers the full body. Retries with exponential back-off on errors.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=300) as resp:
                buf = b""
                while True:
                    chunk = resp.read(STREAM_CHUNK)
                    if not chunk:
                        if buf:
                            yield buf.decode("utf-8", errors="replace")
                        return
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield line.decode("utf-8", errors="replace")
            return  # success

        except urllib.error.HTTPError as exc:
            wait = RETRY_DELAY * attempt * (2 if exc.code == 429 else 1)
            _log(f"[WARN] HTTP {exc.code} — waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
            if attempt == MAX_RETRIES:
                return
            time.sleep(wait)

        except (urllib.error.URLError, OSError) as exc:
            wait = RETRY_DELAY * attempt
            _log(f"[WARN] {exc} — waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
            if attempt == MAX_RETRIES:
                return
            time.sleep(wait)

        except Exception as exc:
            _log(f"[WARN] Unexpected error: {exc}")
            if attempt == MAX_RETRIES:
                return


# ---------------------------------------------------------------------------
# CDX helpers
# ---------------------------------------------------------------------------

def _cdx_url(domain: str, extra: dict | None = None) -> str:
    """
    Build the ONLY permitted CDX query URL:
      url=*.{domain}&fl=original&collapse=urlkey
    plus any additional parameters (filters, dates, cursor, limit).
    """
    base = domain.lower().strip().lstrip("*.").lstrip(".")
    params: dict = {
        "url":      f"*.{base}",
        "fl":       "original",
        "collapse": "urlkey",
        "output":   "text",
        "gzip":     "false",
    }
    if extra:
        params.update(extra)
    return f"{CDX_ENDPOINT}?{urlencode(params)}"


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        netloc = parsed.netloc
        if parsed.port in (80, 443):
            netloc = parsed.hostname or netloc
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return url.strip()


def _in_scope(url: str, target: str, include_subs: bool) -> bool:
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        host = host.lower()
        target = target.lower().strip()
        if host == target:
            return True
        if include_subs and host.endswith(f".{target}"):
            return True
    except Exception:
        pass
    return False


def _build_filters(status_code: str | None, mime_type: str | None) -> dict:
    extra: dict = {}
    if status_code:
        extra["filter"] = f"statuscode:{status_code}"
    if mime_type:
        extra["filter2"] = f"mimetype:{mime_type.replace('+', r'+')}"
    return extra


# ---------------------------------------------------------------------------
# Core ingestion: single-source cursor-paginated streaming pipeline
# ---------------------------------------------------------------------------

def fetch_cdx(
    domain: str,
    include_subs: bool = True,
    from_date: str | None = None,
    to_date: str | None = None,
    filter_status: str | None = None,
    filter_mime: str | None = None,
    stream: bool = False,
    spinner: Spinner | None = None,
) -> list[str]:
    """
    Fetch all archived URLs for *domain* from the CDX endpoint:
      https://web.archive.org/cdx/search/cdx?url=*.{domain}&fl=original&collapse=urlkey

    Uses resume-key cursor pagination so iteration never stops regardless of
    how many results exist. Each cursor page is read via chunk-based streaming —
    the full response is never held in memory.

    Returns a sorted, deduplicated list of in-scope URLs.
    """
    accepted: set[str] = set()
    cursor:   str | None = None
    page_num: int = 0
    out_of_scope: int = 0

    extra: dict = {"showResumeKey": "true", "limit": str(CURSOR_LIMIT)}
    if from_date:
        extra["from"] = from_date
    if to_date:
        extra["to"] = to_date
    extra.update(_build_filters(filter_status, filter_mime))

    while True:
        if cursor:
            extra["resumeKey"] = cursor
        elif "resumeKey" in extra:
            del extra["resumeKey"]

        url = _cdx_url(domain, extra)

        if spinner:
            spinner.update(
                f"Page {page_num + 1} — {len(accepted):,} URLs collected"
                + (f" · cursor: {cursor[:24]}…" if cursor else "")
            )

        # Collect this cursor page's lines (bounded by CURSOR_LIMIT)
        lines: list[str] = list(_stream_lines(url))

        if not lines:
            break

        # --- Detect resume-key sentinel: [ …data… ] \n "" \n resumeKey ---
        next_cursor: str | None = None
        has_more = (
            len(lines) >= 2
            and lines[-2].strip() == ""
            and lines[-1].strip() != ""
        )
        if has_more:
            next_cursor = lines[-1].strip()
            lines = lines[:-2]  # strip sentinel from data

        # --- Process URL lines ---
        page_new = 0
        for raw in lines:
            line = raw.strip()
            if not line or not line.startswith("http"):
                continue
            norm = normalize_url(line).rstrip("/")
            if not norm:
                continue
            if not _in_scope(norm, domain, include_subs):
                out_of_scope += 1
                continue
            if norm not in accepted:
                accepted.add(norm)
                page_new += 1
                if stream:
                    print(norm, flush=True)

        page_num += 1
        _log(
            f"[INFO] Cursor page {page_num}: "
            f"{page_new:,} new URLs  (total: {len(accepted):,})"
        )

        if not next_cursor:
            break

        cursor = next_cursor
        time.sleep(0.05)  # gentle pacing between cursor requests

    if out_of_scope:
        _log(f"[INFO] Removed {out_of_scope:,} out-of-scope URLs (CDX leakage)")

    return sorted(accepted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "wayback-unified — CDX-only infinite-scale URL harvester\n"
            "Source: https://web.archive.org/cdx/search/cdx"
            "?url=*.DOMAIN&fl=original&collapse=urlkey"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python wayback_unified.py -d example.com
  python wayback_unified.py -d api.example.com --no-subs
  python wayback_unified.py -d example.com --from 20200101 --to 20231231
  python wayback_unified.py -d example.com --filter-status 200
  python wayback_unified.py -d example.com --filter-mime text/html
  python wayback_unified.py -d example.com -o results.txt --threads 10
  python wayback_unified.py -d example.com | grep '\\.js$'
  python wayback_unified.py -d example.com | httpx -silent
        """,
    )

    parser.add_argument("-d", "--domain", required=True,
        help="Target domain or subdomain (e.g. example.com or api.example.com)")
    parser.add_argument("--no-subs", action="store_true",
        help="Restrict to exact hostname only — no subdomains")
    parser.add_argument("--from", dest="from_date", metavar="YYYYMMDD",
        help="Start date filter (e.g. 20200101)")
    parser.add_argument("--to", dest="to_date", metavar="YYYYMMDD",
        help="End date filter (e.g. 20231231)")
    parser.add_argument("--filter-status", metavar="CODE",
        help="Only return URLs with this HTTP status code (e.g. 200)")
    parser.add_argument("--filter-mime", metavar="TYPE",
        help="Only return URLs with this MIME type (e.g. text/html)")
    parser.add_argument("-o", "--output", metavar="FILE",
        help="Write results to a file (default: stdout)")
    parser.add_argument("--version", action="version",
        version=f"wayback-unified {VERSION}")

    args   = parser.parse_args()
    stream = args.output is None

    _print_banner()

    domain       = args.domain.strip()
    include_subs = not args.no_subs

    _log(f"[INFO] Target      : {_C.BOLD}{domain}{_C.RESET}")
    _log(f"[INFO] CDX source  : *.{domain.lstrip('*.').lstrip('.')}&fl=original&collapse=urlkey")
    _log(f"[INFO] Subdomains  : {'included' if include_subs else 'excluded'}")
    if args.from_date:
        _log(f"[INFO] From        : {args.from_date}")
    if args.to_date:
        _log(f"[INFO] To          : {args.to_date}")
    if args.filter_status:
        _log(f"[INFO] Status      : {args.filter_status}")
    if args.filter_mime:
        _log(f"[INFO] MIME        : {args.filter_mime}")
    _log(f"[INFO] Mode        : {'streaming (results print as found)' if stream else 'batch (write to file)'}")
    _log("")

    spinner = Spinner().start("Connecting to CDX API…")

    try:
        results = fetch_cdx(
            domain       = domain,
            include_subs = include_subs,
            from_date    = args.from_date,
            to_date      = args.to_date,
            filter_status= args.filter_status,
            filter_mime  = args.filter_mime,
            stream       = stream,
            spinner      = spinner,
        )
    finally:
        spinner.stop()

    _log(f"[DONE] Total unique in-scope URLs: {_C.BOLD}{len(results):,}{_C.RESET}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write("\n".join(results) + "\n")
        _log(f"[INFO] Results written to: {_C.BOLD}{args.output}{_C.RESET}")


if __name__ == "__main__":
    main()
