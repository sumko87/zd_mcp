"""
Zendesk MCP Server — OAuth-proxied, Streamable HTTP.

Comprehensive Zendesk API access via MCP (Model Context Protocol).
Each end user logs in with their OWN Zendesk account (no shared API key); every
tool calls the Zendesk API as the logged-in user via their OAuth access token.

Tool categories:
  - Tickets         : read, create, update, comment, tag, bulk, merge, spam, delete
  - Users           : read, search, create, update, identities, memberships, tickets
  - Organizations   : read, search, create, update, delete, related, tickets
  - Groups          : read, create, update, delete
  - Views & Macros  : list/inspect views, run views, list/apply macros
  - Search          : generic search + count across all entities
  - Metadata        : ticket/user/org fields, forms, tags, brands
  - Satisfaction    : satisfaction ratings

Environment Variables:
  Required:
    - PUBLIC_BASE_URL      : This MCP server's public HTTPS URL (e.g. https://host)
    - ZENDESK_CLIENT_ID    : Zendesk OAuth app client ID
    - ZENDESK_CLIENT_SECRET: Zendesk OAuth app client secret  (NEVER hard-code this)
  Optional:
    - ZENDESK_SUBDOMAIN    : Zendesk subdomain (default "personifyhealth1721848068")
    - ZENDESK_CA_BUNDLE    : Path to a corporate root-CA PEM file. If set, TLS is
                             verified against it instead of the default trust store.
                             SSL verification is ALWAYS on — there is no disable flag.
    - PORT                 : Bind port (injected by Render/Azure; defaults to 8000)

Works locally, on Render, and on Azure Container Apps.
"""
import os

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from fastmcp.server.dependencies import get_access_token
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# === (1) UPSTREAM PROVIDER — Zendesk OAuth =================================
ZENDESK_SUBDOMAIN = os.environ.get("ZENDESK_SUBDOMAIN", "personifyhealth1721848068")
UPSTREAM_AUTHORIZE_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth/authorizations/new"
UPSTREAM_TOKEN_URL = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/oauth/tokens"
API_BASE = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"

# === (2) SCOPES — Zendesk OAuth scopes ====================================
SCOPES = [
    "read", "write",
    "tickets:read", "tickets:write",
    "users:read", "users:write",
    "organizations:read", "organizations:write",
]

# === (3) TLS verification ==================================================
# SSL verification is always enabled. If you sit behind a corporate proxy that
# intercepts TLS, point ZENDESK_CA_BUNDLE at the proxy's root CA (PEM) instead of
# turning verification off. Default (unset) uses httpx's bundled trust store.
_CA_BUNDLE = os.environ.get("ZENDESK_CA_BUNDLE")
_VERIFY: object = _CA_BUNDLE if _CA_BUNDLE else True

# === (4) Secrets from the environment (never hard-code) ====================
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
    # Zendesk issues OPAQUE access tokens (no JWKS), so the upstream API is the
    # real validator. DebugTokenVerifier passes them through to the API. For a
    # provider that issues JWTs, use JWTVerifier(jwks_uri=..., issuer=..., audience=...).
    token_verifier=DebugTokenVerifier(),
    valid_scopes=SCOPES,
)

mcp = FastMCP("Zendesk MCP", auth=auth)


# === (5) HTTP plumbing =====================================================

def _client() -> httpx.AsyncClient:
    """An httpx client authenticated as the logged-in user, with TLS verification on."""
    token = get_access_token().token
    return httpx.AsyncClient(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        verify=_VERIFY,
    )


async def _request(method: str, path: str, *, params: dict = None, json: dict = None) -> dict:
    """
    Call the Zendesk API as the logged-in user.

    Returns parsed JSON on success. On an HTTP/transport error, returns a
    structured dict ({"error": True, ...}) instead of raising, so the model gets
    an actionable message rather than a stack trace.
    """
    async with _client() as c:
        try:
            r = await c.request(method, path, params=params, json=json)
        except httpx.HTTPError as e:
            return {"error": True, "message": f"Request failed: {e}"}

    if r.is_error:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        result = {"error": True, "status_code": r.status_code, "detail": detail}
        if r.status_code == 429 and "Retry-After" in r.headers:
            result["retry_after_seconds"] = r.headers["Retry-After"]
        return result

    if not r.content:
        return {"success": True, "status_code": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"success": True, "status_code": r.status_code, "body": r.text}


