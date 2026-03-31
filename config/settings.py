"""
Configuration settings for GOJEP Tender Extraction Project.
Paths are resolved relative to the repository root.
"""
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _PROJECT_ROOT / ".env"
load_dotenv(_ENV_PATH)

from .secrets import get_secret


def _secret_or_env(secret_name: str, *env_keys: str) -> str | None:
    """Prefer Secret Manager; fall back to environment variables (local dev)."""
    try:
        value = get_secret(secret_name)
        if value:
            return value
    except Exception:
        pass
    for key in env_keys:
        v = os.getenv(key)
        if v:
            return v
    return None


# GOJEP Website URLs
GOJEP_BASE_URL = "https://www.gojep.gov.jm"
GOJEP_HOME_URL = f"{GOJEP_BASE_URL}/epps/home.do"
CURRENT_OPPORTUNITIES_URL = "https://www.gojep.gov.jm/epps/prepareCurrentOpportunities.do?selectedItem=prepareCurrentOpportunities.do"
CAPTCHA_IMAGE_URL = "/epps/genCaptcha/captcha.jpg"
CONTRACT_AWARD_URL = "https://www.gojep.gov.jm/epps/viewCaNotices.do?d-16531-p=1&selectedItem=viewCaNotices.do"

# Selenium Settings
SELENIUM_TIMEOUT = 30
IMPLICIT_WAIT = 10
PAGE_LOAD_TIMEOUT = 30

# Browser Settings
# Prefer the value from .env when set (load_dotenv does not override existing OS env vars).
_env_headless = dotenv_values(_ENV_PATH).get("HEADLESS_MODE")
if _env_headless is not None and str(_env_headless).strip() != "":
    HEADLESS_MODE = str(_env_headless).strip().lower() == "true"
else:
    HEADLESS_MODE = os.getenv("HEADLESS_MODE", "True").lower() == "true"
BROWSER_WIDTH = 1920
BROWSER_HEIGHT = 1080

# GOJEP portal login (contract awards and other authenticated pages may redirect to CAS)
GOJEP_USERNAME = _secret_or_env("gojep-username", "GOJEP_USERNAME")
GOJEP_PASSWORD = _secret_or_env("gojep-password", "GOJEP_PASSWORD")

# OpenRouter API credentials
OPENROUTER_API_KEY = _secret_or_env("openrouter-api-key", "OPENROUTER_API_KEY")

# Supabase Configuration
SUPABASE_URL = _secret_or_env("supabase-url-procurement", "SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = _secret_or_env(
    "supabase-publishable-key-procurement",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_KEY",
)
SUPABASE_SECRET_KEY = _secret_or_env("supabase-secret-key-procurement", "SUPABASE_SECRET_KEY")

# Supabase table names
SUPABASE_TABLE_TENDERS_ALL = "gojep_tenders_all"
SUPABASE_TABLE_TENDERS_CURRENT = "gojep_tenders_current"
SUPABASE_TABLE_ANALYSIS_RESULTS = "gojep_analysis_results"
SUPABASE_TABLE_AWARDS_ALL = "gojep_awards_all"
SUPABASE_TABLE_AWARDS_CURRENT = "gojep_awards_current"
SUPABASE_TABLE_AWARD_DETAILS_ALL = "gojep_award_details_all"
SUPABASE_TABLE_AWARD_ANALYSIS_RESULTS = "gojep_awards_analysis_results"

# OpenRouter API configuration
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/your-repo",
    "X-Title": "CUDA Project",
}

# OpenRouter models: keys name the model slot (not a single task). Map tasks via CAPTCHA_MODEL / ANALYSIS_MODEL.
OPENROUTER_MODELS = {
    "qwen_35_9b": "qwen/qwen3.5-9b",
    "nemotron_3_super_120b_free": "nvidia/nemotron-3-super-120b-a12b:free",
}
# Enable OpenRouter reasoning mode (pass reasoning_details back on follow-up turns if you add multi-turn calls)
OPENROUTER_REASONING_ENABLED = True

# Default request parameters for OpenRouter calls
OPENROUTER_DEFAULT_TEMPERATURE = 0.2
OPENROUTER_DEFAULT_MAX_TOKENS = 1500

# Specific settings for captcha solving requests
CAPTCHA_TEMPERATURE = 0.0
CAPTCHA_MAX_TOKENS = 256

# Which model slot each task uses (values are keys in OPENROUTER_MODELS)
CAPTCHA_MODEL = "qwen_35_9b"
ANALYSIS_MODEL = "nemotron_3_super_120b_free"

# Captcha Settings
CAPTCHA_RETRY_ATTEMPTS = 3
CAPTCHA_SAVE_PATH = str(_PROJECT_ROOT / "tools" / "captcha" / "images")

# Data Export Settings
OUTPUT_FORMAT = "json"
TENDERS_OUTPUT_DIRECTORY = str(_PROJECT_ROOT / "data" / "tenders")
AWARDS_OUTPUT_DIRECTORY = str(_PROJECT_ROOT / "data" / "awards")
AWARDS_DETAILS_OUTPUT_DIRECTORY = str(_PROJECT_ROOT / "data" / "awards" / "details")
OUTPUT_DIRECTORY = TENDERS_OUTPUT_DIRECTORY

# Default analysis JSON output (CLI)
ANALYSIS_DEFAULT_OUTPUT_FILE = str(_PROJECT_ROOT / "data" / "analysis" / "ease_of_fulfillment_analysis.json")

# Pagination Settings
MAX_PAGES_DEFAULT = 1
PAGINATION_DELAY = 2
RESULTS_PER_PAGE = 100

# Detail Page Extraction Settings
EXTRACT_DETAIL_PAGES = True
DETAIL_EXTRACTION_DELAY = 1
MAX_DETAIL_RETRIES = 3

# Awards Extraction Settings
# Contracts that have been awarded (winners, award date, awarded amount, etc.)
EXTRACT_AWARDS = False
AWARDS_PDF_SUBDIR = "pdf"
AWARDS_PDF_DOWNLOAD_DELAY_SEC = float(os.getenv("AWARDS_PDF_DOWNLOAD_DELAY_SEC", "0.2"))
AWARDS_PDF_DOWNLOAD_TIMEOUT = int(os.getenv("AWARDS_PDF_DOWNLOAD_TIMEOUT", "120"))

# Database Settings
SAVE_TO_SUPABASE = True
BATCH_SIZE = 10

# Logging Settings
LOG_LEVEL = "INFO"
LOG_FILE = str(_PROJECT_ROOT / "data" / "logs" / "gojep_scraper.log")

# Data Reconciliation Settings
AUTO_RECONCILE = True

# Repository root (for tools / scripts)
PROJECT_ROOT = str(_PROJECT_ROOT)
