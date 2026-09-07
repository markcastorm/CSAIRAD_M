# CLAUDE.md — Architecture & Agent Guide for CSAIRAD_M

This file is the authoritative reference for any AI agent working in this repository.
Read it fully before making any changes. It documents every module, every function,
every design decision, and the full history of changes made to the pipeline.

---

## Project Purpose

Automated monthly ETL pipeline for CalSavers Retirement Savings Program data.

Source: `https://www.treasurer.ca.gov/calsavers/reports/{YEAR}/index.asp`
Output: `output/CSAIRAD_DATA.csv` — a rolling monthly CSV with 20 numeric fields per row.

The pipeline runs on demand (not scheduled). A human runs it once per month after the
State Treasurer publishes the new Participation & Funding Snapshot PDF.

---

## Repository Layout

```
CSAIRAD_M/
├── orchestrator.py        # Pipeline entry point. Scraping + download + mapper call.
├── calsavers_mapper.py    # PDF parsing, date logic, CSV writing.
├── config.json            # ALL field/section definitions. No field logic lives in code.
├── README.md              # User-facing setup and usage guide.
├── CLAUDE.md              # This file.
├── downloads/             # PDF cache (auto-created). Never delete — reused by mapper.
├── output/                # CSV outputs (auto-created).
└── logs/                  # Timestamped run logs (auto-created).
```

---

## Critical Design Principles

These principles must be preserved in all future changes.

1. **No hardcoded years.** The pipeline always derives the current year from `datetime.now().year`
   and discovers available years from the live site. Never write a literal year like `2026` in code.

2. **No hardcoded PDF URL patterns.** The CalSavers site uses inconsistent PDF URL patterns
   (underscores, spaces, different subdirectories). All PDF URLs are discovered by scraping the
   year's index page. Never construct a PDF URL by string concatenation.

3. **Config drives everything.** All field labels, column names, section layout, and output paths
   live in `config.json`. The Python code is generic — it reads the config and applies it.
   Adding a field requires only a config change, not a code change.

4. **requests, not Playwright.** The site is behind Imperva WAF which blocks browser automation.
   Plain HTTP requests with browser-like headers work. Do not reintroduce Playwright.

5. **Relative output paths.** `csv_path` and `output_dir` in config.json must be relative paths
   so the pipeline runs identically on any machine from the project directory.

6. **Assertions as checkpoints.** Every major pipeline step asserts its output before proceeding.
   Assertions are logged and raise `AssertionError` which is caught in `main()` and returns
   exit code 1. Never silence an assertion.

---

## Module: `orchestrator.py`

Entry point. Run with: `python orchestrator.py`

### Module-level setup (runs on import)

```python
os.makedirs('downloads', exist_ok=True)  # PDF cache
os.makedirs('logs', exist_ok=True)
os.makedirs('output', exist_ok=True)
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')  # frozen at import time
```

`timestamp` is module-level and used in both the log filename and as a fallback PDF filename.
It is frozen at import time — not regenerated per function call.

### Constants

```python
BASE_URL = 'https://www.treasurer.ca.gov'
UA = 'Mozilla/5.0 ... Chrome/125.0.0.0 Safari/537.36'
```

`UA` is a realistic Chrome user-agent. Combined with browser-like Accept headers, it passes
Imperva's basic bot checks when a non-flagged IP is in use.

---

### Function: `assert_with_log(condition, message)`

Core assertion helper. Logs `ASSERTION FAILED: {message}` at ERROR level before raising
`AssertionError`. Used throughout the pipeline to fail fast with a clear log entry.

```python
assert_with_log(bool(known_years), "Could not reach any CalSavers reports year page")
```

### Function: `assert_element_exists(element, element_name, context="")`

Asserts that a value is not None. Returns the element if it exists (allows chaining).
Used less frequently than `assert_with_log` — only when checking for a specific named object.

### Function: `assert_file_exists(filepath, file_description="")`

