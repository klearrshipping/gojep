"""
Shared document download helpers for GOJEP tender documents.

Provides reusable Selenium-based ZIP download functions that both
tenders workflow and email workflow can use.

Folder structure after download:
  <tender_id>/
  ├── tender_data/
  │   └── document_downloads/     ← tenders workflow saves here
  └── email_updates/
      ├── new_documents/
      │   └── document_downloads/  ← email workflow (new_documents) saves here
      └── clarifications/
          └── document_downloads/  ← email workflow (clarifications) saves here
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import zipfile
from typing import Any, Optional

from selenium.common.exceptions import NoAlertPresentException, NoSuchWindowException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import settings as config

logger = logging.getLogger(__name__)

CONTENT_TYPES = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".xml":  "application/xml",
    ".zip":  "application/zip",
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _sanitize_name(name: str) -> str:
    """Sanitize filename for storage."""
    safe = ""
    for ch in name:
        if ch.isascii() and (ch.isalnum() or ch in "-_.() "):
            safe += ch
        else:
            safe += "_"
    return safe


def _wait_for_new_zip(download_dir: str, before_names: set[str], timeout_sec: float) -> Optional[str]:
    """Poll download_dir until a new .zip file appears and is fully written."""
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
    """Accept any open JS alert/confirm. Returns alert text or None."""
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        if log:
            logger.info("Accepted JS alert: %s", text[:200] if text else "")
        return text
    except (NoAlertPresentException, NoSuchWindowException):
        return None


def _switch_to_new_window(driver: Any, main_handle: str, timeout_sec: float = 15.0) -> bool:
    """Switch to a newly opened window/tab. Returns True if switched."""
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


def _close_extra_windows(driver: Any, main_handle: str) -> None:
    """Close all tabs except main_handle and switch back."""
    for h in list(driver.window_handles):
        if h != main_handle:
            try:
                driver.switch_to.window(h)
                driver.close()
            except Exception:
                pass
    try:
        driver.switch_to.window(main_handle)
    except Exception:
        pass


def _complete_selection_window(driver: Any, wait: WebDriverWait, main_handle: str) -> bool:
    """
    Handle the selection popup (select all + confirm) that appears before ZIP download.
    Returns True if handled the "Association with Competition" popup.
    """
    if _complete_association_popup(driver, wait, main_handle):
        return True

    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass
    time.sleep(0.4)

    _lo = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _hi = "abcdefghijklmnopqrstuvwxyz"
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
        except Exception:
            continue

    for cb in driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
        try:
            if cb.is_displayed() and cb.is_enabled() and not cb.is_selected():
                driver.execute_script("arguments[0].click();", cb)
        except Exception:
            continue

    time.sleep(0.2)

    confirm_xpaths = (
        "//button[contains(., 'Proceed without association')]",
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
        except Exception:
            continue

    logger.warning(
        "No confirmation control matched; URL=%s — adjust selectors if download hangs",
        driver.current_url[:200],
    )
    return False


def _complete_association_popup(driver: Any, wait: WebDriverWait, main_handle: str) -> bool:
    """Handle 'Association with Competition' popup that opens before ZIP download."""
    if not driver.find_elements(By.CSS_SELECTOR, "input#action[value='DownloadCftResourceItemsAction']"):
        return False

    logger.info("Association-with-competition popup detected")
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
        except Exception:
            continue
    if not chosen:
        logger.warning("Could not click selectedVal radio; trying generic handlers")
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

    WebDriverWait(driver, 25).until(lambda d: len(d.window_handles) == 1)
    driver.switch_to.window(main_handle)
    time.sleep(0.5)
    logger.info("Returned to main window after association popup")
    return True


def unblock_files(directory: str) -> None:
    """Remove Windows Zone.Identifier 'Mark of the Web' from all files in directory."""
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-Command", f"Get-ChildItem -Path '{directory}' -Recurse | Unblock-File"],
            capture_output=True, timeout=30,
        )
        logger.info("Unblocked files in %s", directory)
    except Exception as e:
        logger.warning("Could not unblock files in %s: %s", directory, e)


def extract_zip(zip_path: str, extract_dir: str) -> bool:
    """Extract ZIP to extract_dir, delete ZIP after, unblock files."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        logger.info("Extracted %s to %s", zip_path, extract_dir)
        os.remove(zip_path)
        logger.info("Deleted zip: %s", zip_path)
        unblock_files(extract_dir)
        return True
    except zipfile.BadZipFile as e:
        logger.warning("Bad ZIP file %s: %s", zip_path, e)
        return False
    except Exception as e:
        logger.warning("Failed to extract %s: %s", zip_path, e)
        return False


# ── Download via tender workspace menu ───────────────────────────────────────

def click_download_zip_button(driver: Any, ui_wait: int) -> Optional[Any]:
    """Find and click the 'Download Zip' button in the tender workspace."""
    wait = WebDriverWait(driver, ui_wait)

    try:
        toggle = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.ID, "ToggleSubmenu")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", toggle)
        toggle.click()
        logger.info("Clicked ToggleSubmenu")
        time.sleep(0.4)
    except TimeoutException:
        logger.info("ToggleSubmenu not found; trying Competition documents link anyway")

    comp_link = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(@href,'viewContractNotices.do')][contains(., 'Competition documents')]")
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comp_link)
    comp_link.click()
    logger.info("Clicked 'Competition documents' link")

    wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "ul.tabbernav, ul[class*='tabbernav']"))
    )

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
    logger.info("Clicked 'Contract documents' tab")

    dl_btn = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[contains(@onclick,'downloadForAnonymousUser') or contains(., 'Download Zip')]",
            )
        )
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dl_btn)
    return dl_btn


