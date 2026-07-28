"""
calculator.py
Applies Atlassian Cloud's points-based rate limiting system to parsed API calls.

Based on: https://developer.atlassian.com/cloud/jira/platform/rate-limiting/

Rate Limiting Systems:
1. Points-based quota (per hour): Each API call costs points based on complexity
2. Burst rate limits (per second): Max requests/second per endpoint
3. Per-issue write limits: Max writes per issue per time window

Points System (base cost of 1 point per request, plus per-object cost on reads):
- Base cost: 1 point per request
- Read operations (GET / query): 1 base + (objects returned × cost per object type)
  - Core domain objects (issues, projects, dashboards, attachments): 1 point each
  - Identity & access objects (users, groups, roles, permissions):   2 points each
- Write operations (POST/PUT/PATCH/DELETE / mutation): 1 point flat (base cost only)

Cloud Quota Limits (points-based quotas enforced from March 2, 2026), measured in
points per hour, reset at the top of each UTC hour. Quota depends on tier + edition:
- Tier 1 – Global Pool (default): single shared 65,000 points/hour across all tenants.
- Tier 2 – Per-Tenant Pool (high-usage apps, after Atlassian review), per tenant:
    - Free:       65,000 points/hour
    - Standard:   100,000 + (10 × users) points/hour
    - Premium:    130,000 + (20 × users) points/hour
    - Enterprise: 150,000 + (30 × users) points/hour
  Tier 2 per-tenant quotas are capped at 500,000 points/hour.

When no user count is supplied, we default to the Tier 1 Global Pool (65,000/hour)
as a conservative baseline; supplying --users switches to the Tier 2 formula.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import DefaultDict
from .parser import APICall

# ---------------------------------------------------------------------------
# Cloud quota limits (points per hour) by plan tier
#
# Tier 1 – Global Pool (default for most apps):
#   All tenants share a single 65,000 point/hour quota.
#
# Tier 2 – Per-Tenant Pool (high-usage apps after review):
#   Free:       65,000 points/hour (flat)
#   Standard:   100,000 + (10 × users) points/hour  — capped at 500,000
#   Premium:    130,000 + (20 × users) points/hour  — capped at 500,000
#   Enterprise: 150,000 + (30 × users) points/hour  — capped at 500,000
#
# Source: https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
# ---------------------------------------------------------------------------

# Tier 1 — Global Pool flat limits (used when --users is not specified)
CLOUD_QUOTA_LIMITS = {
    "free":           65_000,
    "standard":       65_000,   # Tier 1 global pool default
    "premium":        65_000,   # Tier 1 global pool default
    "enterprise":     65_000,   # Tier 1 global pool default
}

# Tier 2 — Per-Tenant Pool formula: base + (multiplier × users), capped at 500,000
TIER2_FORMULA = {
    "free":       {"base": 65_000,  "per_user": 0},
    "standard":   {"base": 100_000, "per_user": 10},
    "premium":    {"base": 130_000, "per_user": 20},
    "enterprise": {"base": 150_000, "per_user": 30},
}
TIER2_CAP = 500_000

# Default plan to warn against (most conservative)
DEFAULT_PLAN = "standard"


def calculate_quota(plan: str, user_count: int = 0) -> int:
    """
    Calculate the effective hourly quota based on plan and user count.
    - If user_count is 0: uses Tier 1 Global Pool flat limit (65,000)
    - If user_count > 0: uses Tier 2 Per-Tenant Pool formula, capped at 500,000
    """
    if user_count <= 0:
        return CLOUD_QUOTA_LIMITS.get(plan, 65_000)

    formula = TIER2_FORMULA.get(plan, TIER2_FORMULA["standard"])
    quota = formula["base"] + (formula["per_user"] * user_count)
    return min(quota, TIER2_CAP)

# ---------------------------------------------------------------------------
# Burst rate limits (requests per second)
#
# CAVEAT: Atlassian's rate limiting documentation describes burst (per-second)
# limits conceptually — "stay within the steady-state request limits; occasional
# spikes ... may be tolerated ... due to a burst buffer" — but does NOT publish
# exact per-endpoint steady-state RPS or token-bucket sizes. The values below are
# therefore heuristic estimates used to surface likely burst risks, not official
# Atlassian figures. Treat flagged endpoints as candidates for review, not as a
# definitive breach determination.
# Source: https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
# ---------------------------------------------------------------------------
BURST_STEADY_STATE_RPS = 10  # requests per second per endpoint (steady-state) — heuristic estimate
BURST_BUCKET_SIZE = 100       # token bucket max size — heuristic estimate

# ---------------------------------------------------------------------------
# Per-issue write limit
#
# CAVEAT: Atlassian documents per-issue write limits conceptually ("restricts how
# frequently you can modify a single issue") but does NOT publish the exact
# threshold. The value below is a heuristic estimate, not an official figure.
# Source: https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
# ---------------------------------------------------------------------------
PER_ISSUE_WRITE_LIMIT = 10    # max writes per issue per minute — heuristic estimate

# ---------------------------------------------------------------------------
# Endpoint pattern → point cost rules
# ---------------------------------------------------------------------------
# Each rule is (regex_pattern, method_filter, base_points, per_object_points, object_type)
# method_filter: None = any, "GET" = reads only, "POST"/"PUT"/"DELETE" = writes
ENDPOINT_RULES = [
    # Issues
    (re.compile(r"^/rest/api/[23]/issue/[^/]+$"), "GET", 1, 1, "issue"),
    (re.compile(r"^/rest/api/[23]/issue/[^/]+$"), None, 1, 0, "write"),
    # Issue search
    (re.compile(r"^/rest/api/[23]/search"), "GET", 1, 1, "issue"),
    # Group members (users cost 2 points each)
    (re.compile(r"^/rest/api/[23]/group/member"), "GET", 1, 2, "user"),
    # Users
    (re.compile(r"^/rest/api/[23]/user"), "GET", 1, 2, "user"),
    # Comments
    (re.compile(r"^/rest/api/[23]/issue/[^/]+/comment"), "GET", 1, 1, "comment"),
    (re.compile(r"^/rest/api/[23]/issue/[^/]+/comment"), None, 1, 0, "write"),
    # Confluence content
    (re.compile(r"^/rest/api/content"), "GET", 1, 1, "content"),
    (re.compile(r"^/rest/api/content"), None, 1, 0, "write"),
    # Agile/Scrum boards
    (re.compile(r"^/rest/agile/"), "GET", 1, 1, "issue"),
    # Catch-all: 1 point base for anything else
    (re.compile(r".*"), None, 1, 0, "generic"),
]


# ---------------------------------------------------------------------------
# Endpoint-aware bytes-per-object estimates
# Used to estimate how many objects were returned in a response, since
# DC access logs only record response size — not object count.
# ---------------------------------------------------------------------------
BYTES_PER_OBJECT = {
    "issue":    3_000,   # ~3KB per Jira issue (key, summary, status, fields, etc.)
    "user":       500,   # ~500 bytes per user (note: users cost 2 pts each!)
    "comment":  1_500,   # ~1.5KB per comment
    "content":  10_000,  # ~10KB per Confluence page
    "generic":  3_000,   # fallback for unknown object types
}


def estimate_object_count(call: APICall, object_type: str = "generic") -> int:
    """
    Estimate how many objects were returned in the response.
    Uses endpoint-aware bytes-per-object estimates since DC access logs
    only record response size in bytes, not actual object counts.

    Guardrails:
    - Minimum: 1 (a successful GET always returns at least 1 object)
    - Maximum: 1,000 (caps runaway estimates from large non-API responses)
    - Writes: always 0 (POST/PUT/PATCH/DELETE cost 1 point flat)
    - Empty response: 1 (assume at least 1 object returned)
    """
    if call.method != "GET":
        return 0
    if call.response_bytes <= 0:
        return 1
    bytes_per_obj = BYTES_PER_OBJECT.get(object_type, BYTES_PER_OBJECT["generic"])
    estimated = max(1, call.response_bytes // bytes_per_obj)
    return min(estimated, 1000)  # cap at 1000 to avoid outliers


def get_endpoint_key(path: str) -> str:
    """Normalize a path to an endpoint key for grouping (strips IDs)."""
    # Replace issue keys like ABC-123
    path = re.sub(r"/[A-Z]+-\d+", "/{issueKey}", path)
    # Replace numeric IDs
    path = re.sub(r"/\d+", "/{id}", path)
    # Strip query string
    path = path.split("?")[0]
    return path


def calculate_points(call: APICall) -> int:
    """Calculate the cloud rate limit points cost for a single API call."""
    endpoint_key = get_endpoint_key(call.path)

    for pattern, method_filter, base_points, per_object_points, object_type in ENDPOINT_RULES:
        if not pattern.match(call.path.split("?")[0]):
            continue
        if method_filter and call.method != method_filter:
            continue

        if call.method in ("POST", "PUT", "PATCH", "DELETE"):
            return base_points  # Writes always cost 1 point flat

        object_count = estimate_object_count(call, object_type)
        return base_points + (object_count * per_object_points)

    return 1  # fallback


def enrich_calls(calls: list[APICall]) -> list[APICall]:
    """Enrich all API calls with point costs and endpoint keys."""
    for call in calls:
        call.endpoint_key = get_endpoint_key(call.path)
        call.points = calculate_points(call)
    return calls


def analyze_hourly_quota(calls: list[APICall], plan: str = DEFAULT_PLAN, user_count: int = 0) -> dict:
    """
    Group calls by hour and calculate total points consumed per hour.
    Returns analysis vs cloud quota limits.

    If user_count > 0, uses Tier 2 Per-Tenant Pool formula.
    Otherwise uses Tier 1 Global Pool flat limit.
    """
    limit = calculate_quota(plan, user_count)
    tier = 2 if user_count > 0 else 1

    hourly: DefaultDict[str, int] = defaultdict(int)
    hourly_calls: DefaultDict[str, int] = defaultdict(int)

    for call in calls:
        hour_key = call.timestamp.strftime("%Y-%m-%d %H:00")
        hourly[hour_key] += call.points
        hourly_calls[hour_key] += 1

    results = []
    for hour, points in sorted(hourly.items()):
        pct = (points / limit) * 100
        results.append({
            "hour": hour,
            "calls": hourly_calls[hour],
            "points": points,
            "limit": limit,
            "usage_pct": round(pct, 1),
            "would_breach": points > limit,
            "risk_level": "🔴 BREACH" if points > limit else ("🟡 WARNING" if pct > 75 else "🟢 OK"),
        })

    return {
        "plan": plan,
        "user_count": user_count,
        "tier": tier,
        "limit_per_hour": limit,
        "hourly_breakdown": results,
        "peak_hour": max(results, key=lambda x: x["points"]) if results else None,
        "breach_count": sum(1 for r in results if r["would_breach"]),
        "warning_count": sum(1 for r in results if not r["would_breach"] and r["usage_pct"] > 75),
    }


def analyze_burst_rates(calls: list[APICall]) -> dict:
    """
    Analyze per-second request rates per endpoint to identify burst limit risks.
    """
    # Group by endpoint + second
    per_endpoint_per_second: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))

    for call in calls:
        second_key = call.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        per_endpoint_per_second[call.endpoint_key][second_key] += 1

    endpoint_analysis = []
    for endpoint, seconds in per_endpoint_per_second.items():
        max_rps = max(seconds.values())
        avg_rps = sum(seconds.values()) / len(seconds)
        breach_seconds = sum(1 for rps in seconds.values() if rps > BURST_STEADY_STATE_RPS)

        endpoint_analysis.append({
            "endpoint": endpoint,
            "max_rps": max_rps,
            "avg_rps": round(avg_rps, 2),
            "breach_seconds": breach_seconds,
            "steady_state_limit": BURST_STEADY_STATE_RPS,
            "risk_level": (
                "🔴 HIGH" if max_rps > BURST_BUCKET_SIZE else
                ("🟡 MEDIUM" if max_rps > BURST_STEADY_STATE_RPS else "🟢 LOW")
            ),
        })

    return {
        "steady_state_rps_limit": BURST_STEADY_STATE_RPS,
        "burst_bucket_size": BURST_BUCKET_SIZE,
        "endpoints": sorted(endpoint_analysis, key=lambda x: x["max_rps"], reverse=True),
    }


def analyze_per_issue_writes(calls: list[APICall]) -> dict:
    """
    Analyze write frequency per issue to identify per-issue write limit risks.
    """
    # Group write calls by issue key + minute
    issue_writes: DefaultDict[str, DefaultDict[str, int]] = defaultdict(lambda: defaultdict(int))

    issue_pattern = re.compile(r"/([A-Z]+-\d+|{issueKey})")

    for call in calls:
        if call.method not in ("POST", "PUT", "PATCH", "DELETE"):
            continue
        match = issue_pattern.search(call.endpoint_key)
        if not match:
            continue
        issue_key = match.group(1)
        minute_key = call.timestamp.strftime("%Y-%m-%d %H:%M")
        issue_writes[issue_key][minute_key] += 1

    risky_issues = []
    for issue, minutes in issue_writes.items():
        max_writes_per_min = max(minutes.values())
        if max_writes_per_min > PER_ISSUE_WRITE_LIMIT:
            risky_issues.append({
                "issue": issue,
                "max_writes_per_minute": max_writes_per_min,
                "limit": PER_ISSUE_WRITE_LIMIT,
                "risk_level": "🔴 HIGH",
            })

    return {
        "per_issue_write_limit": PER_ISSUE_WRITE_LIMIT,
        "risky_issues": risky_issues,
    }


# ---------------------------------------------------------------------------
# Traffic Classification
# ---------------------------------------------------------------------------

# Service account patterns — usernames that indicate automated/system traffic
SERVICE_ACCOUNT_PATTERNS = [
    "svc_", "svc-", "service", "bot", "automation", "admin",
    "integration", "sync", "api", "system", "daemon", "job",
    "tasktop", "qtest", "jenkins", "bamboo", "script"
]


def classify_call(call) -> str:
    """
    Classify an API call as one of:
    - 'authenticated_user'  : Real human user (named username, not a service account)
    - 'service_account'     : Automated service account (named but looks like a service)
    - 'unauthenticated'     : No username (field is '-'), anonymous or system call
    """
    user = call.user.strip()
    
    if user == "-" or user == "":
        return "unauthenticated"
    
    user_lower = user.lower()
    if any(pattern in user_lower for pattern in SERVICE_ACCOUNT_PATTERNS):
        return "service_account"
    
    return "authenticated_user"


def split_calls_by_type(calls: list) -> dict:
    """Split calls into authenticated users, service accounts, and unauthenticated."""
    result = {
        "authenticated_user": [],
        "service_account": [],
        "unauthenticated": [],
    }
    for call in calls:
        result[classify_call(call)].append(call)
    return result