def _csv(ids: list) -> str:
    return ",".join(str(i) for i in ids)


def _custom_fields(mapping: dict) -> list:
    """Convert {field_id: value} into Zendesk's [{"id": int, "value": ...}] shape."""
    return [{"id": int(fid), "value": val} for fid, val in mapping.items()]


# ============================================================================
# TICKETS — READ
# ============================================================================

@mcp.tool
async def list_tickets(per_page: int = 100, page: int = 1, sort_by: str = None, sort_order: str = None) -> dict:
    """
    List tickets in the account (paginated).

    Args:
        per_page: Tickets per page (max 100).
        page: 1-based page number.
        sort_by: Optional field to sort by, e.g. "created_at", "updated_at", "priority", "status".
        sort_order: "asc" or "desc".
    """
    params = {"per_page": per_page, "page": page}
    if sort_by:
        params["sort_by"] = sort_by
    if sort_order:
        params["sort_order"] = sort_order
    return await _request("GET", "/tickets.json", params=params)


@mcp.tool
async def get_ticket(ticket_id: int) -> dict:
    """Get a single ticket by ID."""
    return await _request("GET", f"/tickets/{ticket_id}.json")


@mcp.tool
async def get_many_tickets(ticket_ids: list[int]) -> dict:
    """Get multiple tickets at once by their IDs (more efficient than one-by-one)."""
    return await _request("GET", "/tickets/show_many.json", params={"ids": _csv(ticket_ids)})


@mcp.tool
async def get_ticket_comments(ticket_id: int, per_page: int = 100) -> dict:
    """
    List all comments (the conversation thread) on a ticket.

    Args:
        ticket_id: The ticket ID.
        per_page: Comments per page (max 100).
    """
    return await _request("GET", f"/tickets/{ticket_id}/comments.json", params={"per_page": per_page})


@mcp.tool
async def list_ticket_audits(ticket_id: int) -> dict:
    """List the full audit/change history of a ticket (every event and field change)."""
    return await _request("GET", f"/tickets/{ticket_id}/audits.json")


@mcp.tool
async def get_ticket_related(ticket_id: int) -> dict:
    """Get information related to a ticket (incidents, topic, followup IDs, etc.)."""
    return await _request("GET", f"/tickets/{ticket_id}/related.json")


@mcp.tool
async def list_recent_tickets(per_page: int = 100) -> dict:
    """List tickets the logged-in user has recently viewed."""
    return await _request("GET", "/tickets/recent.json", params={"per_page": per_page})


@mcp.tool
async def get_tickets_from_view(view_id: int, per_page: int = 100) -> dict:
    """
    Retrieve tickets that match a Zendesk view.

    Args:
        view_id: The view ID (from the view URL, e.g. /agent/filters/{view_id}).
        per_page: Tickets per page (max 100).
    """
    return await _request("GET", f"/views/{view_id}/tickets.json", params={"per_page": per_page})


@mcp.tool
async def count_tickets() -> dict:
    """Get the total number of tickets in the account."""
    return await _request("GET", "/tickets/count.json")


# ============================================================================
# TICKETS — WRITE
# ============================================================================

@mcp.tool
async def create_ticket(
    subject: str,
    comment_body: str,
    requester_id: int = None,
    status: str = "new",
    priority: str = None,
    type: str = None,
    assignee_id: int = None,
    group_id: int = None,
    tags: list[str] = None,
    public: bool = False,
    custom_fields: dict = None,
) -> dict:
    """
    Create a new ticket.

    Args:
        subject: Ticket subject line.
        comment_body: The first comment / description.
        requester_id: User ID of the requester (defaults to the logged-in user if omitted).
        status: "new", "open", "pending", "hold", "solved", or "closed".
        priority: "low", "normal", "high", or "urgent".
        type: "problem", "incident", "question", or "task".
        assignee_id: Agent user ID to assign the ticket to.
        group_id: Group ID to assign the ticket to.
        tags: Tags to add to the ticket.
        public: Whether the first comment is public (default False = internal note).
        custom_fields: {field_id: value} for custom ticket fields.
    """
    ticket = {
        "subject": subject,
        "comment": {"body": comment_body, "public": public},
        "status": status,
    }
    if requester_id is not None:
        ticket["requester_id"] = requester_id
    if priority is not None:
        ticket["priority"] = priority
    if type is not None:
        ticket["type"] = type
    if assignee_id is not None:
        ticket["assignee_id"] = assignee_id
    if group_id is not None:
        ticket["group_id"] = group_id
    if tags is not None:
        ticket["tags"] = tags
    if custom_fields is not None:
        ticket["custom_fields"] = _custom_fields(custom_fields)
    return await _request("POST", "/tickets.json", json={"ticket": ticket})