Asserts that a file path exists on disk. Returns the filepath if it exists.

### Function: `assert_data_not_empty(data, data_name)`

Asserts that a list or dict is non-empty. Logs the count if valid.

### Function: `assert_file_downloaded(filepath, min_size_bytes=1000, max_wait_seconds=30)`

Polls for a file to exist and exceed `min_size_bytes`. Retries up to `max_wait_seconds`.
Used after PDF download to confirm the file is complete and not a truncated response.
The 1000-byte minimum catches HTML error pages mistakenly saved as .pdf files.

---

### Function: `make_session()`

Creates a `requests.Session` with:
- Browser-like headers (User-Agent, Accept, Accept-Language, etc.)
- `urllib3.util.retry.Retry` mounted on both https:// and http://:
  - `total=3` — retry up to 3 times
  - `backoff_factor=2` — waits 2s, 4s, 8s between retries
  - `status_forcelist=[500, 502, 503, 504]` — only retry on server errors, not 4xx

Why a session? Cookies and connection pooling are reused across requests. The site may
set session cookies that affect subsequent requests.

Why only 5xx retries? 4xx errors (403 Forbidden, 404 Not Found) are real failures and
should not be retried — they indicate wrong URL or IP block, not transient issues.

### Function: `fetch_soup(session, url, timeout=30)`

Wraps `session.get()` with `raise_for_status()` and returns a `BeautifulSoup` object.
All HTML page fetches go through this function. Logs the URL at INFO level.

---

### Function: `extract_year_month(link_text)` → `(year, month_num)` or `None`

Extracts a `(year, month_number)` tuple from report link text like:
```
"Participation Summary Report for the Month Ending August 31, 2026"
```

Regex: `r'(\w+)\s+\d{1,2},\s+(\d{4})'`

Returns a comparable tuple e.g. `(2026, 8)`. Used for sorting to find the latest report.
Returns `None` if the text does not contain a parseable date.

This function is duplicated in `calsavers_mapper.py` as `_extract_year_month` because
the mapper module is designed to be importable without importing orchestrator.

---

### Function: `find_latest_report(session)` → `(year, pdf_url, report_text)`

**The most critical function in the pipeline.** Replaces two separate functions
(`get_latest_year_url` + `get_latest_participation_report_url`) that existed in v1.

Algorithm:
```
Step 1: Discover the site's full year list
  - Try fetching pages for: current_year, current_year-1, ..., current_year-3
  - On the first successful fetch, collect all <a> tags whose text is exactly 4 digits
  - Build a set of known years (e.g. {2017, 2018, ..., 2026})
  - Assert that at least one year was found

Step 2: Find the latest year that has reports posted
  - Sort known_years descending (e.g. [2026, 2025, 2024, ...])
  - For each year, fetch its index page and scan for <a> links containing
    "Participation Summary Report"
  - Resolve relative hrefs to absolute URLs (BASE_URL + href)
  - Parse (year, month_num) from each link's text using extract_year_month()
  - Track the link with the highest (year, month_num) tuple as `best`
  - If best is not None → return (year, best_href, best_text)
  - If best is None → log warning and try the next lower year
  - If no year has reports → raise AssertionError
```

Why merge the two functions? The original design discovered the latest year first, then
independently fetched that year's page. If the site listed a year with no reports yet
(e.g., 2027 nav link added before any 2027 reports), the second step would fail with no
fallback. The merged function handles this naturally by trying the next year down.

**Key invariant**: The active year's nav link points to `#` (not `/calsavers/reports/YYYY`).
This means the original Playwright selector `ul li a[href*="/calsavers/reports/"]` would
MISS the current active year entirely. This function reads the link TEXT (a 4-digit number)
instead of the href, which works regardless of whether the link is `#` or a real path.

---

### Function: `download_pdf(session, pdf_url, report_text)` → `filepath`

Downloads a PDF using the shared session (connection reuse, shared cookies).

