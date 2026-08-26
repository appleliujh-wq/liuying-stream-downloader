"""Local, no-login Douyin browser fallback.

Douyin periodically requires a browser-generated verification cookie/signature
that yt-dlp cannot create by itself.  This module uses an isolated, temporary
Chrome profile to render the public page and reads only the media URL already
returned to that page.  It never opens or copies the user's normal Chrome
profile and never sends cookies to a remote service.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class DouyinBrowserError(Exception):
    """A user-facing failure while rendering a public Douyin page."""


class DouyinBrowserUnavailable(DouyinBrowserError):
    """Chrome/Edge is not installed or could not be started."""


class DouyinBrowserBlocked(DouyinBrowserError):
    """The page loaded, but did not expose a downloadable public video."""


@dataclass(frozen=True)
class DouyinMedia:
    webpage_url: str
    media_url: str
    title: str
    uploader: str = ""
    thumbnail: str | None = None
    duration: float | None = None
    audio_url: str | None = None
    media_ext: str = "mp4"
    audio_ext: str = "mp3"


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=[\"\']canonical[\"\'][^>]+href=[\"\']([^\"\']+)',
    re.IGNORECASE,
)
_NICKNAME_RE = re.compile(r'"nickname"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', re.IGNORECASE)
_DURATION_RE = re.compile(r'"duration"\s*:\s*(\d+(?:\.\d+)?)', re.IGNORECASE)


def _chrome_candidates() -> list[Path]:
    configured = os.environ.get("LIUYING_CHROME_PATH", "").strip()
    paths = [Path(configured)] if configured else []
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    paths.extend(
        [
            Path(program_files) / "Google/Chrome/Application/chrome.exe",
            Path(local) / "Google/Chrome/Application/chrome.exe",
            Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
            Path(program_files) / "Microsoft/Edge/Application/msedge.exe",
            Path(local) / "Microsoft/Edge/Application/msedge.exe",
            Path(program_files) / "Chromium/Application/chrome.exe",
            Path(local) / "Chromium/Application/chrome.exe",
            Path(program_files) / "BraveSoftware/Brave-Browser/Application/brave.exe",
            Path(local) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ]
    )
    return [p for p in paths if p and p.is_file()]


def chrome_path() -> Path | None:
    """Return an installed Chromium-family browser executable."""
    candidates = _chrome_candidates()
    return candidates[0] if candidates else None


def _decode_url(raw: str) -> str:
    value = html.unescape(raw)
    value = value.replace("\\u0026", "&").replace("\\u003d", "=")
    value = value.replace("\\/", "/").replace("\\\\", "\\")
    return value.rstrip(".,;:)]}'\\\"")


def _is_video_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path_query = f"{parsed.path}?{parsed.query}".lower()
    if "douyinvod.com" not in host:
        return False
    if "mime_type=audio" in path_query or path_query.endswith(".mp3"):
        return False
    return (
        "mime_type=video" in path_query
        or "/video/" in parsed.path.lower()
        or parsed.path.lower().endswith((".mp4", ".m3u8"))
    )


def _is_audio_url(url: str) -> bool:
    low = url.lower()
    return (
        "mime_type=audio" in low
        or low.split("?", 1)[0].endswith((".mp3", ".m4a", ".aac", ".wav"))
        or "music-east.douyinstatic.com" in low
    )


def _json_string(value: str) -> str:
    # These values are simple JSON strings; handle the escapes without
    # evaluating arbitrary code or exposing cookie/session data.
    return (
        value.replace(r"\u0026", "&")
        .replace(r"\u003c", "<")
        .replace(r"\u003e", ">")
        .replace(r"\"", '"')
        .replace(r"\\", "\\")
    )


def _first_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return html.unescape(match.group(1)).strip() if match else ""


def _parse_media(dom: str, webpage_url: str) -> DouyinMedia:
    text = html.unescape(dom)
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        url = _decode_url(match.group(0))
        if url not in urls:
            urls.append(url)
    video_urls = [u for u in urls if _is_video_url(u)]
    audio_urls = [u for u in urls if _is_audio_url(u)]
    if not video_urls:
        visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        visible = re.sub(r"<[^>]+>", " ", visible)
        lowered = visible.lower()
        title_text = _first_text(_TITLE_RE, text).lower()
        if any(token in lowered or token in title_text for token in ("验证码", "安全验证", "登录后查看")):
            raise DouyinBrowserBlocked(
                "抖音要求完成一次安全验证，流影不会绕过验证。请稍后重试，或在浏览器中确认该内容可公开访问。"
            )
        raise DouyinBrowserBlocked(
            "抖音页面已打开，但没有返回公开视频流。可能是私密、地区受限或暂时触发了平台保护。"
        )

    title = _first_text(_TITLE_RE, text)
    for suffix in (" - 抖音", "｜抖音", "| 抖音"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    canonical = _first_text(_CANONICAL_RE, text) or webpage_url
    uploader = ""
    for match in _NICKNAME_RE.finditer(text):
        candidate = _json_string(match.group(1)).strip()
        if candidate and candidate not in {"抖音", "未知用户"}:
            uploader = candidate
            break
    duration_match = _DURATION_RE.search(text)
    duration = float(duration_match.group(1)) if duration_match else None
    cover = ""
    cover_match = re.search(
        r'"cover"\s*:\s*"(https?[^"\\]+)"', text, re.IGNORECASE
    )
    if cover_match:
        cover = _decode_url(cover_match.group(1))
    if not cover:
        og_match = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            text,
            re.IGNORECASE,
        )
        cover = html.unescape(og_match.group(1)) if og_match else ""

    media = video_urls[0]
    audio = audio_urls[0] if audio_urls else None
    return DouyinMedia(
        webpage_url=canonical,
        media_url=media,
        title=title or "抖音视频",
        uploader=uploader,
        thumbnail=cover or None,
        duration=duration,
        audio_url=audio,
        media_ext="m3u8" if ".m3u8" in media.lower() else "mp4",
        audio_ext="m4a" if ".m4a" in (audio or "").lower() else "mp3",
    )


def extract_douyin(url: str, timeout: float = 25.0) -> DouyinMedia:
    """Render a public Douyin URL in an isolated temporary browser profile."""
    browser = chrome_path()
    if not browser:
        raise DouyinBrowserUnavailable(
            "未找到 Chrome/Edge，无法自动打开抖音页面。请安装 Chrome 后重启流影。"
        )
    profile = Path(tempfile.mkdtemp(prefix="liuying-douyin-chrome-"))
    try:
        args = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            "--window-size=1280,1000",
            "--virtual-time-budget=12000",
            f"--user-data-dir={profile}",
            "--dump-dom",
            url,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(8.0, timeout),
                check=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as e:
            raise DouyinBrowserError("抖音页面加载超时，请稍后重试。") from e
        if not proc.stdout:
            raise DouyinBrowserError("本地浏览器没有返回抖音页面，请稍后重试。")
        dom = proc.stdout.decode("utf-8", errors="replace")
        return _parse_media(dom, url)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def download_media(
    media_url: str,
    output_path: Path,
    referer: str,
    progress: Callable[[int, int | None], None] | None = None,
    max_bytes: int | None = None,
) -> int:
    """Download a browser-discovered signed media URL to a local file."""
    request = Request(
        media_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "*/*",
        },
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with urlopen(request, timeout=45) as response, output_path.open("wb") as out:
            content_length = response.headers.get("Content-Length")
            expected = int(content_length) if content_length and content_length.isdigit() else None
            while True:
                block = response.read(1024 * 256)
                if not block:
                    break
                total += len(block)
                if max_bytes and total > max_bytes:
                    raise DouyinBrowserError("文件超过本地工具允许的大小上限。")
                out.write(block)
                if progress:
                    progress(total, expected)
    except DouyinBrowserError:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception as e:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DouyinBrowserError("视频流下载失败，请稍后重试。") from e
    if total <= 0:
        raise DouyinBrowserError("抖音返回了空文件，无法完成下载。")
    return total
