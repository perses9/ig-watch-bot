import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

logger = logging.getLogger("ig-watch-bot")

APP_ID = "936619743392459"  # public X-IG-App-ID used by instagram.com's own web client

# Residential/mobile proxy URL, e.g. http://user:pass@gate.provider.com:8000
# Every request routes through this if set. Without it, all requests come
# from this server's own IP, which datacenter hosts (Railway, any VPS) get
# blocked on regardless of how browser-like the request looks.
PROXY_URL = os.environ.get("PROXY_URL") or None

# Overridable so tests can point the real check functions at a local server
# instead of exercising a copy of the logic.
BASE_URL = "https://www.instagram.com"

# How many times to ask the JSON API before falling back. It refuses from
# some exit IPs and answers from others, and every request leaves through a
# different one, so retrying is a fresh chance rather than the same request
# twice. Cheap: these responses are a few hundred bytes, so six attempts still
# cost a fraction of one page fetch - and every attempt that lands is one
# fewer "couldn't verify" the user has to look at.
API_ATTEMPTS = int(os.environ.get("API_ATTEMPTS", "6"))

# Page attempts after that. Kept at one by default - diagnostics against the
# live site showed the page returning a shell with no profile data in it, so
# repeating a ~600KB fetch mostly buys bandwidth, not answers.
CHECK_ATTEMPTS = int(os.environ.get("CHECK_ATTEMPTS", "1"))

BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
}

# Residential proxies bill by the gigabyte, so track roughly how much this
# bot is pulling. Surfaced by /uptime to make the burn rate visible before
# it shows up as an empty balance.
_bytes_used = 0
# Same total, split by which request produced it. A single number can only
# say the bill is too high; this says which path to go and fix.
_bytes_by_path = {}


def _count_response(resp, path: str = "other", body_transferred: bool = True) -> None:
    """Count what crossed the wire, not what we ended up with.

    Responses arrive gzipped, and the decoded text is several times larger
    than the transfer the proxy actually bills for - counting the decoded
    length overstates usage badly enough to be misleading.

    body_transferred=False is for HEAD requests. The server still sends a
    Content-Length describing the body it *would* have returned - ~600KB for
    an Instagram profile page - while transferring none of it. Counting that
    made the cheapest request in the bot look like the most expensive one,
    and turned a working setup into an apparent $77/month emergency.
    """
    global _bytes_used
    counted = 0
    if body_transferred:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit():
            counted += int(declared)
        else:
            # No Content-Length (chunked): fall back to the decoded body with
            # a rough compression factor, the best estimate available.
            counted += len(resp.text or "") // 4
    counted += 400  # request headers, TLS handshake, protocol overhead
    _bytes_used += counted
    _bytes_by_path[path] = _bytes_by_path.get(path, 0) + counted


def bytes_used() -> int:
    return _bytes_used


def bytes_by_path() -> dict:
    """Bytes attributed to each request type, largest first."""
    return dict(sorted(_bytes_by_path.items(), key=lambda kv: -kv[1]))