Filename derivation:
- Regex extracts month name and year from `report_text`
  e.g. "...August 31, 2026" → `participation_summary_august_2026.pdf`
- Falls back to `participation_summary_{timestamp}.pdf` if parsing fails

Streams the download in 8 KB chunks (does not load the entire PDF into memory).
Asserts HTTP 200 and minimum file size (1000 bytes) before returning.

---

### Function: `main()`

Orchestrates the pipeline:

```
1. make_session()
2. find_latest_report(session)        → (year, pdf_url, report_text)
3. download_pdf(session, pdf_url, ...) → saved_path
4. calsavers_mapper.main(saved_path)  → exit_code (0, 1, or 2)
5. Return mapper exit code
```

Exception handling:
- `AssertionError` → logged, returns 1 (expected failures with a clear cause)
- `Exception` → full traceback logged, returns 1 (unexpected failures)

---

## Module: `calsavers_mapper.py`

PDF parser and CSV writer. Can be run standalone or called from orchestrator.

Standalone usage:
```bash
python calsavers_mapper.py downloads/participation_summary_august_2026.pdf
python calsavers_mapper.py downloads/participation_summary_august_2026.pdf 2026-08
```

When called from orchestrator, `report_month=None` is passed and auto-detection runs.

### Constants

```python
CONFIG_PATH = 'config.json'

MONTH_NAMES = {1: 'january', 2: 'february', ..., 12: 'december'}
```

`MONTH_NAMES` maps month numbers to lowercase strings used in PDF filenames.

---

### Function: `clean_value(raw, data_type)` → `int | float | None`

Converts a raw string from the PDF to a numeric value.

Strips: `$`, `,`, `%`, `*`, whitespace.

Then:
- `'integer'` → `int(float(cleaned))` (handles "272,054" → 272054)
- `'float'` or `'currency'` → `float(cleaned)` (handles "$1,950,268,841" → 1950268841.0)

Returns `None` on any parse failure. `None` values are written as empty strings in the CSV.

---

### Function: `parse_date_from_text(text)` → `datetime | None`

Extracts a date from strings like `'1/31/2026'` or `'12/31/2025'`.
Used as a utility — not called directly in the main extraction flow.
The main flow uses `detect_section_date()` instead.

---

### Function: `extract_region_text(page, region)` → `str`

Crops a `pdfplumber` page object to a half-width region and returns extracted text.

- `region='left'`  → `crop((0, 0, w/2, h))`
- `region='right'` → `crop((w/2 - 30, 0, w, h))` — 30px left shift prevents clipping the
  first character of right-column labels when the PDF renderer bleeds text slightly left
- `region='full'`  → no crop