# ── Generic ZIP download ──────────────────────────────────────────────────────

def download_zip(
    driver,
    download_dir: str,
    dest_zip_path: str,
    ui_wait: int = 30,
    file_wait: int = 180,
) -> tuple[bool, str]:
    """
    Generic ZIP download: click button, handle popup, wait for ZIP, extract.

    Args:
        driver:       Selenium WebDriver (must be on the page with download button).
        download_dir: Chrome's download directory.
        dest_zip_path: Where to move the downloaded ZIP (includes filename).
        ui_wait:      Selenium WebDriverWait timeout.
        file_wait:    Max seconds to wait for ZIP to appear.

    Returns:
        (success: bool, message: str)
    """
    try:
        dl_btn = WebDriverWait(driver, ui_wait).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(@onclick,'downloadForAnonymousUser') or contains(., 'Download Zip')]")
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dl_btn)
    except Exception as e:
        return False, f"Download button not found: {e}"

    main_handle = driver.current_window_handle
    before = set(os.listdir(os.path.abspath(download_dir)))
    logger.info("Clicking 'Download Zip' button")
    dl_btn.click()

    time.sleep(0.6)
    if _try_accept_alert(driver):
        time.sleep(0.3)

    if _switch_to_new_window(driver, main_handle, timeout_sec=20.0):
        _complete_selection_window(driver, WebDriverWait(driver, ui_wait), main_handle)
        time.sleep(0.4)
        _try_accept_alert(driver)
    else:
        _try_accept_alert(driver)

    logger.info("Waiting for .zip file in %s", download_dir)
    zip_path = _wait_for_new_zip(download_dir, before, float(file_wait))
    if not zip_path:
        return False, f"No ZIP appeared within {file_wait}s"

    _close_extra_windows(driver, main_handle)

    if os.path.abspath(zip_path) != os.path.abspath(dest_zip_path):
        os.makedirs(os.path.dirname(dest_zip_path) or ".", exist_ok=True)
        shutil.move(zip_path, dest_zip_path)
        logger.info("Moved ZIP to: %s", dest_zip_path)

    return True, dest_zip_path


# ── Supabase upload ───────────────────────────────────────────────────────────

def upload_file_to_supabase(local_path: str, competition_unique_id: str, subfolder: str = "") -> bool:
    """Upload a single file to Supabase Storage under tender-documents/<id>/<subfolder>/<filename>."""
    import requests as _requests

    filename = _sanitize_name(os.path.basename(local_path))
    storage_path = f"{competition_unique_id}/{subfolder}/{filename}" if subfolder else f"{competition_unique_id}/{filename}"
    url = f"{config.SUPABASE_URL.rstrip('/')}/storage/v1/object/tender-documents/{storage_path}"
    suffix = os.path.splitext(local_path)[1].lower()
    content_type = CONTENT_TYPES.get(suffix, "application/octet-stream")

    headers = {
        "apikey":        config.SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}",
    }

    try:
        with open(local_path, "rb") as f:
            resp = _requests.post(
                url,
                headers={**headers, "Content-Type": content_type, "x-upsert": "true"},
                data=f,
            )
        if resp.status_code in (200, 201):
            logger.info("  Uploaded to storage: %s", storage_path)
            return True
        else:
            logger.error("  Storage upload failed (%s): %s — %s", resp.status_code, storage_path, resp.text[:200])
            return False
    except Exception as e:
        logger.error("  Upload error for %s: %s", local_path, e)
        return False


def sync_downloaded_files(
    extract_dir: str,
    competition_unique_id: str,
    subfolder: str = "",
    track_existing: set | None = None,
) -> dict[str, Any]:
    """
    Upload new files from extract_dir to Supabase Storage.

    Args:
        extract_dir:          Directory containing downloaded files.
        competition_unique_id: Tender ID used in storage path.
        subfolder:            Subfolder under competition_unique_id in storage.
        track_existing:       Set of filenames already present (skip upload).

    Returns:
        {new_files_downloaded, skipped_already_uploaded, failed_uploads, downloaded_files}
    """
    EXTRACTABLE = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xml"}
    if track_existing is None:
        track_existing = set()

    new_count = 0
    skipped_count = 0
    failed_count = 0
    downloaded_files: list[str] = []

    for fname in os.listdir(extract_dir):
        if fname in track_existing:
            skipped_count += 1
            continue
        fpath = os.path.join(extract_dir, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in EXTRACTABLE:
            continue

        if upload_file_to_supabase(fpath, competition_unique_id, subfolder):
            downloaded_files.append(fname)
            new_count += 1
        else:
            failed_count += 1

    return {
        "new_files_downloaded": new_count,
        "skipped_already_uploaded": skipped_count,
        "failed_uploads": failed_count,
        "downloaded_files": downloaded_files,
    }