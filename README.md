# wayback-unified

  A precise Wayback Machine URL harvester built around one core idea: **target exactly what you want — a specific subdomain or a full domain — and get every archived URL for it, no leakage, no noise.**

  ---

  ## The main feature

  Most Wayback tools return URLs for the entire root domain even when you only asked for a subdomain. wayback-unified scopes results strictly to whatever you target:

  - `-d api.example.com` → only `api.example.com` (and its sub-subdomains if `--no-subs` is not set)
  - `-d example.com` → the full domain and all subdomains
  - `-d example.com --no-subs` → the root domain only, nothing else

  ---

  ## How it works

  All URLs come from a single source — the Wayback CDX API:

  ```
  https://web.archive.org/cdx/search/cdx?url=*.TARGET&fl=original&collapse=urlkey
  ```

  The pipeline:

  1. Connects to the CDX API with resume-key cursor pagination
  2. Streams the response in chunks — the full body is never loaded into memory
  3. Iterates cursor pages until the API signals completion (safe for any result size)
  4. Scope-filters, normalizes, and deduplicates results as they arrive
  5. Prints to stdout as URLs are found (or writes to a file with `-o`)

  No external dependencies — Python standard library only.

  ---

  ## Install

  ```bash
  git clone https://github.com/xhackingz/wayback-unified
  cd wayback-unified
  python wayback_unified.py -d example.com
  ```

  **Requirements:** Python 3.10+

  ### Update

  ```bash
  git pull
  ```

  ---

  ## Usage

  ```
  python wayback_unified.py -d <target> [options]
  ```

  | Option | Description |
  |---|---|
  | `-d`, `--domain` | Target domain or subdomain |
  | `--no-subs` | Exact hostname only, no subdomains |
  | `--from YYYYMMDD` | Start date filter |
  | `--to YYYYMMDD` | End date filter |
  | `--filter-status CODE` | Filter by archived HTTP status code |
  | `--filter-mime TYPE` | Filter by MIME type |
  | `-o FILE` | Write results to a file instead of stdout |

  ---

  ## Examples

  ```bash
  # All URLs for a domain (subdomains included)
  python wayback_unified.py -d example.com

  # Specific subdomain only
  python wayback_unified.py -d api.example.com

  # Root domain only, no subdomains
  python wayback_unified.py -d example.com --no-subs

  # Date range
  python wayback_unified.py -d example.com --from 20200101 --to 20231231

  # HTTP 200 responses only
  python wayback_unified.py -d example.com --filter-status 200

  # HTML pages only
  python wayback_unified.py -d example.com --filter-mime text/html

  # Save to file
  python wayback_unified.py -d example.com -o results.txt

  # Pipe into other tools
  python wayback_unified.py -d example.com | grep "\.js$"
  python wayback_unified.py -d example.com | httpx -silent
  ```

  ---

  ## License

  [MIT](LICENSE)
  