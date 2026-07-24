import os
from dataclasses import dataclass
from typing import Optional

import httpx

APP_ID = "936619743392459"  # public X-IG-App-ID used by instagram.com's own web client

# Residential/mobile proxy URL, e.g. http://user:pass@gate.provider.com:8000
# Every request routes through this if set. Without it, all requests come
# from this server's own IP, which datacenter hosts (Railway, any VPS) get
# blocked on regardless of how browser-like the request looks.
PROXY_URL = os.environ.get("PROXY_URL") or None


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=PROXY_URL)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _api_headers(username: str, csrftoken: Optional[str]) -> dict:
    # Mirrors what a logged-out Chrome browser actually sends when it loads a
    # profile page — same host, same headers, real referer, and (if we have
    # one from warm_up_client) the CSRF token cookie the page itself set.
    # Skipping straight to the API with no prior visit/cookies is itself a
    # bot signature real browsers never produce.
    headers = {
        **BROWSER_HEADERS,
        "Accept": "*/*",
        "X-IG-App-ID": APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Referer": f"https://www.instagram.com/{username}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if csrftoken:
        headers["X-CSRFToken"] = csrftoken
    return headers


async def warm_up_client(client: httpx.AsyncClient) -> None:
    """Visit the homepage first to pick up real session cookies (csrftoken, etc.),
    same as what happens before any real browser ever calls the profile API.
    Failures here are non-fatal — check_instagram_status still works without
    cookies, just with a slightly weaker signal."""
    try:
        await client.get("https://www.instagram.com/", headers=BROWSER_HEADERS, timeout=10)
    except httpx.HTTPError:
        pass


@dataclass
class CheckResult:
    # status is "live" or "not_found"; None means the check itself failed (rate limit, timeout, etc.)
    status: Optional[str]
    error: Optional[str] = None
    full_name: Optional[str] = None
    follower_count: Optional[int] = None
    is_private: Optional[bool] = None
    profile_pic_url: Optional[str] = None


async def check_instagram_status(username: str, client: httpx.AsyncClient) -> CheckResult:
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    csrftoken = client.cookies.get("csrftoken")
    try:
        resp = await client.get(url, headers=_api_headers(username, csrftoken), timeout=10, follow_redirects=True)
    except httpx.HTTPError as exc:
        return CheckResult(status=None, error=type(exc).__name__)

    if resp.status_code == 200:
        try:
            user = resp.json().get("data", {}).get("user")
        except ValueError:
            return CheckResult(status=None, error="bad_json")
        if not user:
            return CheckResult(status="not_found")
        return CheckResult(
            status="live",
            full_name=user.get("full_name") or None,
            follower_count=(user.get("edge_followed_by") or {}).get("count"),
            is_private=user.get("is_private"),
            profile_pic_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        )

    if resp.status_code == 404:
        return CheckResult(status="not_found")

    if resp.status_code == 429:
        return CheckResult(status=None, error="rate_limited")

    return CheckResult(status=None, error=f"http_{resp.status_code}")
