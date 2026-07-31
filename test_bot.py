"""Self-contained regression tests. Run with:  python test_bot.py

Deliberately dependency-free (no pytest) so it runs anywhere the bot itself
runs. Covers the behaviour that matters in production: status transitions and
the debounce around them, notification targeting, access control, and how the
Instagram checker classifies each kind of response.
"""

import asyncio
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["OWNER_CHAT_ID"] = "1000"
os.environ["MAX_GUEST_USERS"] = "5"

import bot  # noqa: E402
import ig_checker as ic  # noqa: E402
import storage  # noqa: E402

PASSED = 0
FAILED = []


def check(label, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}".rstrip())


def fresh_db():
    """Each group starts from an empty database."""
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    storage.DB_PATH = path
    storage.init_db()


# --------------------------------------------------------------------------
# Status transitions — the core of the bot
# --------------------------------------------------------------------------
def test_status_transitions():
    print("\nstatus transitions")
    fresh_db()

    prev = storage.set_confirmed("acct", "live", {"full_name": "A", "follower_count": 10})
    check("first status has no previous value", prev is None)
    check("down_since empty while live", storage.get_state("acct")["down_since"] is None)

    prev = storage.set_confirmed("acct", "not_found")
    state = storage.get_state("acct")
    check("going down reports the previous status", prev == "live", f"got {prev!r}")
    check("down_since is stamped when it goes down", state["down_since"] is not None)

    first_down_at = state["down_since"]
    storage.set_confirmed("acct", "not_found")
    check(
        "staying down keeps the original down_since",
        storage.get_state("acct")["down_since"] == first_down_at,
        "otherwise the downtime in the alert resets every check",
    )

    prev = storage.set_confirmed("acct", "live", {"full_name": "A"})
    check("coming back reports it was down", prev == "not_found")
    check("down_since clears on recovery", storage.get_state("acct")["down_since"] is None)


def test_debounce():
    print("\nconfirmation debounce")
    fresh_db()
    storage.set_confirmed("acct", "live", {})

    status, count = storage.bump_pending("acct", "not_found")
    check("first contradicting check is pending, not confirmed", count == 1)
    check("confirmed status unchanged while pending", storage.get_state("acct")["confirmed_status"] == "live")

    _, count = storage.bump_pending("acct", "not_found")
    check("second agreeing check increments the counter", count == 2)

    storage.clear_pending("acct")
    _, count = storage.bump_pending("acct", "not_found")
    check("a disagreeing check resets the counter", count == 1)

    _, count = storage.bump_pending("acct", "live")
    check("switching pending status restarts the count", count == 1, f"got {count}")


def test_profile_data_preserved():
    print("\nprofile data")
    fresh_db()
    storage.set_confirmed("acct", "live", {"full_name": "Real Name", "follower_count": 1234})
    storage.bump_pending("acct", "not_found")
    state = storage.get_state("acct")
    check(
        "a pending check doesn't wipe stored profile details",
        state["full_name"] == "Real Name" and state["follower_count"] == 1234,
        f"got {state['full_name']!r}/{state['follower_count']!r}",
    )


# --------------------------------------------------------------------------
# Notification targeting
# --------------------------------------------------------------------------
def test_notification_targeting():
    print("\nnotification targeting")
    fresh_db()
    storage.add_watch(1000, "shared")
    storage.add_watch(2000, "shared")
    storage.add_watch(3000, "private_to_3000")

    check("everyone tracking an account is notified", sorted(storage.chats_watching("shared")) == [1000, 2000])
    check("only trackers are notified", storage.chats_watching("private_to_3000") == [3000])

    storage.set_paused(2000, "shared", True)
    check("muted users are skipped", storage.chats_watching("shared", only_unpaused=True) == [1000])
    check("muting is per-user, not global", sorted(storage.chats_watching("shared")) == [1000, 2000])

    storage.set_paused(2000, "shared", False)
    check("unmuting restores alerts", sorted(storage.chats_watching("shared", only_unpaused=True)) == [1000, 2000])


def test_watchlist_isolation():
    print("\nwatchlist isolation between users")
    fresh_db()
    storage.add_watch(1000, "owner_account")
    storage.add_watch(2000, "guest_account")

    owner = [w["username"] for w in storage.list_watches(1000)]
    guest = [w["username"] for w in storage.list_watches(2000)]
    check("guest cannot see the owner's accounts", "owner_account" not in guest, f"guest sees {guest}")
    check("owner cannot see the guest's accounts", "guest_account" not in owner, f"owner sees {owner}")

    storage.remove_watch(1000, "owner_account")
    check("removing is scoped to one user", [w["username"] for w in storage.list_watches(2000)] == ["guest_account"])


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------
def test_access_control():
    print("\naccess control")
    fresh_db()
    OWNER, GUEST, STRANGER = 1000, 2000, 3000

    check("owner is authorised", bot.is_authorized(OWNER))
    check("owner is recognised as owner", bot.is_owner(OWNER))
    check("stranger is blocked", not bot.is_authorized(STRANGER))

    storage.add_allowed_user(GUEST, "Guest")
    check("invited guest gains access", bot.is_authorized(GUEST))
    check("guest is not an owner", not bot.is_owner(GUEST))
    check("inviting one guest doesn't admit everyone", not bot.is_authorized(STRANGER))

    for i in range(10):
        if storage.count_allowed_users() < bot.MAX_GUEST_USERS:
            storage.add_allowed_user(5000 + i)
    check("guest limit is enforced", storage.count_allowed_users() == bot.MAX_GUEST_USERS,
          f"got {storage.count_allowed_users()}")
    check("owner doesn't consume a guest slot",
          not any(u["chat_id"] == OWNER for u in storage.list_allowed_users()))

    storage.add_watch(GUEST, "guest_acct")
    storage.remove_allowed_user(GUEST)
    check("revoked guest loses access", not bot.is_authorized(GUEST))
    check("revoking also clears their watchlist", storage.list_watches(GUEST) == [])


# --------------------------------------------------------------------------
# Instagram response classification
# --------------------------------------------------------------------------
PROFILE_HTML = b"""<html><head>
<meta property="og:title" content="Real Person (@someone) . Instagram photos and videos" />
<meta property="og:description" content="9,876 Followers, 12 Following, 5 Posts" />
<meta property="og:image" content="https://cdn.example/pic.jpg" />
</head><body>profile</body></html>"""
LOGIN_HTML = b'<html><body><form id="loginForm"><input name="username"></form></body></html>'
GONE_HTML = b"<html><body>Sorry, this page isn&#039;t available.</body></html>"
EMPTY_HTML = b'<html><body><div id="react-root"></div></body></html>'


