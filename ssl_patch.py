"""
SSL Certificate Verification Patch for Corporate Environments

This module patches httpx to disable SSL verification globally.
Import this BEFORE importing FastMCP or any other libraries.
"""
import ssl
import httpx
from typing import Any

# Store the original AsyncClient
_original_async_client = httpx.AsyncClient

class PatchedAsyncClient(httpx.AsyncClient):
    """AsyncClient that always disables SSL verification"""
    
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Force verify=False for all requests
        kwargs['verify'] = False
        super().__init__(*args, **kwargs)

# Monkey patch httpx.AsyncClient
httpx.AsyncClient = PatchedAsyncClient

# Also patch the default SSL context
ssl._create_default_https_context = ssl._create_unverified_context

print("✅ SSL verification disabled for all httpx requests")
