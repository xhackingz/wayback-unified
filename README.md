# wayback-unified

**Maximum-coverage Wayback Machine URL harvester** — a single unified tool that combines the best techniques from three leading open-source tools to extract the most possible archived URLs from the [Wayback Machine (archive.org)](https://web.archive.org/).

---

## What It Does

`wayback-unified` queries the Wayback Machine's CDX API using **three complementary strategies in parallel**, then merges and deduplicates everything into a clean, sorted list of unique URLs.

### Techniques Combined

| Source Tool | Technique Used |
|---|---|
| [waymore](https://github.com/xnl-h4ck3r/waymore) | Page-based CDX pagination with `showNumPages`, concurrent page fetching, MIME/status filters, date ranges, collapse |
| [waybackurls](https://github.com/tomnomnom/waybackurls) | Wildcard subdomain query (`*.domain/*`), `collapse=urlkey`, JSON output, archived version fetching |
| [waybackpy](https://github.com/akamhy/waybackpy) | Resume-key pagination (`showResumeKey` + `resumeKey`), field selection (`fl=original`), efficient large-set traversal |

### How It Maximizes Results

1. **Method 1 — Page-based pagination**: Queries total page count first, then fetches all pages concurrently using a thread pool.
2. **Method 2 — Resume-key pagination**: Iterates through results using the CDX API's `resumeKey` cursor, capturing anything the page method may have missed.
3. **Method 3 — Exact domain query**: A clean non-wildcard query using `collapse=urlkey` and JSON parsing for additional coverage.
4. **Final deduplication**: All results are merged into a Python `set`, normalized (port stripping, trailing slashes), and sorted.

---

## Installation

### Option 1 — Run directly (no install needed)

```bash
git clone https://github.com/xhackingz/wayback-unified
cd wayback-unified
python wayback_unified.py -d example.com
```

### Option 2 — Install as a package

```bash
pip install .
wayback-unified -d example.com
```

### Requirements

- Python 3.7+
- No external dependencies required (uses Python standard library only)

---

## Usage

```
python wayback_unified.py -d <domain> [OPTIONS]
```

### Options

| Flag | Description |
|---|---|
| `-d`, `--domain` | Target domain (required), e.g. `example.com` |
| `--subs` | Include subdomains (default: enabled) |
| `--no-subs` | Exclude subdomains, only query the root domain |
| `--from YYYYMMDD` | Start date filter, e.g. `20200101` |
| `--to YYYYMMDD` | End date filter, e.g. `20231231` |
| `--filter-status CODE` | Only return URLs with this HTTP status code |
| `--filter-mime TYPE` | Only return URLs with this MIME type |
| `-o`, `--output FILE` | Write results to a file instead of stdout |
| `-t`, `--threads N` | Number of concurrent threads (default: 5) |
| `--skip-resume-key` | Skip Method 2 — faster but slightly less coverage |
| `--versions` | Fetch all unique archived versions of a specific URL |
| `--version` | Show version and exit |

---

## Examples

**Basic usage — collect all archived URLs for a domain:**
```bash
python wayback_unified.py -d example.com
```

**Include subdomains (already on by default):**
```bash
python wayback_unified.py -d example.com --subs
```

**Only the root domain, no subdomains:**
```bash
python wayback_unified.py -d example.com --no-subs
```

**Filter by date range:**
```bash
python wayback_unified.py -d example.com --from 20200101 --to 20231231
```

**Only return successfully archived pages (HTTP 200):**
```bash
python wayback_unified.py -d example.com --filter-status 200
```

**Only return HTML pages:**
```bash
python wayback_unified.py -d example.com --filter-mime text/html
```

**Combine filters:**
```bash
python wayback_unified.py -d example.com --filter-status 200 --filter-mime text/html --from 20210101
```

**Save results to a file:**
```bash
python wayback_unified.py -d example.com -o results.txt
```

**Use more threads for faster page fetching:**
```bash
python wayback_unified.py -d example.com --threads 10 -o results.txt
```

**Pipe results into other tools:**
```bash
python wayback_unified.py -d example.com | grep "\.js$"
python wayback_unified.py -d example.com | grep "api"
python wayback_unified.py -d example.com | httpx -silent
```

**Get all archived versions of a specific URL (unique by content digest):**
```bash
python wayback_unified.py --versions -d "https://example.com/login"
```

**Fast mode (skip resume-key method):**
```bash
python wayback_unified.py -d example.com --skip-resume-key -o results.txt
```

---

## Output Format

Results are printed to **stdout**, one URL per line. Log messages go to **stderr** so you can safely pipe output without mixing logs into results.

```
https://example.com/
https://example.com/about
https://example.com/api/v1/users
https://sub.example.com/login
...
```

---

## How the CDX API is Used

All three methods query the [Wayback Machine CDX API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server) (`https://web.archive.org/cdx/search/cdx`) with different parameter strategies:

```
# Method 1 — Page-based (waymore-style)
/cdx/search/cdx?url=*.example.com/*&output=text&fl=original&collapse=urlkey&matchType=domain&page=N

# Method 2 — Resume-key (waybackpy-style)
/cdx/search/cdx?url=*.example.com/*&output=text&fl=original&collapse=urlkey&gzip=false&showResumeKey=true&limit=50000&resumeKey=...

# Method 3 — Exact (waybackurls-style)
/cdx/search/cdx?url=example.com/*&output=json&collapse=urlkey
```

---

## Rate Limiting

The tool automatically handles rate limiting:
- Detects `429 Too Many Requests` and backs off with exponential wait
- Detects `503 Service Unavailable` and retries
- Up to 3 retries per request by default

To be respectful to the Wayback Machine, avoid running with very high thread counts (stay at 5–10).

---

## License

[MIT](LICENSE)

---

## Credits

This tool is a synthesis of techniques from:
- [waymore](https://github.com/xnl-h4ck3r/waymore) by [@xnl-h4ck3r](https://github.com/xnl-h4ck3r)
- [waybackurls](https://github.com/tomnomnom/waybackurls) by [@tomnomnom](https://github.com/tomnomnom)
- [waybackpy](https://github.com/akamhy/waybackpy) by [@akamhy](https://github.com/akamhy)
