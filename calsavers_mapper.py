import os
import json
import logging
import sys
import re
import traceback
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pdfplumber

# ============================================================================
# DIRECTORY SETUP
# ============================================================================
os.makedirs('downloads', exist_ok=True)
os.makedirs('logs', exist_ok=True)
os.makedirs('output', exist_ok=True)

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'logs/mapper_{timestamp}.log'),
        logging.StreamHandler()
    ]
)

# ============================================================================
# CONFIGURATION
# ============================================================================
CONFIG_PATH = 'config.json'

MONTH_NAMES = {
    1: 'january', 2: 'february', 3: 'march',  4: 'april',
    5: 'may',     6: 'june',     7: 'july',    8: 'august',
    9: 'september', 10: 'october', 11: 'november', 12: 'december'
}

# ============================================================================
# ASSERTION FUNCTIONS
# ============================================================================

def assert_with_log(condition, message):
    if not condition:
        logging.error(f"ASSERTION FAILED: {message}")
        raise AssertionError(message)
    logging.debug(f"Assertion passed: {message}")

def assert_file_exists(filepath, description=""):
    desc = description or filepath
    if not os.path.exists(filepath):
        msg = f"File not found: {desc} at {filepath}"
        logging.error(f"ASSERTION FAILED: {msg}")
        raise AssertionError(msg)
    logging.info(f"File verified: {desc}")
    return filepath

# ============================================================================
# VALUE CLEANING
# ============================================================================

def clean_value(raw, data_type):
    """
    Convert raw PDF string to numeric based on type.
    Handles $1,643,775,197  /  35.57%  /  272,054  /  $204
    """
    if raw is None:
        return None
    raw = str(raw).strip()
    # Strip $, commas, %, asterisks, spaces
    cleaned = re.sub(r'[$,%*\s]', '', raw)
    if not cleaned or cleaned in ('-', ''):
        return None
    try:
        if data_type == 'integer':
            return int(float(cleaned))
        elif data_type in ('float', 'currency'):
            return float(cleaned)
    except (ValueError, TypeError):
        logging.warning(f"Could not parse value '{raw}' as {data_type}")
        return None

# ============================================================================
# PDF DATE PARSING
# ============================================================================

def parse_date_from_text(text):
    """
    Extract date from strings like '1/31/2026' or '12/31/2025'.
    Returns datetime or None.
    """
    if not text:
        return None
    match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', str(text))
    if match:
        try:
            return datetime(
                int(match.group(3)),
                int(match.group(1)),
                int(match.group(2))
            )
        except ValueError:
            return None
    return None

# ============================================================================
# PDF SECTION EXTRACTION  (text-based — reliable for 2-column PDF layout)
# ============================================================================

def extract_region_text(page, region):
    """
    Crop the page to left or right half, then extract text.
    Avoids chart noise and cross-column merging issues.
    """
    w = page.width
    h = page.height

    if region == 'left':
        cropped = page.crop((0, 0, w / 2, h))
    elif region == 'right':
        # Shift left by 30px to avoid clipping first letter of right-side text
        cropped = page.crop((w / 2 - 30, 0, w, h))
    else:
        cropped = page

    return cropped.extract_text() or ''


def find_first_value(text, label):
    """
    Find the first numeric-looking token after a label in extracted text.

    Handles:  272,054  |  $1,643,775,197  |  35.57%  |  -$6  |  -1.6%

    Returns the raw string or None.
    """
    # Escape the label, allow optional * and whitespace after it
    escaped = re.escape(label)
    # Anchor to start of line to prevent partial matches
    # e.g. "Funded Accounts" should NOT match inside "Self-Enrolled Funded Accounts"
    # Value token: optional -, optional $, then digits with commas/dots, optional %
    pattern = rf'^{escaped}\*?\*?\s+(-?\$?[\d][\d,]*\.?\d*%?)'
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        # Fallback: match after any whitespace gap (for indented lines)
        pattern = rf'^\s+{escaped}\*?\*?\s+(-?\$?[\d][\d,]*\.?\d*%?)'
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1)
    return None


def detect_section_date(text, section_title):
    """
    Find the current date in a section's text block.
    Looks for date patterns like '1/31/2026' near the section title.
    Returns datetime or None.
    """
    # Find the section title position, then look for dates after it
    title_match = re.search(re.escape(section_title), text, re.IGNORECASE)
    if not title_match:
        return None

    # Search for dates after the title
    after_title = text[title_match.end():]
    # The FIRST date found is the "current" date column
    date_match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', after_title)
    if date_match:
        try:
            return datetime(
                int(date_match.group(3)),
                int(date_match.group(1)),
                int(date_match.group(2))
            )
        except ValueError:
            pass
    return None


