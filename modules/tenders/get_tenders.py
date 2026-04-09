"""
GOJEP tender listings extractor.

Flow:
1) Open current opportunities URL
2) Solve CAPTCHA
3) Set results per page to 100
4) Extract listing rows (including title URL)
5) Ignore tenders published within last 24 hours (Jamaica time)
6) Save extracted rows to JSON
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from config import settings as config
from tools.captcha.solve_captcha import CaptchaSolver
from db.tender_row_mapping import resource_id_from_url

logger = logging.getLogger(__name__)

JAMAICA_TZ = ZoneInfo("America/Jamaica")


class GOJEPScraper:
    def __init__(self) -> None:
        self.driver: Optional[webdriver.Chrome] = None
        self.captcha_solver = CaptchaSolver()
        self._setup_logging()
        os.makedirs(config.TENDERS_OUTPUT_DIRECTORY, exist_ok=True)

    def _setup_logging(self) -> None:
        level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler()],
        )

    def _setup_driver(self, headless: Optional[bool] = None, download_dir: Optional[str] = None) -> None:
        options = Options()
        use_headless = config.HEADLESS_MODE if headless is None else headless
        if use_headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--allow-running-insecure-content")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--disable-features=InsecureDownloadWarnings")
        options.add_argument("--disable-extensions")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-in-process-stack-traces")
        options.add_argument("--disable-logging")
        options.add_argument("--disable-dev-tools")
        options.add_argument("--shm-size=2gb")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if download_dir:
            os.makedirs(download_dir, exist_ok=True)
            # Chrome expects an absolute path; forward slashes are safest cross-platform, but Windows might prefer backslashes
            dd = os.path.normpath(os.path.abspath(download_dir)).replace("/", "\\")
            options.add_experimental_option(
                "prefs",
                {
                    "download.default_directory": dd,
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "safebrowsing.enabled": False,
                    "safebrowsing.disable_download_protection": True,
                    "profile.default_content_settings.popups": 0,
                    "profile.default_content_setting_values.automatic_downloads": 1,
                    "plugins.always_open_pdf_externally": True,
                },
            )
        driver_path = ChromeDriverManager().install()
        # webdriver_manager may occasionally return a non-executable file path on Windows.
        if driver_path.lower().endswith("third_party_notices.chromedriver"):
            driver_path = os.path.join(os.path.dirname(driver_path), "chromedriver.exe")
        service = Service(driver_path)
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.implicitly_wait(config.IMPLICIT_WAIT)
        self.driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        if not use_headless:
            try:
                self.driver.maximize_window()
            except Exception:  # noqa: BLE001
                pass

    def _navigate_and_submit_captcha(self) -> None:
        if not self.driver:
            raise ValueError("Driver not initialized")

        logger.info("Navigating to opportunities page")
        self.driver.get(config.CURRENT_OPPORTUNITIES_URL)

        WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "Captcha"))
        )

        solution = self.captcha_solver.solve_captcha(self.driver)
        self.captcha_solver.input_captcha_solution(self.driver, solution)

        try:
            submit_button = self.driver.find_element(By.XPATH, "//input[@type='submit'] | //button[@type='submit']")
            submit_button.click()
        except NoSuchElementException:
            captcha_input = self.driver.find_element(By.ID, "Captcha")
            captcha_input.submit()

        WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "CFTResults"))
        )

    def _set_results_per_page_100(self) -> None:
        if not self.driver:
            raise ValueError("Driver not initialized")

        logger.info("Setting results per page to 100")
        dropdown = WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "T01_pss"))
        )
        current_results = self.driver.find_element(By.ID, "CFTResults")
        Select(dropdown).select_by_value("100")
        WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(EC.staleness_of(current_results))
        WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "CFTResults"))
        )

    @staticmethod
    def _to_absolute(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return f"{config.GOJEP_BASE_URL}{url}"
        return f"{config.GOJEP_BASE_URL}/{url}"

    @staticmethod
    def _parse_date(raw_value: str) -> Optional[datetime]:
        try:
            # Source dates are Jamaica local timestamps in format DD/MM/YYYY HH:MM:SS
            dt = datetime.strptime(raw_value.strip(), "%d/%m/%Y %H:%M:%S")
            return dt.replace(tzinfo=JAMAICA_TZ)
        except Exception:  # noqa: BLE001
            return None

    def _extract_rows(self, latest_publication_date: Optional[datetime] = None, known_active_ids: Optional[set[str]] = None) -> tuple[List[Dict[str, Any]], bool]:
        if not self.driver:
            raise ValueError("Driver not initialized")
            
        if known_active_ids is None:
            known_active_ids = set()

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        container = soup.find("div", {"id": "CFTResults"})
        if not container:
            return []

        table = container.find("table", {"id": "T01"})
        if not table:
            return []

        tbody = table.find("tbody")
        if not tbody:
            return []

        jamaica_now = datetime.now(JAMAICA_TZ)

        records: List[Dict[str, Any]] = []
        should_stop = False
        
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 10:
                continue

            # Publication Date Check First (for early exit watermark)
            publication_raw = cells[9].get_text(" ", strip=True)
            publication_dt = self._parse_date(publication_raw)
            
            if publication_dt and latest_publication_date:
                # If we encounter a tender older than our watermark, stop paginating completely.
                if publication_dt < latest_publication_date:
                    should_stop = True
                    break

            # Deadline Check
            deadline_raw = cells[4].get_text(" ", strip=True)
            deadline_dt = self._parse_date(deadline_raw)
            
            if deadline_dt and deadline_dt < jamaica_now:
                continue

            title_link = cells[1].find("a")
            title = title_link.get_text(strip=True) if title_link else cells[1].get_text(strip=True)
            title_url = self._to_absolute(title_link.get("href")) if title_link else None

            # Known ID Delta Check
            res_id = resource_id_from_url(title_url)
            if res_id and res_id in known_active_ids:
                continue

            info_img = cells[3].find("img")
            description = info_img.get("title", "").strip() if info_img else None

            notice_link = cells[8].find("a")
            notice_pdf_url = self._to_absolute(notice_link.get("href")) if notice_link else None

            records.append(
                {
                    "row_number": cells[0].get_text(strip=True),
                    "title": title,
                    "title_url": title_url,
                    "procuring_entity": cells[2].get_text(" ", strip=True),
                    "description": description,
                    "bids_submission_deadline": deadline_raw,
                    "procurement_type": cells[5].get_text(" ", strip=True),
                    "procedure": cells[6].get_text(" ", strip=True),
                    "status": cells[7].get_text(" ", strip=True) or None,
                    "notice_pdf_url": notice_pdf_url,
                    "publication_date": publication_raw,
                    "publication_date_iso_jamaica": publication_dt.isoformat() if publication_dt else None,
                    "extracted_at_jamaica": jamaica_now.isoformat(),
                    "source_url": self.driver.current_url,
                }
            )

        return records, should_stop

    def _save_json(self, records: List[Dict[str, Any]]) -> str:
        ts = datetime.now(JAMAICA_TZ).strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, f"tenders_{ts}.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        return output_path

    def run_extraction(
        self,
        latest_publication_date: Optional[datetime] = None,
        known_active_ids: Optional[set[str]] = None,
        max_pages: Optional[int] = None,
    ) -> str:
        self._setup_driver()
        try:
            self._navigate_and_submit_captcha()
            self._set_results_per_page_100()
            records: List[Dict[str, Any]] = []
            pages_extracted = 0
            
            while True:
                page_records, should_stop = self._extract_rows(latest_publication_date, known_active_ids)
                records.extend(page_records)
                pages_extracted += 1
                
                if should_stop:
                    logger.info("Reached tenders older than latest publication watermark. Stopping extraction.")
                    break
                    
                if max_pages is not None and pages_extracted >= max_pages:
                    logger.info(f"Reached max pages limit ({max_pages}). Stopping extraction.")
                    break

                current_results = self.driver.find_element(By.ID, "CFTResults")
                next_buttons = self.driver.find_elements(
                    By.XPATH, "//button[@title='Next' and not(contains(@class, 'Disabled'))]"
                )
                if not next_buttons:
                    break
                next_buttons[0].click()
                WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(EC.staleness_of(current_results))
                WebDriverWait(self.driver, config.SELENIUM_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "CFTResults"))
                )
                
            if not records:
                logger.info("No new tenders found to save.")
                return ""
                
            output_path = self._save_json(records)
            logger.info("Saved %s records to %s", len(records), output_path)
            return output_path
        finally:
            if self.driver:
                self.driver.quit()


if __name__ == "__main__":
    scraper = GOJEPScraper()
    scraper.run_extraction()

