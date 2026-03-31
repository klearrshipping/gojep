"""
Automated Data Reconciliation Module for GOJEP Scraper
Compares JSON extraction with Supabase database and fixes discrepancies
"""
import json
import os
import re
import logging
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from config.settings import (
    TENDERS_OUTPUT_DIRECTORY,
    SUPABASE_TABLE_TENDERS_ALL,
    SUPABASE_TABLE_TENDERS_CURRENT,
)
from db.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

class DataReconciliation:
    """Handles automatic reconciliation between JSON extracts and Supabase database"""
    
    def __init__(self, supabase_client: SupabaseClient = None):
        """Initialize reconciliation with Supabase client"""
        self.client = supabase_client or SupabaseClient()
        
        # Field length limits (adjust based on your database schema)
        self.field_limits = {
            'title': 300,
            'description': 1500,
            'detailed_description': 3000,
            'procuring_entity': 200,
            'procurement_type': 100,
            'procedure': 100,
            'evaluation_mechanism': 100,
            'procurement_method': 100,
            'retender_flag': 50,
            'procurement_technique': 50,
            'funding_source': 100,
            'special_differential_treatment': 200,
            'project_reference_number': 200,
            'country_contract_performance': 100,
            'non_petroleum_indicator': 50,
            'competition_unique_id': 100
        }
    
    def clean_text(self, text: str) -> str:
        """Clean text by removing problematic Unicode characters"""
        if not text:
            return text
        
        # Replace problematic Unicode characters
        replacements = {
            '–': '-',   # En dash to hyphen
            '—': '-',   # Em dash to hyphen
            '"': '"',   # Left double quote
            '"': '"',   # Right double quote
            ''': "'",   # Left single quote
            ''': "'",   # Right single quote
            '…': '...',  # Ellipsis
            '®': '(R)',  # Registered trademark
            '™': '(TM)', # Trademark
            '©': '(C)',  # Copyright
            '\u00a0': ' ',  # Non-breaking space
            '\u2028': ' ',  # Line separator
            '\u2029': ' ',  # Paragraph separator
        }
        
        for old, new in replacements.items():
            text = text.replace(old, new)
        
        # Remove control characters except newlines and tabs
        text = ''.join(char for char in text if ord(char) >= 32 or char in '\n\t')
        
        # Normalize whitespace
        text = ' '.join(text.split())
        
        return text.strip()
    
    def truncate_field(self, text: str, field_name: str) -> str:
        """Truncate text to fit database field limits"""
        if not text:
            return text
        
        max_length = self.field_limits.get(field_name, 500)  # Default 500 chars
        
        if len(text) <= max_length:
            return text
        
        # Truncate and add ellipsis
        truncated = text[:max_length-3] + "..."
        logger.debug(f"Truncated {field_name} from {len(text)} to {len(truncated)} chars")
        return truncated
    
    def fix_date_format(self, date_str: str, field_name: str) -> str:
        """Fix date formatting issues"""
        if not date_str:
            return None
        
        # If it's already in proper ISO format, return as is
        if isinstance(date_str, str) and 'T' in date_str and date_str.startswith('20'):
            return date_str
        
        # Try to parse and reformat problematic date formats
        try:
            # Handle various date formats
            if '/' in str(date_str):
                # Try DD/MM/YYYY HH:MM format
                dt = datetime.strptime(str(date_str), '%d/%m/%Y %H:%M')
                return dt.isoformat()
            elif '-' in str(date_str) and len(str(date_str)) > 10:
                # Try YYYY-MM-DD HH:MM format
                dt = datetime.strptime(str(date_str), '%Y-%m-%d %H:%M')
                return dt.isoformat()
        except ValueError as e:
            logger.warning(f"Could not parse date '{date_str}' for field {field_name}: {e}")
        
        # If we can't parse it, return None to avoid database errors
        logger.warning(f"Setting {field_name} to None due to unparseable date: {date_str}")
        return None
    
    def clean_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Clean a single record to fix known data quality issues"""
        cleaned = record.copy()
        
        # Track what we cleaned for reporting
        cleaning_applied = []
        
        # Clean and truncate text fields
        text_fields = ['title', 'description', 'detailed_description', 'procuring_entity',
                      'procurement_type', 'procedure', 'evaluation_mechanism', 'procurement_method',
                      'retender_flag', 'procurement_technique', 'funding_source',
                      'special_differential_treatment', 'project_reference_number',
                      'country_contract_performance', 'non_petroleum_indicator', 'competition_unique_id']
        
        for field in text_fields:
            if field in cleaned and cleaned[field]:
                original = cleaned[field]
                # Clean Unicode issues
                cleaned[field] = self.clean_text(str(cleaned[field]))
                # Truncate to field limits
                cleaned[field] = self.truncate_field(cleaned[field], field)
                
                if cleaned[field] != original:
                    cleaning_applied.append(f"{field}_cleaned")
        
        # Fix date formatting
        date_fields = ['publication_date', 'submission_deadline', 'original_submission_deadline',
                      'clarification_period_end', 'bid_opening_date', 'site_visit_date']
        
        for field in date_fields:
            if field in cleaned and cleaned[field]:
                original = cleaned[field]
                cleaned[field] = self.fix_date_format(cleaned[field], field)
                
                if cleaned[field] != original:
                    cleaning_applied.append(f"{field}_date_fixed")
        
        # Ensure boolean fields are proper booleans
        bool_fields = ['detail_page_extracted', 'framework_agreement_establishment',
                      'contract_awarded_in_lots', 'pe_audit_correspondence']
        
        for field in bool_fields:
            if field in cleaned and cleaned[field] is not None:
                if isinstance(cleaned[field], str):
                    original = cleaned[field]
                    cleaned[field] = cleaned[field].lower() in ['true', '1', 'yes', 'on']
                    if cleaned[field] != original:
                        cleaning_applied.append(f"{field}_bool_fixed")
        
        # Clean list fields
        list_fields = ['ppc_ncc_categories', 'cpv_codes']
        for field in list_fields:
            if field in cleaned and cleaned[field]:
                if isinstance(cleaned[field], list):
                    original_count = len(cleaned[field])
                    cleaned[field] = [self.clean_text(str(item)) for item in cleaned[field]]
                    # Remove empty items
                    cleaned[field] = [item for item in cleaned[field] if item.strip()]
                    if len(cleaned[field]) != original_count:
                        cleaning_applied.append(f"{field}_list_cleaned")
        
        # Add cleaning metadata
        if cleaning_applied:
            cleaned['_cleaning_applied'] = cleaning_applied
            logger.debug(f"Applied cleaning to record {cleaned.get('resource_id')}: {cleaning_applied}")
        
        return cleaned
    
    def get_database_resource_ids(self) -> set:
        """Get all resource IDs currently in the database"""
        logger.info("Fetching all database resource IDs...")
        
        all_resource_ids = set()
        page_size = 1000
        offset = 0
        
        while True:
            try:
                page_result = self.client.supabase.table(SUPABASE_TABLE_TENDERS_ALL)\
                    .select('resource_id')\
                    .range(offset, offset + page_size - 1)\
                    .execute()
                
                if not page_result.data:
                    break
                
                page_ids = {record['resource_id'] for record in page_result.data}
                all_resource_ids.update(page_ids)
                
                logger.debug(f"Fetched page {offset//page_size + 1}: {len(page_ids)} records")
                
                if len(page_result.data) < page_size:
                    break
                
                offset += page_size
                
            except Exception as e:
                logger.error(f"Error fetching database resource IDs: {e}")
                break
        
        logger.info(f"Found {len(all_resource_ids)} records in database ({SUPABASE_TABLE_TENDERS_ALL}).")
        return all_resource_ids
    
    def find_latest_json_file(self, extraction_dir: Optional[str] = None) -> str:
        """Find the most recent JSON extraction file."""
        if extraction_dir is None:
            extraction_dir = TENDERS_OUTPUT_DIRECTORY
        if not os.path.exists(extraction_dir):
            raise FileNotFoundError(f"Extraction directory '{extraction_dir}' not found")
        
        json_files = [
            f for f in os.listdir(extraction_dir)
            if f.endswith(".json") and f.startswith("tenders_")
        ]
        if not json_files:
            raise FileNotFoundError(f"No tenders_*.json files found in '{extraction_dir}'")

        latest_file = max(json_files, key=lambda f: os.path.getmtime(os.path.join(extraction_dir, f)))
        file_path = os.path.join(extraction_dir, latest_file)
        
        logger.info(f"Using latest extraction file: {latest_file}")
        return file_path
    
    def reconcile_data(self, json_file_path: str = None, json_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Main reconciliation function
        Returns summary of reconciliation results
        """
        logger.info("Starting data reconciliation.")
        
        if json_data is None:
            # Find JSON file if not provided
            if not json_file_path:
                json_file_path = self.find_latest_json_file()
            
            # Load JSON data
            logger.info(f"Loading JSON data from: {json_file_path}")
            with open(json_file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
        else:
            logger.info(f"Using pre-processed JSON data with {len(json_data)} records.")
        
        # Get database state
        db_resource_ids = self.get_database_resource_ids()
        
        # Find differences
        json_resource_ids = {record.get('resource_id') for record in json_data if record.get('resource_id')}
        missing_from_db = json_resource_ids - db_resource_ids
        
        # Create reconciliation summary
        summary = {
            'json_total': len(json_data),
            'json_unique_ids': len(json_resource_ids),
            'database_total': len(db_resource_ids),
            'missing_from_db': len(missing_from_db),
            'success_rate_before': len(db_resource_ids) / len(json_data) * 100,
            'records_to_fix': [],
            'cleaning_stats': {
                'records_cleaned': 0,
                'successful_insertions': 0,
                'failed_insertions': 0,
                'cleaning_operations': {}
            }
        }
        
        logger.info("Reconciliation analysis:")
        logger.info(f"   JSON records: {summary['json_total']:,}")
        logger.info(f"   Database records: {summary['database_total']:,}")
        logger.info(f"   Missing from DB: {summary['missing_from_db']:,}")
        logger.info(f"   Success rate: {summary['success_rate_before']:.1f}%")
        
        # If no missing records, we're done
        if not missing_from_db:
            logger.info("No reconciliation needed - all records present in database.")
            return summary
        
        # Find and clean missing records
        logger.info(f"Cleaning {len(missing_from_db)} missing records...")
        
        missing_records = [r for r in json_data if r.get('resource_id') in missing_from_db]
        cleaned_records = []
        cleaning_operations = {}
        
        for record in missing_records:
            cleaned = self.clean_record(record)
            cleaned_records.append(cleaned)
            
            # Track cleaning operations
            if '_cleaning_applied' in cleaned:
                for operation in cleaned['_cleaning_applied']:
                    cleaning_operations[operation] = cleaning_operations.get(operation, 0) + 1
                # Remove metadata before database insertion
                del cleaned['_cleaning_applied']
        
        summary['cleaning_stats']['records_cleaned'] = len(cleaned_records)
        summary['cleaning_stats']['cleaning_operations'] = cleaning_operations
        
        # Insert cleaned records in batches
        logger.info(f"Inserting {len(cleaned_records)} cleaned records...")
        
        batch_size = 20  # Smaller batches for better error handling
        successful_inserts = 0
        failed_inserts = 0
        
        for i in range(0, len(cleaned_records), batch_size):
            batch = cleaned_records[i:i+batch_size]
            batch_num = i // batch_size + 1
            
            try:
                result = self.client.insert_tenders_batch(batch, table_name=SUPABASE_TABLE_TENDERS_ALL)
                batch_success = result.get('success', 0)
                batch_failed = result.get('failed', 0)
                
                successful_inserts += batch_success
                failed_inserts += batch_failed
                
                if batch_success > 0:
                    logger.info(f"   Batch {batch_num}: {batch_success} inserted, {batch_failed} failed.")
                else:
                    logger.warning(f"   Batch {batch_num}: All {len(batch)} records failed.")
                    
            except Exception as e:
                failed_inserts += len(batch)
                logger.error(f"   Batch {batch_num}: Exception - {str(e)[:100]}")
        
        # Update summary
        summary['cleaning_stats']['successful_insertions'] = successful_inserts
        summary['cleaning_stats']['failed_insertions'] = failed_inserts
        
        # Final database count
        final_db_count = len(self.get_database_resource_ids())
        summary['database_total_after'] = final_db_count
        summary['success_rate_after'] = final_db_count / len(json_data) * 100
        
        # Log final results
        logger.info("Reconciliation completed:")
        logger.info(f"   Records cleaned and processed: {summary['cleaning_stats']['records_cleaned']:,}")
        logger.info(f"   Successfully inserted: {successful_inserts:,}")
        logger.info(f"   Failed insertions: {failed_inserts:,}")
        logger.info(f"   Final database total: {final_db_count:,}")
        logger.info(f"   Final success rate: {summary['success_rate_after']:.1f}%")
        
        if summary['success_rate_after'] >= 99.0:
            logger.info("Reconciliation achieved excellent results.")
        elif summary['success_rate_after'] >= 95.0:
            logger.info("Reconciliation achieved good results.")
        else:
            logger.warning("Reconciliation results need attention.")
        
        return summary

def run_reconciliation(json_file_path: str = None, json_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Standalone function to run reconciliation
    Can be called from other modules
    """
    reconciler = DataReconciliation()
    return reconciler.reconcile_data(json_file_path=json_file_path, json_data=json_data)

if __name__ == "__main__":
    # If run directly, perform reconciliation
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    
    try:
        summary = run_reconciliation()
        print("\n" + "="*60)
        print("RECONCILIATION SUMMARY:")
        print(f"   - JSON total: {summary['json_total']:,}")
        print(f"   - Database before: {summary['database_total']:,}")
        print(f"   - Database after: {summary.get('database_total_after', summary['database_total']):,}")
        print(f"   - Success rate: {summary.get('success_rate_after', summary['success_rate_before']):.1f}%")
        print("="*60)
    except Exception as e:
        logger.error(f"Reconciliation failed: {e}")
        raise 