def extract_sections_from_pdf(pdf_path, config):
    """
    Parse all configured sections from the PDF using TEXT extraction.

    For each section:
    1. Crop the page to left/right half
    2. Extract text from that region
    3. Find each field's value using regex: label → first number after it

    Returns dict:
      {
        section_id: {
          'date': datetime | None,
          'values': {csv_column: numeric_value}
        }
      }
    """
    assert_file_exists(pdf_path, "PDF file")
    sections_data = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        logging.info(
            f"PDF '{os.path.basename(pdf_path)}': "
            f"page size {page.width:.0f} x {page.height:.0f}"
        )

        # Cache extracted text per region to avoid re-cropping
        text_cache = {}

        for section_cfg in config['sections']:
            section_id = section_cfg['id']
            region     = section_cfg.get('page_region', 'full')
            pdf_title  = section_cfg['pdf_title']

            # ── Get text for this page region ──
            if region not in text_cache:
                text_cache[region] = extract_region_text(page, region)
            region_text = text_cache[region]

            logging.info(
                f"Section '{section_id}': region='{region}', "
                f"text length={len(region_text)} chars"
            )

            # ── Log first few lines for diagnosis ──
            text_lines = region_text.split('\n')
            for i, line in enumerate(text_lines[:15]):
                logging.info(f"  [text line {i:2d}] {line}")

            # ── Find section date ──
            section_date = detect_section_date(region_text, pdf_title)
            logging.info(
                f"Section '{section_id}': current date = "
                f"{section_date.strftime('%m/%d/%Y') if section_date else 'NOT FOUND'}"
            )

            # ── Extract each field value ──
            field_values = {}

            for field in section_cfg['fields']:
                label   = field['pdf_row_label']
                csv_col = field['csv_column']
                dtype   = field['type']

                raw_val = find_first_value(region_text, label)
                val     = clean_value(raw_val, dtype)
                field_values[csv_col] = val

                logging.info(
                    f"  [{section_id}] '{label}' "
                    f"-> {csv_col} = {val}  (raw: {raw_val!r})"
                )

            sections_data[section_id] = {
                'date':   section_date,
                'values': field_values
            }
            logging.info(
                f"Section '{section_id}': "
                f"{sum(1 for v in field_values.values() if v is not None)}"
                f"/{len(section_cfg['fields'])} fields extracted"
            )

    return sections_data

# ============================================================================
# REPORT MONTH AUTO-DETECTION
# ============================================================================

def detect_report_month(pdf_path):
    """
    Automatically determine the report month (YYYY-MM) from:
      1. PDF filename  e.g. participation_summary_january_2026.pdf -> 2026-01
      2. PDF title text e.g. "Snapshot as of 1/31/2026"           -> 2026-01

    Returns 'YYYY-MM' string or raises ValueError if undetectable.
    """
    month_name_to_num = {v: k for k, v in MONTH_NAMES.items()}

    # ── Strategy 1: parse filename ──
    filename = os.path.basename(pdf_path).lower()
    for month_name, month_num in month_name_to_num.items():
        pattern = rf'{month_name}_(\d{{4}})'
        m = re.search(pattern, filename)
        if m:
            year = int(m.group(1))
            result = f"{year}-{month_num:02d}"
            logging.info(f"Report month detected from filename: {result} ('{os.path.basename(pdf_path)}')")
            return result

    # ── Strategy 2: parse PDF title text ──
    logging.info("Filename parse failed — scanning PDF title text for report month...")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Search first page text for "as of M/DD/YYYY" or "Month DD, YYYY"
            text = pdf.pages[0].extract_text() or ''

            # Match "as of 1/31/2026" style
            m = re.search(r'as of\s+(\d{1,2})/\d{1,2}/(\d{4})', text, re.IGNORECASE)
            if m:
                month_num = int(m.group(1))
                year      = int(m.group(2))
                result    = f"{year}-{month_num:02d}"
                logging.info(f"Report month detected from PDF title text: {result}")
                return result

            # Match "January 31, 2026" style
            m = re.search(r'(\w+)\s+\d{1,2},\s+(\d{4})', text, re.IGNORECASE)
            if m:
                month_name = m.group(1).lower()
                year       = int(m.group(2))
                month_num  = month_name_to_num.get(month_name)
                if month_num:
                    result = f"{year}-{month_num:02d}"
                    logging.info(f"Report month detected from PDF text date: {result}")
                    return result
    except Exception as e:
        logging.warning(f"PDF text scan failed: {e}")

    raise ValueError(
        f"Could not auto-detect report month from '{pdf_path}'. "
        "Pass it explicitly: python calsavers_mapper.py <pdf> <YYYY-MM>"
    )