@mcp.tool
async def update_ticket(
    ticket_id: int,
    status: str = None,
    priority: str = None,
    type: str = None,
    subject: str = None,
    assignee_id: int = None,
    group_id: int = None,
    requester_id: int = None,
    organization_id: int = None,
    tags: list[str] = None,
    custom_fields: dict = None,
    comment_body: str = None,
    comment_public: bool = False,
) -> dict:
    """
    Update fields on a single ticket. Only the arguments you pass are changed.

    Args:
        ticket_id: The ticket ID.
        status: "new", "open", "pending", "hold", "solved", or "closed".
        priority: "low", "normal", "high", or "urgent".
        type: "problem", "incident", "question", or "task".
        subject: New subject line.
        assignee_id: Reassign to this agent user ID.
        group_id: Reassign to this group ID.
        requester_id: Change the requester user ID.
        organization_id: Change the ticket's organization ID.
        tags: Replace the ticket's tags with this list.
        custom_fields: {field_id: value} for custom ticket fields.
        comment_body: Optionally add a comment as part of the update.
        comment_public: Whether that comment is public (default False = internal note).
    """
    ticket: dict = {}
    if status is not None:
        ticket["status"] = status
    if priority is not None:
        ticket["priority"] = priority
    if type is not None:
        ticket["type"] = type
    if subject is not None:
        ticket["subject"] = subject
    if assignee_id is not None:
        ticket["assignee_id"] = assignee_id
    if group_id is not None:
        ticket["group_id"] = group_id
    if requester_id is not None:
        ticket["requester_id"] = requester_id
    if organization_id is not None:
        ticket["organization_id"] = organization_id
    if tags is not None:
        ticket["tags"] = tags
    if custom_fields is not None:
        ticket["custom_fields"] = _custom_fields(custom_fields)
    if comment_body is not None:
        ticket["comment"] = {"body": comment_body, "public": comment_public}
    return await _request("PUT", f"/tickets/{ticket_id}.json", json={"ticket": ticket})


@mcp.tool
async def add_ticket_comment(ticket_id: int, comment_body: str, public: bool = False) -> dict:
    """
    Add a comment to a ticket.

    Args:
        ticket_id: The ticket ID.
        comment_body: The comment text.
        public: True for a public reply, False for an internal note (default).
    """
    payload = {"ticket": {"comment": {"body": comment_body, "public": public}}}
    return await _request("PUT", f"/tickets/{ticket_id}.json", json=payload)


@mcp.tool
async def add_ticket_tags(ticket_id: int, tags: list[str]) -> dict:
    """Add tags to a ticket (keeps existing tags)."""
    return await _request("PUT", f"/tickets/{ticket_id}/tags.json", json={"tags": tags})


@mcp.tool
async def set_ticket_tags(ticket_id: int, tags: list[str]) -> dict:
    """Replace ALL tags on a ticket with the given list."""
    return await _request("POST", f"/tickets/{ticket_id}/tags.json", json={"tags": tags})


@mcp.tool
async def remove_ticket_tags(ticket_id: int, tags: list[str]) -> dict:
    """Remove the given tags from a ticket."""
    return await _request("DELETE", f"/tickets/{ticket_id}/tags.json", json={"tags": tags})


@mcp.tool
async def update_ticket_custom_fields(ticket_id: int, custom_fields: dict) -> dict:
    """
    Update custom fields on a ticket.

    Args:
        ticket_id: The ticket ID.
        custom_fields: {field_id: value}, e.g. {"360001234567": "value"}.
    """
    payload = {"ticket": {"custom_fields": _custom_fields(custom_fields)}}
    return await _request("PUT", f"/tickets/{ticket_id}.json", json=payload)


