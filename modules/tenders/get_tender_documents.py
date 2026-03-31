"""
Download tender document ZIPs from GOJEP using a browser session (CAPTCHA + UI).

Flow:
1) Open GOJEP home, **Log in** via CAS if needed (``tools/login/gojep_login``).
2) Load records from tender_details_*.json.
3) For each record: open ``title_url``. If the page shows a CAPTCHA, solve and submit it;
   otherwise continue. Then use the page menu:
   Show Menu (#ToggleSubmenu) → "Competition documents" → "Contract documents" tab
   → "Download Zip file". The **Association with Competition** popup (``associateUserToCFT.jsp``,
   ``DownloadCftResourceItemsAction``) is filled by choosing **onlyMe** (or **allUser**) and **Select**,
   then the script returns to the main window while the ZIP downloads. Chrome saves into ``data/tenders/documents/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import NoAlertPresentException, NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import settings as config
from tools.captcha.solve_captcha import CaptchaSolver
from tools.login.gojep_login import ensure_gojep_logged_in

from .get_tenders import GOJEPScraper

logger = logging.getLogger(__name__)

DOCUMENTS_SUBDIR = "documents"


def _captcha_visible_on_page(driver: Any, wait_sec: float = 4.0) -> bool:
    """True if GOJEP-style CAPTCHA markup is present (listing or tender workspace)."""
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if (
            driver.find_elements(By.CLASS_NAME, "Captcha")
            or driver.find_elements(By.ID, "CAPTCHA")
            or driver.find_elements(By.ID, "Captcha")
        ):
            return True
        time.sleep(0.2)
    return False


def _handle_captcha_on_current_page(driver: Any, captcha_solver: CaptchaSolver, ui_wait: int) -> None:
    """
    After ``driver.get(...)``, if a CAPTCHA is shown, solve it and submit; then wait for tender UI.

    If no CAPTCHA, returns immediately. Mirrors submit behaviour from ``get_tenders._navigate_and_submit_captcha``
    but does not navigate to the current-opportunities listing.
    """
    poll_sec = min(10.0, max(4.0, float(ui_wait)))
    if not _captcha_visible_on_page(driver, wait_sec=poll_sec):
        logger.info("No CAPTCHA on this page; continuing to competition documents")
        return

    logger.info("CAPTCHA detected on tender page; solving")
    WebDriverWait(driver, ui_wait).until(
        lambda d: d.find_elements(By.CLASS_NAME, "Captcha")
        or d.find_elements(By.ID, "CAPTCHA")
        or d.find_elements(By.ID, "Captcha")
    )

    solution = captcha_solver.solve_captcha(driver)
    captcha_solver.input_captcha_solution(driver, solution)

    try:
        submit_button = driver.find_element(By.XPATH, "//input[@type='submit'] | //button[@type='submit']")
        submit_button.click()
    except NoSuchElementException:
        captcha_input = driver.find_element(By.ID, "Captcha")
        captcha_input.submit()

    # Tender workspace exposes the same menu used for ZIP download.
    WebDriverWait(driver, ui_wait).until(
        EC.presence_of_element_located((By.ID, "ToggleSubmenu"))
    )


def _resource_id_from_record(rec: Dict[str, Any]) -> Optional[str]:
    rid = rec.get("resource_id_from_url")
    if rid:
        return str(rid).strip() or None
    url = rec.get("title_url")
    if not url:
        return None
    q = parse_qs(urlparse(url).query)
    v = q.get("resourceId", [None])[0]
    return str(v).strip() if v else None


def _latest_tender_details_json() -> Optional[str]:
    d = config.TENDERS_OUTPUT_DIRECTORY
    if not os.path.isdir(d):
        return None
    candidates = [
        os.path.join(d, name)
        for name in os.listdir(d)
        if name.startswith("tender_details_") and name.endswith(".json")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_tender_detail_records(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "records" in data:
        rows = data["records"]
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("Expected tender_details JSON with a top-level 'records' array or a JSON array")
    if not isinstance(rows, list):
        raise ValueError("records must be a list")
    return [r for r in rows if isinstance(r, dict)]


def _wait_for_new_zip(download_dir: str, before_names: set[str], timeout_sec: float) -> Optional[str]:
    """Return path to a new .zip file that appears after a download click."""
    deadline = time.time() + timeout_sec
    abs_d = os.path.abspath(download_dir)
    while time.time() < deadline:
        try:
            names = os.listdir(abs_d)
        except OSError:
            time.sleep(0.3)
            continue
        if any(n.endswith(".crdownload") for n in names):
            time.sleep(0.35)
            continue
        for n in names:
            if n in before_names:
                continue
            if not n.lower().endswith(".zip"):
                continue
            path = os.path.join(abs_d, n)
            try:
                s1 = os.path.getsize(path)
                time.sleep(0.25)
                s2 = os.path.getsize(path)
                if s1 == s2 and s1 > 0:
                    return path
            except OSError:
                continue
        time.sleep(0.35)
    return None


def _try_accept_alert(driver: Any, log: bool = True) -> Optional[str]:
    """If a JS alert/confirm is open, accept it. Returns alert text or None."""
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        if log:
            logger.info("Accepted JS alert: %s", text[:200] if text else "")
        return text
    except NoAlertPresentException:
        return None


def _switch_to_new_window(driver: Any, main_handle: str, timeout_sec: float = 15.0) -> bool:
    """Wait for a second window/tab and switch to it. Returns True if switched."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        handles = driver.window_handles
        if len(handles) > 1:
            for h in handles:
                if h != main_handle:
                    driver.switch_to.window(h)
                    logger.info("Switched to new window: %s", driver.current_url[:120])
                    return True
        time.sleep(0.2)
    return False


