"""
Cleanup expired tenders from gojep_tenders_current.
"""
import os
import sys
import logging
from datetime import datetime, timezone

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.client.supabase_client import SupabaseClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "logs", "cleanup_expired.log"))
    ]
)
logger = logging.getLogger("cleanup_expired")

def run_cleanup():
    try:
        print(f"Starting cleanup at {datetime.now(timezone.utc).isoformat()}...")
        client = SupabaseClient()
        count = client.delete_expired_tenders()
        print(f"Successfully deleted {count} expired records from current tenders.")
        return True
    except Exception as e:
        logger.error(f"Cleanup failed: {e}", exc_info=True)
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    run_cleanup()