@mcp.tool
async def bulk_update_tickets(
    ticket_ids: list[int],
    status: str = None,
    priority: str = None,
    type: str = None,
    assignee_id: int = None,
    group_id: int = None,
    additional_tags: list[str] = None,
    remove_tags: list[str] = None,
    comment_body: str = None,
    comment_public: bool = False,
) -> dict:
    """
    Update many tickets at once (async job). Only the arguments you pass are changed.

    Args:
        ticket_ids: Tickets to update.
        status / priority / type: Apply these values to all listed tickets.
        assignee_id / group_id: Reassign all listed tickets.
        additional_tags: Tags to add to all listed tickets.
        remove_tags: Tags to remove from all listed tickets.
        comment_body: Optional comment added to all listed tickets.
        comment_public: Whether that comment is public (default False).

    Returns a job_status object; the update runs asynchronously on Zendesk.
    """
    ticket: dict = {}
    if status is not None:
        ticket["status"] = status
    if priority is not None:
        ticket["priority"] = priority
    if type is not None:
        ticket["type"] = type
    if assignee_id is not None:
        ticket["assignee_id"] = assignee_id
    if group_id is not None:
        ticket["group_id"] = group_id
    if additional_tags is not None:
        ticket["additional_tags"] = additional_tags
    if remove_tags is not None:
        ticket["remove_tags"] = remove_tags
    if comment_body is not None:
        ticket["comment"] = {"body": comment_body, "public": comment_public}
    return await _request(
        "PUT", "/tickets/update_many.json",
        params={"ids": _csv(ticket_ids)}, json={"ticket": ticket},
    )


@mcp.tool
async def merge_tickets(target_id: int, source_ids: list[int], target_comment: str = None, source_comment: str = None) -> dict:
    """
    Merge one or more source tickets into a target ticket.

    Args:
        target_id: The ticket that remains open after the merge.
        source_ids: Tickets to merge into the target (they get closed).
        target_comment: Private note added to the target ticket.
        source_comment: Private note added to each source ticket.
    """
    payload: dict = {"ids": source_ids}
    if target_comment is not None:
        payload["target_comment"] = target_comment
    if source_comment is not None:
        payload["source_comment"] = source_comment
    return await _request("POST", f"/tickets/{target_id}/merge.json", json=payload)


@mcp.tool
async def mark_ticket_as_spam(ticket_id: int) -> dict:
    """Mark a ticket as spam and suspend its requester."""
    return await _request("PUT", f"/tickets/{ticket_id}/mark_as_spam.json")


@mcp.tool
async def delete_ticket(ticket_id: int) -> dict:
    """Delete a ticket by ID (requires sufficient permissions)."""
    return await _request("DELETE", f"/tickets/{ticket_id}.json")


# ============================================================================
# USERS
# ============================================================================

@mcp.tool
async def get_current_user() -> dict:
    """Get the profile of the currently logged-in user (the OAuth identity)."""
    return await _request("GET", "/users/me.json")


@mcp.tool
async def list_users(per_page: int = 100, page: int = 1, role: str = None) -> dict:
    """
    List users (paginated).

    Args:
        per_page: Users per page (max 100).
        page: 1-based page number.
        role: Optional filter: "end-user", "agent", or "admin".
    """
    params = {"per_page": per_page, "page": page}
    if role:
        params["role"] = role
    return await _request("GET", "/users.json", params=params)


@mcp.tool
async def get_user(user_id: int) -> dict:
    """Get a user by ID (email, role, organization_id, custom fields, etc.)."""
    return await _request("GET", f"/users/{user_id}.json")


@mcp.tool
async def get_many_users(user_ids: list[int]) -> dict:
    """Get multiple users at once by their IDs."""
    return await _request("GET", "/users/show_many.json", params={"ids": _csv(user_ids)})


@mcp.tool
async def search_users(query: str) -> dict:
    """
    Search users by email, name, phone, or external ID.

    Args:
        query: Search string (e.g. an email address or a name).
    """
    return await _request("GET", "/users/search.json", params={"query": query})


@mcp.tool
async def create_user(name: str, email: str, role: str = "end-user", phone: str = None, organization_id: int = None, user_fields: dict = None, verified: bool = True) -> dict:
    """
    Create a new user.

    Args:
        name: Full name.
        email: Email address (primary identity).
        role: "end-user", "agent", or "admin".
        phone: Optional phone number.
        organization_id: Optional organization to assign.
        user_fields: Optional {field_key: value} custom user fields.
        verified: Mark the email as verified so no verification email is sent (default True).
    """
    user: dict = {"name": name, "email": email, "role": role, "verified": verified}
    if phone is not None:
        user["phone"] = phone
    if organization_id is not None:
        user["organization_id"] = organization_id
    if user_fields is not None:
        user["user_fields"] = user_fields
    return await _request("POST", "/users.json", json={"user": user})