# ============================================================================
# PRIOR MONTH PDF DOWNLOAD
# ============================================================================

_MONTH_ORDER = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12
}

def _extract_year_month(link_text):
    """Extract (year, month_num) from report link text. Returns None if not matched."""
    m = re.search(r'(\w+)\s+\d{1,2},\s+(\d{4})', link_text, re.IGNORECASE)
    if m:
        month_num = _MONTH_ORDER.get(m.group(1).lower())
        if month_num:
            return (int(m.group(2)), month_num)
    return None


def get_prior_month_pdf(report_month_str, base_url):
    """
    Download the prior month's participation report PDF by:
    1. Fetching the appropriate year's index page
    2. Finding the exact report link for the prior month (no URL guessing)
    3. Downloading from the discovered URL

    report_month_str : 'YYYY-MM'
    Returns local file path, or None if not found / download fails.
    """
    report_dt  = datetime.strptime(report_month_str, '%Y-%m')
    prior_dt   = report_dt - relativedelta(months=1)
    month_name = MONTH_NAMES[prior_dt.month]
    prior_path = os.path.join(
        'downloads',
        f"participation_summary_{month_name}_{prior_dt.year}.pdf"
    )

    if os.path.exists(prior_path):
        logging.info(f"Prior month PDF already cached: {prior_path}")
        return prior_path

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    }

    target = (prior_dt.year, prior_dt.month)
    year_url = f"{base_url}/calsavers/reports/{prior_dt.year}/index.asp"
    logging.info(f"Scraping prior month link from: {year_url}")

    try:
        r = requests.get(year_url, headers=headers, timeout=30)
        if r.status_code != 200:
            logging.warning(f"Year page returned HTTP {r.status_code}: {year_url}")
            return None

        soup = BeautifulSoup(r.text, 'html.parser')

        pdf_url = None
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            if 'Participation Summary Report' not in text:
                continue
            ym = _extract_year_month(text)
            if ym == target:
                href = a['href']
                pdf_url = href if href.startswith('http') else base_url + href
                logging.info(f"Prior month link found: {text} -> {pdf_url}")
                break

        if pdf_url is None:
            logging.warning(
                f"Prior month report ({prior_dt.year}-{prior_dt.month:02d}) "
                f"not listed on {year_url}"
            )
            return None

        r2 = requests.get(pdf_url, headers=headers, timeout=60, stream=True)
        if r2.status_code != 200:
            logging.warning(f"Prior PDF download failed (HTTP {r2.status_code}): {pdf_url}")
            return None

        with open(prior_path, 'wb') as f:
            for chunk in r2.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        logging.info(
            f"Prior month PDF saved: {prior_path} "
            f"({os.path.getsize(prior_path):,} bytes)"
        )
        return prior_path

    except Exception as e:
        logging.warning(f"Failed to download prior month PDF: {e}")
        return None

# ============================================================================
# ROW BUILDING LOGIC
# ============================================================================

def build_csv_row(current_sections, prior_sections, report_month, config):
    """
    Apply the mapping decision logic for every field across all sections.

    Rules:
      - Section date MATCHES report month  → write value directly
      - Section date DIFFERS from report month:
          prior available and value DIFFERS → write to current month row
          prior available and value SAME    → skip field (flag)
          prior NOT available               → write value (best effort)

    Returns:
      row            : {csv_column: value}
      skip_row       : True if ALL fields were identical to prior (flag entire row)
      skipped_fields : list of csv_column names that were skipped
    """
    report_dt      = datetime.strptime(report_month, '%Y-%m')
    row            = {}
    written_fields = []
    skipped_fields = []

    for section_cfg in config['sections']:
        section_id   = section_cfg['id']
        section_data = current_sections.get(section_id, {})
        section_date = section_data.get('date')
        curr_values  = section_data.get('values', {})

        date_matches = (
            section_date is not None
            and section_date.year  == report_dt.year
            and section_date.month == report_dt.month
        )

        for field in sorted(section_cfg['fields'], key=lambda x: x['order']):
            csv_col     = field['csv_column']
            current_val = curr_values.get(csv_col)

            if date_matches:
                # Date matches → always write
                row[csv_col] = current_val
                written_fields.append(csv_col)

            else:
                # Date mismatch → compare with prior month
                if prior_sections:
                    prior_val = (
                        prior_sections
                        .get(section_id, {})
                        .get('values', {})
                        .get(csv_col)
                    )

                    if current_val != prior_val:
                        row[csv_col] = current_val
                        written_fields.append(csv_col)
                        logging.info(
                            f"  WRITE  '{csv_col}': changed {prior_val} -> {current_val}"
                        )
                    else:
                        row[csv_col] = None
                        skipped_fields.append(csv_col)
                        logging.info(
                            f"  SKIP   '{csv_col}': same as prior ({current_val})"
                        )
                else:
                    # No prior PDF — write anyway (best effort)
                    row[csv_col] = current_val
                    written_fields.append(csv_col)
                    logging.warning(
                        f"  WRITE* '{csv_col}': no prior PDF to compare, writing value {current_val}"
                    )

    skip_row = (len(written_fields) == 0)
    return row, skip_row, skipped_fields