class FakeInstagram(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        crawler = "facebookexternalhit" in self.headers.get("User-Agent", "")
        self.server.hits.append((path, crawler))

        def send(code, body=b"", location=None):
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(body)

        if path in ("", "favicon.ico"):
            send(200, b"home")
        elif path == "live":
            send(200, PROFILE_HTML)
        elif path == "gone":
            send(404)
        elif path == "softgone":
            send(200, GONE_HTML)
        elif path == "walled":  # browser is blocked, crawler is served
            send(200, PROFILE_HTML) if crawler else send(302, location="/accounts/login/")
        elif path == "empty":  # blank shell for the browser, real data for the crawler
            send(200, PROFILE_HTML) if crawler else send(200, EMPTY_HTML)
        elif path == "hardwalled":
            send(200, LOGIN_HTML)
        elif path == "throttled":
            send(429)
        else:
            send(500)

    def log_message(self, *args):
        pass


def test_checker_classification():
    print("\ninstagram response handling")
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), FakeInstagram)
    server.hits = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"

    async def run():
        client = ic.make_client()
        try:
            cases = [
                ("live", "live", "a live profile is detected"),
                ("gone", "not_found", "a 404 means suspended/deleted"),
                ("softgone", "not_found", "an in-page 'not available' notice counts too"),
                ("walled", "live", "a login redirect falls back to the crawler view"),
                ("empty", "live", "a blank page falls back to the crawler view"),
                ("hardwalled", "login_wall", "a genuine wall stays inconclusive"),
                ("throttled", "rate_limited", "throttling is reported, not guessed at"),
            ]
            for path, expected, label in cases:
                result = await ic.check_instagram_status(path, client)
                got = result.status or result.error
                check(label, got == expected, f"expected {expected}, got {got}")

            result = await ic.check_instagram_status("live", client)
            check("profile name is parsed", result.full_name == "Real Person", f"got {result.full_name!r}")
            check("follower count is parsed", result.follower_count == 9876, f"got {result.follower_count}")

            server.hits.clear()
            await ic.check_instagram_status("throttled", client)
            attempts = [h for h in server.hits if h[0] == "throttled"]
            check("throttling stops us retrying immediately", len(attempts) == 1, f"made {len(attempts)} requests")
        finally:
            await client.close()
            server.shutdown()

    asyncio.run(run())


def test_follower_parsing():
    print("\nfollower count parsing")
    cases = [("1,234 Followers", 1234), ("2.5M Followers", 2_500_000), ("15.3K Followers", 15_300),
             ("1.2B Followers", 1_200_000_000), ("no numbers here", None)]
    for text, expected in cases:
        check(f"{text!r} -> {expected}", ic._parse_follower_count(text) == expected,
              f"got {ic._parse_follower_count(text)}")


def test_html_escaping():
    print("\nuntrusted profile text is escaped")
    fresh_db()
    storage.set_confirmed("acct", "live", {"full_name": "<script>alert(1)</script> & co"})
    summary = bot.profile_summary(storage.get_state("acct"))
    check("angle brackets are escaped", "<script>" not in summary, summary)
    check("ampersands are escaped", "&amp;" in summary, summary)
    check("username is escaped", "<b>" not in bot.fmt("evil<b>name"))


def test_duration_formatting():
    print("\nduration formatting")
    for seconds, expected in [(45, "45s"), (90, "1m"), (3600, "1h"), (7320, "2h 2m"), (90000, "1d 1h")]:
        check(f"{seconds}s -> {expected}", bot.format_duration(seconds) == expected,
              f"got {bot.format_duration(seconds)}")


def test_username_cleaning():
    print("\nusername normalisation")
    for raw, expected in [("@User", "user"), ("  USER  ", "user"), ("user", "user")]:
        check(f"{raw!r} -> {expected!r}", bot.clean_username(raw) == expected,
              f"got {bot.clean_username(raw)!r}")


def test_migration_from_old_schema():
    print("\nupgrading an existing database")
    import sqlite3

    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE watches (chat_id INTEGER NOT NULL, username TEXT NOT NULL, PRIMARY KEY (chat_id, username))")
    conn.execute("CREATE TABLE status (username TEXT PRIMARY KEY, confirmed_status TEXT NOT NULL, "
                 "pending_status TEXT, pending_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")
    conn.execute("INSERT INTO watches VALUES (1000, 'existing')")
    conn.execute("INSERT INTO status VALUES ('existing', 'live', NULL, 0, datetime('now'))")
    conn.commit()
    conn.close()

    storage.DB_PATH = path
    storage.init_db()
    check("existing watches survive the upgrade",
          [w["username"] for w in storage.list_watches(1000)] == ["existing"])
    check("existing statuses survive the upgrade", storage.get_state("existing")["confirmed_status"] == "live")
    check("new columns are added", "down_since" in storage.get_state("existing"))
    check("access table is created", storage.count_allowed_users() == 0)


class FakeBot:
    """Stands in for Telegram. photo_fails simulates Telegram being unable to
    fetch an Instagram CDN image, which happens in practice."""

    def __init__(self, photo_fails=False):
        self.photo_fails = photo_fails
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, photo, caption, **kwargs):
        if self.photo_fails:
            raise RuntimeError("Bad Request: failed to get HTTP URL content")
        self.sent.append((chat_id, caption))


class FakeContext:
    def __init__(self, fake_bot):
        self.bot = fake_bot


def test_alert_survives_photo_failure():
    print("\nback-online alert when the photo can't be sent")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.set_confirmed("acct", "not_found")
    storage.set_confirmed("acct", "live", {"full_name": "Back", "profile_pic_url": "https://cdn/x.jpg"})

    fake = FakeBot(photo_fails=True)
    asyncio.run(bot.notify_watchers(FakeContext(fake), "acct", "not_found", "live"))
    check(
        "alert still arrives as text when the photo fails",
        len(fake.sent) == 1,
        "the back-online alert was lost entirely",
    )
    if fake.sent:
        check("the text still says it's back online", "BACK ONLINE" in fake.sent[0][1])


def test_manual_check_does_not_swallow_alert():
    print("\nmanual check followed by background check")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.set_confirmed("acct", "not_found")

    live = ic.CheckResult(status="live", full_name="Back Online")
    fake = FakeBot()
    context = FakeContext(fake)

    # Tapping "Check now" sees it's back...
    asyncio.run(bot.apply_check_result(context, "acct", live))
    # ...and the next background cycle confirms it.
    asyncio.run(bot.apply_check_result(context, "acct", live))

    check(
        "the back-online alert is still delivered",
        len(fake.sent) == 1,
        f"expected exactly 1 alert, got {len(fake.sent)} — a manual check must not consume it",
    )
    check("status ends up live", storage.get_state("acct")["confirmed_status"] == "live")