@mcp.tool
async def update_user(
    user_id: int,
    name: str = None,
    email: str = None,
    role: str = None,
    organization_id: int = None,
    phone: str = None,
    notes: str = None,
    user_fields: dict = None,
) -> dict:
    """
    Update a user. Only the arguments you pass are changed.

    Args:
        user_id: The user ID.
        name: New display name.
        email: New email (added as an identity).
        role: "end-user", "agent", or "admin".
        organization_id: Assign to this organization.
        phone: Phone number.
        notes: Agent-visible notes about the user.
        user_fields: {field_key: value} custom user fields, e.g. {"member_id": "12345"}.
    """
    user: dict = {}
    if name is not None:
        user["name"] = name
    if email is not None:
        user["email"] = email
    if role is not None:
        user["role"] = role
    if organization_id is not None:
        user["organization_id"] = organization_id
    if phone is not None:
        user["phone"] = phone
    if notes is not None:
        user["notes"] = notes
    if user_fields is not None:
        user["user_fields"] = user_fields
    return await _request("PUT", f"/users/{user_id}.json", json={"user": user})


@mcp.tool
async def create_or_update_user(name: str, email: str, role: str = "end-user", organization_id: int = None, user_fields: dict = None) -> dict:
    """
    Create a user, or update them if one with the same email/identity already exists.

    Args:
        name: Full name.
        email: Email address (used to match an existing user).
        role: "end-user", "agent", or "admin".
        organization_id: Optional organization to assign.
        user_fields: Optional {field_key: value} custom user fields.
    """
    user: dict = {"name": name, "email": email, "role": role}
    if organization_id is not None:
        user["organization_id"] = organization_id
    if user_fields is not None:
        user["user_fields"] = user_fields
    return await _request("POST", "/users/create_or_update.json", json={"user": user})


@mcp.tool
async def delete_user(user_id: int) -> dict:
    """Soft-delete (deactivate) a user by ID."""
    return await _request("DELETE", f"/users/{user_id}.json")


@mcp.tool
async def get_user_related(user_id: int) -> dict:
    """Get a user's related counts (assigned/requested/ccd tickets, org subscriptions, etc.)."""
    return await _request("GET", f"/users/{user_id}/related.json")


@mcp.tool
async def list_user_identities(user_id: int) -> dict:
    """List a user's identities (emails, phone numbers, external IDs)."""
    return await _request("GET", f"/users/{user_id}/identities.json")


@mcp.tool
async def list_user_group_memberships(user_id: int) -> dict:
    """List the groups a user belongs to."""
    return await _request("GET", f"/users/{user_id}/group_memberships.json")


@mcp.tool
async def list_user_organizations(user_id: int) -> dict:
    """List the organizations a user is a member of."""
    return await _request("GET", f"/users/{user_id}/organizations.json")


@mcp.tool
async def get_user_tickets_requested(user_id: int) -> dict:
    """Get all tickets requested by a user."""
    return await _request("GET", f"/users/{user_id}/tickets/requested.json")


@mcp.tool
async def get_user_tickets_ccd(user_id: int) -> dict:
    """Get all tickets a user is CC'd on."""
    return await _request("GET", f"/users/{user_id}/tickets/ccd.json")


@mcp.tool
async def get_user_tickets_assigned(user_id: int) -> dict:
    """Get all tickets assigned to a user (agents)."""
    return await _request("GET", f"/users/{user_id}/tickets/assigned.json")


# ============================================================================
# ORGANIZATIONS
# ============================================================================

@mcp.tool
async def list_organizations(per_page: int = 100, page: int = 1) -> dict:
    """List organizations (paginated)."""
    return await _request("GET", "/organizations.json", params={"per_page": per_page, "page": page})


@mcp.tool
async def get_organization(org_id: int) -> dict:
    """Get an organization by ID."""
    return await _request("GET", f"/organizations/{org_id}.json")


@mcp.tool
async def get_many_organizations(org_ids: list[int]) -> dict:
    """Get multiple organizations at once by their IDs."""
    return await _request("GET", "/organizations/show_many.json", params={"ids": _csv(org_ids)})


