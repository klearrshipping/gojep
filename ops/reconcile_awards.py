"""
Data Reconciliation module for GOJEP Awards.
"""
import json
import logging
import os
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timezone

from config.settings import AWARDS_OUTPUT_DIRECTORY, SUPABASE_TABLE_AWARDS_ALL
from db.supabase_client import SupabaseClient, AWARD_FIELD_DEFAULTS

logger = logging.getLogger(__name__)

class AwardReconciliation:
    def __init__(self):
        try:
            self.db_client = SupabaseClient()
            self._db_connected = True
        except Exception as e:
            logger.error(f"Failed to initialise database client: {e}")
            self._db_connected = False
            
    def get_database_resource_ids(self) -> set:
        """Get all existing resource IDs from the database."""
        if not self._db_connected:
            return set()
            
        logger.info(f"Fetching existing resource IDs from {SUPABASE_TABLE_AWARDS_ALL}...")
        try:
            # Query all resource IDs in batches
            existing_ids = set()
            page_size = 1000
            for offset in range(0, 100000, page_size):
                result = self.db_client.supabase.table(SUPABASE_TABLE_AWARDS_ALL)\
                    .select('resource_id')\
                    .range(offset, offset + page_size - 1)\
                    .execute()
                
                if not result.data:
                    break
                    
                for row in result.data:
                    if row.get('resource_id'):
                        existing_ids.add(row['resource_id'])
                        
                if len(result.data) < page_size:
                    break
                    
            logger.info(f"Found {len(existing_ids)} existing awards in database.")
            return existing_ids
            
        except Exception as e:
            logger.error(f"Error fetching database IDs: {str(e)}")
            return set()
            
    def reconcile_data(self, json_file_path: str = None, json_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Main reconciliation function"""
        logger.info("Starting award data reconciliation.")
        
        if json_data is None and json_file_path:
            logger.info(f"Loading JSON data from: {json_file_path}")
            with open(json_file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
        elif json_data is None:
            raise ValueError("Must provide either json_data or json_file_path")
            
        logger.info(f"Using JSON data with {len(json_data)} records.")
        
        db_resource_ids = self.get_database_resource_ids()
        db_total_before = len(db_resource_ids)
        
        # We always want to upsert new details or fix anything.
        # But for statistics we can divide into new vs exist.
        new_records = []
        updated_records = []
        
        for record in json_data:
            rid = record.get('resource_id')
            if not rid:
                continue
                
            if rid in db_resource_ids:
                updated_records.append(record)
            else:
                new_records.append(record)
                
        logger.info(f"Reconciliation plan: Insert {len(new_records)} new, update {len(updated_records)} existing.")
        
        all_to_process = new_records + updated_records
        successful_insertions = 0
        
        if all_to_process and self._db_connected:
            logger.info(f"Pushing {len(all_to_process)} records to database...")
            batch_result = self.db_client.insert_awards_batch(all_to_process)
            successful_insertions = batch_result.get('success', 0)
            logger.info(f"Database batch insert completed: {successful_insertions} successful.")
            
        db_total_after = 0
        if self._db_connected:
            db_total_after = len(self.get_database_resource_ids())
            
        summary = {
            'json_total': len(json_data),
            'database_total': db_total_before,
            'new_records_found': len(new_records),
            'existing_updated': len(updated_records),
            'database_total_after': db_total_after,
            'cleaning_stats': {
                'successful_insertions': successful_insertions,
            }
        }
        
        return summary

def run_reconciliation(json_file_path: str = None, json_data: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    reconciler = AwardReconciliation()
    return reconciler.reconcile_data(json_file_path=json_file_path, json_data=json_data)
