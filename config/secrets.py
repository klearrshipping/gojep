#!/usr/bin/env python3
"""
New Secret Manager
Clean, simple secret management for Google Cloud Secret Manager
"""

import subprocess
import os
from functools import lru_cache

# Project ID for the new secrets project
PROJECT_ID = "secrets-476114"

@lru_cache(maxsize=128)
def get_secret(secret_name: str, project_id: str = PROJECT_ID) -> str:
    """
    Get a secret from Google Cloud Secret Manager
    
    Args:
        secret_name: Name of the secret to retrieve
        project_id: Google Cloud project ID (defaults to PROJECT_ID)
        
    Returns:
        The secret value as a string
        
    Raises:
        Exception: If the secret cannot be retrieved
    """
    try:
        # Use PowerShell to execute gcloud command (Windows compatibility)
        result = subprocess.run([
            "powershell", "-Command", 
            f"gcloud secrets versions access latest --secret={secret_name} --project={project_id}"
        ], capture_output=True, text=True, check=True)
        
        return result.stdout.strip()
        
    except subprocess.CalledProcessError as e:
        raise Exception(f"Failed to get secret '{secret_name}': {e.stderr}")
    except FileNotFoundError:
        raise Exception("PowerShell command not found.")

def get_secret_or_fallback(secret_name: str, fallback_env_var: str = None, default: str = None, project_id: str = PROJECT_ID) -> str:
    """
    Get a secret with fallback to environment variable and default value
    
    Args:
        secret_name: Name of the secret in Secret Manager
        fallback_env_var: Environment variable to check if secret fails
        default: Default value if both secret and env var fail
        project_id: Google Cloud project ID
        
    Returns:
        The secret/env var/default value as a string
    """
    try:
        return get_secret(secret_name, project_id)
    except Exception as e:
        print(f"Warning: Could not get secret '{secret_name}': {e}")
        
        if fallback_env_var and os.environ.get(fallback_env_var):
            print(f"Using fallback environment variable '{fallback_env_var}'")
            return os.environ.get(fallback_env_var)
        
        if default:
            print(f"Using default value for '{secret_name}'")
            return default
            
        raise Exception(f"No secret, environment variable, or default available for '{secret_name}'")

def test_secret_access(project_id: str = PROJECT_ID):
    """Test that we can access secrets"""
    try:
        # Test with a known secret
        test_value = get_secret("supabase-url-procurement", project_id)
        print(f"Secret access working for project '{project_id}'! (got: {test_value[:20]}...)")
        return True
    except Exception as e:
        print(f"Secret access failed for project '{project_id}': {e}")
        return False

def list_secrets(project_id: str = PROJECT_ID):
    """List all available secrets"""
    try:
        result = subprocess.run([
            "powershell", "-Command", 
            f"gcloud secrets list --project={project_id}"
        ], capture_output=True, text=True, check=True)
        
        print(f"Available secrets in project '{project_id}':")
        print(result.stdout)
        return True
    except Exception as e:
        print(f"Failed to list secrets: {e}")
        return False

if __name__ == "__main__":
    print("Testing new secret manager...")
    test_secret_access()
    print("\nListing available secrets:")
    list_secrets()