**Why crop by half?** The PDF is a 2-column landscape layout. Without cropping, `pdfplumber`
merges text from both columns onto the same line (e.g., "Registered 282,576 Funded Accounts
662,734"). The regex then finds the wrong value for "Registered" if "Funded Accounts" appears
first. Cropping ensures each column's text is isolated.

---

### Function: `find_first_value(text, label)` → `str | None`

Finds the first numeric token after a label in extracted PDF text.

Primary pattern (anchored to start of line):
```python
rf'^{re.escape(label)}\*?\*?\s+(-?\$?[\d][\d,]*\.?\d*%?)'
```

Fallback pattern (allows leading whitespace for indented rows):
```python
rf'^\s+{re.escape(label)}\*?\*?\s+(-?\$?[\d][\d,]*\.?\d*%?)'
```

**Why `\*?\*?`?** Some PDF labels have asterisks appended (e.g., "Multiple Employer Accounts*")
as footnote markers. The pattern accepts zero, one, or two trailing asterisks.

**Why anchor to `^`?** Without the start-of-line anchor, "Funded Accounts" would match inside
"Self-Enrolled Funded Accounts" because the regex engine finds the first occurrence. The anchor
ensures only the exact label at the start of a line (after optional whitespace) matches.

The token pattern `(-?\$?[\d][\d,]*\.?\d*%?)` matches:
- Optional leading `-` (negative values)
- Optional `$`
- Required leading digit
- Optional comma-separated digits
- Optional decimal point and digits
- Optional trailing `%`

Returns the raw matched string (e.g., `'$1,950,268,841'`). Numeric conversion is done
separately by `clean_value()`.

---

### Function: `detect_section_date(text, section_title)` → `datetime | None`

Locates the `section_title` in the text, then finds the FIRST date pattern (`M/DD/YYYY`)
appearing after it. This first date is the "current" column date in the PDF table header
(the second column, shown before the prior-period comparison columns).

Why is the date important? Some PDFs are published with a section date from the previous
quarter (e.g., a May 2026 PDF may show "5/31/2026" for the funding section but "2/28/2026"
for another). The date mismatch check in `build_csv_row()` triggers prior-month comparison
logic when this occurs.

---

### Function: `extract_sections_from_pdf(pdf_path, config)` → `dict`

Main PDF extraction driver. Iterates over all sections defined in `config['sections']`.

For each section:
1. Gets cached region text (avoids re-cropping the same half twice)
2. Logs the first 15 lines of extracted text for diagnosis
3. Calls `detect_section_date()` to get the section's current date
4. For each field, calls `find_first_value()` + `clean_value()` to get the numeric value
5. Stores results in a dict keyed by `section_id`

Return structure:
```python
{
  'employer_registration': {
    'date': datetime(2026, 8, 31),
    'values': {
      'CSAIRAD.EMPLOYERS.REGISTERED.M': 282576,
      ...
    }
  },
  ...
}
```

The text cache (`text_cache`) avoids extracting the same region (e.g., 'right') multiple times
when multiple sections share the same half of the page.

---

### Function: `detect_report_month(pdf_path)` → `'YYYY-MM'`

Two-strategy auto-detection of the report month:

**Strategy 1 — filename:**
Iterates over all month names and searches for `{month_name}_{year}` in the filename.
e.g., `participation_summary_august_2026.pdf` → `'2026-08'`

**Strategy 2 — PDF text:**
Opens the PDF and scans page 1 text for:
- `"as of M/DD/YYYY"` pattern → extracts month and year
- `"Month DD, YYYY"` pattern → extracts month name and year

Raises `ValueError` if neither strategy succeeds.

---

### Function: `_extract_year_month(link_text)` → `(year, month_num)` or `None`

Private helper (underscore prefix). Same logic as `extract_year_month()` in orchestrator.py.
Duplicated here so the mapper is independently importable/runnable without orchestrator.

### Function: `get_prior_month_pdf(report_month_str, base_url)` → `filepath | None`

Downloads the prior month's PDF for comparison. This is only called when a section date
mismatch is detected (i.e., the PDF was published early with stale data for some sections).

Algorithm:
1. Compute prior month using `relativedelta(months=1)`
2. Check if the PDF is already in `downloads/` — return cached path if so
3. Fetch the prior year's index page (`/calsavers/reports/{prior_year}/index.asp`)
4. Scan for the exact `"Participation Summary Report"` link matching the prior (year, month)
5. Download from the discovered URL (never constructs a URL by guessing)
6. Return the local path, or `None` if not found or download fails

**Why `None` instead of raising?** Prior month data is used for comparison only. If it's
unavailable (e.g., the first month of a new year, prior year's page is restructured), the
mapper falls back to writing current values without comparison (best effort), logged as a
warning.

---

### Function: `build_csv_row(current_sections, prior_sections, report_month, config)` → `(row, skip_row, skipped_fields)`

Applies the field mapping decision logic. This is the core business logic of the mapper.

For each field across all sections:

| Condition | Action |
|---|---|
| Section date matches report month | Write current value directly |
| Section date differs AND prior available AND value changed | Write current value |
| Section date differs AND prior available AND value same | Write `None` (skip field) |
| Section date differs AND no prior PDF | Write current value (best effort, warning logged) |

`skip_row` is `True` only if ALL fields were skipped (every value identical to prior).
When `skip_row` is `True`, the mapper returns exit code 2 without updating the CSV.

Fields are sorted by `order` before processing to ensure consistent column ordering.

---

### Function: `get_ordered_fields(config)` → `list`

Returns all fields from all sections, sorted by `order`. Ensures consistent column ordering
regardless of the order sections appear in `config.json`.

### Function: `get_ordered_columns(config)` → `list`

Returns just the `csv_column` IDs in order. Used to build CSV rows.

### Function: `build_csv_headers(config)` → `(header_line_0, header_line_1)`

Builds the two CSV header lines dynamically from config:
- Line 1: Column IDs (e.g., `,CSAIRAD.EMPLOYERS.REGISTERED.M,...`)
- Line 2: Descriptions (e.g., `,"Employers, Employers Registered",...`)

Leading comma is the date column placeholder.

---

### Function: `append_to_csv(row, report_month, config)`

Upserts one row into the master CSV at `config['csv_path']`.

Behavior:
- If file does not exist → creates it with two header rows
- If file exists → reads existing data rows (skips header rows)
- If a row with the same `report_month` exists → overwrites it (idempotent)
- If no matching row → appends new row

The entire file is rewritten on every call. This is acceptable for a file with ~12 rows/year.

### Function: `write_output_file(row, report_month, skipped_fields, config)`

Writes `output/{YYYY-MM}_mapped.csv` — a single-row CSV with the same header structure
as the master CSV. Used for auditing individual runs.

---

### Function: `main(current_pdf_path, report_month=None)` → `int`

Mapper entry point. Called from orchestrator with `report_month=None`.

Steps:
1. Auto-detect or accept report month
2. Load `config.json`
3. Extract current month PDF sections
4. Detect section date mismatches
5. Conditionally download prior month PDF
6. Build CSV row with mapping logic
7. Handle full-skip case (exit 2)
8. Append to master CSV
9. Write per-run output file
10. Return 0

---

## Configuration Schema (`config.json`)

```json
{
  "csv_path": "output/CSAIRAD_DATA.csv",   // Relative path — works on any machine
  "output_dir": "output",
  "base_url": "https://www.treasurer.ca.gov",
  "sections": [
    {
      "id": "employer_registration",        // Internal key — used in logs and dicts
      "pdf_title": "Employer Registration Status Changes",  // Must match PDF exactly
      "page_region": "left",                // "left" | "right" | "full"
      "fields": [
        {
          "order": 1,                       // Controls CSV column position
          "pdf_row_label": "Registered",    // Must match PDF row label exactly
          "csv_column": "CSAIRAD.EMPLOYERS.REGISTERED.M",
          "description": "Employers, Employers Registered",
          "type": "integer"                 // "integer" | "float" | "currency"
        }
      ]
    }
  ]
}
```

Fields `table_header_text`, `min_rows`, `min_cols`, `current_col_index` are legacy keys
from an earlier table-based extraction approach. They are present in config.json but not
read by the current text-extraction code. Do not remove them — they document the original
intent and may be used if a future version switches back to table extraction.

---

## Change History

### Session 2026-09-07 — Complete rewrite of scraping layer

**Problem:** Pipeline failing with `ASSERTION FAILED: At least one year link found in nav`.

**Root cause (confirmed from screenshot):**
The site returns an Imperva WAF block page (Error 16 — Access Denied) to the Playwright
browser. The block page is an iframe with no real content, so no links are found.
The IP (`197.237.175.28` via proxy `45.223.61.127`) was flagged by Imperva.

**Additional structural bugs found by visiting the site with requests + VPN:**

1. **Hardcoded start year** — the original code started at `/calsavers/reports/2022/index.asp`.
   In 2026 this is stale. The latest year (2026) would only be found if the 2022 page lists
   it in the nav — which it does, but only if the page loads at all.

2. **Wrong CSS selector** — `ul li a[href*="/calsavers/reports/"]` would miss the active year
   because the active year's nav link points to `#`, not a real path.

3. **Wrong PDF URL pattern in `get_prior_month_pdf`** — the original code constructed URLs like
   `/calsavers/reports/{year}/{month_name}_{year}.pdf`. Actual PDF URLs use:
   - `/sites/default/files/calsavers/{Month}_{Year}_Participation_Snapshot_Report.pdf`
   - `/sites/default/files/calsavers/{Month}%20{Year}%20Participation%20Snapshot%20Report.pdf`
   - `/sites/default/files/{date}/{Month}%20{Year}%20Participation%20Snapshot%20Report.pdf`
   These patterns are inconsistent and cannot be reliably constructed.

**Changes made:**

| File | Change |
|---|---|
| `orchestrator.py` | Removed Playwright entirely. Added `requests`, `beautifulsoup4`, `urllib3.util.retry`. |
| `orchestrator.py` | `make_session()` — new function, creates session with browser headers + retry adapter (3 retries, 2s/4s/8s backoff, 5xx only). |
| `orchestrator.py` | `fetch_soup()` — new function, wraps session.get + BeautifulSoup. |
| `orchestrator.py` | `get_latest_year_url()` + `get_latest_participation_report_url()` — REMOVED. |
| `orchestrator.py` | `find_latest_report()` — NEW. Merged replacement. Starts from `datetime.now().year`, reads year link texts (not hrefs), tries years highest→lowest until one has reports. Handles empty new-year page gracefully. |
| `orchestrator.py` | `download_pdf_with_requests()` renamed to `download_pdf()`, now uses shared session. |
| `orchestrator.py` | `config.json → csv_path` changed from absolute machine-specific path to `"output/CSAIRAD_DATA.csv"`. |
| `calsavers_mapper.py` | Added `beautifulsoup4` import. |
| `calsavers_mapper.py` | Added `_MONTH_ORDER` dict and `_extract_year_month()` helper. |
| `calsavers_mapper.py` | `get_prior_month_pdf()` — completely rewritten. Now scrapes the prior year's index page to discover the real PDF URL instead of constructing a guessed URL. |

**Validation:** All 20 fields confirmed matching source PDF via field-by-field regex comparison.

---

## What Can Still Fail

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Imperva WAF blocks IP** | High without VPN | VPN must be active. No code fix possible. |
| **PDF row label text changes** | Low | Check logs for `(raw: None)`. Update `pdf_row_label` in config.json. |
| **Site URL structure changes** | Very low | `find_latest_report` would fail with AssertionError. Inspect site manually and update `BASE_URL` or the URL construction. |
| **New year page listed with no reports** | Once per year (January) | Handled — `find_latest_report` falls back to prior year automatically. |
| **Prior month PDF not yet posted** | Occasionally | `get_prior_month_pdf` returns `None`, mapper writes with best-effort (logged as warning). |
| **Multi-page PDF** | If format changes | `extract_sections_from_pdf` reads `pages[0]` only. Update to iterate pages if needed. |

---

## Running in Different Environments

Always run from the project root:
```bash
cd D:\Projects\CSAIRAD_M
python orchestrator.py
```

All paths are relative to the working directory. Do not run from a different directory.

The pipeline creates `downloads/`, `logs/`, and `output/` automatically if they do not exist.

**VPN must be active.** Without a VPN, `fetch_soup()` will receive an Imperva block page
with status 200 (the block page itself is a valid HTTP response). The BeautifulSoup parse
will find no year links and `find_latest_report` will raise `AssertionError`.

Note: The Imperva block page returns HTTP 200 with an iframe body, not a 403. The retry
adapter will NOT retry it because it is not a 5xx response. The failure will be detected
only when no year links or report links are found (assertion failure), not at the HTTP level.
