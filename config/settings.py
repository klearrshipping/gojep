"""
Configuration settings for GOJEP Tender Extraction Project.
Paths are resolved relative to the repository root.

════════════════════════════════════════════════════════════════
PIPELINE WORKFLOWS
════════════════════════════════════════════════════════════════

── TENDER PIPELINE (run in order via: python cli/tenders.py) ──

  Step 1: get-current-tenders
    - Scrape GOJEP portal (sorted by deadline, 48h horizon)
    - Wipe and repopulate gojep_tenders_current
    - Upsert into gojep_tenders_all (permanent archive)
    - Insert new rows into gojep_contract_analysis (never wiped)
    - Cross-check gojep_analysis_results: mark already-analysed
      tenders as detail_page_extracted=true, previously_analysed=true
      so downstream steps skip them

  Step 2: get-tender-details
    - Query gojep_tenders_current WHERE detail_page_extracted=false
    - For each: fetch detail page via HTTP, parse fields
    - Update gojep_tenders_current and gojep_contract_analysis

  Step 3: get-tender-documents
    LOGIN FLOW (tools/login/gojep_login.py):
      1. Navigate to https://www.gojep.gov.jm/epps/home.do
      2. Click "Log in" link (/epps/authenticate/login?selectedItem=authenticate/login)
      3. CAS form appears — enter Username / Password, click Login
      4. Redirected back to GOJEP — session established
    DOWNLOAD FLOW (per tender):
      1. Navigate to detail_url for the tender
      2. If CAPTCHA appears — solve and submit
      3. Click #ToggleSubmenu ("Show Menu")
      4. Click "Competition documents" link
      5. Click "Contract documents" tab
      6. Click "Download Zip" button
      7. Handle popup (Association with Competition or Anonymous Download)
      8. Wait for .zip to land in data/tenders/documents/
      9. Extract zip → data/tenders/documents/<competition_unique_id>/
    - Skips tenders already confirmed in gojep_analysis_results

  Step 4: extract-document-text
    - Scan all folders in data/tenders/documents/
    - Run Docling (via WSL Python) to extract text from PDFs/DOCXs
    - Output JSON files into extracted_docs/ inside each folder
    - Already-extracted folders are skipped

  Step 5: batch-analyse (modal_app/batch_analyse.py)
    - Query gojep_analysis_results for already-processed tender_folders
    - Skip folders confirmed in DB (tender_folder + resource_id + competition_unique_id match)
    - Send new folder chunks to Gemma 4 on Modal GPU (L40S)
    - Save results to analysis.json sidecar + gojep_analysis_results
    - Stamp analysis_timestamp on gojep_contract_analysis

════════════════════════════════════════════════════════════════
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

# RunPod
RUNPOD_API_KEY     = _secret_or_env("runpod-api-key", "RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "kv5we7hhojr6y6")

# Lightning AI
LIGHTNING_API_KEY = os.getenv("LIGHTNING_API_KEY")
LIGHTNING_USER_ID = os.getenv("LIGHTNING_USER_ID")

# OpenRouter API credentials
OPENROUTER_API_KEY = _secret_or_env("openrouter-api-key", "OPENROUTER_API_KEY")

# Local LLM configuration (llama.cpp / Gemma 4 Enterprise)
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://localhost:8000/v1/chat/completions")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "gemma-4-e2b")
LOCAL_LLM_MAX_TOKENS = int(os.getenv("LOCAL_LLM_MAX_TOKENS", "8192"))
LOCAL_LLM_TIMEOUT = int(os.getenv("LOCAL_LLM_TIMEOUT", "300"))
# Set to "false" to fall back to OpenRouter for analysis
ANALYSIS_USE_LOCAL_LLM = os.getenv("ANALYSIS_USE_LOCAL_LLM", "true").lower() == "true"

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
SUPABASE_TABLE_CONTRACT_ANALYSIS = "gojep_contract_analysis"

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
    "qwen3_6_plus_free": "qwen/qwen3.6-plus:free",
    "gemma4_31b": "google/gemma-4-31b-it",
    "qwen3_vl_8b": "qwen/qwen3-vl-8b-instruct",
    "qwen3_vl_32b": "qwen/qwen3-vl-32b-instruct",
}

# Model used for pre-extraction document classification
CLASSIFIER_MODEL = "gemma4_31b"

# Default request parameters for OpenRouter calls
OPENROUTER_DEFAULT_TEMPERATURE = 0.1
OPENROUTER_DEFAULT_MAX_TOKENS = 4000

# Specific settings for captcha solving requests
CAPTCHA_TEMPERATURE = 0.0
CAPTCHA_MAX_TOKENS = 256

# Which model slot each task uses (values are keys in OPENROUTER_MODELS)
CAPTCHA_MODEL = "qwen3_vl_8b"
ANALYSIS_MODEL = "qwen3_vl_32b"

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

# WSL Python interpreter path — used by extract_documents.py for Docling
# Points to the venv created inside WSL that has docling + torch installed
WSL_PYTHON = os.getenv(
    "WSL_PYTHON",
    "/mnt/c/Users/Administrator/Desktop/projects/gojep/venv_wsl/bin/python",
)