# ============================================================================
# CSV WRITING
# ============================================================================

def get_ordered_columns(config):
    """Return all CSV column names in the order defined in config."""
    return [f['csv_column'] for f in get_ordered_fields(config)]


def get_ordered_fields(config):
    """Return all fields sorted by their order defined in config."""
    all_fields = []
    for section in config['sections']:
        all_fields.extend(section['fields'])
    all_fields.sort(key=lambda f: f['order'])
    return all_fields


def build_csv_headers(config):
    """Build the two header lines dynamically from config order."""
    ordered_fields = get_ordered_fields(config)

    col_ids = [f['csv_column'] for f in ordered_fields]
    descs   = [f'"{f["description"]}"' for f in ordered_fields]

    header_line_0 = ',' + ','.join(col_ids) + '\n'
    header_line_1 = ',' + ','.join(descs) + '\n'
    return header_line_0, header_line_1


def append_to_csv(row, report_month, config):
    """
    Append or update the report_month row in the CSV.
    Creates the file with headers from config if it doesn't exist.
    """
    csv_path = config['csv_path']
    ordered_cols = get_ordered_columns(config)

    # ── Build headers from config (independent of file) ──
    header_line_0, header_line_1 = build_csv_headers(config)

    # ── Read existing data rows if file exists, otherwise start fresh ──
    data_lines = []
    if os.path.exists(csv_path):
        with open(csv_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # Skip header rows (first 2), keep data rows
        data_lines = lines[2:] if len(lines) > 2 else []
    else:
        logging.info(f"CSV not found — creating new file: {csv_path}")

    # ── Check for existing row with same month ──
    existing_idx = None
    for i, line in enumerate(data_lines):
        if line.startswith(report_month):
            existing_idx = i
            break

    # ── Build the new CSV line ──
    values = [str(row.get(col, '')) for col in ordered_cols]
    new_line = report_month + ',' + ','.join(values) + '\n'

    if existing_idx is not None:
        logging.warning(
            f"Row '{report_month}' already exists at line {existing_idx + 3} — overwriting"
        )
        data_lines[existing_idx] = new_line
    else:
        data_lines.append(new_line)

    # ── Write back ──
    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
        f.write(header_line_0)
        f.write(header_line_1)
        f.writelines(data_lines)

    logging.info(f"CSV updated: {csv_path}")
    logging.info(f"Row written: {new_line.strip()}")


def write_output_file(row, report_month, skipped_fields, config):
    """
    Write the mapped row to output/<report_month>_mapped.csv
    Uses the same header structure as the target CSV, derived from config.
    """
    output_dir  = config.get('output_dir', 'output')
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, f"{report_month}_mapped.csv")

    # Use config-driven headers and column order
    header_line_0, header_line_1 = build_csv_headers(config)
    ordered_cols = get_ordered_columns(config)

    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        f.write(header_line_0)
        f.write(header_line_1)
        values = []
        for col in ordered_cols:
            val = row.get(col, '')
            values.append('' if val is None else str(val))
        f.write(report_month + ',' + ','.join(values) + '\n')

    logging.info(f"Output file written: {output_path}")
    return output_path

# ============================================================================
# MAIN
# ============================================================================

