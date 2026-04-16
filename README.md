# wayback-unified

  A Wayback Machine URL harvester focused on one thing: **targeting exactly the subdomain or domain you specify — and only that.**

  ---

  ## What problem this solves

  Tools like **waymore**, **waybackurls**, and **waybackpy** are each powerful on their own, but combining their techniques introduces a scope problem — when you ask for `api.example.com`, you often get results for `example.com`, unrelated subdomains, or parent domains leaking through the CDX index.

  wayback-unified fixes that. It uses a single, strict CDX query and filters every result at the hostname level before output, so what you ask for is exactly what you get.

  ---

  ## Main feature: precise scope targeting

  - `-d api.example.com` → only `api.example.com` (plus its sub-subdomains unless `--no-subs`)
  - `-d example.com` → the full domain and all subdomains
  - `-d example.com --no-subs` → the root domain only, nothing else

  No bleed, no leakage, no unexpected results from other hosts.

  ---

  ## How it works

  Single data source — the Wayback CDX API:

  ```
  https://web.archive.org/cdx/search/cdx?url=*.TARGET&fl=original&collapse=urlkey
  ```

  The pipeline:

  1. Connects to the CDX endpoint with resume-key cursor pagination
  2. Streams the response in 64 KB chunks — the full body is never loaded into memory
  3. Iterates cursor pages until the API signals completion (safe for any dataset size)
  4. Applies strict hostname-level scope filtering on every URL before accepting it
  5. Deduplicates and normalizes results as they arrive
  6. Prints to stdout as URLs are found, or writes to a file with `-o`

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
  | `--no-subs` | Exact hostname only — no subdomains |
  | `--from YYYYMMDD` | Start date filter |
  | `--to YYYYMMDD` | End date filter |
  | `--filter-status CODE` | Filter by archived HTTP status code (e.g. `200`) |
  | `--filter-mime TYPE` | Filter by MIME type (e.g. `text/html`) |
  | `-o FILE` | Write results to a file instead of stdout |

  ---

  ## Examples

  ```bash
  # All URLs for a domain (subdomains included by default)
  python wayback_unified.py -d example.com

  # Specific subdomain only
  python wayback_unified.py -d api.example.com

  # Root domain, no subdomains
  python wayback_unified.py -d example.com --no-subs

  # Date range
  python wayback_unified.py -d example.com --from 20200101 --to 20231231

  # HTTP 200 only
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
  