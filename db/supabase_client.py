"""
Supabase client for GOJEP Tender data storage and retrieval
"""
import logging
from typing import List, Dict, Any, Optional, Iterable
from urllib.parse import quote
from collections import OrderedDict

import requests
from datetime import datetime, timedelta, timezone

from config.settings import (
    SUPABASE_URL,
    SUPABASE_PUBLISHABLE_KEY,
    SUPABASE_SECRET_KEY,
    SUPABASE_TABLE_TENDERS_ALL,
    SUPABASE_TABLE_TENDERS_CURRENT,
    SUPABASE_TABLE_AWARDS_ALL,
    SUPABASE_TABLE_AWARDS_CURRENT,
)

logger = logging.getLogger(__name__)


TENDER_FIELD_DEFAULTS: Dict[str, Any] = {
    'resource_id': None,
    'competition_unique_id': None,
    'row_number': None,
    'title': None,
    'detail_url': None,
    'procuring_entity': None,
    'procuring_entity_url': None,
    'procurement_type': None,
    'services_subtype': None,
    'procurement_method': None,
    'procedure': None,
    'evaluation_mechanism': None,
    'procurement_technique': None,
    'description': None,
    'detailed_description': None,
    'combined_description': None,
    'ppc_ncc_categories': [],
    'cpv_codes': [],
    'retender_flag': None,
    'framework_agreement_establishment': None,
    'contract_awarded_in_lots': None,
    'pe_audit_correspondence': None,
    'special_differential_treatment': None,
    'project_reference_number': None,
    'country_contract_performance': None,
    'funding_source': None,
    'non_petroleum_indicator': None,
    'number_of_stages': None,
    'submission_deadline': None,
    'original_submission_deadline': None,
    'clarification_period_end': None,
    'bid_opening_date': None,
    'site_visit_date': None,
    'publication_date': None,
    'submission_deadline_parsed': None,
    'publication_date_parsed': None,
    'bid_deadline_days_remaining': None,
    'bid_deadline_hours_remaining': None,
    'pdf_url': None,
    'extraction_timestamp': None,
    'source_url': None,
    'detail_page_extracted': False,
    'extraction_errors': [],
}

AWARD_FIELD_DEFAULTS: Dict[str, Any] = {
    'resource_id': None,
    'row_number': None,
    'procurement_method': None,
    'procurement_type': None,
    'procuring_entity': None,
    'title': None,
    'contract_amount': None,
    'contract_amount_raw': None,
    'award_date': None,
    'award_date_raw': None,
    'award_date_parsed': None,
    'contract_url': None,
    'pdf_url': None,
    'pdf_resource_id': None,
    'official_name': None,
    'postal_address': None,
    'tender_reference_number': None,
    'name_of_contractor': None,
    'ppc_category_code_and_title': [],
    'cpv_codes': [],
    'contract_price_amount': None,
    'contract_price_currency': None,
    'level_of_competition': None,
    'contract_award_criteria': None,
    'funding_source': None,
    'funding_providers': None,
    'principal_site_of_performance': None,
    'commencement_date': None,
    'duration': None,
    'justification': None,
    'date_of_dispatch_of_notice': None,
    'extraction_timestamp': None,
    'source_url': None,
    'extraction_errors': [],
}

ARRAY_FIELDS = {'ppc_ncc_categories', 'cpv_codes', 'extraction_errors'}
BOOLEAN_FIELDS = {
    'framework_agreement_establishment',
    'contract_awarded_in_lots',
    'pe_audit_correspondence',
    'detail_page_extracted',
}
TIMESTAMP_FIELDS = {
    'submission_deadline',
    'original_submission_deadline',
    'clarification_period_end',
    'bid_opening_date',
    'site_visit_date',
    'publication_date',
    'submission_deadline_parsed',
    'publication_date_parsed',
    'extraction_timestamp',
}

AWARD_ARRAY_FIELDS = {'ppc_category_code_and_title', 'cpv_codes', 'extraction_errors'}
AWARD_TIMESTAMP_FIELDS = {
    'award_date_parsed',
    'extraction_timestamp',
}
AWARD_BOOLEAN_FIELDS = set()


class SupabaseResponse:
    def __init__(self, data=None, count: Optional[int] = None):
        self.data = data
        self.count = count


