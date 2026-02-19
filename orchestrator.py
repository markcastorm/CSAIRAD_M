import os
import logging
import sys
import traceback
import requests
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
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

# ============================================================================
# CONFIGURATION
# ============================================================================
HEADLESS = True   # Set to False to watch the browser during execution

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

def get_latest_year_url(page):
    """
    Use Playwright to navigate to the base reports page and find the
    latest year link from the navigation list.
    """
    start_url = f'{BASE_URL}/calsavers/reports/2022/index.asp'
    logging.info(f"Navigating to: {start_url}")
    page.goto(start_url)
    page.wait_for_load_state('networkidle')

    # Collect all year links from the <ul> nav
    year_links = page.locator('ul li a[href*="/calsavers/reports/"]').all()
    assert_with_log(len(year_links) > 0, "At least one year link found in nav")

    best_year = 0
    best_href = None

    for link in year_links:
        href = link.get_attribute('href')
        text = link.inner_text().strip()
        match = re.search(r'/calsavers/reports/(\d{4})/', href)
        if match:
            year = int(match.group(1))
            if year > best_year:
                best_year = year
                best_href = href

    assert_with_log(best_href is not None, "Latest year link found")
    logging.info(f"Latest year found: {best_year} -> {best_href}")
    return best_year, BASE_URL + best_href


def get_latest_participation_report_url(page, year_index_url):
    """
    Navigate to the latest year's index page via Playwright,
    then find all 'Participation Summary Report' links and pick the latest month.
    """
    logging.info(f"Navigating to year index: {year_index_url}")
    page.goto(year_index_url)
    page.wait_for_load_state('networkidle')

    # Find all links whose text contains 'Participation Summary Report'
    report_links = page.locator('a:has-text("Participation Summary Report")').all()
    assert_with_log(len(report_links) > 0, "At least one Participation Summary Report link found")
    logging.info(f"Found {len(report_links)} Participation Summary Report link(s)")

    best = None  # (year, month_num, href, text)

    for link in report_links:
        href = link.get_attribute('href')
        text = link.inner_text().strip()
        ym = extract_year_month(text)
        if ym:
            year, month_num = ym
            if best is None or (year, month_num) > (best[0], best[1]):
                best = (year, month_num, href, text)
            logging.info(f"  Found report: {text} -> {href}")
        else:
            logging.warning(f"  Could not parse date from: {text}")

    assert_with_log(best is not None, "At least one parseable Participation Summary Report found")
    _, _, href, text = best
    full_url = BASE_URL + href if href.startswith('/') else href
    logging.info(f"Latest report selected: {text}")
    logging.info(f"URL: {full_url}")
    return full_url, text


def download_pdf_with_requests(pdf_url, report_text):
    """
    Download the PDF using requests (not Playwright).
    Saves to downloads/ with a descriptive filename derived from the link text.
    """
    # Build filename from report text, e.g. "january_2026.pdf"
    match = re.search(r'(\w+)\s+\d{1,2},\s+(\d{4})', report_text, re.IGNORECASE)
    if match:
        month = match.group(1).lower()
        year = match.group(2)
        filename = f"participation_summary_{month}_{year}.pdf"
    else:
        filename = f"participation_summary_{timestamp}.pdf"

    filepath = os.path.join('downloads', filename)
    logging.info(f"Downloading PDF via requests: {pdf_url}")
    logging.info(f"Saving to: {filepath}")

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    response = requests.get(pdf_url, headers=headers, timeout=60, stream=True)
    assert_with_log(
        response.status_code == 200,
        f"HTTP 200 received for PDF download (got {response.status_code})"
    )

    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=HEADLESS)
        page = browser.new_page()

        try:
            # Step 1: Find the latest year
            latest_year, year_index_url = get_latest_year_url(page)

            # Step 2: On that year's page, find the latest monthly report link
            pdf_url, report_text = get_latest_participation_report_url(page, year_index_url)

            # Step 3: Download with requests
            saved_path = download_pdf_with_requests(pdf_url, report_text)

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
            try:
                page.screenshot(path=f'logs/error_{timestamp}_assertion.png')
                logging.error(f"Screenshot saved: logs/error_{timestamp}_assertion.png")
            except Exception:
                pass
            return 1

        except Exception as e:
            logging.error("=" * 60)
            logging.error("UNEXPECTED ERROR OCCURRED")
            logging.error(f"Error Type: {type(e).__name__}")
            logging.error(f"Error Message: {str(e)}")
            logging.error("=" * 60)
            logging.error(traceback.format_exc())
            try:
                page.screenshot(path=f'logs/error_{timestamp}_unexpected.png')
                logging.error(f"Screenshot saved: logs/error_{timestamp}_unexpected.png")
            except Exception:
                pass
            return 1

        finally:
            browser.close()
            logging.info("Browser closed")


if __name__ == "__main__":
    sys.exit(main())