def _complete_association_with_competition_popup(driver: Any, wait: WebDriverWait, main_handle: str) -> bool:
    """
    ``associateUserToCFT.jsp`` — "Association with Competition" popup opened before ZIP download.
    Radios ``selectedVal`` (onlyMe / allUser); ``Select`` runs ``addUser()`` → ``window.opener.setParentHref`` then ``window.close()``.
    """
    if not driver.find_elements(By.CSS_SELECTOR, "input#action[value='DownloadCftResourceItemsAction']"):
        return False

    logger.info("Association-with-competition popup detected (DownloadCftResourceItemsAction)")
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(0.3)

    chosen = False
    for val in ("onlyMe", "allUser"):
        try:
            r = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, f"input[name='selectedVal'][value='{val}']")))
            driver.execute_script("arguments[0].click();", r)
            chosen = True
            logger.info("Selected association option: %s", val)
            break
        except Exception:  # noqa: BLE001
            continue
    if not chosen:
        logger.warning("Could not click selectedVal radio (onlyMe/allUser); trying generic handlers")
        return False

    time.sleep(0.2)
    select_btn = wait.until(
        EC.element_to_be_clickable(
            (
                By.CSS_SELECTOR,
                "form[name='associateUserForm'] button.RedBTN, "
                "form[name='associateUserForm'] button[onclick*='addUser']",
            )
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", select_btn)
    select_btn.click()

    # Popup calls window.close(); parent continues the download.
    WebDriverWait(driver, 25).until(lambda d: len(d.window_handles) == 1)
    driver.switch_to.window(main_handle)
    time.sleep(0.5)
    logger.info("Returned to main window after association popup")
    return True


def _complete_selection_window(driver: Any, wait: WebDriverWait, main_handle: str) -> None:
    """
    On a popup that asks which documents to include: select all and confirm.
    Extend selectors here if GOJEP changes the markup.
    """
    if _complete_association_with_competition_popup(driver, wait, main_handle):
        return

    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.4)

    _lo = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _hi = "abcdefghijklmnopqrstuvwxyz"
    # "Select all" style controls (case-insensitive)
    for xpath in (
        f"//a[contains(translate(., '{_lo}', '{_hi}'), 'select all')]",
        f"//button[contains(translate(., '{_lo}', '{_hi}'), 'select all')]",
        f"//label[contains(translate(., '{_lo}', '{_hi}'), 'select all')]",
    ):
        try:
            el = driver.find_element(By.XPATH, xpath)
            if el.is_displayed():
                driver.execute_script("arguments[0].click();", el)
                time.sleep(0.25)
                logger.info("Clicked Select-all control")
                break
        except Exception:  # noqa: BLE001
            continue

    # Tick visible checkboxes that are not selected
    for cb in driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
        try:
            if cb.is_displayed() and cb.is_enabled() and not cb.is_selected():
                driver.execute_script("arguments[0].click();", cb)
        except Exception:  # noqa: BLE001
            continue

    time.sleep(0.2)

    # Confirm: Download / OK / Continue / Submit
    confirm_xpaths = (
        "//button[contains(., 'Download')]",
        "//input[@type='button' and contains(@value,'Download')]",
        "//input[@type='submit' and (contains(@value,'Download') or contains(@value,'OK'))]",
        "//button[@type='submit']",
        "//input[@type='submit']",
        "//button[contains(., 'OK') or contains(., 'Ok')]",
        "//button[contains(., 'Continue')]",
        "//a[contains(@href,'download') and contains(., 'Download')]",
    )
    for xpath in confirm_xpaths:
        try:
            el = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
            logger.info("Clicked confirmation control matching: %s", xpath[:80])
            time.sleep(0.4)
            return
        except Exception:  # noqa: BLE001
            continue

    logger.warning(
        "No confirmation control matched on selection window; URL=%s — adjust selectors if download hangs",
        driver.current_url[:200],
    )


def _close_extra_windows(driver: Any, main_handle: str) -> None:
    """Close all tabs except ``main_handle`` and switch back."""
    for h in list(driver.window_handles):
        if h != main_handle:
            try:
                driver.switch_to.window(h)
                driver.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        driver.switch_to.window(main_handle)
    except Exception:  # noqa: BLE001
        pass


def download_zip_via_ui(driver, download_dir: str, dest_zip_path: str, ui_wait: int, file_wait: int) -> None:
    """
    From the tender workspace (prepareViewCfTWS) page: open menu → Competition documents →
    Contract documents tab → Download Zip file.

    If a **new window** opens (file selection), switches to it, selects items / confirms, then
    waits for a new .zip in ``download_dir`` and moves it to ``dest_zip_path``. Extra tabs are
    closed after the file appears.
    """
    wait = WebDriverWait(driver, ui_wait)

    try:
        logger.info("[CHECKPOINT] Waiting for ToggleSubmenu")
        toggle = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.ID, "ToggleSubmenu")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", toggle)
        toggle.click()
        logger.info("[CHECKPOINT] Clicked ToggleSubmenu successfully")
        time.sleep(0.4)
    except TimeoutException:
        logger.info("ToggleSubmenu not found or not clickable; trying Competition documents link anyway")

    logger.info("[CHECKPOINT] Waiting for Competition documents link")
    comp_link = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(@href,'viewContractNotices.do')][contains(., 'Competition documents')]")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comp_link)
    comp_link.click()
    logger.info("[CHECKPOINT] Clicked 'Competition documents' link successfully")

    logger.info("[CHECKPOINT] Waiting for tabbernav")
    wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "ul.tabbernav, ul[class*='tabbernav']"))
    )

    logger.info("[CHECKPOINT] Waiting for Contract documents tab link")
    tab_link = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//ul[contains(@class,'tabbernav')]//a[contains(@href,'listContractDocuments.do')]",
            )
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab_link)
    tab_link.click()
    logger.info("[CHECKPOINT] Clicked 'Contract documents' tab link successfully")

    logger.info("[CHECKPOINT] Waiting for Download Zip button (dl_btn)")
    dl_btn = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[contains(@onclick,'downloadForAnonymousUser') or contains(., 'Download Zip')]",
            )
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dl_btn)

    main_handle = driver.current_window_handle
    before = set(os.listdir(os.path.abspath(download_dir)))
    logger.info("[CHECKPOINT] Clicking 'Download Zip' button")
    dl_btn.click()

    time.sleep(0.6)
    logger.info("[CHECKPOINT] After click: Checking for JS alert")
    # JS alert (e.g. confirm) can appear before or instead of a new window
    if _try_accept_alert(driver):
        time.sleep(0.3)

    if _switch_to_new_window(driver, main_handle, timeout_sec=20.0):
        logger.info("[CHECKPOINT] Switch to new window succeeded -> _complete_selection_window")
        # Stay on the popup until ZIP is triggered via opener; association popup closes itself.
        _complete_selection_window(driver, WebDriverWait(driver, ui_wait), main_handle)
        time.sleep(0.4)
        _try_accept_alert(driver)
    else:
        logger.info("[CHECKPOINT] No new window appeared. Assuming download triggered directly.")
        _try_accept_alert(driver)

    logger.info("[CHECKPOINT] Waiting for .zip file in %s", download_dir)

    zip_path = _wait_for_new_zip(download_dir, before, float(file_wait))
    if not zip_path:
        raise TimeoutError(f"No new .zip appeared in {download_dir} within {file_wait}s")

    if os.path.abspath(zip_path) != os.path.abspath(dest_zip_path):
        os.makedirs(os.path.dirname(dest_zip_path) or ".", exist_ok=True)
        shutil.move(zip_path, dest_zip_path)

    _close_extra_windows(driver, main_handle)


