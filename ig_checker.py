import html
import os
import re
from dataclasses import dataclass
from typing import Optional

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

APP_ID = "936619743392459"  # public X-IG-App-ID used by instagram.com's own web client

# Residential/mobile proxy URL, e.g. http://user:pass@gate.provider.com:8000
# Every request routes through this if set. Without it, all requests come
# from this server's own IP, which datacenter hosts (Railway, any VPS) get
# blocked on regardless of how browser-like the request looks.
PROXY_URL = os.environ.get("PROXY_URL") or None

# Overridable so tests can point the real check functions at a local server
# instead of exercising a copy of the logic.
BASE_URL = "https://www.instagram.com"

BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

# Text Instagram serves on the "this account doesn't exist" page. Checked
# against the HTML body when the page returns 200 instead of a clean 404.
NOT_FOUND_MARKERS = (
    "Sorry, this page isn't available.",
    "Sorry, this page isn&#039;t available.",
    "The link you followed may be broken",
    "Page Not Found",
)

# Markers of the logged-out login page. Only consulted after we've already
# ruled out a not-found page and failed to find profile metadata, so a real
# profile page's "Log in" header link can't be mistaken for the login wall.
LOGIN_WALL_MARKERS = (
    'name="username"',
    "Log in to Instagram",
    "loginForm",
    "LoginAndSignupPage",
)

OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.I)
OG_DESC_RE = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', re.I)
OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', re.I)
FOLLOWERS_RE = re.compile(r"([\d,.]+)\s*([KMB]?)\s+Followers", re.I)
# og:title looks like: "Full Name (@username) • Instagram photos and videos"
FULL_NAME_RE = re.compile(r"^(.*?)\s*\(@")