@mcp.tool
async def search_organizations(name: str) -> dict:
    """Search organizations by name (exact/partial match via the search API)."""
    return await _request("GET", "/search.json", params={"query": f"type:organization name:{name}"})


@mcp.tool
async def autocomplete_organizations(name: str) -> dict:
    """Autocomplete organization names by a prefix (min 2 characters)."""
    return await _request("GET", "/organizations/autocomplete.json", params={"name": name})


@mcp.tool
async def create_organization(name: str, domain_names: list[str] = None, details: str = None, notes: str = None, organization_fields: dict = None, tags: list[str] = None) -> dict:
    """
    Create a new organization.

    Args:
        name: Organization name (must be unique).
        domain_names: Email domains that auto-map users to this org.
        details: Free-text details.
        notes: Agent-visible notes.
        organization_fields: {field_key: value} custom organization fields.
        tags: Tags to add.
    """
    org: dict = {"name": name}
    if domain_names is not None:
        org["domain_names"] = domain_names
    if details is not None:
        org["details"] = details
    if notes is not None:
        org["notes"] = notes
    if organization_fields is not None:
        org["organization_fields"] = organization_fields
    if tags is not None:
        org["tags"] = tags
    return await _request("POST", "/organizations.json", json={"organization": org})


@mcp.tool
async def update_organization(org_id: int, name: str = None, domain_names: list[str] = None, details: str = None, notes: str = None, organization_fields: dict = None, tags: list[str] = None) -> dict:
    """
    Update an organization. Only the arguments you pass are changed.

    Args:
        org_id: The organization ID.
        name: New name.
        domain_names: Replace the org's email domains.
        details: Free-text details.
        notes: Agent-visible notes.
        organization_fields: {field_key: value} custom organization fields.
        tags: Replace the org's tags.
    """
    org: dict = {}
    if name is not None:
        org["name"] = name
    if domain_names is not None:
        org["domain_names"] = domain_names
    if details is not None:
        org["details"] = details
    if notes is not None:
        org["notes"] = notes
    if organization_fields is not None:
        org["organization_fields"] = organization_fields
    if tags is not None:
        org["tags"] = tags
    return await _request("PUT", f"/organizations/{org_id}.json", json={"organization": org})


@mcp.tool
async def delete_organization(org_id: int) -> dict:
    """Delete an organization by ID."""
    return await _request("DELETE", f"/organizations/{org_id}.json")


@mcp.tool
async def get_organization_related(org_id: int) -> dict:
    """Get an organization's related counts (tickets, users, etc.)."""
    return await _request("GET", f"/organizations/{org_id}/related.json")


@mcp.tool
async def list_organization_tickets(org_id: int, per_page: int = 100) -> dict:
    """List all tickets belonging to an organization."""
    return await _request("GET", f"/organizations/{org_id}/tickets.json", params={"per_page": per_page})


# ============================================================================
# GROUPS
# ============================================================================

@mcp.tool
async def list_groups(per_page: int = 100) -> dict:
    """List all agent groups."""
    return await _request("GET", "/groups.json", params={"per_page": per_page})


@mcp.tool
async def get_group(group_id: int) -> dict:
    """Get a group by ID."""
    return await _request("GET", f"/groups/{group_id}.json")


@mcp.tool
async def list_assignable_groups() -> dict:
    """List groups that tickets can be assigned to."""
    return await _request("GET", "/groups/assignable.json")


@mcp.tool
async def create_group(name: str, description: str = None) -> dict:
    """Create a new agent group."""
    group: dict = {"name": name}
    if description is not None:
        group["description"] = description
    return await _request("POST", "/groups.json", json={"group": group})


@mcp.tool
async def update_group(group_id: int, name: str = None, description: str = None) -> dict:
    """Update a group's name or description."""
    group: dict = {}
    if name is not None:
        group["name"] = name
    if description is not None:
        group["description"] = description
    return await _request("PUT", f"/groups/{group_id}.json", json={"group": group})


@mcp.tool
async def delete_group(group_id: int) -> dict:
    """Delete a group by ID."""
    return await _request("DELETE", f"/groups/{group_id}.json")


# ============================================================================
# VIEWS & MACROS
# ============================================================================

@mcp.tool
async def list_views(per_page: int = 100) -> dict:
    """List all views available to the logged-in user."""
    return await _request("GET", "/views.json", params={"per_page": per_page})