# Text Instagram serves on the "this account doesn't exist" page. Checked
# against the HTML body when the page returns 200 instead of a clean 404.
#
# Deliberately apostrophe-free: the same sentence reaches us as a straight
# quote, a curly quote, an HTML entity, or a JSON ’ escape depending on
# where in the page it appears, and matching one spelling missed the rest.
NOT_FOUND_MARKERS = (
    "Sorry, this page",
    "The link you followed may be broken",
    "Page Not Found",
    "page may have been removed",
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

# Instagram's page is a JavaScript app that ships its data as JSON inside
# <script> tags to hydrate itself. Logged-out visitors increasingly get that
# shell with no og: meta tags at all, so the embedded JSON is the only place
# the profile actually appears.
JSON_USERNAME_RE = re.compile(r'"username"\s*:\s*"([^"]+)"')
JSON_FULL_NAME_RE = re.compile(r'"full_name"\s*:\s*"([^"]*)"')
JSON_FOLLOWERS_RE = re.compile(
    r'"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)|"follower_count"\s*:\s*(\d+)'
)
JSON_PROFILE_PIC_RE = re.compile(r'"profile_pic_url(?:_hd)?"\s*:\s*"([^"]+)"')
JSON_IS_PRIVATE_RE = re.compile(r'"is_private"\s*:\s*(true|false)')

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


ALLOW_DIRECT_FALLBACK = os.environ.get("ALLOW_DIRECT_FALLBACK", "1") not in ("0", "false", "False")


async def _try_direct(username: str) -> Optional["CheckResult"]:
    """Last resort when the proxy is unreachable: ask without it.

    Instagram treats datacenter IPs harshly, so this usually fails - but the
    alternative is a bot that goes completely blind the moment the proxy
    provider runs out of traffic, which is exactly when you'd most want to
    know an account came back. A long shot beats nothing.
    """
    if not ALLOW_DIRECT_FALLBACK:
        return None

    client = AsyncSession(impersonate="chrome136")
    try:
        result = await _check_via_api(username, client)
        if result.status is not None:
            return result
    except RequestException:
        return None
    finally:
        await client.close()
    return None


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
        resp = await client.get(f"{BASE_URL}/", headers=BROWSER_HEADERS, timeout=15)
        _count_response(resp, "warmup")
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


def _json_unescape(value: str) -> str:
    """Decode a JSON string body (\\u0026, \\/, and friends)."""
    try:
        return json.loads(f'"{value}"')
    except ValueError:
        return value


def _parse_embedded_json(page: str, username: str) -> Optional[dict]:
    """Pull the profile out of the JSON the page carries to hydrate itself.

    Returns None unless this specific username appears, so another account
    mentioned somewhere on the page (a suggestion, a related profile) can't be
    mistaken for the one being checked.
    """
    found = {m.group(1) for m in JSON_USERNAME_RE.finditer(page)}
    if not any(u.lower() == username.lower() for u in found):
        return None

    profile = {}

    name_match = JSON_FULL_NAME_RE.search(page)
    if name_match and name_match.group(1).strip():
        profile["full_name"] = _json_unescape(name_match.group(1)).strip()

    followers_match = JSON_FOLLOWERS_RE.search(page)
    if followers_match:
        count = followers_match.group(1) or followers_match.group(2)
        profile["follower_count"] = int(count)

    pic_match = JSON_PROFILE_PIC_RE.search(page)
    if pic_match:
        profile["profile_pic_url"] = _json_unescape(pic_match.group(1))

    private_match = JSON_IS_PRIVATE_RE.search(page)
    if private_match:
        profile["is_private"] = private_match.group(1) == "true"

    return profile


async def _probe_exists(username: str, client: AsyncSession) -> CheckResult:
    """Ask only for the headers, not the page.

    The full profile page is ~600KB, and a metered residential proxy charges
    by the gigabyte — polling one account every 15 seconds would run to
    gigabytes a day. A HEAD request costs a fraction of that and still
    carries the status code, which is the signal that matters: Instagram
    answers 404 for accounts that are suspended, deactivated, or gone.

    That's the common case for this bot, since the whole point is watching a
    banned account until it returns. Only once the answer stops being 404 do
    we spend the bandwidth on the real page.

    status="not_found" is a definitive answer; anything else means "keep
    looking" rather than "it's live".
    """
    try:
        resp = await client.head(
            f"{BASE_URL}/{username}/?hl=en", headers=_doc_headers(), timeout=15, allow_redirects=False
        )
    except RequestException as exc:
        return CheckResult(status=None, error=type(exc).__name__)

    # HEAD: Content-Length describes a body that was never sent.
    _count_response(resp, "head-probe", body_transferred=False)

    if resp.status_code == 404:
        return CheckResult(status="not_found")
    if resp.status_code == 429:
        return CheckResult(status=None, error="rate_limited")
    return CheckResult(status=None, error=f"head_{resp.status_code}")


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
    _count_response(resp, "page")

    if any(marker in page for marker in NOT_FOUND_MARKERS):
        return CheckResult(status="not_found")

    profile = _parse_profile_html(page)
    if profile.get("full_name") or profile.get("follower_count") is not None:
        return CheckResult(status="live", **profile)

    # No og: tags — this is the JavaScript app shell, so read the data it
    # carries to render itself. The account existing in there is proof enough
    # that it's live, even when none of the optional details are present.
    embedded = _parse_embedded_json(page, username)
    if embedded is not None:
        return CheckResult(status="live", **embedded)

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
    """The JSON API — primary check.

    Answers 404 for accounts that are suspended, deactivated, or gone, and
    returns the full profile when they're live. Sometimes 401s and wants a
    login, which is why the page fallback still exists, but when it does
    answer it's both definitive and far smaller than the 600KB page."""
    url = f"{BASE_URL}/api/v1/users/web_profile_info/?username={username}"
    csrftoken = client.cookies.get("csrftoken")
    try:
        resp = await client.get(url, headers=_api_headers(username, csrftoken), timeout=15, allow_redirects=True)
    except RequestException as exc:
        return CheckResult(status=None, error=type(exc).__name__)

    _count_response(resp, "api")

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


async def proxy_status(client: AsyncSession) -> dict:
    """Whether requests are leaving through the proxy, and from which IP."""
    info = {"configured": bool(PROXY_URL)}
    if PROXY_URL:
        # Show the host only — the URL carries credentials.
        info["endpoint"] = PROXY_URL.split("@")[-1]
    try:
        resp = await client.get("https://ipv4.icanhazip.com", timeout=10)
        info["exit_ip"] = (resp.text or "").strip()[:45]
    except RequestException as exc:
        info["exit_ip_error"] = type(exc).__name__
        info["exit_ip_detail"] = proxy_failure_reason(exc)
    return info


def proxy_failure_reason(exc: Exception) -> str:
    """Turn a proxy-layer exception into something you can act on.

    Every proxy failure arrives as the same 'ProxyError' class name, but the
    causes need opposite responses: wrong credentials, an account with no
    traffic left, and an unreachable endpoint all look identical while only
    one of them is fixed by adding money. The underlying message does say
    which - it just never reached the user."""
    text = str(exc)
    low = text.lower()
    if "407" in low or "authentication" in low or "authorization" in low:
        return (
            "the proxy rejected the username/password (407). Check PROXY_URL "
            "credentials — note that adding funds does not change them."
        )
    if "403" in low or "forbidden" in low:
        return (
            "the proxy accepted the login but refused the request (403). Usually "
            "means the plan has no traffic left, or the target host isn't allowed "
            "on your plan."
        )
    if "could not resolve" in low or "name or service" in low or "dns" in low:
        return "the proxy hostname doesn't resolve. Check the endpoint spelling."
    if "refused" in low or "timed out" in low or "timeout" in low or "connect" in low:
        return (
            "couldn't open a connection to the proxy at all. Check the host and "
            "port, and that the plan is active."
        )
    return text[:200] if text else "no detail returned by the proxy layer."


async def diagnose(username: str, client: AsyncSession) -> list:
    """Report what Instagram actually sends back, for each persona we try.

    Classification here depends on recognising Instagram's markup, and their
    responses vary by IP, region, and over time. When a check comes back
    inconclusive this shows the real response instead of leaving us guessing
    at which marker to add next.
    """
    reports = []

    # Report the HEAD probe separately: the cheap path depends on Instagram
    # answering 404 here for missing accounts, and that's worth being able
    # to confirm rather than assume.
    probe = await _probe_exists(username, client)
    reports.append(
        {
            "persona": "HEAD probe",
            "head_verdict": probe.status or probe.error,
        }
    )

    for label, headers in (("browser", _doc_headers()), ("crawler", _crawler_headers())):
        report = {"persona": label}
        try:
            resp = await client.get(
                f"{BASE_URL}/{username}/?hl=en", headers=headers, timeout=15, allow_redirects=False
            )
        except RequestException as exc:
            report["error"] = type(exc).__name__
            report["error_detail"] = proxy_failure_reason(exc)
            reports.append(report)
            continue

        page = resp.text or ""
        title = TITLE_RE.search(page)
        visible = TAG_RE.sub(" ", page[:4000])
        visible = " ".join(visible.split())

        usernames = {m.group(1) for m in JSON_USERNAME_RE.finditer(page)}
        report.update(
            {
                "status": resp.status_code,
                "location": resp.headers.get("location"),
                "bytes": len(page),
                "title": title.group(1).strip()[:80] if title else None,
                "og_title": bool(OG_TITLE_RE.search(page)),
                "og_description": bool(OG_DESC_RE.search(page)),
                "json_username_match": any(u.lower() == username.lower() for u in usernames),
                "json_usernames_seen": len(usernames),
                "json_full_name": bool(JSON_FULL_NAME_RE.search(page)),
                "json_followers": bool(JSON_FOLLOWERS_RE.search(page)),
                "not_found_marker": next((m for m in NOT_FOUND_MARKERS if m in page), None),
                "login_marker": next((m for m in LOGIN_WALL_MARKERS if m in page), None),
                "text": visible[:400],
            }
        )
        reports.append(report)

    return reports


async def check_instagram_status(
    username: str, client: AsyncSession, known_status: Optional[str] = None
) -> CheckResult:
    """known_status is unused now, kept so callers don't need changing.

    The JSON API goes first. Diagnostics against the live site showed it
    answering definitively (404 for an account that's gone) in exactly the
    situation where the page was useless: a 600KB shell carrying no profile
    data at all, and a HEAD request returning 200 for an account that was
    actually suspended. It's also a fraction of the size, so the accurate
    path is the cheap one.
    """
    # The API answers 401 from some exit IPs and correctly from others, and
    # the proxy hands out a different residential IP per request - so a refusal
    # is worth retrying rather than giving up on. These responses are a few
    # hundred bytes, making several attempts far cheaper than one page fetch.
    last = None
    for attempt in range(API_ATTEMPTS):
        api_result = await _check_via_api(username, client)
        if api_result.status is not None:
            return api_result
        if api_result.error == "rate_limited":
            return api_result

        # The proxy itself is down, so every further request through it fails
        # the same way. Try once without it rather than going blind.
        if api_result.error == "ProxyError":
            direct = await _try_direct(username)
            if direct is not None:
                logger.info("%s: proxy down, answered on a direct connection", username)
                return direct
            return api_result

        last = api_result
        if attempt < API_ATTEMPTS - 1:
            await asyncio.sleep(0.5)

    # Before spending ~600KB on a page that increasingly carries no profile
    # data at all, ask for headers only. A 404 here is definitive and costs
    # almost nothing, which matters because "suspended" is the state this bot
    # spends nearly all its time confirming.
    #
    # Only the 404 is trusted. A 200 has been observed for an account that was
    # actually suspended, so anything else means "keep looking" rather than
    # "it's live" - the asymmetry is the whole reason this is safe to consult.
    probe = await _probe_exists(username, client)
    if probe.status == "not_found":
        logger.info("%s: API inconclusive, HEAD probe answered 404", username)
        return probe
    if probe.error == "rate_limited":
        return probe

    for attempt in range(CHECK_ATTEMPTS):
        result = await _check_via_html(username, client)
        if result.status is not None:
            return result
        if result.error == "rate_limited":
            return result
        last = result
        if attempt < CHECK_ATTEMPTS - 1:
            await asyncio.sleep(1)

    return last
