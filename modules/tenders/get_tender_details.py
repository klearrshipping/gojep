"""
GOJEP tender detail extractor.

Reads title URLs from extracted tenders JSON and parses all fields
from the detail page <dl class="Grid"> structure.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from config import settings as config

logger = logging.getLogger(__name__)


class TenderDetailExtractor:
    def __init__(self) -> None:
        self.session = requests.Session()
        self._setup_logging()
        os.makedirs(config.TENDERS_OUTPUT_DIRECTORY, exist_ok=True)

    def _setup_logging(self) -> None:
        level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler()],
        )

    @staticmethod
    def _to_absolute(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return f"{config.GOJEP_BASE_URL}{url}"
        return f"{config.GOJEP_BASE_URL}/{url}"

    @staticmethod
    def _label_to_key(label: str) -> str:
        cleaned = label.strip().rstrip(":").lower()
        cleaned = re.sub(r"\s+", "_", cleaned)
        cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned).strip("_")
        return cleaned

    @staticmethod
    def _extract_resource_id_from_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        parsed = urlparse(url)
        resource_id = parse_qs(parsed.query).get("resourceId", [None])[0]
        if resource_id:
            return resource_id
        if "resourceId=" in url:
            return url.split("resourceId=", 1)[1].split("&", 1)[0]
        return None

    @staticmethod
    def _extract_dd_value(dd_tag) -> Any:
        if dd_tag is None:
            return None

        # If multiple lines/values are present with <br>, capture as list.
        has_line_breaks = bool(dd_tag.find("br"))
        lines = [line.strip() for line in dd_tag.get_text("\n", strip=True).split("\n") if line.strip()]
        if has_line_breaks and len(lines) > 1:
            return lines
        if len(lines) == 1:
            return lines[0]
        if len(lines) > 1:
            return lines
        return None

    def _parse_grid_fields(self, soup: BeautifulSoup) -> Dict[str, Any]:
        grid = soup.find("dl", class_="Grid")
        if not grid:
            return {}

        details: Dict[str, Any] = {}
        terms = grid.find_all("dt")
        values = grid.find_all("dd")

        for dt_tag, dd_tag in zip(terms, values):
            raw_label = dt_tag.get_text(" ", strip=True).rstrip(":")
            key = self._label_to_key(raw_label)
            value = self._extract_dd_value(dd_tag)
            details[key] = value
            details[f"{key}_label"] = raw_label

            # Capture href for linked fields (e.g., Name of procuring entity)
            link = dd_tag.find("a")
            if link and link.get("href"):
                details[f"{key}_url"] = self._to_absolute(link.get("href"))

        return details

    def _extract_one(self, title_url: str) -> Dict[str, Any]:
        absolute_url = self._to_absolute(title_url)
        if not absolute_url:
            return {}

        response = self.session.get(absolute_url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        parsed_fields = self._parse_grid_fields(soup)

        return {
            "title_url": absolute_url,
            "resource_id_from_url": self._extract_resource_id_from_url(absolute_url),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "fields": parsed_fields,
        }

    @staticmethod
    def _load_title_urls_from_json(input_json_path: str) -> List[str]:
        with open(input_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        urls: List[str] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            u = row.get("title_url")
            if u:
                urls.append(u)
        # keep order, remove duplicates
        seen = set()
        unique_urls = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            unique_urls.append(u)
        return unique_urls

    def extract_from_tenders_json(self, input_json_path: str) -> str:
        urls = self._load_title_urls_from_json(input_json_path)
        logger.info("Found %s title URLs in %s", len(urls), input_json_path)

        output: List[Dict[str, Any]] = []
        failures: List[Dict[str, str]] = []

        for i, url in enumerate(urls, start=1):
            try:
                logger.info("Extracting detail %s/%s", i, len(urls))
                output.append(self._extract_one(url))
            except Exception as e:  # noqa: BLE001
                failures.append({"title_url": url, "error": str(e)})

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(config.TENDERS_OUTPUT_DIRECTORY, f"tender_details_{timestamp}.json")
        payload = {
            "source_file": input_json_path,
            "total_urls": len(urls),
            "successful": len(output),
            "failed": len(failures),
            "records": output,
            "failures": failures,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        logger.info("Saved detail output to %s", output_path)
        return output_path


def _latest_tenders_json() -> Optional[str]:
    if not os.path.isdir(config.TENDERS_OUTPUT_DIRECTORY):
        return None
    candidates = [
        os.path.join(config.TENDERS_OUTPUT_DIRECTORY, name)
        for name in os.listdir(config.TENDERS_OUTPUT_DIRECTORY)
        if name.startswith("tenders_") and name.endswith(".json")
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


if __name__ == "__main__":
    extractor = TenderDetailExtractor()
    source_path = _latest_tenders_json()
    if not source_path:
        raise FileNotFoundError(f"No tenders_*.json file found in {config.TENDERS_OUTPUT_DIRECTORY}")
    extractor.extract_from_tenders_json(source_path)