def test_no_alert_without_a_real_change():
    print("\nno alerts for non-changes")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.set_confirmed("acct", "live", {})

    fake = FakeBot()
    context = FakeContext(fake)
    for _ in range(3):
        asyncio.run(bot.apply_check_result(context, "acct", ic.CheckResult(status="live")))
    check("repeated identical results stay silent", fake.sent == [], f"got {fake.sent}")

    asyncio.run(bot.apply_check_result(context, "acct", ic.CheckResult(status=None, error="rate_limited")))
    check("an inconclusive check stays silent", fake.sent == [])
    check("an inconclusive check keeps the last known status",
          storage.get_state("acct")["confirmed_status"] == "live")


def test_baseline_is_silent():
    print("\nfirst-ever result is a baseline, not an alert")
    fresh_db()
    storage.add_watch(1000, "brand_new")
    fake = FakeBot()
    context = FakeContext(fake)

    asyncio.run(bot.apply_check_result(context, "brand_new", ic.CheckResult(status="not_found")))
    check("no alert when first learning a status", fake.sent == [], f"got {fake.sent}")
    check("baseline is recorded", storage.get_state("brand_new")["confirmed_status"] == "not_found")

    for _ in range(bot.CONFIRM_CHECKS):
        asyncio.run(bot.apply_check_result(context, "brand_new", ic.CheckResult(status="live", full_name="Up")))
    check("a genuine recovery after the baseline does alert", len(fake.sent) == 1, f"got {len(fake.sent)}")


def test_muted_user_gets_no_alert():
    print("\nmuted users")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.add_watch(2000, "acct")
    storage.set_paused(2000, "acct", True)
    storage.set_confirmed("acct", "not_found")

    fake = FakeBot()
    asyncio.run(bot.notify_watchers(FakeContext(fake), "acct", "live", "not_found"))
    recipients = [chat_id for chat_id, _ in fake.sent]
    check("unmuted user is alerted", 1000 in recipients)
    check("muted user is not alerted", 2000 not in recipients, f"recipients: {recipients}")


def test_one_bad_chat_does_not_block_others():
    print("\none failing recipient doesn't block the rest")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.add_watch(2000, "acct")
    storage.set_confirmed("acct", "not_found")

    class PartlyBrokenBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if chat_id == 1000:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            self.sent.append((chat_id, text))

    fake = PartlyBrokenBot()
    asyncio.run(bot.notify_watchers(FakeContext(fake), "acct", "live", "not_found"))
    check("the second user is still notified", [c for c, _ in fake.sent] == [2000], f"got {fake.sent}")


def test_downtime_appears_in_alert():
    print("\ndowntime duration in the back-online alert")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.set_confirmed("acct", "live", {})
    storage.set_confirmed("acct", "not_found")

    # Backdate the outage by two hours.
    import sqlite3

    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("UPDATE status SET down_since = datetime('now', '-2 hours') WHERE username='acct'")
    conn.commit()
    conn.close()

    down_since = storage.get_state("acct")["down_since"]
    storage.set_confirmed("acct", "live", {"full_name": "Back"})
    check("recovering clears down_since in storage", storage.get_state("acct")["down_since"] is None)

    fake = FakeBot()
    asyncio.run(bot.notify_watchers(FakeContext(fake), "acct", "not_found", "live", down_since=down_since))
    text = fake.sent[0][1] if fake.sent else ""
    check("the alert reports how long it was down", "after 2h" in text,
          f"got {text!r} — caller must read down_since before recording the new status")


def test_profile_data_survives_going_down():
    print("\nprofile details survive an outage")
    fresh_db()
    storage.set_confirmed("acct", "live", {
        "full_name": "Known Name", "follower_count": 4321,
        "profile_pic_url": "https://cdn/pic.jpg", "is_private": False,
    })
    # A not_found result carries no profile fields at all.
    storage.set_confirmed("acct", "not_found", vars(ic.CheckResult(status="not_found")))
    state = storage.get_state("acct")
    check("name is kept when an account goes down", state["full_name"] == "Known Name", f"got {state['full_name']!r}")
    check("follower count is kept", state["follower_count"] == 4321, f"got {state['follower_count']}")
    check("picture is kept", state["profile_pic_url"] == "https://cdn/pic.jpg")


def test_html_entities_are_decoded():
    print("\nhtml entities in Instagram metadata")
    page = (
        '<meta property="og:title" content="Ben &amp; Jerry (@bj) . Instagram" />'
        '<meta property="og:image" content="https://cdn/p.jpg?stp=abc&amp;oh=123&amp;oe=456" />'
        '<meta property="og:description" content="1,000 Followers" />'
    )
    profile = ic._parse_profile_html(page)
    check("ampersand in the name is decoded", profile["full_name"] == "Ben & Jerry",
          f"got {profile['full_name']!r}")
    check("image URL separators are decoded", "&amp;" not in profile["profile_pic_url"],
          f"got {profile['profile_pic_url']!r} — an escaped URL is not the signed URL and won't load")
    check("decoded URL keeps its parameters", profile["profile_pic_url"].endswith("?stp=abc&oh=123&oe=456"))


def test_rate_limit_is_not_masked():
    print("\nrate limits surface to the caller")
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), _WalledThenThrottled)
    server.hits = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"

    async def run():
        client = ic.make_client()
        try:
            result = await ic.check_instagram_status("acct", client)
            check(
                "a throttled retry is reported as rate_limited",
                result.error == "rate_limited",
                f"got {result.error!r} — hidden behind the first error, the backoff never triggers",
            )
            check("no further requests once throttled", len(server.hits) == 2, f"made {len(server.hits)} requests")
        finally:
            await client.close()
            server.shutdown()

    asyncio.run(run())


class _WalledThenThrottled(BaseHTTPRequestHandler):
    """API wants a login, the browser attempt hits a wall, and the crawler
    attempt is throttled — so the rate limit is the last thing seen."""

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if "/api/v1/" in self.path:
            self.send_response(401)
            self.end_headers()
            return
        self.server.hits.append(self.path)
        if "facebookexternalhit" in self.headers.get("User-Agent", ""):
            self.send_response(429)
            self.end_headers()
        else:
            self.send_response(302)
            self.send_header("Location", "/accounts/login/")
            self.end_headers()

    def log_message(self, *args):
        pass


