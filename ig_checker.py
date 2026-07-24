from dataclasses import dataclass
from typing import Optional

import httpx

APP_ID = "936619743392459"  # public X-IG-App-ID used by instagram.com's own web client
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "X-IG-App-ID": APP_ID,
    "Accept": "application/json",
}


@dataclass
class CheckResult:
    # status is "live" or "not_found"; None means the check itself failed (rate limit, timeout, etc.)
    status: Optional[str]
    error: Optional[str] = None


async def check_instagram_status(username: str, client: httpx.AsyncClient) -> CheckResult:
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10, follow_redirects=True)
    except httpx.HTTPError as exc:
        return CheckResult(status=None, error=type(exc).__name__)

    if resp.status_code == 200:
        try:
            user = resp.json().get("data", {}).get("user")
        except ValueError:
            return CheckResult(status=None, error="bad_json")
        return CheckResult(status="live" if user else "not_found")

    if resp.status_code == 404:
        return CheckResult(status="not_found")

    if resp.status_code == 429:
        return CheckResult(status=None, error="rate_limited")

    return CheckResult(status=None, error=f"http_{resp.status_code}")