class SupabaseQuery:
    def __init__(self, client: "SupabaseClient", table_name: str):
        self.client = client
        self.table_name = table_name
        self._reset()

    def _reset(self):
        self.operation = "select"
        self.select_columns = "*"
        self.filters: Dict[str, List[str]] = {}
        self.orderings: List[str] = []
        self.limit_value: Optional[int] = None
        self.offset_value: Optional[int] = None
        self.range_start: Optional[int] = None
        self.range_end: Optional[int] = None
        self.count_option: Optional[str] = None
        self.payload: Optional[Any] = None
        self.on_conflict: Optional[str] = None
        return self

    def _add_filter(self, field: str, expression: str):
        self.filters.setdefault(field, []).append(expression)

    def select(self, columns: str = "*", count: Optional[str] = None):
        self.operation = "select"
        self.select_columns = columns or "*"
        self.count_option = count
        return self

    def eq(self, field: str, value: Any):
        self._add_filter(field, f"eq.{self._encode_value(value)}")
        return self

    def neq(self, field: str, value: Any):
        self._add_filter(field, f"neq.{self._encode_value(value)}")
        return self

    def gte(self, field: str, value: Any):
        self._add_filter(field, f"gte.{self._encode_value(value)}")
        return self

    def lt(self, field: str, value: Any):
        self._add_filter(field, f"lt.{self._encode_value(value)}")
        return self

    def in_(self, field: str, values: Iterable[Any]):
        encoded = ",".join(self._encode_value(v) for v in values)
        self._add_filter(field, f"in.({encoded})")
        return self

    def order(self, field: str, desc: bool = False):
        direction = "desc" if desc else "asc"
        self.orderings.append(f"{field}.{direction}")
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def range(self, start: int, end: int):
        self.range_start = start
        self.range_end = end
        return self

    def insert(self, data: Any):
        self.operation = "insert"
        self.payload = data
        return self

    def upsert(self, data: Any, on_conflict: Optional[str] = None):
        self.operation = "upsert"
        self.payload = data
        self.on_conflict = on_conflict
        return self

    def update(self, data: Dict[str, Any]):
        self.operation = "update"
        self.payload = data
        return self

    def delete(self):
        self.operation = "delete"
        self.payload = None
        return self

    def execute(self) -> SupabaseResponse:
        method = "GET"
        params: Dict[str, Any] = {}
        prefer: List[str] = []
        headers = self.client.base_headers.copy()
        url = f"{self.client.base_url}/{self.table_name}"

        if self.operation == "select":
            params["select"] = self.select_columns
            if self.count_option:
                prefer.append(f"count={self.count_option}")
        elif self.operation == "insert":
            method = "POST"
            prefer.append("return=representation")
        elif self.operation == "upsert":
            method = "POST"
            prefer.extend(["return=representation", "resolution=merge-duplicates"])
            if self.on_conflict:
                params["on_conflict"] = self.on_conflict
        elif self.operation == "update":
            method = "PATCH"
            prefer.append("return=representation")
        elif self.operation == "delete":
            method = "DELETE"
            prefer.append("return=representation")

        # Apply filters
        for field, expressions in self.filters.items():
            params[field] = expressions

        # Apply ordering
        if self.orderings:
            params["order"] = ",".join(self.orderings)

        # Apply limit/offset
        if self.range_start is not None and self.range_end is not None:
            params["limit"] = self.range_end - self.range_start + 1
            params["offset"] = self.range_start
        else:
            if self.limit_value is not None:
                params["limit"] = self.limit_value
            if self.offset_value is not None:
                params["offset"] = self.offset_value

        if prefer:
            headers["Prefer"] = ",".join(prefer)

        json_payload = None
        if self.operation in {"insert", "upsert", "update"} and self.payload is not None:
            json_payload = self.payload

        response = self.client.session.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            json=json_payload
        )

        if not response.ok:
            self._reset()
            raise Exception(f"Supabase request failed ({response.status_code}): {response.text}")

        data = None
        if response.content:
            try:
                data = response.json()
            except ValueError:
                data = response.text

        count = None
        content_range = response.headers.get("Content-Range")
        if content_range and "/" in content_range:
            try:
                count_part = content_range.split("/")[-1]
                if count_part != "*":
                    count = int(count_part)
            except ValueError:
                count = None

        result = SupabaseResponse(data=data, count=count)
        self._reset()
        return result

    @staticmethod
    def _encode_value(value: Any) -> str:
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif value is None:
            value = "null"
        return quote(str(value), safe="*-_.~:TtZz+")