def main(current_pdf_path, report_month=None):
    """
    Entry point for the mapper.

    Args:
        current_pdf_path : path to the already-downloaded current month PDF
        report_month     : 'YYYY-MM' e.g. '2026-01'  (auto-detected if omitted)
    """
    try:
        # ── Auto-detect report month if not provided ──
        if not report_month:
            report_month = detect_report_month(current_pdf_path)

        logging.info("=" * 60)
        logging.info(f"MAPPER STARTING  —  report month: {report_month}")
        logging.info(f"PDF: {current_pdf_path}")
        logging.info("=" * 60)

        # ── Load config ──
        assert_file_exists(CONFIG_PATH, "config.json")
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)

        base_url = config.get('base_url', 'https://www.treasurer.ca.gov')

        # ── Step 1: Parse current month PDF ──
        logging.info("Step 1: Parsing current month PDF...")
        current_sections = extract_sections_from_pdf(current_pdf_path, config)

        # ── Step 2: Detect if any section has a mismatched date ──
        report_dt   = datetime.strptime(report_month, '%Y-%m')
        needs_prior = False

        for section_id, data in current_sections.items():
            sec_date = data.get('date')
            if sec_date and (
                sec_date.year  != report_dt.year or
                sec_date.month != report_dt.month
            ):
                logging.info(
                    f"Step 2: Section '{section_id}' date "
                    f"({sec_date.strftime('%m/%d/%Y')}) != report month "
                    f"({report_month}) — prior PDF required"
                )
                needs_prior = True
                break

        # ── Step 3: Conditionally download prior month PDF ──
        prior_sections = None
        if needs_prior:
            logging.info("Step 3: Downloading prior month PDF for comparison...")
            prior_pdf_path = get_prior_month_pdf(report_month, base_url)
            if prior_pdf_path:
                prior_sections = extract_sections_from_pdf(prior_pdf_path, config)
            else:
                logging.warning("Step 3: Prior month PDF unavailable — proceeding without comparison")
        else:
            logging.info("Step 3: All section dates match report month — no prior PDF needed")

        # ── Step 4: Build CSV row with mapping logic ──
        logging.info("Step 4: Applying field mapping logic...")
        row, skip_row, skipped_fields = build_csv_row(
            current_sections, prior_sections, report_month, config
        )

        # ── Step 5: Handle full-skip case (entire row identical to prior) ──
        if skip_row:
            logging.warning("=" * 60)
            logging.warning(f"WARNING: ROW '{report_month}' SKIPPED")
            logging.warning("All 20 fields are identical to the prior month.")
            logging.warning("No new data detected — CSV not updated.")
            logging.warning("ACTION REQUIRED: Manual review recommended.")
            logging.warning(f"Log file: logs/mapper_{timestamp}.log")
            logging.warning("=" * 60)
            return 2  # Exit code 2 = skipped

        # ── Report partial skips ──
        if skipped_fields:
            logging.warning(
                f"WARNING: {len(skipped_fields)} field(s) skipped "
                f"(identical to prior month):"
            )
            for col in skipped_fields:
                logging.warning(f"  - {col}")

        # ── Step 6: Write to CSV ──
        logging.info("Step 5: Writing row to CSV...")
        append_to_csv(row, report_month, config)

        # ── Step 7: Write output file ──
        logging.info("Step 6: Writing output file...")
        output_path = write_output_file(row, report_month, skipped_fields, config)

        logging.info("=" * 60)
        logging.info(f"MAPPER COMPLETED  —  row '{report_month}' written")
        logging.info(f"Fields written : {20 - len(skipped_fields)}/20")
        logging.info(f"Fields skipped : {len(skipped_fields)}/20")
        logging.info(f"Output file    : {output_path}")
        logging.info("=" * 60)
        return 0

    except AssertionError as e:
        logging.error("=" * 60)
        logging.error("ASSERTION FAILED")
        logging.error(f"Error: {str(e)}")
        logging.error("=" * 60)
        return 1

    except Exception as e:
        logging.error("=" * 60)
        logging.error("UNEXPECTED ERROR")
        logging.error(f"Type   : {type(e).__name__}")
        logging.error(f"Message: {str(e)}")
        logging.error("=" * 60)
        logging.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Auto-detect month : python calsavers_mapper.py <pdf_path>")
        print("  Explicit month    : python calsavers_mapper.py <pdf_path> <YYYY-MM>")
        print("")
        print("Examples:")
        print("  python calsavers_mapper.py downloads/participation_summary_january_2026.pdf")
        print("  python calsavers_mapper.py downloads/participation_summary_january_2026.pdf 2026-01")
        sys.exit(1)

    exit_code = main(
        current_pdf_path=sys.argv[1],
        report_month=sys.argv[2] if len(sys.argv) >= 3 else None
    )
    sys.exit(exit_code)