def _doc_headers() -> dict:
    # A top-level page navigation looks different from an XHR - wrong
    # Sec-Fetch-* values on an HTML request are themselves a bot signal.
    return {
        **BROWSER_HEADERS,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _crawler_headers() -> dict:
    # Instagram serves og: preview metadata to link-preview crawlers without
    # a login wall - that's how a shared profile link renders a preview card
    # in WhatsApp, Messenger, or Slack. When the logged-out browser view
    # bounces to a login page, this view often still answers with the tags
    # we need.
    return {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _api_headers(username: str, csrftoken: Optional[str]) -> dict:
    # Mirrors what a logged-out Chrome browser actually sends when it loads a
    # profile page — same host, same headers, real referer, and (if we have
    # one from warm_up_client) the CSRF token cookie the page itself set.
    headers = {
        **BROWSER_HEADERS,
        "Accept": "*/*",
        "X-IG-App-ID": APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Referer": f"{BASE_URL}/{username}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if csrftoken:
        headers["X-CSRFToken"] = csrftoken
    return headers


def make_client() -> AsyncSession:
    # impersonate="chrome136" matches a real Chrome's TLS handshake, HTTP/2
    # fingerprint, and header ordering byte-for-byte. A plain HTTP client is
    # detectable as automated at the TLS layer alone - before any header is
    # even read - no matter how convincingly the headers themselves are
    # spoofed.
    return AsyncSession(impersonate="chrome136", proxy=PROXY_URL)


async def warm_up_client(client: AsyncSession) -> None:
    """Visit the homepage first to pick up real session cookies (csrftoken, etc.),
    same as what happens before any real browser ever calls a profile page.
    Failures here are non-fatal — the checks still work without cookies."""
    try:
        await client.get(f"{BASE_URL}/", headers=BROWSER_HEADERS, timeout=15)
    except RequestException:
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


def _parse_follower_count(description: str) -> Optional[int]:
    match = FOLLOWERS_RE.search(description)
    if not match:
        return None
    raw, suffix = match.group(1), match.group(2).upper()
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(value * multiplier)


def _parse_profile_html(page: str) -> dict:
    """Pull what we can out of the og: meta tags Instagram serves to logged-out
    visitors. Everything here is best-effort — absence of a field never means
    the account is down, only that the page didn't advertise it."""
    profile = {}

    # Attribute values arrive HTML-escaped. Un-escaping matters most for the
    # image: Instagram's CDN URLs are signed and full of &amp;-separated
    # parameters, so leaving them escaped yields a URL that simply doesn't
    # work — which is how the profile photo silently broke the alert.
    title_match = OG_TITLE_RE.search(page)
    if title_match:
        name_match = FULL_NAME_RE.match(html.unescape(title_match.group(1)))
        if name_match and name_match.group(1).strip():
            profile["full_name"] = name_match.group(1).strip()

    desc_match = OG_DESC_RE.search(page)
    if desc_match:
        profile["follower_count"] = _parse_follower_count(html.unescape(desc_match.group(1)))

    image_match = OG_IMAGE_RE.search(page)
    if image_match:
        profile["profile_pic_url"] = html.unescape(image_match.group(1))

    return profile


async def _fetch_profile_page(username: str, client: AsyncSession, headers: dict) -> CheckResult:
    """One attempt at the public profile page. hl=en pins the response language:
    the proxy hands out IPs from random countries, and a localised page would
    break both the follower parsing and the not-found markers."""
    url = f"{BASE_URL}/{username}/?hl=en"
    try:
        resp = await client.get(url, headers=headers, timeout=15, allow_redirects=False)
    except RequestException as exc:
        return CheckResult(status=None, error=type(exc).__name__)

    if resp.status_code == 404:
        return CheckResult(status="not_found")

    if resp.status_code == 429:
        return CheckResult(status=None, error="rate_limited")

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        # A bounce to the login wall tells us nothing about the account itself.
        if "login" in location or "accounts" in location:
            return CheckResult(status=None, error="login_wall")
        return CheckResult(status=None, error=f"redirect_{resp.status_code}")

    if resp.status_code != 200:
        return CheckResult(status=None, error=f"http_{resp.status_code}")

    page = resp.text or ""

    if any(marker in page for marker in NOT_FOUND_MARKERS):
        return CheckResult(status="not_found")

    profile = _parse_profile_html(page)
    if profile.get("full_name") or profile.get("follower_count") is not None:
        return CheckResult(status="live", **profile)

    # Ordering matters: only once a not-found page and real profile metadata
    # are both ruled out can login markers be trusted, since a live profile
    # page also contains "Log in" chrome.
    if any(marker in page for marker in LOGIN_WALL_MARKERS):
        return CheckResult(status=None, error="login_wall")

    return CheckResult(status=None, error="no_profile_data")


async def _check_via_html(username: str, client: AsyncSession) -> CheckResult:
    """Primary check: the public profile page. Instagram returns a clean 404
    for accounts that are suspended, deactivated, or never existed, and serves
    og: metadata for live ones — exactly the signal this bot needs, with no
    login required. (The JSON API it used to call now answers 401.)

    Logged-out treatment varies by edge server, and the proxy rotates to a
    different residential IP per request, so one attempt landing on a login
    wall says nothing about the next. Try as a browser, then as a link-preview
    crawler, which Instagram serves og: tags to even when it walls off the
    browser view."""
    attempts = (_doc_headers(), _crawler_headers())
    first_result = None

    for headers in attempts:
        result = await _fetch_profile_page(username, client, headers)
        if result.status is not None:
            return result
        # A rate limit applies to the IP, not the persona, so stop trying - and
        # report it rather than the earlier error. The caller keys its backoff
        # off this value, and burying it behind a first-attempt "login_wall"
        # means the bot keeps hammering an IP that just told it to stop.
        if result.error == "rate_limited":
            return result
        if first_result is None:
            first_result = result

    return first_result


async def _check_via_api(username: str, client: AsyncSession) -> CheckResult:
    """Fallback: the JSON API. Richer data (private flag, exact follower count)
    when it works, but it now returns 401 without a logged-in session."""
    url = f"{BASE_URL}/api/v1/users/web_profile_info/?username={username}"
    csrftoken = client.cookies.get("csrftoken")
    try:
        resp = await client.get(url, headers=_api_headers(username, csrftoken), timeout=15, allow_redirects=True)
    except RequestException as exc:
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


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


async def diagnose(username: str, client: AsyncSession) -> list:
    """Report what Instagram actually sends back, for each persona we try.

    Classification here depends on recognising Instagram's markup, and their
    responses vary by IP, region, and over time. When a check comes back
    inconclusive this shows the real response instead of leaving us guessing
    at which marker to add next.
    """
    reports = []

    for label, headers in (("browser", _doc_headers()), ("crawler", _crawler_headers())):
        report = {"persona": label}
        try:
            resp = await client.get(
                f"{BASE_URL}/{username}/?hl=en", headers=headers, timeout=15, allow_redirects=False
            )
        except RequestException as exc:
            report["error"] = type(exc).__name__
            reports.append(report)
            continue

        page = resp.text or ""
        title = TITLE_RE.search(page)
        visible = TAG_RE.sub(" ", page[:4000])
        visible = " ".join(visible.split())

        report.update(
            {
                "status": resp.status_code,
                "location": resp.headers.get("location"),
                "bytes": len(page),
                "title": title.group(1).strip()[:80] if title else None,
                "og_title": bool(OG_TITLE_RE.search(page)),
                "og_description": bool(OG_DESC_RE.search(page)),
                "not_found_marker": next((m for m in NOT_FOUND_MARKERS if m in page), None),
                "login_marker": next((m for m in LOGIN_WALL_MARKERS if m in page), None),
                "text": visible[:400],
            }
        )
        reports.append(report)

    return reports


async def check_instagram_status(username: str, client: AsyncSession) -> CheckResult:
    result = await _check_via_html(username, client)
    if result.status is not None:
        return result

    # Already throttled: another request would only extend the block, and the
    # caller needs to see the rate limit to back off.
    if result.error == "rate_limited":
        return result

    # HTML was inconclusive — try the API before giving up. It's often blocked
    # now, but when it does answer it's the more detailed of the two.
    api_result = await _check_via_api(username, client)
    if api_result.status is not None:
        return api_result
    if api_result.error == "rate_limited":
        return api_result

    # Both inconclusive: report the HTML failure, since that's the primary path.
    return result
