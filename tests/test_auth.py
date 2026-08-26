#!/usr/bin/env python3
"""Cookie-file validation, auth store, and yt-dlp option wiring."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["LIUYING_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="liuying-auth-")))

from app.auth import (  # noqa: E402
    AuthError,
    apply_cookie_opts,
    auth_status,
    delete_cookies_file,
    has_cookies,
    looks_like_netscape,
    save_cookies_bytes,
    set_browser,
)
from app.downloader import map_ytdlp_error  # noqa: E402


TINY_NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tfake-test-value\n"
)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_netscape_validation() -> None:
    expect(looks_like_netscape(TINY_NETSCAPE), "header + tab line should pass")
    expect(
        looks_like_netscape(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tx\n"),
        "tab-separated domain line should pass",
    )
    expect(
        looks_like_netscape("#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tabc\n"),
        "HttpOnly prefix should pass",
    )
    expect(looks_like_netscape("# Netscape HTTP Cookie File\n"), "header-only should pass")
    expect(not looks_like_netscape(""), "empty should fail")
    expect(not looks_like_netscape("   \n"), "whitespace should fail")
    expect(not looks_like_netscape("<html><body>not cookies</body></html>"), "html should fail")
    expect(not looks_like_netscape('{"sid": "abc"}'), "json should fail")
    expect(not looks_like_netscape("hello world this is not cookies"), "prose should fail")


def test_save_and_status() -> None:
    expect(has_cookies() is False, "no cookies yet")
    st = auth_status()
    expect(st["has_cookies"] is False, "status has_cookies false")
    expect(st["browser"] is None, "browser starts unset")
    expect(st["browsers_available"] == ["chrome", "chromium", "firefox", "edge", "brave"], "browser list")
    expect("cookies" in st["hint"].lower() or "浏览器" in st["hint"], "hint present")
    expect("cookie" not in json.dumps(st).lower() or "cookies" in json.dumps(st).lower(), "status is json-safe")
    # Must not leak cookie values
    dumped = json.dumps(st)
    expect("fake-test-value" not in dumped, "cookie value must not appear in status")

    save_cookies_bytes(TINY_NETSCAPE.encode("utf-8"))
    expect(has_cookies() is True, "has_cookies after save")
    st = auth_status()
    expect(st["has_cookies"] is True, "status after save")
    expect("fake-test-value" not in json.dumps(st), "value still not in status")

    try:
        save_cookies_bytes(b"<html>nope</html>")
        raise AssertionError("html upload should be rejected")
    except AuthError:
        pass

    try:
        save_cookies_bytes(b"x" * (2 * 1024 * 1024 + 10))
        raise AssertionError("huge file should be rejected")
    except AuthError:
        pass

    deleted = delete_cookies_file()
    expect(deleted is True, "delete returns true")
    expect(has_cookies() is False, "cleared")


def test_browser_and_priority() -> None:
    set_browser("chrome")
    st = auth_status()
    expect(st["browser"] == "chrome", "browser saved")
    opts = apply_cookie_opts({})
    expect(opts.get("cookiesfrombrowser") == ("chrome",), "browser wired into yt-dlp")
    expect("cookiefile" not in opts, "no cookiefile when only browser")

    save_cookies_bytes(TINY_NETSCAPE.encode("utf-8"))
    opts = apply_cookie_opts({})
    expect("cookiefile" in opts, "cookies.txt wins")
    expect("cookiesfrombrowser" not in opts, "browser not used when cookies.txt exists")

    delete_cookies_file()
    set_browser(None)
    st = auth_status()
    expect(st["browser"] is None, "browser cleared")
    opts = apply_cookie_opts({})
    expect("cookiefile" not in opts and "cookiesfrombrowser" not in opts, "no cookie opts when empty")

    try:
        set_browser("safari")
        raise AssertionError("safari should be rejected")
    except AuthError:
        pass


def test_map_ytdlp_error() -> None:
    msg = map_ytdlp_error(Exception("Sign in to confirm your age"))
    expect("导入 cookies" in msg or "登录" in msg, f"age error should point to cookies, got {msg!r}")
    expect("不支持登录" not in msg, "old copy should be gone")
    # "webpage" contains "age" — must not be classified as age-gate
    msg2 = map_ytdlp_error(Exception("ERROR: Unable to download webpage: timed out"))
    expect("导入 cookies" not in msg2, f"webpage timeout should not be age error: {msg2!r}")


def test_base_opts_no_hardcoded_none() -> None:
    from app.downloader import _base_opts

    opts = _base_opts()
    # When no cookies configured, keys should not be forced to None
    expect("cookiefile" not in opts or isinstance(opts.get("cookiefile"), str), "cookiefile not hardcoded None")
    expect("cookiesfrombrowser" not in opts or opts["cookiesfrombrowser"] is not None, "cookiesfrombrowser not hardcoded None")
    ea = opts.get("extractor_args", {}).get("youtube", {}).get("player_client")
    expect(isinstance(ea, list) and "web" in ea, f"player_client list missing, got {ea!r}")
    for name in ("android", "web", "tv"):
        expect(name in ea, f"{name} should be in player_client")


def main() -> int:
    test_netscape_validation()
    test_save_and_status()
    test_browser_and_priority()
    test_map_ytdlp_error()
    test_base_opts_no_hardcoded_none()
    print("test_auth.py: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