def run_downloads(
    input_json_path: str,
    *,
    limit: Optional[int] = None,
    resume: bool = False,
    delay_sec: float = 0.35,
    download_timeout: int = 180,
    no_headless: bool = False,
) -> Dict[str, Any]:
    records_in = load_tender_detail_records(input_json_path)
    if limit is not None:
        records_in = records_in[:limit]

    base_out = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, DOCUMENTS_SUBDIR)
    os.makedirs(base_out, exist_ok=True)

    scraper = GOJEPScraper()
    scraper._setup_driver(
        headless=False if no_headless else None,
        download_dir=base_out,
    )
    results: List[Dict[str, Any]] = []
    ui_wait = getattr(config, "SELENIUM_TIMEOUT", 30)
    try:
        ensure_gojep_logged_in(scraper.driver)
        if not scraper.driver:
            raise RuntimeError("WebDriver not available")

        for i, rec in enumerate(records_in):
            title_url = rec.get("title_url")
            rid = _resource_id_from_record(rec)
            comp_id = rec.get("fields", {}).get("competition_unique_id")
            used_id = comp_id if comp_id else rid
            
            row: Dict[str, Any] = {
                "index": i,
                "title_url": title_url,
                "resource_id": rid,
                "competition_unique_id": comp_id,
                "local_relpath": None,
                "bytes": None,
                "error": None,
            }
            if not title_url or not rid:
                row["error"] = "missing_title_url_or_resource_id"
                results.append(row)
                continue

            safe_name = re.sub(r"[^\w\-.]+", "_", str(used_id)) or "unknown"
            dest_zip = os.path.join(base_out, f"{safe_name}.zip")
            extract_dir = os.path.join(base_out, safe_name)

            if resume and os.path.isdir(extract_dir) and any(os.scandir(extract_dir)):
                rel = os.path.join(DOCUMENTS_SUBDIR, safe_name).replace("\\", "/")
                row["local_relpath"] = rel
                row["extracted_dir"] = rel
                row["bytes"] = 0
                results.append(row)
                logger.info("Resume skip (folder exists): %s", rel)
                if delay_sec:
                    time.sleep(delay_sec)
                continue

            try:
                logger.info("Launch %s/%s title_url resourceId=%s", i + 1, len(records_in), rid)
                scraper.driver.get(title_url)
                WebDriverWait(scraper.driver, ui_wait).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(0.4)
                _handle_captcha_on_current_page(scraper.driver, scraper.captcha_solver, ui_wait)

                download_zip_via_ui(
                    scraper.driver,
                    base_out,
                    dest_zip,
                    ui_wait=ui_wait,
                    file_wait=download_timeout,
                )

                # Extract the zip file contents to a folder with the same name
                os.makedirs(extract_dir, exist_ok=True)
                zip_size = os.path.getsize(dest_zip) if os.path.exists(dest_zip) else 0
                
                try:
                    with zipfile.ZipFile(dest_zip, 'r') as zip_ref:
                        zip_ref.extractall(extract_dir)
                    logger.info("Extracted %s to %s", dest_zip, extract_dir)
                    row["extracted_dir"] = os.path.join(DOCUMENTS_SUBDIR, safe_name).replace("\\", "/")
                    
                    # Ensure Zip deletion post-extraction
                    os.remove(dest_zip)
                    logger.info("Cleaned up and deleted zip: %s", dest_zip)
                except zipfile.BadZipFile:
                    logger.warning("Downloaded file %s is not a valid ZIP file", dest_zip)
                    row["error"] = "bad_zip_file"

                rel = os.path.join(DOCUMENTS_SUBDIR, safe_name).replace("\\", "/")
                row["local_relpath"] = rel
                row["bytes"] = zip_size
                results.append(row)
                logger.info("Saved and extracted %s (%s bytes of raw zip)", rel, zip_size)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)
                results.append(row)
                logger.warning("Failed resourceId=%s: %s", rid, e)

            if delay_sec:
                time.sleep(delay_sec)

    finally:
        if scraper.driver:
            scraper.driver.quit()

    ok = sum(1 for r in results if r.get("local_relpath") and not r.get("error"))
    failed = sum(1 for r in results if r.get("error"))
    payload = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "source_tender_details": os.path.abspath(input_json_path),
        "documents_directory": os.path.abspath(base_out),
        "total_input": len(records_in),
        "saved_ok": ok,
        "failed": failed,
        "records": results,
    }
    return payload


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download tender ZIPs via Competition documents → Contract documents → Download Zip",
    )
    p.add_argument(
        "--input-json",
        type=str,
        default=None,
        help="tender_details_*.json path (default: newest under data/tenders)",
    )
    p.add_argument("--limit", type=int, default=None, help="Process only first N tenders (testing)")
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip when cft_<resourceId>.zip already exists under data/tenders/documents",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.35,
        help="Seconds to sleep between tenders (default: 0.35)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Max seconds to wait for .zip file after clicking Download (default: 180)",
    )
    p.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Write manifest JSON path (default: tender_documents_<ts>.json under data/tenders)",
    )
    p.add_argument(
        "--no-headless",
        action="store_true",
        help="Always show Chrome (ignore HEADLESS_MODE; use for debugging)",
    )
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    input_path = args.input_json or _latest_tender_details_json()
    if not input_path or not os.path.isfile(input_path):
        raise SystemExit("No --input-json and no tender_details_*.json found under data/tenders")

    payload = run_downloads(
        input_path,
        limit=args.limit,
        resume=args.resume,
        delay_sec=max(0.0, args.delay),
        download_timeout=max(30, args.timeout),
        no_headless=args.no_headless,
    )

    out_dir = config.TENDERS_OUTPUT_DIRECTORY
    os.makedirs(out_dir, exist_ok=True)
    if args.output_json:
        out_path = args.output_json
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(out_dir, f"tender_documents_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("Wrote manifest: %s", out_path)
    print(out_path)


if __name__ == "__main__":
    main()
