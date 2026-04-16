"""
OpenRouter LLM API call with retry logic.
"""

import logging
import time

import requests

from config import settings as config
from modules.analysis.prompt import ANALYSIS_SYSTEM_PROMPT, MAX_RETRIES, RETRY_BASE_DELAY

logger = logging.getLogger(__name__)


def _call_llm(context: str, system_prompt: str = None, max_tokens: int = 4000) -> str:
    """
    Send context to OpenRouter for LLM analysis.
    Uses ANALYSIS_SYSTEM_PROMPT by default; pass system_prompt to override (e.g. consolidation).
    Retries on transient 5xx errors and 429 rate limits with exponential backoff.
    """
    messages = [
        {"role": "system", "content": system_prompt or ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]

    model_key = config.ANALYSIS_MODEL
    model_id = config.OPENROUTER_MODELS.get(model_key)
    if not model_id:
        raise ValueError(f"ANALYSIS_MODEL '{model_key}' not found in OPENROUTER_MODELS")

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/gojep-platform",
        "X-Title": "GOJEP Tender Analyser",
    }
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": True},
    }
    call_timeout = 120

    delay = RETRY_BASE_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(config.OPENROUTER_URL, headers=headers, json=payload, timeout=call_timeout)
            if resp.status_code == 429:
                wait = delay + (attempt * 2)
                print(f"  -> Rate limited (429), waiting {wait}s before retry {attempt}/{MAX_RETRIES}...", flush=True)
                time.sleep(wait)
                delay *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                logger.warning(f"LLM returned empty content. Full response: {str(data)[:1000]}")
                raise RuntimeError("Model returned empty content")
            return content
        except requests.exceptions.Timeout:
            if attempt == MAX_RETRIES:
                raise
            print(f"  -> Timeout on attempt {attempt}/{MAX_RETRIES}, retrying in {delay}s...", flush=True)
            time.sleep(delay)
            delay *= 2
        except requests.exceptions.HTTPError as e:
            if resp.status_code >= 500 and attempt < MAX_RETRIES:
                print(f"  -> Server error ({resp.status_code}), retrying in {delay}s...", flush=True)
                time.sleep(delay)
                delay *= 2
            else:
                raise

    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts")
