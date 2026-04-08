"""
CAPTCHA Solver for GOJEP Website
Handles captcha image extraction and solving using LLM APIs
"""
import os
import base64
import time
from io import BytesIO
from PIL import Image
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from config.settings import (
    OPENROUTER_API_KEY,
    CAPTCHA_MODEL,
    OPENROUTER_MODELS,
    CAPTCHA_SAVE_PATH,
    SELENIUM_TIMEOUT,
    GOJEP_BASE_URL,
    CAPTCHA_MAX_TOKENS,
    CAPTCHA_TEMPERATURE,
    CAPTCHA_RETRY_ATTEMPTS,
)
import logging

logger = logging.getLogger(__name__)


class CaptchaSolver:
    def __init__(self):
        self.openrouter_base_url = "https://openrouter.ai/api/v1"
        
        # Validate OpenRouter configuration
        if not OPENROUTER_API_KEY:
            raise ValueError("OPENROUTER_API_KEY is required")
        
        if CAPTCHA_MODEL not in OPENROUTER_MODELS:
            raise ValueError(f"CAPTCHA_MODEL '{CAPTCHA_MODEL}' not found in OPENROUTER_MODELS")
        
        self.model_name = OPENROUTER_MODELS[CAPTCHA_MODEL]
        
        # Create captcha images directory
        os.makedirs(CAPTCHA_SAVE_PATH, exist_ok=True)
    
    def extract_captcha_image(self, driver):
        """
        Extract captcha image from the GOJEP webpage
        
        Args:
            driver: Selenium WebDriver instance
            
        Returns:
            str: Path to saved captcha image
        """
        try:
            # Wait for captcha image to load
            captcha_img = WebDriverWait(driver, SELENIUM_TIMEOUT).until(
                EC.presence_of_element_located((By.ID, "CAPTCHA"))
            )
            
            # Get the captcha image source
            img_src = captcha_img.get_attribute("src")
            
            # If it's a relative URL, make it absolute
            if img_src.startswith("/"):
                img_src = GOJEP_BASE_URL + img_src
            
            # Convert Selenium cookies to requests format
            selenium_cookies = driver.get_cookies()
            cookies_dict = {cookie['name']: cookie['value'] for cookie in selenium_cookies}
            
            # Download the image
            response = requests.get(img_src, cookies=cookies_dict)
            response.raise_for_status()
            
            # Save the image
            timestamp = int(time.time())
            img_path = os.path.join(CAPTCHA_SAVE_PATH, f"captcha_{timestamp}.jpg")
            
            with open(img_path, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"Captcha image saved to: {img_path}")
            return img_path
            
        except Exception as e:
            logger.error(f"Error extracting captcha image: {str(e)}")
            raise
    
    def solve_captcha_with_openrouter(self, image_path):
        """
        Solve captcha using OpenRouter API
        
        Args:
            image_path (str): Path to captcha image
            
        Returns:
            str: Solved captcha text
        """
        try:
            # Encode image to base64
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode('utf-8')
            
            # Prepare headers
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/gojep-tender-scraper",
                "X-Title": "GOJEP Tender Scraper"
            }
            
            # Prepare the payload (reasoning optional; matches OpenRouter chat completions API)
            payload = {
                "model": self.model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Please solve this CAPTCHA by identifying the text/numbers shown in the image. Return only the text/numbers without any additional explanation."
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": CAPTCHA_MAX_TOKENS,
                "temperature": CAPTCHA_TEMPERATURE,
            }
            # Reasoning disabled — unnecessary for OCR and can cause empty content responses
            
            # Make the API request
            response = requests.post(
                f"{self.openrouter_base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60
            )
            
            response.raise_for_status()
            result = response.json()
            
            if "choices" not in result or not result["choices"]:
                raise Exception("No response choices returned from OpenRouter")
            
            raw_content = result["choices"][0]["message"].get("content") or ""
            captcha_solution = raw_content.strip()
            
            # Clean up the solution (remove any extra formatting)
            captcha_solution = captcha_solution.replace('"', '').replace("'", '').strip()
            
            filtered_solution = "".join(ch for ch in captcha_solution if ch.isalnum())
            if not filtered_solution:
                raise ValueError(f"Captcha solution '{captcha_solution}' did not contain alphanumeric characters")
            
            logger.info(f"OpenRouter ({CAPTCHA_MODEL} - {self.model_name}) solved captcha: {captcha_solution}")
            return filtered_solution
            
        except Exception as e:
            logger.error(f"Error solving captcha with OpenRouter: {str(e)}")
            raise
    
    def solve_captcha(self, driver):
        """
        Main method to extract and solve captcha
        
        Args:
            driver: Selenium WebDriver instance
            
        Returns:
            str: Solved captcha text
        """
        for attempt in range(CAPTCHA_RETRY_ATTEMPTS):
            try:
                logger.info(f"Captcha solving attempt {attempt + 1}/{CAPTCHA_RETRY_ATTEMPTS}")
                
                # Extract captcha image
                image_path = self.extract_captcha_image(driver)
                
                # Solve captcha using OpenRouter
                solution = self.solve_captcha_with_openrouter(image_path)
                
                # Automatically delete the captcha image immediately after solving
                try:
                    if os.path.exists(image_path):
                        os.remove(image_path)
                        logger.debug(f"Cleaned up temporary captcha image: {image_path}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete captcha image {image_path}: {cleanup_err}")
                
                return solution
                
            except Exception as e:
                logger.warning(f"Captcha solving attempt {attempt + 1} failed: {str(e)}")
                
                # Ensure the image is deleted even if solving fails
                try:
                    if 'image_path' in locals() and os.path.exists(image_path):
                        os.remove(image_path)
                except Exception:
                    pass
                
                if attempt < CAPTCHA_RETRY_ATTEMPTS - 1:
                    # Click refresh button to get new captcha
                    try:
                        refresh_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Refresh code')]")
                        refresh_btn.click()
                        time.sleep(2)  # Wait for new captcha to load
                    except:
                        logger.warning("Could not refresh captcha")
                else:
                    raise
        
        raise Exception("Failed to solve captcha after all attempts")
    
    def input_captcha_solution(self, driver, solution):
        """
        Input the solved captcha into the form
        
        Args:
            driver: Selenium WebDriver instance
            solution (str): Solved captcha text
        """
        try:
            captcha_input = None
            for by, selector in (
                (By.ID, "Captcha"),
                (By.ID, "captcha"),
                (By.NAME, "captcha"),
                (By.CSS_SELECTOR, ".Captcha input[type='text']"),
            ):
                try:
                    captcha_input = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    break
                except Exception:  # noqa: BLE001
                    continue
            if captcha_input is None:
                captcha_input = WebDriverWait(driver, SELENIUM_TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "Captcha"))
                )

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
                captcha_input,
            )
            time.sleep(0.15)
            try:
                captcha_input.click()
            except Exception:  # noqa: BLE001
                driver.execute_script("arguments[0].focus();", captcha_input)

            captcha_input.clear()
            captcha_input.send_keys(solution)

            entered = captcha_input.get_attribute("value") or ""
            if entered.strip() != solution.strip():
                driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
                    "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                    captcha_input,
                    solution,
                )
                entered = captcha_input.get_attribute("value") or ""
                logger.info("Captcha applied via JS fallback; value length=%s", len(entered))

            logger.info("Captcha solution '%s' entered (field value length=%s)", solution, len(entered))
            
        except Exception as e:
            logger.error(f"Error inputting captcha solution: {str(e)}")
            raise 