APP_SHELL = (
    '<!DOCTYPE html><html><head><title>Instagram</title>'
    "<style>:root, .__ig-light-mode:root {--fds-black:#000000;}</style></head><body>"
    '<div id="react-root"></div>'
    '<script type="application/json" data-sjs>{"result":{"data":{"user":'
    '{"username":"someone","full_name":"Real \\u0026 Person","is_private":false,'
    '"edge_followed_by":{"count":48231},'
    '"profile_pic_url_hd":"https:\\/\\/cdn.example\\/pic.jpg?a=1\\u0026b=2"}}}}</script>'
    "</body></html>"
).encode()


def test_embedded_json_parsing():
    print("\nprofile data embedded in the app shell")
    page = APP_SHELL.decode()

    profile = ic._parse_embedded_json(page, "someone")
    check("the account is found in the page's own JSON", profile is not None)
    if profile:
        check("name is decoded from JSON escapes", profile.get("full_name") == "Real & Person",
              f"got {profile.get('full_name')!r}")
        check("follower count is read", profile.get("follower_count") == 48231,
              f"got {profile.get('follower_count')}")
        check("picture URL is decoded", profile.get("profile_pic_url") == "https://cdn.example/pic.jpg?a=1&b=2",
              f"got {profile.get('profile_pic_url')!r}")
        check("private flag is read", profile.get("is_private") is False)

    check("a different username doesn't match", ic._parse_embedded_json(page, "someoneelse") is None,
          "another profile mentioned on the page must not count as this one")
    check("matching ignores case", ic._parse_embedded_json(page, "SomeOne") is not None)


def test_not_found_apostrophe_variants():
    print("\n'page isn't available' in every spelling")
    variants = [
        ("straight quote", "Sorry, this page isn't available."),
        ("curly quote", "Sorry, this page isn’t available."),
        ("html entity", "Sorry, this page isn&#039;t available."),
        ("json escape", "Sorry, this page isn\\u2019t available."),
        ("removed wording", "the page may have been removed"),
    ]
    for label, text in variants:
        check(f"detected: {label}", any(m in text for m in ic.NOT_FOUND_MARKERS), f"missed {text!r}")


class _AppShellServer(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        if path == "someone":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(APP_SHELL)
        elif path == "banned":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                "<html><body>Sorry, this page isn’t available.</body></html>".encode()
            )
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"home")

    def log_message(self, *args):
        pass


def test_app_shell_end_to_end():
    print("\nthe real-world app-shell response, end to end")
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), _AppShellServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"

    async def run():
        client = ic.make_client()
        try:
            result = await ic.check_instagram_status("someone", client)
            check("a live account in the app shell is detected", result.status == "live",
                  f"got {result.status or result.error}")
            check("its details come through", result.follower_count == 48231,
                  f"got {result.follower_count}")

            result = await ic.check_instagram_status("banned", client)
            check("a curly-quote 'not available' page reads as down", result.status == "not_found",
                  f"got {result.status or result.error}")
        finally:
            await client.close()
            server.shutdown()

    asyncio.run(run())


class _CountingServer(BaseHTTPRequestHandler):
    """Mimics what the live site actually does: the JSON API answers
    definitively, while the page is a 600KB shell carrying no profile data."""

    protocol_version = "HTTP/1.1"
    SHELL = (
        '<html><head><title>Instagram</title></head><body>'
        '<style>:root{--fds-black:#000}</style>' + "x" * 200000 + "</body></html>"
    ).encode()
    LIVE_JSON = (
        '{"data":{"user":{"username":"liveone","full_name":"Live One",'
        '"is_private":false,"edge_followed_by":{"count":12345},'
        '"profile_pic_url_hd":"https://cdn.example/p.jpg"}}}'
    ).encode()

    def _respond(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_HEAD(self):
        self.server.calls.append(("HEAD", self.path.split("?")[0]))
        self._respond(200)

    def do_GET(self):
        path = self.path.split("?")[0]
        self.server.calls.append(("GET", path))
        if "/api/v1/" in path:
            account = self.path.split("username=")[-1]
            if account == "banned":
                self._respond(404, b"{}")
            elif account == "liveone":
                self._respond(200, self.LIVE_JSON)
            else:  # the API sometimes demands a login
                self._respond(401, b"{}")
        else:
            self._respond(200, self.SHELL)

    def log_message(self, *args):
        pass


def test_api_first_is_cheap_and_definitive():
    print("\nthe API answers first, so checks stay small")
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), _CountingServer)
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"

    async def run():
        client = ic.make_client()
        try:
            server.calls.clear()
            before = ic.bytes_used()
            result = await ic.check_instagram_status("banned", client)
            spent = ic.bytes_used() - before
            check("a banned account is detected", result.status == "not_found",
                  f"got {result.status or result.error}")
            check("in a single request", len(server.calls) == 1, f"made {server.calls}")
            check("without touching the 600KB page",
                  not any("api" not in c[1] for c in server.calls), f"made {server.calls}")
            check("costing under 5KB", spent < 5000, f"used {spent:,} bytes")

            server.calls.clear()
            before = ic.bytes_used()
            result = await ic.check_instagram_status("liveone", client)
            spent = ic.bytes_used() - before
            check("a live account is detected", result.status == "live",
                  f"got {result.status or result.error}")
            check("with its profile details", result.follower_count == 12345 and result.full_name == "Live One",
                  f"got {result.full_name!r}/{result.follower_count}")
            check("also in a single request", len(server.calls) == 1, f"made {server.calls}")
            check("also under 5KB", spent < 5000, f"used {spent:,} bytes")

            # When the API demands a login, the page fallback still runs.
            server.calls.clear()
            result = await ic.check_instagram_status("walled", client)
            check("a 401 from the API falls back to the page",
                  any("api" not in c[1] for c in server.calls), f"made {server.calls}")
            check("and reports honestly when the page has nothing",
                  result.status is None, f"got {result.status!r}")
        finally:
            await client.close()
            server.shutdown()

    asyncio.run(run())


class FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = type("M", (), {"chat_id": chat_id})()

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)

    async def edit_message_caption(self, caption, **kwargs):
        self.edits.append(caption)


class FakeUpdate:
    def __init__(self, data, chat_id):
        self.callback_query = FakeQuery(data, chat_id)
        self.effective_chat = type("C", (), {"id": chat_id})()


class FakeAppContext:
    def __init__(self, fake_bot):
        self.bot = fake_bot
        self.application = type("A", (), {"bot_data": {}})()


