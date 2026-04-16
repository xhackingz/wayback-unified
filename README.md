# wayback-unified

A unified Wayback Machine URL harvester that combines three CDX query techniques to return the maximum number of unique archived URLs for any domain or subdomain.

---

## Features

- **Triple-method CDX querying** — runs page-based pagination, resume-key pagination, and an exact prefix query simultaneously, then merges all results
- **Strict subdomain scope isolation** — when targeting a subdomain like `api.example.com`, results are filtered to that scope only — no leakage to the parent domain
- **Concurrent page fetching** — fetches all CDX pages in parallel using a thread pool for speed
- **Auto deduplication** — all results are merged, normalized, and deduplicated before output
- **Date range filtering** — limit results to a specific time window
- **Status code filtering** — only return URLs archived with a given HTTP status (e.g. `200`)
- **MIME type filtering** — restrict by content type (e.g. `text/html`, `application/json`)
- **Archived versions mode** — fetch all unique versions of a specific URL, deduped by content digest
- **Pipeline-friendly** — results go to stdout, logs go to stderr, easy to pipe into other tools
- **Auto rate limit handling** — detects 429/503 responses and retries with backoff
- **No external dependencies** — uses Python standard library only

---

## Install

```bash
git clone https://github.com/xhackingz/wayback-unified
cd wayback-unified
python wayback_unified.py -d example.com
```

**Requirements:** Python 3.7+

---

## Usage

```
python wayback_unified.py -d <target> [options]
```

| Option | Description |
|---|---|
| `-d`, `--domain` | Target domain or subdomain |
| `--no-subs` | Exact hostname only, no subdomains |
| `--from YYYYMMDD` | Start date |
| `--to YYYYMMDD` | End date |
| `--filter-status CODE` | Filter by HTTP status code |
| `--filter-mime TYPE` | Filter by MIME type |
| `-o FILE` | Save output to file |
| `-t`, `--threads N` | Concurrent threads (default: 5) |
| `--skip-resume-key` | Skip resume-key method (faster, slightly less coverage) |
| `--versions` | Fetch all unique archived versions of a URL |

---

## Examples

```bash
# All URLs for a domain (subdomains included by default)
python wayback_unified.py -d example.com

# Scope to a specific subdomain only
python wayback_unified.py -d api.example.com

# Only the root domain, no subdomains
python wayback_unified.py -d example.com --no-subs

# Filter by date range
python wayback_unified.py -d example.com --from 20200101 --to 20231231

# Only HTTP 200 responses
python wayback_unified.py -d example.com --filter-status 200

# Only HTML pages
python wayback_unified.py -d example.com --filter-mime text/html

# Save to file
python wayback_unified.py -d example.com -o results.txt

# Pipe into other tools
python wayback_unified.py -d example.com | grep "\.js$"
python wayback_unified.py -d example.com | httpx -silent

# Get all unique archived versions of a URL
python wayback_unified.py --versions -d "https://example.com/login"
```

---

## License

[MIT](LICENSE)
