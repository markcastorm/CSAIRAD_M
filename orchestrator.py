import os
import logging
import sys
import traceback
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import datetime
import calsavers_mapper

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
        logging.FileHandler(f'logs/{timestamp}.log'),
        logging.StreamHandler()
    ]
)

BASE_URL = 'https://www.treasurer.ca.gov'

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)

# ============================================================================
# ASSERTION FUNCTIONS
# ============================================================================

def assert_with_log(condition, message):
    if not condition:
        logging.error(f"ASSERTION FAILED: {message}")
        raise AssertionError(message)
    logging.debug(f"Assertion passed: {message}")

def assert_element_exists(element, element_name, context=""):
    context_msg = f" in {context}" if context else ""
    if element is None:
        msg = f"Element '{element_name}' not found{context_msg}"
        logging.error(f"ASSERTION FAILED: {msg}")
        raise AssertionError(msg)
    logging.debug(f"Element '{element_name}' found successfully{context_msg}")
    return element

def assert_file_exists(filepath, file_description=""):
    desc = file_description or filepath
    if not os.path.exists(filepath):
        msg = f"File not found: {desc} at {filepath}"
        logging.error(f"ASSERTION FAILED: {msg}")
        raise AssertionError(msg)
    logging.info(f"File verified: {desc}")
    return filepath

def assert_data_not_empty(data, data_name):
    if not data or len(data) == 0:
        msg = f"No data found for: {data_name}"
        logging.error(f"ASSERTION FAILED: {msg}")
        raise AssertionError(msg)
    logging.info(f"Data validated: {data_name} contains {len(data)} items")
    return data

def assert_file_downloaded(filepath, min_size_bytes=1000, max_wait_seconds=30):
    import time
    waited = 0
    while waited < max_wait_seconds:
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            assert_with_log(size >= min_size_bytes, f"File {filepath} has valid size: {size} bytes")
            return filepath
        time.sleep(1)
        waited += 1
    raise AssertionError(f"File not downloaded after {max_wait_seconds}s: {filepath}")

# ============================================================================
# HTTP SESSION
# ============================================================================