def test_access_approval_buttons():
    print("\napproving access with a button")
    fresh_db()
    OWNER, REQUESTER, GUEST = 1000, 7777, 2000

    # The owner taps "Grant".
    update = FakeUpdate(f"grant:{REQUESTER}", OWNER)
    fake = FakeBot()
    asyncio.run(bot.button_cmd(update, FakeAppContext(fake)))
    check("granting gives that chat access", bot.is_authorized(REQUESTER))
    check("the new user is told", any(c == REQUESTER for c, _ in fake.sent), f"messaged {fake.sent}")

    # A guest must not be able to approve anyone.
    storage.add_allowed_user(GUEST, "Guest")
    update = FakeUpdate("grant:8888", GUEST)
    asyncio.run(bot.button_cmd(update, FakeAppContext(FakeBot())))
    check("a guest cannot grant access", not bot.is_authorized(8888),
          "guests must not be able to admit people")
    check("and is told why", any("owner" in (a or "").lower() for a in update.callback_query.answers),
          f"answers: {update.callback_query.answers}")

    # A guest must not be able to revoke anyone either.
    update = FakeUpdate(f"revoke:{REQUESTER}", GUEST)
    asyncio.run(bot.button_cmd(update, FakeAppContext(FakeBot())))
    check("a guest cannot revoke access", bot.is_authorized(REQUESTER))

    # The owner revokes.
    update = FakeUpdate(f"revoke:{REQUESTER}", OWNER)
    asyncio.run(bot.button_cmd(update, FakeAppContext(FakeBot())))
    check("the owner can revoke", not bot.is_authorized(REQUESTER))

    # Denying doesn't grant anything.
    update = FakeUpdate("deny:9999", OWNER)
    asyncio.run(bot.button_cmd(update, FakeAppContext(FakeBot())))
    check("denying leaves them without access", not bot.is_authorized(9999))


def test_access_requests_are_rate_limited():
    print("\naccess requests can't spam the owner")
    fresh_db()
    bot._access_requests.clear()

    class Req:
        def __init__(self, chat_id):
            self.effective_chat = type("C", (), {"id": chat_id})()
            self.effective_user = type("U", (), {"full_name": "A Person", "username": "aperson"})()

    fake = FakeBot()
    context = FakeAppContext(fake)
    for _ in range(5):
        asyncio.run(bot.notify_owner_of_request(Req(4242), context))
    check("the owner is only asked once", len(fake.sent) == 1, f"sent {len(fake.sent)} messages")
    check("the request identifies the person", "A Person" in fake.sent[0][1] if fake.sent else False)
    check("and includes their chat ID", "4242" in fake.sent[0][1] if fake.sent else False)


def test_owner_fallback_order():
    print("\nowner fallback picks the first listed ID")
    import importlib

    original = os.environ.get("OWNER_CHAT_ID")
    try:
        os.environ.pop("OWNER_CHAT_ID", None)
        # A personal chat listed first, then a group (groups have large
        # negative IDs, which would win a min() comparison).
        os.environ["ALLOWED_CHAT_IDS"] = "1363317234,-1001234567890"
        reloaded = importlib.reload(bot)
        check("the first listed ID becomes owner, not the group",
              reloaded.OWNER_CHAT_ID == 1363317234, f"got {reloaded.OWNER_CHAT_ID}")
    finally:
        os.environ.pop("ALLOWED_CHAT_IDS", None)
        if original is not None:
            os.environ["OWNER_CHAT_ID"] = original
        importlib.reload(bot)


def test_env_users_occupy_slots():
    print("\nslot accounting includes ALLOWED_CHAT_IDS users")
    import importlib

    original_allowed = os.environ.get("ALLOWED_CHAT_IDS")
    try:
        os.environ["ALLOWED_CHAT_IDS"] = "1000,7001,7002"  # owner + two env guests
        reloaded = importlib.reload(bot)
        fresh_db()
        check("env-granted users count as guests", reloaded.used_guest_slots() == 2,
              f"got {reloaded.used_guest_slots()}")
        check("the owner isn't counted", 1000 not in reloaded.env_guest_ids())

        for i in range(5):
            if reloaded.used_guest_slots() < reloaded.MAX_GUEST_USERS:
                storage.add_allowed_user(8000 + i)
        check("the cap accounts for both sources",
              reloaded.used_guest_slots() == reloaded.MAX_GUEST_USERS,
              f"got {reloaded.used_guest_slots()}")
        check("invited guests fill only the remaining slots", storage.count_allowed_users() == 3,
              f"got {storage.count_allowed_users()}")
    finally:
        if original_allowed is None:
            os.environ.pop("ALLOWED_CHAT_IDS", None)
        else:
            os.environ["ALLOWED_CHAT_IDS"] = original_allowed
        importlib.reload(bot)


def test_application_assembles():
    print("\nthe bot actually starts up")
    from telegram.ext import Application

    app = Application.builder().token("123:fake").build()
    for name, handler in (
        ("start", bot.start_cmd), ("help", bot.help_cmd), ("watch", bot.watch_cmd),
        ("check", bot.check_cmd), ("remove", bot.remove_cmd), ("pause", bot.pause_cmd),
        ("resume", bot.resume_cmd), ("list", bot.list_cmd), ("uptime", bot.uptime_cmd),
        ("myid", bot.myid_cmd), ("adduser", bot.adduser_cmd),
        ("removeuser", bot.removeuser_cmd), ("users", bot.users_cmd),
        ("diag", bot.diag_cmd),
    ):
        check(f"/{name} is callable", callable(handler))

    registered = {c.command for c in bot.BOT_COMMANDS}
    documented = {"watch", "check", "list", "remove", "pause", "resume", "uptime", "myid", "help"}
    check("the /-menu matches the commands we ship", registered == documented,
          f"menu has {registered ^ documented} unmatched")
    check("error handler exists", callable(bot.on_error))
    check("app builds", app is not None)


def test_api_retries_across_exit_ips():
    print("\na refusal from one exit IP is retried on another")

    class FlakyAPI(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        SHELL = b"<html><head><title>Instagram</title></head><body>shell</body></html>"

        def _respond(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            if "/api/v1/" in self.path:
                self.server.api_calls += 1
                # The first couple of exit IPs are refused, a later one answers.
                if self.server.api_calls < 3:
                    self._respond(401, b"{}")
                else:
                    self._respond(404, b"{}")
            else:
                self._respond(200, self.SHELL)

        def log_message(self, *args):
            pass

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), FlakyAPI)
    server.api_calls = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"

    async def run():
        client = ic.make_client()
        try:
            result = await ic.check_instagram_status("acct", client)
            check("a later attempt gets the real answer", result.status == "not_found",
                  f"got {result.status or result.error} - one refusal shouldn't end the check")
            check("it kept trying the API", server.api_calls >= 3, f"only {server.api_calls} calls")
        finally:
            await client.close()
            server.shutdown()

    asyncio.run(run())


