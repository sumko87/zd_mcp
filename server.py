"""
Zendesk MCP Server — OAuth-proxied, Streamable HTTP.

Provides comprehensive Zendesk API access via MCP (Model Context Protocol).
Each end user logs in with their OWN Zendesk account (no shared API key).

Features:
  - Ticket Management: Get, update, comment, tag tickets
  - User Management: Get, search, update users
  - Organization Management: Full CRUD for organizations
  - Search & Query: Generic search across all Zendesk entities

Environment Variables Required:
  - PUBLIC_BASE_URL: Your MCP server URL (e.g., https://your-host.com)
  - ZENDESK_CLIENT_ID: Zendesk OAuth app client ID: zendesk_mcp
  - ZENDESK_CLIENT_SECRET: Zendesk OAuth app client secret : da4312539d17d8e5ef30c7b9149c8664874f73ef918339dee0fe9d59999c6d1c
  - ZENDESK_SUBDOMAIN: Your Zendesk subdomain (e.g., "personifyhealth1721848068")

Works locally, on Render, and on Azure Container Apps.
"""
# IMPORTANT: Import SSL patch FIRST to disable certificate verification
import ssl_patch

import os
import ssl
import warnings

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Disable SSL warnings for corporate environments
warnings.filterwarnings('ignore', message='Unverified HTTPS request')
ssl._create_default_https_context = ssl._create_unverified_context

# === (1) UPSTREAM PROVIDER — Zendesk OAuth =========================
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "personifyhealth1721848068")
UPSTREAM_AUTHORIZE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth/authorizations/new"
UPSTREAM_TOKEN_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth/tokens"
API_BASE = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

# === (2) SCOPES — Zendesk OAuth scopes ===
SCOPES = ["read", "write", "tickets:read", "tickets:write", "users:read", "users:write", "organizations:read", "organizations:write"]
# ===========================================================================

# --- From the environment (set these where you run/deploy; never hard-code) ---
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]        # e.g. https://your-host
CLIENT_ID = os.environ["ZENDESK_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZENDESK_CLIENT_SECRET"]

auth = OAuthProxy(
    upstream_authorization_endpoint=UPSTREAM_AUTHORIZE_URL,
    upstream_token_endpoint=UPSTREAM_TOKEN_URL,
    upstream_client_id=CLIENT_ID,
    upstream_client_secret=CLIENT_SECRET,
    base_url=PUBLIC_BASE_URL,
    redirect_path="/auth/callback",
    # DebugTokenVerifier suits providers that issue OPAQUE tokens with no JWKS
    # (e.g. Zendesk): the upstream API is the real validator. For a provider that
    # issues JWTs, use JWTVerifier(jwks_uri=..., issuer=..., audience=...) instead.
    token_verifier=DebugTokenVerifier(),
    valid_scopes=SCOPES,
)

mcp = FastMCP("Zendesk MCP", auth=auth)


def _headers() -> dict:
    # The logged-in user's upstream access token, used to call the API as them.
    return {"Authorization": f"Bearer {get_access_token().token}"}


# === (3) ZENDESK TOOLS =====================================================
# Each tool is async, has a clear docstring (the model reads it), and calls the
# Zendesk API as the logged-in user via _headers().

# ============================================================================
# TICKET MANAGEMENT TOOLS
# ============================================================================

