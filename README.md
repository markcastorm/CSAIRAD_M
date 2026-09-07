# CSAIRAD_M — CalSavers Participation Data Pipeline

Automated pipeline that scrapes the California State Treasurer's CalSavers reports website, downloads the latest monthly Participation & Funding Snapshot PDF, extracts 20 data fields from it, and writes a structured row to a master CSV file.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [Project Structure](#project-structure)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [How to Run](#how-to-run)
7. [Output Files](#output-files)
8. [Pipeline Stages in Detail](#pipeline-stages-in-detail)
9. [Exit Codes](#exit-codes)
10. [Known Constraints](#known-constraints)
11. [Troubleshooting](#troubleshooting)
12. [Extending the Pipeline](#extending-the-pipeline)

---

## What It Does

Every month, the California State Treasurer publishes a "Participation & Funding Snapshot" PDF at:

```
https://www.treasurer.ca.gov/calsavers/reports/{YEAR}/index.asp
```

This pipeline:

1. **Discovers** the latest year and latest monthly report link by reading the live page — no hardcoded URLs or year values.
2. **Downloads** the PDF to `downloads/`.
3. **Parses** 20 numeric fields from 3 sections of the PDF using text extraction and regex.
4. **Applies mapping logic** — if a PDF section's date doesn't match the report month (can happen when reports are published early), it compares against the prior month's PDF to decide whether to write or skip each field.
5. **Writes** a dated row to `output/CSAIRAD_DATA.csv` and a per-run output file to `output/`.

---

## Project Structure

```
CSAIRAD_M/
├── orchestrator.py        # Entry point — scraping, discovery, download, calls mapper
├── calsavers_mapper.py    # PDF parsing, mapping logic, CSV writing
├── config.json            # Field definitions, section layout, output paths
│
├── downloads/             # Downloaded PDFs (auto-created)
│   └── participation_summary_august_2026.pdf
│
├── output/                # CSV outputs (auto-created)
│   ├── CSAIRAD_DATA.csv         # Master rolling CSV (all months appended)
│   └── 2026-08_mapped.csv       # Per-run output for audit
│
└── logs/                  # Timestamped log files (auto-created)
    ├── 20260907_103221.log        # Orchestrator log
    └── mapper_20260907_103221.log # Mapper log
```

---

## Requirements

| Package | Purpose |
|---|---|
| `requests` | HTTP client for fetching pages and PDFs |
| `urllib3` | Used by requests for retry logic |
| `beautifulsoup4` | HTML parsing for link discovery |
| `pdfplumber` | PDF text extraction |
| `python-dateutil` | `relativedelta` for prior month calculation |

Python 3.8 or later is required.

---

## Installation

```bash
pip install requests urllib3 beautifulsoup4 pdfplumber python-dateutil
```

No Playwright or browser dependency is required. The pipeline uses plain HTTP requests.

---

## Configuration

All field mappings and output paths live in `config.json`. You do not need to edit `orchestrator.py` or `calsavers_mapper.py` to add, remove, or rename fields.

### Top-level keys

| Key | Value | Description |
|---|---|---|
| `csv_path` | `"output/CSAIRAD_DATA.csv"` | Relative path to the master output CSV. Relative to the working directory when you run the script. |
| `output_dir` | `"output"` | Directory for per-run output files. |
| `base_url` | `"https://www.treasurer.ca.gov"` | Root URL — only change if the site moves. |

### Section definition

Each entry in `sections` maps one visual section of the PDF to a set of CSV columns:

```json
{
  "id": "employer_registration",
  "pdf_title": "Employer Registration Status Changes",
  "page_region": "left",
  "fields": [ ... ]
}
```

| Key | Description |
|---|---|
| `id` | Internal identifier used in logging and data structures |
| `pdf_title` | Exact text of the section heading in the PDF — used to locate the date column |
| `page_region` | `"left"` or `"right"` — which half of the PDF page to crop before extracting text |
| `fields` | List of field definitions (see below) |

### Field definition

```json
{
  "order": 1,
  "pdf_row_label": "Registered",
  "csv_column": "CSAIRAD.EMPLOYERS.REGISTERED.M",
  "description": "Employers, Employers Registered",
  "type": "integer"
}
```

| Key | Description |
|---|---|
| `order` | Controls the column order in the output CSV |
| `pdf_row_label` | Exact text label on the PDF row — used by the regex to locate the value |
| `csv_column` | The column ID written to the CSV header and data row |
| `description` | Human-readable label written to the second CSV header row |
| `type` | `"integer"`, `"float"`, or `"currency"` — controls numeric parsing |

### Current fields (20 total)

| Order | CSV Column | PDF Label | Type |
|---|---|---|---|
| 1 | `CSAIRAD.EMPLOYERS.REGISTERED.M` | Registered | integer |
| 2 | `CSAIRAD.EMPLOYERS.UPLOADEDROSTER.M` | Uploaded Roster | integer |
| 3 | `CSAIRAD.EMPLOYERS.STARTEDPAYROLLDEDUCTIONS.M` | Started Payroll Deductions | integer |
| 4 | `CSAIRAD.EMPLOYERS.FACILITATINGDEDUCTIONS.M` | Facilitated Deductions in last 90 days | integer |
| 5 | `CSAIRAD.EMPLOYERS.EXEMPTED.M` | Exempt | integer |
| 6 | `CSAIRAD.PARTICIPANTS.FUNDEDACCOUNTS.M` | Funded Accounts | integer |
| 7 | `CSAIRAD.PARTICIPANTS.PAYROLLCONTRIBUTINGACCOUNTS.M` | Payroll Contributing Accounts | integer |
| 8 | `CSAIRAD.PARTICIPANTS.MULTIPLEEMPLOYERACCOUNTS.M` | Multiple Employer Accounts | integer |
| 9 | `CSAIRAD.PARTICIPANTS.SELFENROLLEDFUNDEDACCOUNTS.M` | Self-Enrolled Funded Accounts | integer |
| 10 | `CSAIRAD.PARTICIPANTS.EFFECTIVEOPTOUTRATE.M` | Effective Opt-Out Rate | float |
| 11 | `CSAIRAD.FUNDING.TOTALASSETS.M` | Total Assets | currency |
| 12 | `CSAIRAD.FUNDING.AVGFUNDEDACCOUNTBAL.M` | Average Funded Account Balance | currency |
| 13 | `CSAIRAD.FUNDING.TTLCONTRIBUTIONSAMOUNT.M` | Total Contributions Amount | currency |
| 14 | `CSAIRAD.FUNDING.AVGMONTHLYCONTRIBUTIONAMOUNT.M` | Average Monthly Contribution Amount | currency |
| 15 | `CSAIRAD.FUNDING.MEDIANMONTHLYCONTRIBUTIONAMOUNT.M` | Median Monthly Contribution Amount | currency |
| 16 | `CSAIRAD.FUNDING.AVERAGECONTRIBUTIONRATE.M` | Average Contribution Rate | float |
| 17 | `CSAIRAD.FUNDING.AMOUNTWITHDRAWALS.M` | Amount of Withdrawals | currency |
| 18 | `CSAIRAD.FUNDING.ACCOUNTSWITHFULLWITHDRAWAL.M` | Accounts with a Full Withdrawal | integer |
| 19 | `CSAIRAD.FUNDING.ACCOUNTSWITHPARTIALWITHDRAWAL.M` | Accounts with a Partial Withdrawal | integer |
| 20 | `CSAIRAD.FUNDING.WITHDRAWALRATE.M` | Withdrawal Rate | float |

---

## How to Run

Always run from the project root directory so that relative paths (`output/`, `downloads/`, `logs/`) resolve correctly.

```bash
cd D:\Projects\CSAIRAD_M
python orchestrator.py
```

**VPN required.** The site (treasurer.ca.gov) is protected by Imperva WAF, which blocks automated HTTP requests from certain IPs. A VPN with a clean IP must be active before running.

### Run the mapper standalone (if you already have the PDF)

```bash
# Auto-detect month from filename
python calsavers_mapper.py downloads/participation_summary_august_2026.pdf

# Specify month explicitly
python calsavers_mapper.py downloads/participation_summary_august_2026.pdf 2026-08
```

---

## Output Files

### `output/CSAIRAD_DATA.csv` — Master rolling CSV

The primary output. Each run appends one row. If a row for the same month already exists it is overwritten (idempotent re-runs).

Structure:
- **Row 1**: Column IDs (`CSAIRAD.EMPLOYERS.REGISTERED.M`, ...)
- **Row 2**: Human-readable descriptions
- **Row 3+**: Data rows, one per report month (`2026-08,282576,253879,...`)

Example:
```
,CSAIRAD.EMPLOYERS.REGISTERED.M,...
,"Employers, Employers Registered",...
2026-08,282576,253879,79795,51485,268571,662734,...
```

### `output/{YYYY-MM}_mapped.csv` — Per-run audit file

Same structure as the master CSV but contains only the row for the current run. Useful for reviewing what was written without opening the full master file.

### `downloads/participation_summary_{month}_{year}.pdf`

The raw PDF file downloaded from the site. Kept on disk so that re-runs do not re-download and so the prior month PDF can be reused if already present.

### `logs/{timestamp}.log` and `logs/mapper_{timestamp}.log`

Full timestamped logs for every run. The orchestrator writes to `{timestamp}.log` and the mapper writes to `mapper_{timestamp}.log`. Both are written simultaneously with stdout.

---

## Pipeline Stages in Detail

```
orchestrator.py
│
├── Stage 1: find_latest_report(session)
│   ├── Fetch current year's index page
│   ├── Read all year links from the page nav
│   ├── Try years highest → lowest until one has report links
│   └── Return (year, pdf_url, report_text)
│
├── Stage 2: download_pdf(session, pdf_url, report_text)
│   ├── Derive filename from report link text (e.g. participation_summary_august_2026.pdf)
│   └── Stream download to downloads/
│
└── Stage 3: calsavers_mapper.main(pdf_path)
    │
    ├── detect_report_month(pdf_path)
    │   ├── Strategy 1: parse month and year from filename
    │   └── Strategy 2: scan PDF text for "as of M/DD/YYYY"
    │
    ├── extract_sections_from_pdf(pdf_path, config)
    │   ├── Crop left half  → extract employer_registration fields
    │   └── Crop right half → extract participant_savings + funding_amounts fields
    │
    ├── [if date mismatch] get_prior_month_pdf(report_month, base_url)
    │   ├── Scrape prior year's index page for the exact prior month link
    │   └── Download from discovered URL (no URL guessing)
    │
    ├── build_csv_row(current, prior, report_month, config)
    │   ├── Date matches → write value directly
    │   ├── Date mismatch + value changed  → write
    │   ├── Date mismatch + value same     → skip (flag)
    │   └── Date mismatch + no prior       → write (best effort)
    │
    ├── append_to_csv(row, report_month, config)
    │   └── Upsert into output/CSAIRAD_DATA.csv
    │
    └── write_output_file(row, report_month, config)
        └── Write output/{YYYY-MM}_mapped.csv
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Full success — row written to CSV |
| `1` | Error — assertion failed or unexpected exception. Check logs. |
| `2` | Skipped — all 20 fields were identical to the prior month. CSV not updated. Manual review required. |

---

## Known Constraints

### VPN required
The site is behind Imperva WAF (Incapsula). Without a VPN or a non-flagged IP, the site returns an "Access Denied — Error 16" block page with no content. The pipeline will fail immediately if blocked. There is no code-level workaround for this.

### PDF format dependency
The mapper uses text extraction and regex to find field values by their row label. If California State Treasurer changes the label text (e.g. "Registered Employers" instead of "Registered"), the field will return `None`. The config's `pdf_row_label` must match the PDF exactly. Check logs for `(raw: None)` lines to detect this.

### Single-page PDF assumption
The extraction always reads `pdf.pages[0]`. All current reports fit on one page. If the PDF format changes to multi-page, `extract_sections_from_pdf` will need updating.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `ASSERTION FAILED: Could not reach any CalSavers reports year page` | IP blocked by Imperva / VPN not active | Enable VPN with a clean IP |
| `ASSERTION FAILED: No Participation Summary Report links found` | Site nav changed, or current year has no reports yet | Check the site manually; if nav changed, update `find_latest_report` |
| Field shows `None` in output | PDF label changed or text extraction failed | Check the mapper log for `(raw: None)`, compare `pdf_row_label` to actual PDF text |
| `FileNotFoundError` for CSV | `csv_path` in config.json points to a non-existent directory | Set `csv_path` to a relative path like `output/CSAIRAD_DATA.csv` |
| Exit code `2` | All fields identical to prior month | Download the PDF manually and inspect — the report may not have been updated yet |

---

## Extending the Pipeline

### Add a new field

1. Open `config.json`.
2. Add a new entry to the appropriate section's `fields` array.
3. Set `order` to the next integer after the current maximum.
4. Set `pdf_row_label` to the exact text label from the PDF.
5. Set `csv_column`, `description`, and `type`.
6. Run the pipeline — no code changes needed.

### Add a new PDF section

1. Add a new object to the `sections` array in `config.json`.
2. Set `id`, `pdf_title`, and `page_region` (`"left"` or `"right"`).
3. Add `fields` as above.
4. Run the pipeline — the mapper reads all sections dynamically from config.

### Change the output CSV path

Edit `csv_path` in `config.json`. Use a relative path (relative to the working directory when you run the script) or an absolute path.
