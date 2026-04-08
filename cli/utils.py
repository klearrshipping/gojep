import argparse
import logging

logger = logging.getLogger(__name__)

def trigger_sync(args: argparse.Namespace, resync: bool = False) -> None:
    """Helper to trigger Rclone sync if --sync flag is present."""
    if getattr(args, "sync", False):
        try:
            from tools.sync_drive import sync
            print("\n>>> Triggering pre-operation sync...")
            # Note: sync() should be called with quiet=True if integrated
            sync(resync=resync, quiet=True)
        except ImportError:
            logger.warning("Could not import tools.sync_drive. Ensure Rclone is set up.")
        except Exception as e:
            logger.error(f"Sync failed: {e}")