@mcp.tool
async def get_tickets_from_view(view_id: str, per_page: int = 100) -> dict:
    """
    Retrieve tickets from a Zendesk view.
    
    Args:
        view_id: The ID of the Zendesk view to query
        per_page: Number of tickets to retrieve per page (default 100, max 500)
    
    Returns:
        JSON response with tickets array and pagination info
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get(f"/views/{view_id}/tickets", params={"per_page": per_page})
        r.raise_for_status()
        return r.json()


@mcp.tool
async def get_ticket(ticket_id: int) -> dict:
    """
    Get details of a single ticket by ID.
    
    Args:
        ticket_id: The Zendesk ticket ID
    
    Returns:
        JSON response with ticket details
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get(f"/tickets/{ticket_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool
async def update_ticket_comment(ticket_id: int, comment_body: str, public: bool = False) -> dict:
    """
    Add a comment to a ticket (internal note or public reply).
    
    Args:
        ticket_id: The Zendesk ticket ID
        comment_body: The text content of the comment
        public: If True, comment is public; if False, it's an internal note (default False)
    
    Returns:
        JSON response with updated ticket details
    """
    payload = {
        "ticket": {
            "comment": {
                "body": comment_body,
                "public": public
            }
        }
    }
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.put(f"/tickets/{ticket_id}", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def update_ticket_tags(ticket_ids: list[int], tags: list[str]) -> dict:
    """
    Add tags to one or more tickets (bulk operation).
    
    Args:
        ticket_ids: List of ticket IDs to update
        tags: List of tags to add to the tickets
    
    Returns:
        JSON response with job status for bulk update
    """
    ids_param = ",".join(str(tid) for tid in ticket_ids)
    payload = {
        "ticket": {
            "additional_tags": " ".join(tags)
        }
    }
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.put(f"/tickets/update_many.json?ids={ids_param}", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def update_ticket_custom_fields(ticket_id: int, custom_fields: dict) -> dict:
    """
    Update custom fields on a ticket.
    
    Args:
        ticket_id: The Zendesk ticket ID
        custom_fields: Dictionary mapping field IDs to values, e.g. {"360001234567": "value"}
    
    Returns:
        JSON response with updated ticket details
    """
    fields_array = [{"id": int(field_id), "value": value} for field_id, value in custom_fields.items()]
    payload = {
        "ticket": {
            "custom_fields": fields_array
        }
    }
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.put(f"/tickets/{ticket_id}", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def create_ticket(
    requester_id: int,
    subject: str,
    comment_body: str,
    status: str = "new",
    tags: list[str] = None,
    custom_fields: dict = None
) -> dict:
    """
    Create a new Zendesk ticket.
    
    Args:
        requester_id: The Zendesk user ID who is requesting the ticket
        subject: The ticket subject line
        comment_body: The initial comment/description for the ticket
        status: Ticket status - "new", "open", "pending", "hold", "solved", "closed" (default "new")
        tags: Optional list of tags to add to the ticket
        custom_fields: Optional dictionary mapping field IDs to values
    
    Returns:
        JSON response with created ticket details including ticket ID
    """
    payload = {
        "ticket": {
            "requester_id": requester_id,
            "subject": subject,
            "comment": {
                "body": comment_body,
                "public": False
            },
            "status": status
        }
    }
    
    if tags is not None:
        payload["ticket"]["tags"] = tags
    
    if custom_fields is not None:
        fields_array = [{"id": int(field_id), "value": value} for field_id, value in custom_fields.items()]
        payload["ticket"]["custom_fields"] = fields_array
    
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.post("/tickets", json=payload)
        r.raise_for_status()
        return r.json()


# ============================================================================
# USER MANAGEMENT TOOLS
# ============================================================================

@mcp.tool
async def get_user(user_id: int) -> dict:
    """
    Get details of a Zendesk user by ID.
    
    Args:
        user_id: The Zendesk user ID
    
    Returns:
        JSON response with user details including organization_id, email, role, etc.
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get(f"/users/{user_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool
async def update_user(
    user_id: int,
    organization_id: int = None,
    phone: str = None,
    user_fields: dict = None
) -> dict:
    """
    Update user properties including organization, phone, and custom user fields.
    
    Args:
        user_id: The Zendesk user ID
        organization_id: Optional organization ID to assign user to
        phone: Optional phone number
        user_fields: Optional dictionary of custom user field values, e.g. {"member_id": "12345"}
    
    Returns:
        JSON response with updated user details
    """
    payload = {"user": {}}
    
    if organization_id is not None:
        payload["user"]["organization_id"] = organization_id
    
    if phone is not None:
        payload["user"]["phone"] = phone
    
    if user_fields is not None:
        payload["user"]["user_fields"] = user_fields
    
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.put(f"/users/{user_id}", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def search_users(query: str) -> dict:
    """
    Search for users by email, name, or other criteria.
    
    Args:
        query: Search query string (e.g., email address, name)
    
    Returns:
        JSON response with array of matching users
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get("/users/search", params={"query": query})
        r.raise_for_status()
        return r.json()


# ============================================================================
# ORGANIZATION MANAGEMENT TOOLS
# ============================================================================

@mcp.tool
async def get_organization(org_id: int) -> dict:
    """
    Get details of a Zendesk organization by ID.
    
    Args:
        org_id: The Zendesk organization ID
    
    Returns:
        JSON response with organization details
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get(f"/organizations/{org_id}")
        r.raise_for_status()
        return r.json()


@mcp.tool
async def search_organizations(name: str) -> dict:
    """
    Search for organizations by name.
    
    Args:
        name: Organization name to search for
    
    Returns:
        JSON response with array of matching organizations
    """
    search_query = f"type:organization name:{name}"
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get("/search", params={"query": search_query})
        r.raise_for_status()
        return r.json()


@mcp.tool
async def create_organization(
    name: str,
    organization_fields: dict = None,
    tags: list[str] = None
) -> dict:
    """
    Create a new Zendesk organization.
    
    Args:
        name: Organization name
        organization_fields: Optional dictionary of custom organization fields
        tags: Optional list of tags to add to the organization
    
    Returns:
        JSON response with created organization details
    """
    payload = {
        "organization": {
            "name": name
        }
    }
    
    if organization_fields is not None:
        payload["organization"]["organization_fields"] = organization_fields
    
    if tags is not None:
        payload["organization"]["tags"] = tags
    
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.post("/organizations", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def update_organization(org_id: int, organization_fields: dict) -> dict:
    """
    Update custom fields on an organization.
    
    Args:
        org_id: The Zendesk organization ID
        organization_fields: Dictionary of custom organization field values
    
    Returns:
        JSON response with updated organization details
    """
    payload = {
        "organization": {
            "organization_fields": organization_fields
        }
    }
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.put(f"/organizations/{org_id}", json=payload)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def list_organizations(per_page: int = 100) -> dict:
    """
    List all organizations with pagination support.
    
    Args:
        per_page: Number of organizations to retrieve per page (default 100)
    
    Returns:
        JSON response with organizations array and pagination info
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get("/organizations.json", params={"per_page": per_page})
        r.raise_for_status()
        return r.json()


# ============================================================================
# SEARCH & QUERY TOOLS
# ============================================================================

@mcp.tool
async def search_zendesk(query: str, type: str = None) -> dict:
    """
    Generic Zendesk search across tickets, users, and organizations.
    
    Args:
        query: Search query string (supports Zendesk search syntax)
        type: Optional filter by type: "ticket", "user", "organization", or None for all
    
    Returns:
        JSON response with search results
    """
    params = {"query": query}
    if type:
        params["query"] = f"type:{type} {query}"
    
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get("/search", params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool
async def get_user_tickets(user_id: int) -> dict:
    """
    Get all tickets requested by a specific user.
    
    Args:
        user_id: The Zendesk user ID
    
    Returns:
        JSON response with array of tickets
    """
    async with httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=30.0) as c:
        r = await c.get(f"/users/{user_id}/tickets/requested")
        r.raise_for_status()
        return r.json()
# ===========================================================================


# Health endpoint for deploy probes and quick liveness checks.
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    # Render/Azure inject PORT; locally it defaults to 8000. Always bind 0.0.0.0.
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="http", host="0.0.0.0", port=port)