class SupabaseClient:
    def __init__(self):
        """Initialize Supabase REST client"""
        if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
            raise ValueError("Supabase URL and publishable key are required")

        self.base_url = f"{SUPABASE_URL.rstrip('/')}/rest/v1"
        auth_key = SUPABASE_SECRET_KEY or SUPABASE_PUBLISHABLE_KEY
        api_key = SUPABASE_SECRET_KEY or SUPABASE_PUBLISHABLE_KEY
        self.base_headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.session = requests.Session()
        self.session.headers.update(self.base_headers)

        # Backwards compatibility shim (`self.supabase.table(...)`)
        self.supabase = self
        logger.info("Supabase REST client initialized successfully")

    def table(self, table_name: str) -> SupabaseQuery:
        return SupabaseQuery(self, table_name)
    
    def test_connection(self, table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> bool:
        """Test the Supabase connection"""
        try:
            # Try a simple query to test connection
            self.supabase.table(table_name).select('*').limit(1).execute()
            logger.info("Supabase connection test successful.")
            return True
        except Exception as e:
            logger.error(f"Supabase connection test failed: {str(e)}")
            return False
    
    def insert_tender(self, tender_data: Dict[str, Any], table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> bool:
        """Insert a single tender record"""
        try:
            # Clean and prepare data
            cleaned_data = self._prepare_tender_data(tender_data)
            
            # Insert with upsert to handle duplicates
            result = self.supabase.table(table_name).upsert(
                cleaned_data,
                on_conflict='resource_id'
            ).execute()
            
            logger.debug(f"Inserted tender {cleaned_data.get('resource_id')}")
            return True
            
        except Exception as e:
            logger.error(f"Error inserting tender {tender_data.get('resource_id', 'unknown')}: {str(e)}")
            return False
    
    def insert_tenders_batch(self, tenders_list: List[Dict[str, Any]], table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> Dict[str, int]:
        """Insert multiple tender records in batch"""
        try:
            # Clean and prepare all data
            cleaned_data = [self._prepare_tender_data(tender) for tender in tenders_list]
            
            # Insert batch with upsert
            result = self.supabase.table(table_name).upsert(
                cleaned_data,
                on_conflict='resource_id'
            ).execute()
            
            success_count = len(result.data) if result.data else 0
            logger.info(f"Inserted {success_count}/{len(tenders_list)} records into {table_name}.")
            
            return {
                'total': len(tenders_list),
                'success': success_count,
                'failed': len(tenders_list) - success_count
            }
            
        except Exception as e:
            logger.error(f"Error batch inserting tenders: {str(e)}")
            return {
                'total': len(tenders_list),
                'success': 0,
                'failed': len(tenders_list)
            }

    def insert_awards_batch(self, awards_list: List[Dict[str, Any]], table_name: str = SUPABASE_TABLE_AWARDS_ALL) -> Dict[str, int]:
        """Insert multiple award records in batch"""
        try:
            # Clean and prepare all data
            cleaned_data = [self._prepare_award_data(award) for award in awards_list]
            
            # Insert batch with upsert
            result = self.supabase.table(table_name).upsert(
                cleaned_data,
                on_conflict='resource_id'
            ).execute()
            
            success_count = len(result.data) if result.data else 0
            logger.info(f"Inserted {success_count}/{len(awards_list)} records into {table_name}.")
            
            return {
                'total': len(awards_list),
                'success': success_count,
                'failed': len(awards_list) - success_count
            }
            
        except Exception as e:
            logger.error(f"Error batch inserting awards: {str(e)}")
            return {
                'total': len(awards_list),
                'success': 0,
                'failed': len(awards_list)
            }
    
    def get_tender_by_resource_id(self, resource_id: str, table_name: str = SUPABASE_TABLE_TENDERS_ALL) -> Optional[Dict[str, Any]]:
        """Get a tender by resource ID"""
        try:
            result = self.supabase.table(table_name).select('*').eq('resource_id', resource_id).execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0]
            return None
            
        except Exception as e:
            logger.error(f"Error getting tender {resource_id}: {str(e)}")
            return None
    
    def get_tenders_without_details(self, limit: int = 100, table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> List[Dict[str, Any]]:
        """Get tenders that haven't had their detail pages extracted yet"""
        try:
            result = self.supabase.table(table_name).select(
                'resource_id, detail_url, title'
            ).eq('detail_page_extracted', False).limit(limit).execute()
            
            return result.data if result.data else []
            
        except Exception as e:
            logger.error(f"Error getting tenders without details: {str(e)}")
            return []
    
    def update_tender_details(self, resource_id: str, detail_data: Dict[str, Any], table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> bool:
        """Update a tender with detail page data"""
        try:
            # Prepare detail data and mark as extracted
            detail_data['detail_page_extracted'] = True
            cleaned_data = self._prepare_tender_data(detail_data)
            
            result = self.supabase.table(table_name).update(
                cleaned_data
            ).eq('resource_id', resource_id).execute()
            
            logger.debug(f"Updated tender details for {resource_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating tender details {resource_id}: {str(e)}")
            return False
    
    def get_statistics(self, table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> Dict[str, Any]:
        """Get database statistics"""
        try:
            # Total count
            total_result = self.supabase.table(table_name)\
                .select('resource_id', count='exact')\
                .limit(1)\
                .execute()
            total_count = total_result.count or (len(total_result.data) if total_result.data else 0)
            
            # Count with details extracted
            details_result = self.supabase.table(table_name)\
                .select('resource_id', count='exact')\
                .eq('detail_page_extracted', True)\
                .limit(1)\
                .execute()
            details_count = details_result.count or (len(details_result.data) if details_result.data else 0)
            
            # Recent extractions (last 24 hours)
            threshold_dt = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=24)
            threshold = threshold_dt.isoformat()
            recent_result = self.supabase.table(table_name)\
                .select('resource_id', count='exact')\
                .gte('extraction_timestamp', threshold)\
                .limit(1)\
                .execute()
            recent_count = recent_result.count or (len(recent_result.data) if recent_result.data else 0)
            
            return {
                'total_tenders': total_count,
                'with_details': details_count,
                'without_details': total_count - details_count,
                'recent_extractions': recent_count,
                'completion_rate': (details_count / total_count * 100) if total_count > 0 else 0
            }
            
        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}")
            return {}

    def get_latest_publication_date(self, table_name: str = SUPABASE_TABLE_TENDERS_ALL) -> Optional[datetime]:
        """Fetch the most recent publication date from the database."""
        try:
            result = self.supabase.table(table_name)\
                .select('publication_date_parsed')\
                .order('publication_date_parsed', desc=True)\
                .limit(1)\
                .execute()
            if result.data and len(result.data) > 0 and result.data[0].get('publication_date_parsed'):
                date_str = result.data[0]['publication_date_parsed']
                return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return None
        except Exception as e:
            logger.error(f"Error getting latest publication date: {str(e)}")
            return None

    def get_latest_award_date(self, table_name: str = SUPABASE_TABLE_AWARDS_ALL) -> Optional[datetime]:
        """Fetch the most recent award_date from the database."""
        try:
            result = self.supabase.table(table_name)\
                .select('award_date_parsed')\
                .order('award_date_parsed', desc=True)\
                .limit(1)\
                .execute()
            if result.data and len(result.data) > 0 and result.data[0].get('award_date_parsed'):
                date_str = result.data[0]['award_date_parsed']
                return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return None
        except Exception as e:
            logger.error(f"Error getting latest award date: {str(e)}")
            return None

    def get_active_tender_ids(self, table_name: str = SUPABASE_TABLE_TENDERS_ALL) -> set[str]:
        """Fetch resource IDs of tenders whose submission deadline is in the future."""
        active_ids = set()
        try:
            current_iso = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
            page_size = 1000
            for offset in range(0, 100000, page_size):
                result = self.supabase.table(table_name)\
                    .select('resource_id')\
                    .gte('submission_deadline_parsed', current_iso)\
                    .range(offset, offset + page_size - 1)\
                    .execute()
                
                if not result.data:
                    break
                    
                for row in result.data:
                    if row.get('resource_id'):
                        active_ids.add(row['resource_id'])
                        
                if len(result.data) < page_size:
                    break
            return active_ids
        except Exception as e:
            logger.error(f"Error fetching active tender IDs: {str(e)}")
            return active_ids

    def delete_expired_tenders(self, reference_time: Optional[datetime] = None, table_name: str = SUPABASE_TABLE_TENDERS_CURRENT) -> int:
        """Remove tenders whose submission deadline has already passed."""
        if reference_time is None:
            reference_time = datetime.utcnow().replace(tzinfo=timezone.utc)
        elif reference_time.tzinfo is None:
            reference_time = reference_time.replace(tzinfo=timezone.utc)
        else:
            reference_time = reference_time.astimezone(timezone.utc)

        cutoff = reference_time.isoformat()
        try:
            result = self.supabase.table(table_name)\
                .delete()\
                .lt('submission_deadline_parsed', cutoff)\
                .execute()

            deleted_count = len(result.data) if result.data else 0
            logger.info(f"Deleted {deleted_count} expired tenders (submission deadline before {cutoff}).")
            return deleted_count
        except Exception as e:
            logger.error(f"Error deleting expired tenders: {str(e)}")
            return 0
    
    def _prepare_tender_data(self, tender_data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean and prepare tender data for database insertion"""
        cleaned: Dict[str, Any] = {k: (v.copy() if isinstance(v, list) else v) for k, v in TENDER_FIELD_DEFAULTS.items()}

        # Overlay incoming data but only keep known fields
        for key, value in tender_data.items():
            if key not in cleaned:
                continue
            cleaned[key] = value

        # Normalise array fields
        for field in ARRAY_FIELDS:
            value = cleaned.get(field)
            if value is None:
                cleaned[field] = []
            elif isinstance(value, list):
                cleaned[field] = value
            elif value == '':
                cleaned[field] = []
            else:
                cleaned[field] = [value]

        # Normalise boolean fields
        for field in BOOLEAN_FIELDS:
            value = cleaned.get(field)
            if isinstance(value, str):
                cleaned[field] = value.strip().lower() in {'true', 'yes', '1', 'on'}

        # Ensure extraction_errors is always list of strings
        if cleaned['extraction_errors']:
            cleaned['extraction_errors'] = [str(err) for err in cleaned['extraction_errors']]

        if not cleaned['extraction_timestamp']:
            cleaned['extraction_timestamp'] = datetime.utcnow().replace(tzinfo=timezone.utc)

        # Normalise timestamps
        for field in TIMESTAMP_FIELDS:
            value = cleaned.get(field)
            cleaned[field] = self._normalise_timestamp(value)

        ordered_cleaned: Dict[str, Any] = OrderedDict()
        for key in TENDER_FIELD_DEFAULTS:
            ordered_cleaned[key] = cleaned.get(key)

        return ordered_cleaned

    def _prepare_award_data(self, award_data: Dict[str, Any]) -> Dict[str, Any]:
        """Clean and prepare award data for database insertion"""
        cleaned: Dict[str, Any] = {k: (v.copy() if isinstance(v, list) else v) for k, v in AWARD_FIELD_DEFAULTS.items()}

        # Overlay incoming data but only keep known fields
        for key, value in award_data.items():
            if key not in cleaned:
                continue
            cleaned[key] = value

        # Normalise array fields
        for field in AWARD_ARRAY_FIELDS:
            value = cleaned.get(field)
            if value is None:
                cleaned[field] = []
            elif isinstance(value, list):
                cleaned[field] = value
            elif value == '':
                cleaned[field] = []
            else:
                cleaned[field] = [value]

        # Normalise boolean fields
        for field in AWARD_BOOLEAN_FIELDS:
            value = cleaned.get(field)
            if isinstance(value, str):
                cleaned[field] = value.strip().lower() in {'true', 'yes', '1', 'on'}

        # Ensure extraction_errors is always list of strings
        if cleaned['extraction_errors']:
            cleaned['extraction_errors'] = [str(err) for err in cleaned['extraction_errors']]

        if not cleaned['extraction_timestamp']:
            cleaned['extraction_timestamp'] = datetime.utcnow().replace(tzinfo=timezone.utc)

        # Normalise timestamps
        for field in AWARD_TIMESTAMP_FIELDS:
            value = cleaned.get(field)
            cleaned[field] = self._normalise_timestamp(value)

        ordered_cleaned: Dict[str, Any] = OrderedDict()
        for key in AWARD_FIELD_DEFAULTS:
            ordered_cleaned[key] = cleaned.get(key)

        return ordered_cleaned

    @staticmethod
    def _normalise_timestamp(value: Any) -> Optional[str]:
        if not value:
            return None

        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            value = value.replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return value
        else:
            return value

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds")