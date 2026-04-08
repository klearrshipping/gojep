"""
GOJEP CAS login helper.

Workflow:
1. Navigate to https://www.gojep.gov.jm/epps/home.do
2. Click the "Log in" link (/epps/authenticate/login?selectedItem=authenticate/login)
3. On the CAS form enter Username / Password and submit
4. Session is now established for subsequent tender page navigation
"""

from __future__ import annotations

import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import settings as config

logger = logging.getLogger(__name__)


def ensure_gojep_logged_in(driver) -> None:
    """
    Log in to GOJEP via CAS using the documented login flow.
    Safe to call when already logged in — checks first before proceeding.
    """
    if not config.GOJEP_USERNAME or not config.GOJEP_PASSWORD:
        raise ValueError("GOJEP_USERNAME and GOJEP_PASSWORD must be set in settings.")

    # Step 1 — Navigate to home page
    logger.info("Navigating to GOJEP home page ...")
    driver.get(config.GOJEP_HOME_URL)
    WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    # Check if already logged in (Log in link absent = already authenticated)
    login_links = driver.find_elements(
        By.XPATH, "//a[contains(@href,'authenticate/login')]"
    )
    if not login_links:
        logger.info("Already logged in to GOJEP.")
        return

    # Step 2 — Click the Log in link
    logger.info("Clicking Log in link ...")
    login_links[0].click()

    # Step 3 — Wait for CAS login form
    WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
        EC.presence_of_element_located((By.ID, "Username"))
    )
    logger.info("CAS login form detected — entering credentials ...")

    username_field = driver.find_element(By.ID, "Username")
    username_field.clear()
    username_field.send_keys(config.GOJEP_USERNAME)

    password_field = driver.find_element(By.ID, "Password")
    password_field.clear()
    password_field.send_keys(config.GOJEP_PASSWORD)

    # Step 4 — Submit
    driver.find_element(By.CSS_SELECTOR, "input[type='submit'][value='Login']").click()

    # Wait until redirected back to GOJEP (away from CAS)
    WebDriverWait(driver, config.SELENIUM_TIMEOUT).until(
        lambda d: "gojep.gov.jm/cas" not in d.current_url
    )
    logger.info("Logged in to GOJEP successfully. Current URL: %s", driver.current_url)
