#!/usr/bin/env python3
"""Classify sample URLs for the four platforms; reject a random site."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.downloader import UnsupportedURLError, classify_url  # noqa: E402


def expect(url: str, platform: str) -> None:
    got = classify_url(url)
    assert got == platform, f"{url!r} -> {got!r}, expected {platform!r}"


def expect_reject(url: str) -> None:
    try:
        got = classify_url(url)
    except UnsupportedURLError:
        return
    raise AssertionError(f"{url!r} should be rejected, got {got!r}")


def main() -> int:
    expect("https://www.youtube.com/watch?v=dQw4w9wgGcQ", "youtube")
    expect("https://youtu.be/dQw4w9wgGcQ", "youtube")
    expect("https://m.youtube.com/watch?v=xxxx", "youtube")
    expect("https://music.youtube.com/watch?v=xxxx", "youtube")
    expect("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili")
    expect("https://b23.tv/abcdef", "bilibili")
    expect("https://m.bilibili.com/video/BVxxxx", "bilibili")
    expect("https://www.douyin.com/video/7123456789", "douyin")
    expect("https://v.douyin.com/AbCdEf/", "douyin")
    expect("6.66 abc:/ 标题 https://v.douyin.com/AbCdEf/ 复制此链接", "douyin")
    expect("https://www.tiktok.com/@user/video/123", "tiktok")
    expect("https://twitter.com/user/status/1234567890", "twitter")
    expect("https://x.com/user/status/1234567890", "twitter")
    expect("https://mobile.twitter.com/user/status/1", "twitter")
    expect("youtube.com/watch?v=dQw4w9wgGcQ", "youtube")
    expect_reject("https://vimeo.com/123456")
    expect_reject("https://www.example.com/watch?v=1")
    expect_reject("https://www.iqiyi.com/v_xxx.html")
    expect_reject("https://www.youtube.com.evil.example/watch?v=1")
    expect_reject("ftp://youtube.com/watch?v=1")
    expect_reject("")
    expect_reject("1.23 :2pm k@C.Hv 05/17 dAT:/ 让你你们挠脚心，不是让你们弹琴 # 家有熊娃儿")
    expect_reject("https://www.douyin.com/user/self/search/%E5%B4%94%E7%B1%B3")
    print("test_url.py: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