def make_session():
    """Create a requests session with browser-like headers and automatic retries."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })
    # Retry on connection errors and 5xx responses (not 4xx — those are real failures)
    retry = Retry(
        total=3,
        backoff_factor=2,          # waits 2s, 4s, 8s between retries
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session

def fetch_soup(session, url, timeout=30):
    """Fetch a URL and return a BeautifulSoup object."""
    logging.info(f"Fetching: {url}")
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return BeautifulSoup(r.text, 'html.parser')

# ============================================================================
# HELPER: Month ordering for "latest month" detection
# ============================================================================

MONTH_ORDER = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12
}

def extract_year_month(link_text):
    """
    Extract (year, month_number) from link text like:
    'Participation Summary Report for the Month Ending January 31, 2026'
    Returns None if not matched.
    """
    match = re.search(
        r'(\w+)\s+\d{1,2},\s+(\d{4})',
        link_text,
        re.IGNORECASE
    )
    if match:
        month_name = match.group(1).lower()
        year = int(match.group(2))
        month_num = MONTH_ORDER.get(month_name)
        if month_num:
            return (year, month_num)
    return None

# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def find_latest_report(session):
    """
    Discover the latest available Participation Summary Report in one pass:

    1. Start from the current calendar year's page to get the full year list.
    2. Try years from highest to lowest until one has report links posted.
       This handles the case where a new year is listed in the nav but
       no reports have been uploaded yet.
    3. Return (latest_year, pdf_url, report_text).

    Falls back up to 4 years back on any HTTP or parsing failure.
    """
    current_year = datetime.now().year

    # Step 1 — fetch any reachable year page to get the site's full year list
    known_years = set()
    for attempt_year in range(current_year, current_year - 4, -1):
        url = f'{BASE_URL}/calsavers/reports/{attempt_year}/index.asp'
        try:
            soup = fetch_soup(session, url)
            for a in soup.find_all('a'):
                text = a.get_text(strip=True)
                if re.fullmatch(r'\d{4}', text):
                    known_years.add(int(text))
            if not known_years:
                known_years.add(attempt_year)
            logging.info(f"Year list discovered from page {attempt_year}: {sorted(known_years, reverse=True)}")
            break
        except Exception as e:
            logging.warning(f"Year {attempt_year} page failed: {e}")

    assert_with_log(bool(known_years), "Could not reach any CalSavers reports year page")

    # Step 2 — try years from highest to lowest until one has reports posted
    for year in sorted(known_years, reverse=True):
        url = f'{BASE_URL}/calsavers/reports/{year}/index.asp'
        try:
            soup = fetch_soup(session, url)
        except Exception as e:
            logging.warning(f"Could not fetch year {year} page: {e}")
            continue

        best = None  # (year, month_num, href, text)
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            if 'Participation Summary Report' not in text:
                continue
            href = a['href']
            if not href.startswith('http'):
                href = BASE_URL + href
            ym = extract_year_month(text)
            if ym:
                logging.info(f"  Found report: {text} -> {href}")
                if best is None or ym > (best[0], best[1]):
                    best = (ym[0], ym[1], href, text)
            else:
                logging.warning(f"  Could not parse date from: {text}")

        if best:
            _, _, href, text = best
            logging.info(f"Latest report year  : {year}")
            logging.info(f"Latest report selected: {text}")
            logging.info(f"URL: {href}")
            return year, href, text

        logging.warning(f"Year {year} page has no reports yet — trying prior year")

    raise AssertionError(
        "No Participation Summary Report links found on any year page. "
        "The site structure may have changed."
    )


def download_pdf(session, pdf_url, report_text):
    """
    Download the PDF using the shared session.
    Saves to downloads/ with a filename derived from the report link text.
    """
    match = re.search(r'(\w+)\s+\d{1,2},\s+(\d{4})', report_text, re.IGNORECASE)
    if match:
        month = match.group(1).lower()
        year = match.group(2)
        filename = f"participation_summary_{month}_{year}.pdf"
    else:
        filename = f"participation_summary_{timestamp}.pdf"

    filepath = os.path.join('downloads', filename)
    logging.info(f"Downloading PDF: {pdf_url}")
    logging.info(f"Saving to: {filepath}")

    r = session.get(pdf_url, timeout=60, stream=True)
    assert_with_log(
        r.status_code == 200,
        f"HTTP 200 received for PDF download (got {r.status_code})"
    )

    with open(filepath, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    size = os.path.getsize(filepath)
    logging.info(f"Download complete. File size: {size:,} bytes")
    assert_file_downloaded(filepath, min_size_bytes=1000)
    return filepath


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    logging.info("=" * 60)
    logging.info(f"STARTING SCRIPT - {timestamp}")
    logging.info("=" * 60)

    session = make_session()

    try:
        # Steps 1 & 2: Find the latest year that has reports, get its latest report link
        latest_year, pdf_url, report_text = find_latest_report(session)

        # Step 3: Download the PDF
        saved_path = download_pdf(session, pdf_url, report_text)

        logging.info("=" * 60)
        logging.info("SCRAPER COMPLETED — starting mapper...")
        logging.info(f"Report : {report_text}")
        logging.info(f"PDF    : {saved_path}")
        logging.info("=" * 60)

        # Step 4: Run mapper (report_month auto-detected from filename)
        mapper_exit = calsavers_mapper.main(
            current_pdf_path=saved_path,
            report_month=None   # auto-detected from PDF filename
        )

        if mapper_exit == 0:
            logging.info("Pipeline completed: scrape -> download -> map -> output")
        elif mapper_exit == 2:
            logging.warning("Pipeline completed with SKIP: all fields identical to prior month")
        else:
            logging.error("Mapper returned an error — check mapper log for details")

        return mapper_exit

    except AssertionError as e:
        logging.error("=" * 60)
        logging.error("ASSERTION FAILED")
        logging.error(f"Error: {str(e)}")
        logging.error("=" * 60)
        return 1

    except Exception as e:
        logging.error("=" * 60)
        logging.error("UNEXPECTED ERROR OCCURRED")
        logging.error(f"Error Type: {type(e).__name__}")
        logging.error(f"Error Message: {str(e)}")
        logging.error("=" * 60)
        logging.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