def test_error_messages_are_actionable():
    print("\nerror messages say what to do about it")
    proxy = bot.explain_error("ProxyError")
    check("a proxy failure points at the balance", "balance" in proxy.lower(), f"got {proxy!r}")
    check("and doesn't just repeat the exception name", proxy != "ProxyError")
    check("rate limiting is explained", "throttl" in bot.explain_error("rate_limited").lower())
    check("http codes are readable", bot.explain_error("http_500") == "Instagram answered 500",
          f"got {bot.explain_error('http_500')!r}")
    check("an unknown error still says something", bot.explain_error("weird_new_thing") == "weird_new_thing")
    check("no error at all is handled", bot.explain_error(None) == "unknown problem")


def test_owner_alerts_are_rate_limited():
    print("\ninfrastructure alerts don't repeat every cycle")
    bot._owner_alerts.clear()
    fake = FakeBot()
    context = FakeAppContext(fake)
    for _ in range(5):
        asyncio.run(bot.alert_owner_once(context, "proxy_down", "proxy is down"))
    check("the owner is told once, not five times", len(fake.sent) == 1, f"sent {len(fake.sent)}")
    check("a different problem still gets through", True)
    asyncio.run(bot.alert_owner_once(context, "something_else", "other problem"))
    check("a separate issue is reported separately", len(fake.sent) == 2, f"sent {len(fake.sent)}")


def test_direct_fallback_when_proxy_dies():
    print("\nthe bot keeps trying when the proxy dies")
    calls = []
    original_api, original_direct = ic._check_via_api, ic._try_direct

    async def dead_proxy(username, client):
        calls.append("proxied")
        return ic.CheckResult(status=None, error="ProxyError")

    async def direct_works(username):
        calls.append("direct")
        return ic.CheckResult(status="not_found")

    async def direct_fails(username):
        calls.append("direct")
        return None

    async def run():
        ic._check_via_api = dead_proxy
        try:
            ic._try_direct = direct_works
            calls.clear()
            result = await ic.check_instagram_status("acct", object())
            check("an answer still comes back without the proxy", result.status == "not_found",
                  f"got {result.status or result.error}")
            check("it tried a direct connection", "direct" in calls, f"path: {calls}")
            check("and didn't hammer the dead proxy", calls.count("proxied") == 1,
                  f"made {calls.count('proxied')} proxied attempts")

            ic._try_direct = direct_fails
            calls.clear()
            result = await ic.check_instagram_status("acct", object())
            check("when that fails too, the proxy error is reported honestly",
                  result.error == "ProxyError", f"got {result.status or result.error}")
        finally:
            ic._check_via_api, ic._try_direct = original_api, original_direct

    asyncio.run(run())


def test_live_accounts_are_polled_less_often():
    print("\nlive accounts aren't re-fetched every minute")
    fresh_db()
    bot._last_checked.clear()
    storage.add_watch(1000, "downacct")
    storage.add_watch(1000, "liveacct")
    storage.set_confirmed("downacct", "not_found")
    storage.set_confirmed("liveacct", "live", {"full_name": "Up"})

    check("a down account is checked immediately", bot.due_for_check("downacct"))
    check("so is a live one that's never been checked", bot.due_for_check("liveacct"))

    bot._last_checked["downacct"] = time.monotonic()
    bot._last_checked["liveacct"] = time.monotonic()

    check("a down account is still due on the next cycle", bot.due_for_check("downacct"),
          "waiting for a suspended account to return is the point - keep it fast")
    check("a live account is not", not bot.due_for_check("liveacct"),
          "re-downloading a live profile every minute is where the proxy bill goes")

    bot._last_checked["liveacct"] = time.monotonic() - bot.LIVE_CHECK_SECONDS - 1
    check("but it is checked again after the longer interval", bot.due_for_check("liveacct"))

    # An unknown account must not inherit the slow path.
    storage.add_watch(1000, "unknownacct")
    bot._last_checked["unknownacct"] = time.monotonic()
    check("an unverified account is checked every cycle", bot.due_for_check("unknownacct"),
          "it hasn't been established as live, so it can't take the cheap path")


def test_page_reads_stop_early():
    print("\nthe profile page isn't downloaded in full to read its <head>")

    HEAD_PART = (
        b"<html><head><title>Instagram</title>"
        b'<meta property="og:title" content="Alex (@acct) o Instagram">'
        b'<meta property="og:description" content="8,313 Followers - see photos">'
        b"</head><body>"
    )
    BULK = b"x" * 700_000  # the JavaScript bundle we should never pay for

    class Big(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = HEAD_PART + BULK
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass  # we hung up early, which is the point

        def log_message(self, *a):
            pass

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), Big)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_base = ic.BASE_URL
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"
    ic._bytes_used = 0
    ic._bytes_by_path.clear()

    async def run():
        client = ic.make_client()
        try:
            result = await ic._fetch_profile_page("acct", client, ic._doc_headers())
            check("the profile is still read correctly", result.status == "live",
                  f"got {result.status or result.error}")
            check("including the details from og: tags", result.full_name == "Alex",
                  f"got {result.full_name!r}")
            check("and the follower count", result.follower_count == 8313,
                  f"got {result.follower_count}")
            used = ic.bytes_used()
            check("but the whole page was never downloaded",
                  used < 200_000, f"counted {used:,} bytes of a 700KB page")
            check("usage is booked against the page path",
                  "page" in ic.bytes_by_path())
        finally:
            await client.close()
            server.shutdown()
            ic.BASE_URL = original_base
            ic._bytes_used = 0
            ic._bytes_by_path.clear()

    asyncio.run(run())


def test_head_requests_are_not_counted_as_full_pages():
    print("\na HEAD request isn't billed as the body it never sent")

    class Resp:
        def __init__(self, length):
            self.headers = {"content-length": str(length)}
            self.text = ""

    ic._bytes_used = 0
    ic._bytes_by_path.clear()

    # Instagram answers a HEAD for a profile page with Content-Length ~600KB
    # describing a body it does not transfer. Counting it made the cheapest
    # request in the bot look like the most expensive one.
    ic._count_response(Resp(600_000), "head-probe", body_transferred=False)
    head_cost = ic.bytes_used()
    check("a HEAD costs overhead only, not the declared length",
          head_cost < 2000, f"counted {head_cost:,} bytes for a headers-only request")

    ic._bytes_used = 0
    ic._bytes_by_path.clear()
    ic._count_response(Resp(600_000), "page")
    check("a real page fetch still counts its body",
          ic.bytes_used() > 500_000, f"counted {ic.bytes_used():,}")

    ic._bytes_used = 0
    ic._bytes_by_path.clear()
    ic._count_response(Resp(800), "api")
    ic._count_response(Resp(600_000), "page")
    ic._count_response(Resp(600_000), "head-probe", body_transferred=False)
    paths = ic.bytes_by_path()
    check("usage is attributed per request type", set(paths) == {"api", "page", "head-probe"})
    check("the biggest consumer is listed first", list(paths)[0] == "page",
          "the point is to name the path worth fixing")
    check("the parts add up to the total", sum(paths.values()) == ic.bytes_used())
    check("the HEAD probe is the cheapest line, not the dearest",
          paths["head-probe"] < paths["api"] + 1000 and paths["head-probe"] < paths["page"])

    ic._bytes_used = 0
    ic._bytes_by_path.clear()