@mcp.tool
async def get_view(view_id: int) -> dict:
    """Get a view's definition by ID."""
    return await _request("GET", f"/views/{view_id}.json")


@mcp.tool
async def list_active_views() -> dict:
    """List only the active views."""
    return await _request("GET", "/views/active.json")


@mcp.tool
async def count_view_tickets(view_id: int) -> dict:
    """Get the number of tickets currently matching a view."""
    return await _request("GET", f"/views/{view_id}/count.json")


@mcp.tool
async def list_macros(per_page: int = 100) -> dict:
    """List available macros."""
    return await _request("GET", "/macros.json", params={"per_page": per_page})


@mcp.tool
async def get_macro(macro_id: int) -> dict:
    """Get a macro's definition by ID."""
    return await _request("GET", f"/macros/{macro_id}.json")


@mcp.tool
async def apply_macro_to_ticket(ticket_id: int, macro_id: int) -> dict:
    """
    Apply a macro to a ticket. Previews the macro's changes, then commits them.

    Args:
        ticket_id: The ticket to apply the macro to.
        macro_id: The macro to apply.
    """
    preview = await _request("GET", f"/tickets/{ticket_id}/macros/{macro_id}/apply.json")
    if preview.get("error"):
        return preview
    ticket = preview.get("result", {}).get("ticket")
    if not ticket:
        return {"error": True, "message": "Macro returned no ticket changes to apply.", "preview": preview}
    return await _request("PUT", f"/tickets/{ticket_id}.json", json={"ticket": ticket})


# ============================================================================
# SEARCH
# ============================================================================

@mcp.tool
async def search(query: str, type: str = None, sort_by: str = None, sort_order: str = None) -> dict:
    """
    Generic Zendesk search across tickets, users, and organizations.

    Args:
        query: Search string using Zendesk search syntax
               (e.g. "status:open priority:high", "created>2024-01-01").
        type: Optional entity filter: "ticket", "user", or "organization".
        sort_by: Optional field to sort by, e.g. "created_at", "updated_at".
        sort_order: "asc" or "desc".
    """
    full_query = f"type:{type} {query}" if type else query
    params: dict = {"query": full_query}
    if sort_by:
        params["sort_by"] = sort_by
    if sort_order:
        params["sort_order"] = sort_order
    return await _request("GET", "/search.json", params=params)


@mcp.tool
async def count_search(query: str, type: str = None) -> dict:
    """Count how many results a search query returns (without fetching them)."""
    full_query = f"type:{type} {query}" if type else query
    return await _request("GET", "/search/count.json", params={"query": full_query})


# ============================================================================
# METADATA (fields, forms, tags, brands)
# ============================================================================

@mcp.tool
async def list_ticket_fields() -> dict:
    """List all ticket fields (including custom field IDs and their definitions)."""
    return await _request("GET", "/ticket_fields.json")


@mcp.tool
async def list_user_fields() -> dict:
    """List all custom user fields and their keys."""
    return await _request("GET", "/user_fields.json")


@mcp.tool
async def list_organization_fields() -> dict:
    """List all custom organization fields and their keys."""
    return await _request("GET", "/organization_fields.json")


@mcp.tool
async def list_ticket_forms() -> dict:
    """List all ticket forms."""
    return await _request("GET", "/ticket_forms.json")


@mcp.tool
async def list_tags(per_page: int = 100) -> dict:
    """List the most-used tags in the account."""
    return await _request("GET", "/tags.json", params={"per_page": per_page})


@mcp.tool
async def list_brands() -> dict:
    """List all brands configured in the account."""
    return await _request("GET", "/brands.json")


# ============================================================================
# SATISFACTION
# ============================================================================

@mcp.tool
async def list_satisfaction_ratings(per_page: int = 100, score: str = None) -> dict:
    """
    List CSAT (satisfaction) ratings.

    Args:
        per_page: Ratings per page (max 100).
        score: Optional filter: "good", "bad", "good_with_comment", etc.
    """
    params: dict = {"per_page": per_page}
    if score:
        params["score"] = score
    return await _request("GET", "/satisfaction_ratings.json", params=params)


# ============================================================================
# HEALTH
# ============================================================================

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    """Liveness probe for deploy platforms."""
    return PlainTextResponse("ok")


if __name__ == "__main__":
    # Render/Azure inject PORT; locally it defaults to 8000. Always bind 0.0.0.0.
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="http", host="0.0.0.0", port=port)
