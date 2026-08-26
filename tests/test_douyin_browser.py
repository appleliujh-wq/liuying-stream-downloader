#!/usr/bin/env python3
"""Pure parser tests for the local Douyin browser fallback."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.douyin_browser import DouyinBrowserBlocked, _parse_media  # noqa: E402


def main() -> int:
    dom = r'''
    <title>一个公开视频 - 抖音</title>
    <link rel="canonical" href="https://www.douyin.com/note/123" />
    <script>
      {"nickname":"测试作者","duration":12.5,
       "play":"https://v26-web.douyinvod.com/a/video/file/?mime_type=video_mp4&amp;br=1080&amp;ft=abc",
       "music":"https://lf26-music-east.douyinstatic.com/obj/a.mp3?is_ssr=1"}
    </script>
    '''
    media = _parse_media(dom, "https://v.douyin.com/example/")
    assert media.webpage_url.endswith("/note/123")
    assert media.title == "一个公开视频"
    assert media.uploader == "测试作者"
    assert media.duration == 12.5
    assert media.media_url.startswith("https://v26-web.douyinvod.com/")
    assert media.audio_url and media.audio_url.endswith("is_ssr=1")

    try:
        _parse_media("<title>需要验证</title><p>验证码</p>", "https://v.douyin.com/x")
    except DouyinBrowserBlocked:
        pass
    else:
        raise AssertionError("a page without a media URL should be rejected")
    print("test_douyin_browser.py: all assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