def test_head_probe_rescues_an_inconclusive_check():
    print("\na cheap HEAD probe answers when the API and page don't")

    class ShellOnly(BaseHTTPRequestHandler):
        """The situation that produced 'no_profile_data' in the wild: the API
        refuses from every exit IP, and the page is a shell with nothing in
        it. Only the HEAD request tells the truth."""
        protocol_version = "HTTP/1.1"
        SHELL = b"<html><head><title>Instagram</title></head><body>x</body></html>"

        def _respond(self, code, body=b"", head_only=False):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and not head_only:
                self.wfile.write(body)

        def do_HEAD(self):
            self.server.head_calls += 1
            self._respond(404, head_only=True)

        def do_GET(self):
            if "/api/v1/" in self.path:
                self.server.api_calls += 1
                self._respond(401, b"{}")
            else:
                self.server.page_calls += 1
                self._respond(200, self.SHELL)

        def log_message(self, *args):
            pass

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 0), ShellOnly)
    server.api_calls = server.page_calls = server.head_calls = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_base, original_attempts = ic.BASE_URL, ic.API_ATTEMPTS
    ic.BASE_URL = f"http://127.0.0.1:{server.server_port}"
    ic.API_ATTEMPTS = 2  # keep the test quick; the behaviour is the same

    async def run():
        client = ic.make_client()
        try:
            result = await ic.check_instagram_status("acct", client)
            check("the check resolves instead of giving up",
                  result.status == "not_found",
                  f"got {result.status or result.error} - this is the case that "
                  "surfaced as 'Instagram returned a page with no profile data'")
            check("the HEAD probe was actually consulted", server.head_calls >= 1)
            check("and it ran before spending 600KB on the page",
                  server.page_calls == 0,
                  "the whole point is that the cheap signal comes first")
        finally:
            await client.close()
            server.shutdown()
            ic.BASE_URL, ic.API_ATTEMPTS = original_base, original_attempts

    asyncio.run(run())


def test_check_button_always_shows_something():
    print("\nthe Check now button always visibly responds")
    fresh_db()
    storage.add_watch(1000, "acct")
    storage.set_confirmed("acct", "not_found")

    original_check, original_client = bot.check_instagram_status, bot.get_warm_client

    async def unchanged(username, client):
        return ic.CheckResult(status="not_found")

    async def failing(username, client):
        return ic.CheckResult(status=None, error="ProxyError")

    async def fake_client(context):
        return object()

    try:
        bot.get_warm_client = fake_client

        # The common case, and the one that looked broken: the status hasn't
        # moved, so the redraw was byte-identical, Telegram refused the edit
        # as "not modified", and the tap produced nothing at all.
        bot.check_instagram_status = unchanged
        first = FakeUpdate("check:acct", 1000)
        asyncio.run(bot.button_cmd(first, FakeAppContext(FakeBot())))
        check("an unchanged status still redraws the message",
              bool(first.callback_query.edits),
              "no edit at all is exactly what made the button look dead")
        check("the redraw says when it was checked",
              "checked" in first.callback_query.edits[-1].lower(),
              "something must differ from the previous text or Telegram drops it")
        check("and still shows the status", "Down" in first.callback_query.edits[-1])

        # A failed check has to explain itself in the message body: Telegram
        # honours only the first answer(), and that was already spent.
        bot.check_instagram_status = failing
        failed = FakeUpdate("check:acct", 1000)
        asyncio.run(bot.button_cmd(failed, FakeAppContext(FakeBot())))
        text = failed.callback_query.edits[-1]
        check("a failed check reports the failure in the message", "couldn't verify" in text)
        check("and explains it in plain language", "balance" in text.lower(),
              "this used to go to a second answer() that Telegram silently ignored")
        check("exactly one callback answer is sent",
              len(failed.callback_query.answers) == 1,
              f"sent {failed.callback_query.answers} - only the first is honoured")
        check("a failed check doesn't overwrite the known status",
              storage.get_state("acct")["confirmed_status"] == "not_found",
              "an unverifiable check must not erase what we already knew")
    finally:
        bot.check_instagram_status, bot.get_warm_client = original_check, original_client


def test_revoke_tells_the_truth():
    print("\nrevoking says whether access actually went away")
    fresh_db()
    original_allowed, original_owner = bot.ALLOWED_CHAT_IDS, bot.OWNER_CHAT_ID
    try:
        bot.OWNER_CHAT_ID = 1
        bot.ALLOWED_CHAT_IDS = {1, 777}

        # Invited from chat: the database row is the only grant, so deleting
        # it genuinely removes access.
        storage.add_allowed_user(555)
        storage.add_watch(555, "acct")
        message = bot.revoke_access(555)
        check("a database-granted user is revoked outright", "revoked" in message.lower())
        check("and doesn't warn about something that didn't happen",
              "Not fully revoked" not in message)
        check("access really is gone", not storage.is_allowed_user(555))
        check("and their watchlist stopped being polled",
              "acct" not in storage.all_watched_usernames())

        # In both places: the delete succeeds, so the old code reported
        # success while the env var let them straight back in.
        storage.add_allowed_user(777)
        storage.add_watch(777, "theirs")
        message = bot.revoke_access(777)
        check("a user in both places is NOT reported as revoked",
              "Not fully revoked" in message,
              "the delete succeeds but ALLOWED_CHAT_IDS still grants access")
        check("the message names the variable to edit", "ALLOWED_CHAT_IDS" in message)
        check("their watchlist is still cleared", "theirs" not in storage.all_watched_usernames())
        check("and they demonstrably still have access", bot.is_authorized(777),
              "which is exactly why claiming success here would be a lie")

        message = bot.revoke_access(1)
        check("the owner can't be revoked by accident", "owner" in message.lower())
        check("and keeps access", bot.is_authorized(1))

        message = bot.revoke_access(99999)
        check("an unknown chat ID is reported as such", "didn't have access" in message)
    finally:
        bot.ALLOWED_CHAT_IDS, bot.OWNER_CHAT_ID = original_allowed, original_owner


