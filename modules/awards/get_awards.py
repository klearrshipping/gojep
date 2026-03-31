"""
GOJEP Contract Awards Extractor

Navigates to the contract awards page and extracts the tabular listing:
Procurement Method, PE, Title, Contract amount, Date, and Notice PDF link.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from config import settings as config
from tools.captcha.solve_captcha import CaptchaSolver

logger = logging.getLogger(__name__)

# Match tenders pagination: Next in .Pagination, enabled when not .Disabled
_AWARDS_NEXT_XPATH = (
    "//div[contains(@class,'Pagination')]//button[@title='Next' and not(contains(@class, 'Disabled'))]"
)


@dataclass
class AwardRow:
    row_number: Optional[str]
    procurement_method: Optional[str]
    procuring_entity: Optional[str]
    title: Optional[str]
    contract_amount: Optional[float]
    contract_amount_raw: Optional[str]
    award_date: Optional[str]  # ISO 8601 if parsed, else raw
    award_date_raw: Optional[str]
    contract_url: Optional[str]
    resource_id: Optional[str]
    pdf_url: Optional[str]
    pdf_resource_id: Optional[str]
    extraction_timestamp: str
    source_url: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_number": self.row_number,
            "procurement_method": self.procurement_method,
            "procuring_entity": self.procuring_entity,
            "title": self.title,
            "contract_amount": self.contract_amount,
            "contract_amount_raw": self.contract_amount_raw,
            "award_date": self.award_date,
            "award_date_raw": self.award_date_raw,
            "contract_url": self.contract_url,
            "resource_id": self.resource_id,
            "pdf_url": self.pdf_url,
            "pdf_resource_id": self.pdf_resource_id,
            "extraction_timestamp": self.extraction_timestamp,
            "source_url": self.source_url,
        }


class GOJEPAwardsScraper:
    def __init__(self) -> None:
        self.driver: Optional[webdriver.Chrome] = None
        self.captcha_solver: Optional[CaptchaSolver] = None
        self._init_logging()

        if getattr(config, "OPENROUTER_API_KEY", None):
            try:
                self.captcha_solver = CaptchaSolver()
            except Exception as e:  # noqa: BLE001
                # CAPTCHA solving will be skipped until we detect a CAPTCHA.
                logger.warning("Captcha solver init failed; will fail later if CAPTCHA appears: %s", e)

    def _init_logging(self) -> None:
        level = getattr(logging, str(getattr(config, "LOG_LEVEL", "INFO")).upper(), logging.INFO)
        log_file = getattr(config, "LOG_FILE", None)
        log_dir = os.path.dirname(os.path.abspath(log_file)) if log_file else None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        handlers: List[logging.Handler] = [logging.StreamHandler()]
        if log_file:
            handlers.insert(0, logging.FileHandler(log_file))

        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=handlers,
        )

    def setup_driver(self) -> None:
        chrome_options = Options()
        if getattr(config, "HEADLESS_MODE", False):
            chrome_options.add_argument("--headless")

        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument(f"--window-size={getattr(config, 'BROWSER_WIDTH', 1920)},{getattr(config, 'BROWSER_HEIGHT', 1080)}")
        chrome_options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )

        driver_path = ChromeDriverManager().install()
        if driver_path.lower().endswith("third_party_notices.chromedriver"):
            driver_path = os.path.join(os.path.dirname(driver_path), "chromedriver.exe")
        service = Service(driver_path)
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.driver.implicitly_wait(getattr(config, "IMPLICIT_WAIT", 10))
        self.driver.set_page_load_timeout(getattr(config, "PAGE_LOAD_TIMEOUT", 30))

    def cleanup(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None

    def navigate_to_awards(self) -> None:
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        logger.info("Navigating to contract award page: %s", config.CONTRACT_AWARD_URL)
        self.driver.get(config.CONTRACT_AWARD_URL)

        # Wait for CAS login, CAPTCHA, or the results table.
        WebDriverWait(self.driver, getattr(config, "SELENIUM_TIMEOUT", 30)).until(
            lambda d: d.find_elements(By.ID, "LoginForm")
            or d.find_elements(By.ID, "T01")
            or d.find_elements(By.ID, "CAPTCHA")
            or d.find_elements(By.ID, "Captcha")
            or d.find_elements(By.CLASS_NAME, "Captcha")
        )

    def maybe_login(self) -> None:
        """
        If the CAS login form (#LoginForm) is present, submit credentials.

        If not logged in and credentials are missing, raises RuntimeError.
        If no login page, no-op.
        """
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        if not self.driver.find_elements(By.ID, "LoginForm"):
            return

        username = getattr(config, "GOJEP_USERNAME", None) or ""
        password = getattr(config, "GOJEP_PASSWORD", None) or ""
        if not username.strip() or not password:
            raise RuntimeError(
                "CAS login page detected but GOJEP_USERNAME / GOJEP_PASSWORD are not set "
                "(configure .env or Secret Manager)."
            )

        logger.info("CAS login page detected; submitting credentials")
        wait = WebDriverWait(self.driver, getattr(config, "SELENIUM_TIMEOUT", 30))
        user_el = wait.until(EC.presence_of_element_located((By.ID, "Username")))
        pass_el = self.driver.find_element(By.ID, "Password")
        user_el.clear()
        user_el.send_keys(username)
        pass_el.clear()
        pass_el.send_keys(password)

        form = self.driver.find_element(By.ID, "LoginForm")
        form.submit()

        timeout = getattr(config, "SELENIUM_TIMEOUT", 30)
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: bool(
                    d.find_elements(By.ID, "T01")
                    or d.find_elements(By.ID, "CAPTCHA")
                    or d.find_elements(By.ID, "Captcha")
                    or d.find_elements(By.CLASS_NAME, "Captcha")
                )
            )
        except TimeoutException as e:
            if self.driver.find_elements(By.ID, "LoginForm"):
                raise RuntimeError(
                    "Login did not complete: still on CAS login page (check credentials)."
                ) from e
            raise

        logger.info("Login step finished; proceeding to awards or CAPTCHA")

    def maybe_solve_captcha(self) -> None:
        """
        If the page shows a CAPTCHA field/image, solve it and submit.

        If CAPTCHA isn't present, this is a no-op.
        """
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        wait = WebDriverWait(self.driver, 3)
        captcha_present = False
        try:
            wait.until(lambda d: bool(d.find_elements(By.ID, "Captcha") or d.find_elements(By.ID, "CAPTCHA") or d.find_elements(By.CLASS_NAME, "Captcha")))
            captcha_present = True
        except TimeoutException:
            captcha_present = False

        if not captcha_present:
            return

        if not self.captcha_solver:
            raise RuntimeError("CAPTCHA detected but CaptchaSolver is not available (OPENROUTER_API_KEY missing or init failed).")

        logger.info("CAPTCHA detected; solving...")
        solution = self.captcha_solver.solve_captcha(self.driver)
        self.captcha_solver.input_captcha_solution(self.driver, solution)

        # Try to submit using a submit input/button, otherwise submit the captcha input itself.
        submitted = False
        try:
            submit_button = self.driver.find_element(By.XPATH, "//input[@type='submit'] | //button[@type='submit']")
            submit_button.click()
            submitted = True
        except NoSuchElementException:
            pass

        if not submitted:
            try:
                captcha_input = self.driver.find_element(By.ID, "Captcha")
                captcha_input.submit()
            except NoSuchElementException:
                # Last resort: refresh.
                self.driver.refresh()

        # Wait for the table results.
        WebDriverWait(self.driver, getattr(config, "SELENIUM_TIMEOUT", 30)).until(
            EC.presence_of_element_located((By.ID, "T01"))
        )

    def _set_results_per_page_100(self) -> None:
        """Same pattern as tenders: #T01_pss → 100, wait for table refresh."""
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        timeout = getattr(config, "SELENIUM_TIMEOUT", 30)
        logger.info("Setting awards results per page to 100")
        dropdown = WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.ID, "T01_pss"))
        )
        current_table = self.driver.find_element(By.ID, "T01")
        Select(dropdown).select_by_value("100")
        WebDriverWait(self.driver, timeout).until(EC.staleness_of(current_table))
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.ID, "T01"))
        )

    @staticmethod
    def _to_absolute(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return config.GOJEP_BASE_URL + url
        return f"{config.GOJEP_BASE_URL}/{url}"

    @staticmethod
    def _extract_resource_id(url: Optional[str]) -> Optional[str]:
        if not url or "resourceId=" not in url:
            return None
        return url.split("resourceId=", 1)[1].split("&", 1)[0]

    @staticmethod
    def _parse_contract_amount(raw: Optional[str]) -> Optional[float]:
        if not raw:
            return None
        # Example: "23,561,465.89"
        cleaned = raw.strip()
        cleaned = cleaned.replace(",", "")
        cleaned = re.sub(r"[^\d.\-]", "", cleaned)
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _parse_award_datetime(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """
        Returns (iso_value_or_raw, original_raw).
        """
        if not raw:
            return None, None
        raw = " ".join(str(raw).split())

        # Example: "25/03/2026 12:09:10"
        m = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})", raw)
        if not m:
            return raw, raw

        dt_str = f"{m.group(1)} {m.group(2)}"
        try:
            dt = datetime.strptime(dt_str, "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.isoformat(), raw
        except ValueError:
            return raw, raw

    def extract_awards_from_current_page(self, latest_award_date: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], bool]:
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        WebDriverWait(self.driver, getattr(config, "SELENIUM_TIMEOUT", 30)).until(
            EC.presence_of_element_located((By.ID, "T01"))
        )

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        table = soup.find("table", {"id": "T01"})
        if not table:
            logger.warning("Could not find awards table#T01")
            return []

        tbody = table.find("tbody")
        if not tbody:
            return []

        extracted: List[Dict[str, Any]] = []
        should_stop = False
        rows_tr = tbody.find_all("tr")
        for row in rows_tr:
            cells = row.find_all("td")
            # Expected cells: 0 row#, 1 procurement method, 2 PE, 3 title, 4 amount, 5 date, 6 pdf
            if len(cells) < 6:
                continue

            row_number = cells[0].get_text(strip=True) if len(cells) > 0 else None
            procurement_method = cells[1].get_text(" ", strip=True) if len(cells) > 1 else None
            procuring_entity = cells[2].get_text(" ", strip=True) if len(cells) > 2 else None

            title = None
            contract_url = None
            resource_id = None
            title_cell = cells[3] if len(cells) > 3 else None
            if title_cell:
                title_link = title_cell.find("a")
                if title_link:
                    title = title_link.get_text(" ", strip=True)
                    contract_url = self._to_absolute(title_link.get("href"))
                    resource_id = self._extract_resource_id(contract_url)
                else:
                    title = title_cell.get_text(" ", strip=True)

            contract_amount_raw = cells[4].get_text(" ", strip=True) if len(cells) > 4 else None
            contract_amount = self._parse_contract_amount(contract_amount_raw)

            award_date_raw = cells[5].get_text(" ", strip=True) if len(cells) > 5 else None
            award_date, award_date_raw = self._parse_award_datetime(award_date_raw)

            if latest_award_date and award_date:
                try:
                    row_dt = datetime.fromisoformat(award_date)
                    if row_dt <= latest_award_date:
                        logger.info(f"Watermark hit: Award date {award_date} is older than or equal to latest record {latest_award_date.isoformat()}. Stopping.")
                        should_stop = True
                        break
                except ValueError:
                    pass

            pdf_url = None
            pdf_resource_id = None
            if len(cells) > 6:
                pdf_cell = cells[6]
                pdf_link = pdf_cell.find("a")
                if pdf_link and pdf_link.get("href"):
                    pdf_url = self._to_absolute(pdf_link.get("href"))
                    pdf_resource_id = self._extract_resource_id(pdf_url)

            now_iso = datetime.now(timezone.utc).isoformat()
            extracted.append(
                AwardRow(
                    row_number=row_number or None,
                    procurement_method=procurement_method or None,
                    procuring_entity=procuring_entity or None,
                    title=title or None,
                    contract_amount=contract_amount,
                    contract_amount_raw=contract_amount_raw or None,
                    award_date=award_date,
                    award_date_raw=award_date_raw,
                    contract_url=contract_url,
                    resource_id=resource_id,
                    pdf_url=pdf_url,
                    pdf_resource_id=pdf_resource_id,
                    extraction_timestamp=now_iso,
                    source_url=self.driver.current_url,
                ).to_dict()
            )

        logger.info("Extracted %s award rows from current page", len(extracted))
        return extracted, should_stop

    def extract_all_awards(self, max_pages: Optional[int] = 1, latest_award_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        If max_pages is None: scrape until pagination ends or watermark reached.
        If max_pages == 1: only current page.
        """
        if not self.driver:
            raise ValueError("WebDriver not initialized")

        timeout = getattr(config, "SELENIUM_TIMEOUT", 30)
        all_rows: List[Dict[str, Any]] = []
        current_page = 1
        while True:
            logger.info("Extracting awards page %s", current_page)
            page_rows, should_stop = self.extract_awards_from_current_page(latest_award_date)
            all_rows.extend(page_rows)
            
            if should_stop:
                logger.info("Watermark reached. Halting pagination.")
                break

            if max_pages is not None and current_page >= max_pages:
                break

            current_table = self.driver.find_element(By.ID, "T01")
            next_buttons = self.driver.find_elements(By.XPATH, _AWARDS_NEXT_XPATH)
            if not next_buttons:
                break
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_buttons[0])
            next_buttons[0].click()
            WebDriverWait(self.driver, timeout).until(EC.staleness_of(current_table))
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.ID, "T01"))
            )
            current_page += 1

        return all_rows

    def _requests_session_from_driver(self) -> requests.Session:
        session = requests.Session()
        if not self.driver:
            return session
        try:
            ua = self.driver.execute_script("return navigator.userAgent")
        except Exception:  # noqa: BLE001
            ua = None
        if ua:
            session.headers["User-Agent"] = ua
        for c in self.driver.get_cookies():
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or None,
                path=c.get("path") or "/",
            )
        return session

    @staticmethod
    def _response_body_looks_like_pdf(content: bytes) -> bool:
        return bool(content and len(content) >= 4 and content[:4] == b"%PDF")

    @staticmethod
    def _sanitize_pdf_name_part(value: Any) -> str:
        if value is None:
            return ""
        s = str(value).strip()
        if not s:
            return ""
        return re.sub(r"[^\w\-.]+", "_", s)

    @staticmethod
    def _pdf_filename_and_dedup_key(row: Dict[str, Any], index: int) -> tuple[str, str]:
        """
        Build a filesystem-safe name tied to the extracted row: row number, contract resource id,
        notice resource id. Row numbers repeat each page; contract + notice ids keep names unique.

        Returns (filename ending in .pdf, key for deduplicating the same notice PDF).
        """
        row_number = row.get("row_number")
        contract_id = row.get("resource_id")
        notice_id = row.get("pdf_resource_id")

        parts: List[str] = []
        rp = GOJEPAwardsScraper._sanitize_pdf_name_part(row_number)
        if rp:
            parts.append(f"row{rp}")
        cp = GOJEPAwardsScraper._sanitize_pdf_name_part(contract_id)
        if cp:
            parts.append(f"contract{cp}")
        np = GOJEPAwardsScraper._sanitize_pdf_name_part(notice_id)
        if np:
            parts.append(f"notice{np}")
        if not parts:
            parts.append(f"award_index{index}")

        filename = "_".join(parts) + ".pdf"
        dedup_key = str(notice_id or contract_id or f"idx_{index}")
        return filename, dedup_key

    def download_pdfs_for_awards(
        self,
        awards: List[Dict[str, Any]],
        awards_output_dir: Optional[str] = None,
        resume: bool = False,
    ) -> None:
        """
        Download notice PDFs using the current browser session cookies (must run before quit).

        Mutates each award dict: sets pdf_local_relpath on success, pdf_download_error on failure.

        If ``resume`` is True, PDFs that already exist on disk are linked without re-fetching and
        without the inter-request delay (for continuing an interrupted bulk download).
        """
        if not self.driver or not awards:
            return

        base_dir = awards_output_dir or config.AWARDS_OUTPUT_DIRECTORY
        pdf_subdir = getattr(config, "AWARDS_PDF_SUBDIR", "pdf")
        pdf_dir = os.path.join(base_dir, pdf_subdir)
        os.makedirs(pdf_dir, exist_ok=True)

        delay = float(getattr(config, "AWARDS_PDF_DOWNLOAD_DELAY_SEC", 0.2))
        timeout = int(getattr(config, "AWARDS_PDF_DOWNLOAD_TIMEOUT", 120))
        session = self._requests_session_from_driver()
        try:
            session.headers["Referer"] = self.driver.current_url
        except Exception:  # noqa: BLE001
            pass

        if resume:
            logger.info(
                "Resume mode: existing files under %s are reused with no delay; missing PDFs are downloaded.",
                pdf_dir,
            )

        relpath_by_notice_id: dict[str, str] = {}
        ok = 0
        failed = 0
        skipped = 0
        attempted = 0
        skipped_existing = 0

        for i, row in enumerate(awards):
            url = row.get("pdf_url")
            if not url:
                skipped += 1
                continue

            filename, notice_key = self._pdf_filename_and_dedup_key(row, i)
            if notice_key in relpath_by_notice_id:
                row["pdf_local_relpath"] = relpath_by_notice_id[notice_key]
                row.pop("pdf_download_error", None)
                continue

            dest_path = os.path.join(pdf_dir, filename)
            rel = os.path.join(pdf_subdir, filename).replace("\\", "/")

            if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
                row["pdf_local_relpath"] = rel
                row.pop("pdf_download_error", None)
                relpath_by_notice_id[notice_key] = rel
                ok += 1
                skipped_existing += 1
                if delay and not resume:
                    time.sleep(delay)
                continue

            try:
                resp = session.get(url, timeout=timeout, allow_redirects=True)
                attempted += 1
                if resp.status_code != 200:
                    row["pdf_download_error"] = f"HTTP {resp.status_code}"
                    failed += 1
                    logger.warning("PDF download failed %s (%s): %s", filename, notice_key, row["pdf_download_error"])
                    continue
                body = resp.content
                if not self._response_body_looks_like_pdf(body):
                    row["pdf_download_error"] = "response is not a PDF"
                    failed += 1
                    logger.warning("PDF download not PDF bytes for %s (%s)", filename, notice_key)
                    continue
                with open(dest_path, "wb") as f:
                    f.write(body)
                row["pdf_local_relpath"] = rel
                row.pop("pdf_download_error", None)
                relpath_by_notice_id[notice_key] = rel
                ok += 1
            except requests.RequestException as e:
                attempted += 1
                row["pdf_download_error"] = str(e)
                failed += 1
                logger.warning("PDF download error %s (%s): %s", filename, notice_key, e)

            if delay:
                time.sleep(delay)

            if attempted % 200 == 0 and attempted > 0:
                logger.info("PDF downloads progress: ok=%s failed=%s skipped_no_url=%s", ok, failed, skipped)

        logger.info(
            "PDF downloads finished: linked_or_saved=%s (already_on_disk=%s) failed=%s rows_without_pdf_url=%s",
            ok,
            skipped_existing,
            failed,
            skipped,
        )

    def save_awards(self, awards: List[Dict[str, Any]], output_dir: Optional[str] = None) -> str:
        output_dir = output_dir or config.AWARDS_OUTPUT_DIRECTORY
        os.makedirs(output_dir, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(output_dir, f"awards_{ts}.json")

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(awards, f, ensure_ascii=False, indent=2)

        logger.info("Saved %s awards to %s", len(awards), out_path)
        return out_path

    def run(
        self,
        max_pages: Optional[int] = 1,
        save_json: bool = True,
        download_pdfs: bool = True,
        resume_pdfs: bool = False,
        latest_award_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        self.setup_driver()
        try:
            self.navigate_to_awards()
            self.maybe_login()
            self.maybe_solve_captcha()
            self._set_results_per_page_100()
            awards = self.extract_all_awards(max_pages=max_pages, latest_award_date=latest_award_date)
            if download_pdfs:
                logger.info("Downloading notice PDFs for %s awards (session still open)", len(awards))
                self.download_pdfs_for_awards(awards, resume=resume_pdfs)
            if save_json:
                self.save_awards(awards)
            return awards
        finally:
            self.cleanup()

    def run_download_pdfs_only(
        self,
        awards: List[Dict[str, Any]],
        save_json: bool = True,
        resume: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Establish a logged-in browser session, then download PDFs for rows in ``awards``
        (typically loaded from a prior ``awards_*.json`` extract). Skips table scraping.
        """
        self.setup_driver()
        try:
            self.navigate_to_awards()
            self.maybe_login()
            self.maybe_solve_captcha()
            self._set_results_per_page_100()
            logger.info("PDF-only mode: downloading notice PDFs for %s awards", len(awards))
            self.download_pdfs_for_awards(awards, resume=resume)
            if save_json:
                self.save_awards(awards)
            return awards
        finally:
            self.cleanup()


def _load_awards_json(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Awards JSON must be a JSON array")
    return data


def _latest_awards_json_path() -> Optional[str]:
    d = config.AWARDS_OUTPUT_DIRECTORY
    if not os.path.isdir(d):
        return None
    candidates = [
        os.path.join(d, name)
        for name in os.listdir(d)
        if name.startswith("awards_") and name.endswith(".json")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GOJEP Contract Awards Extractor")
    parser.add_argument("--max-pages", type=int, default=1, help="Pages to scrape (0 = all pages)")
    parser.add_argument("--no-save", action="store_true", help="Do not write JSON to disk")
    parser.add_argument("--no-pdf", action="store_true", help="Do not download notice PDFs")
    parser.add_argument(
        "--pdf-only",
        action="store_true",
        help="Skip scraping: load awards from --input-json (or latest awards_*.json) and download PDFs only",
    )
    parser.add_argument(
        "--input-json",
        type=str,
        default=None,
        help="Path to awards JSON (used with --pdf-only; default: newest awards_*.json in data/awards)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="With PDF download: reuse files already in data/awards/pdf/ with no delay, fetch only missing PDFs",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    scraper = GOJEPAwardsScraper()

    if args.resume and args.no_pdf:
        raise SystemExit("--resume only applies when PDFs are downloaded (do not use with --no-pdf)")

    if args.pdf_only:
        if args.no_pdf:
            raise SystemExit("Cannot combine --pdf-only with --no-pdf")
        input_path = args.input_json or _latest_awards_json_path()
        if not input_path:
            raise SystemExit(
                "No awards JSON found. Place awards_*.json under data/awards or pass --input-json PATH"
            )
        if not os.path.isfile(input_path):
            raise SystemExit(f"Input file not found: {input_path}")
        logger.info("Loading awards from %s", input_path)
        awards = _load_awards_json(input_path)
        awards = scraper.run_download_pdfs_only(
            awards, save_json=not args.no_save, resume=args.resume
        )
        print(f"Loaded {len(awards)} awards from {input_path}; PDF download pass complete.")
    else:
        max_pages: Optional[int]
        if args.max_pages == 0:
            max_pages = None
        else:
            max_pages = max(1, args.max_pages)

        awards = scraper.run(
            max_pages=max_pages,
            save_json=not args.no_save,
            download_pdfs=not args.no_pdf,
            resume_pdfs=args.resume,
        )
        print(f"Extracted {len(awards)} awards.")

    preview = awards[:3]
    print(json.dumps(preview, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