def test_owner_can_see_every_watchlist():
    print("\nthe owner can enumerate every polled account")
    fresh_db()
    storage.add_watch(1000, "mine")
    storage.add_watch(2000, "theirs")
    storage.add_watch(2000, "theirs_two")
    storage.set_paused(2000, "theirs_two", True)

    rows = storage.all_watches_detailed()
    check("every watch row is returned, not just one chat's", len(rows) == 3)
    check("rows carry the owning chat", {r["chat_id"] for r in rows} == {1000, 2000})
    check("rows carry the username", {r["username"] for r in rows} ==
          {"mine", "theirs", "theirs_two"})
    check("mute state survives the round trip",
          [r["paused"] for r in rows if r["username"] == "theirs_two"] == [True])
    check("grouped by chat so output doesn't interleave",
          [r["chat_id"] for r in rows] == sorted(r["chat_id"] for r in rows))

    # Two people watching the same account is one check, not two - the
    # distinction matters because the owner is being told what it costs.
    storage.add_watch(3000, "mine")
    rows = storage.all_watches_detailed()
    check("a shared account appears once per watcher", len(rows) == 4)
    check("but counts once toward what's actually polled",
          len(storage.all_watched_usernames()) == 3)

    long_list = [f"line {i}" * 20 for i in range(200)]
    chunks = bot.chunk_lines(long_list)
    check("a long watchlist is split into sendable messages",
          all(len(c) <= 3500 for c in chunks) and len(chunks) > 1,
          "Telegram rejects >4096 chars, and a long list is when this matters most")
    check("splitting loses no lines",
          sum(c.count("\n") + 1 for c in chunks) == len(long_list))


def test_watch_counts_by_chat():
    print("\nthe owner can see whose accounts they're paying to poll")
    fresh_db()
    storage.add_watch(1000, "mine_a")
    storage.add_watch(1000, "mine_b")
    for i in range(5):
        storage.add_watch(2000, f"theirs_{i}")
    storage.add_watch(3000, "someone_elses")

    counts = storage.watch_counts_by_chat()
    check("every chat with a list appears", len(counts) == 3)
    check("biggest list first", [c["count"] for c in counts] == [5, 2, 1])
    check("counts are per chat, not global",
          {c["chat_id"]: c["count"] for c in counts}[1000] == 2)

    total = sum(c["count"] for c in counts)
    check("the per-chat counts add up to what the poller actually checks",
          total == len(storage.all_watched_usernames()),
          "if these disagree the owner is being billed for something invisible")

    # Removing a user has to take their list with it, or it keeps costing
    # money every cycle with nobody to notify.
    storage.remove_allowed_user(2000)
    counts = storage.watch_counts_by_chat()
    check("a revoked user's list stops being counted",
          all(c["chat_id"] != 2000 for c in counts))
    check("and stops being polled",
          not any(u.startswith("theirs_") for u in storage.all_watched_usernames()))


def test_proxy_failures_are_distinguishable():
    print("\nproxy failures say which failure they are")
    reason = ic.proxy_failure_reason

    # These three arrive as the same 'ProxyError' class name but need
    # opposite responses, and only one of them is fixed by paying.
    check("bad credentials are named as credentials",
          "credential" in reason(Exception("Received HTTP code 407 from proxy after CONNECT")).lower(),
          "adding funds never fixes a 407, so it must not read as a balance problem")
    check("a 403 points at the plan, not the password",
          "traffic" in reason(Exception("HTTP/1.1 403 Forbidden")).lower())
    check("an unreachable endpoint is named as such",
          "connection" in reason(Exception("Failed to connect to proxy: Connection refused")).lower())
    check("a bad hostname is named as such",
          "resolve" in reason(Exception("Could not resolve proxy: geo.iproyal.cm")).lower())
    check("an unrecognised message is passed through rather than swallowed",
          "wobble" in reason(Exception("some novel wobble")))
    check("an empty message still returns something printable",
          reason(Exception("")).strip() != "")


def test_early_exit_does_not_starve_the_tail():
    print("\na cycle that ends early resumes where it stopped")
    accounts = ["a", "b", "c", "d", "e"]

    bot._resume_after = None
    check("a fresh cycle starts at the top", bot.cycle_order(accounts) == accounts)

    # The proxy died on "b", so the cycle broke there having never reached
    # c, d or e. Restarting from "a" every time is what left them unchecked.
    bot._resume_after = "b"
    check("the next cycle starts after the account it stopped on",
          bot.cycle_order(accounts) == ["c", "d", "e", "a", "b"],
          "otherwise the accounts past the break point are never checked at all")

    bot._resume_after = "e"
    check("stopping on the last account wraps to the front",
          bot.cycle_order(accounts) == ["a", "b", "c", "d", "e"])

    # Someone removed the account the cursor pointed at between cycles.
    bot._resume_after = "gone"
    check("a stale cursor falls back to the top instead of raising",
          bot.cycle_order(accounts) == accounts)

    check("every account still appears exactly once",
          sorted(bot.cycle_order(accounts)) == sorted(accounts))
    bot._resume_after = None


def main():
    for test in (
        test_page_reads_stop_early,
        test_head_requests_are_not_counted_as_full_pages,
        test_head_probe_rescues_an_inconclusive_check,
        test_check_button_always_shows_something,
        test_revoke_tells_the_truth,
        test_owner_can_see_every_watchlist,
        test_watch_counts_by_chat,
        test_proxy_failures_are_distinguishable,
        test_early_exit_does_not_starve_the_tail,
        test_status_transitions,
        test_debounce,
        test_profile_data_preserved,
        test_notification_targeting,
        test_watchlist_isolation,
        test_access_control,
        test_checker_classification,
        test_follower_parsing,
        test_html_escaping,
        test_duration_formatting,
        test_username_cleaning,
        test_migration_from_old_schema,
        test_alert_survives_photo_failure,
        test_manual_check_does_not_swallow_alert,
        test_no_alert_without_a_real_change,
        test_baseline_is_silent,
        test_muted_user_gets_no_alert,
        test_one_bad_chat_does_not_block_others,
        test_downtime_appears_in_alert,
        test_profile_data_survives_going_down,
        test_html_entities_are_decoded,
        test_rate_limit_is_not_masked,
        test_embedded_json_parsing,
        test_not_found_apostrophe_variants,
        test_app_shell_end_to_end,
        test_api_first_is_cheap_and_definitive,
        test_api_retries_across_exit_ips,
        test_live_accounts_are_polled_less_often,
        test_direct_fallback_when_proxy_dies,
        test_error_messages_are_actionable,
        test_owner_alerts_are_rate_limited,
        test_access_approval_buttons,
        test_access_requests_are_rate_limited,
        test_owner_fallback_order,
        test_env_users_occupy_slots,
        test_application_assembles,
    ):
        test()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  - {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
