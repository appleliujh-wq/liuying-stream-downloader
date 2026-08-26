"""yt-dlp wrapper, URL classification, and a one-at-a-time job queue."""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from app.auth import apply_cookie_opts
from app.douyin_browser import (
    DouyinBrowserBlocked,
    DouyinBrowserError,
    DouyinBrowserUnavailable,
    download_media,
    extract_douyin,
)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(Path(__file__).resolve().parent.parent / "downloads")))
MAX_DURATION_SEC = int(os.environ.get("MAX_DURATION_SEC", str(3 * 60 * 60)))  # 3 hours
MAX_FILESIZE = int(os.environ.get("MAX_FILESIZE", str(4 * 1024 * 1024 * 1024)))  # 4 GiB
INFO_TIMEOUT_SEC = int(os.environ.get("INFO_TIMEOUT_SEC", "45"))
SOCKET_TIMEOUT = int(os.environ.get("SOCKET_TIMEOUT", "30"))
MAX_RECENT_JOBS = 50
DOUYIN_MEDIA_CACHE_SEC = 120
_douyin_media_cache: dict[str, tuple[float, Any]] = {}
_douyin_media_cache_lock = threading.Lock()

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Host (www. stripped) → platform id
_HOST_MAP: dict[str, str] = {}


def _register(platform: str, *hosts: str) -> None:
    for h in hosts:
        _HOST_MAP[h.lower()] = platform


_register(
    "youtube",
    "youtube.com",
    "m.youtube.com",
    "youtu.be",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
)
_register(
    "bilibili",
    "bilibili.com",
    "m.bilibili.com",
    "b23.tv",
    "b23.wtf",
    "space.bilibili.com",
    "live.bilibili.com",
)
_register(
    "douyin",
    "douyin.com",
    "m.douyin.com",
    "v.douyin.com",
    "iesdouyin.com",
    "www.iesdouyin.com",
)
_register(
    "tiktok",
    "tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
)
_register(
    "twitter",
    "twitter.com",
    "mobile.twitter.com",
    "x.com",
    "mobile.x.com",
)

PLATFORM_LABELS = {
    "youtube": "YouTube",
    "bilibili": "哔哩哔哩",
    "douyin": "抖音",
    "tiktok": "TikTok",
    "twitter": "Twitter/X",
}

QUALITY_PRESETS = ("best", "1080p", "720p", "480p", "audio")

# Extractor names yt-dlp may report; used as a second gate after domain check.
_ALLOWED_EXTRACTOR_NEEDLES = (
    "youtube",
    "bilibili",
    "bili",
    "douyin",
    "tiktok",
    "twitter",
)

QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/b",
    "audio": "ba/b",
}

# Already-muxed fallbacks when ffmpeg is missing (no merge).
QUALITY_FORMATS_NO_FFMPEG = {
    "best": "b[ext=mp4]/b",
    "1080p": "b[height<=1080][ext=mp4]/b[height<=1080]/b",
    "720p": "b[height<=720][ext=mp4]/b[height<=720]/b",
    "480p": "b[height<=480][ext=mp4]/b[height<=480]/b",
    "audio": "ba[ext=m4a]/ba/b",
}


class AppError(Exception):
    """User-facing error with a Chinese message."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class UnsupportedURLError(AppError):
    def __init__(self, message: str = "暂不支持该链接。请粘贴 YouTube、哔哩哔哩、抖音或 Twitter/X 的视频地址。") -> None:
        super().__init__(message, code="unsupported")


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def has_ffmpeg() -> bool:
    return ffmpeg_path() is not None


def ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as _v
        return _v
    except Exception:
        return "unknown"


def _strip_www(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        return host[4:]
    return host


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`\]\)]+", re.I)
_DOUYIN_SHARE_TOKEN = re.compile(
    r"(复制打开抖音|打开抖音搜索|长按复制此条消息)|"
    r"(?:^|\s)\d+(?:\.\d+)?\s+[^\n]{0,80}?\w+:/",
    re.I,
)


def _first_http_url(text: str) -> str | None:
    m = _URL_IN_TEXT.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;，。、)）】")


def looks_like_douyin_share_text(text: str) -> bool:
    raw = text or ""
    if _first_http_url(raw):
        return False
    if _DOUYIN_SHARE_TOKEN.search(raw):
        return True
    if re.search(r"[#＃].{0,12}(抖音|douyin)", raw, re.I):
        return True
    # Typical 口令: "... dAT:/ 标题 #话题"
    if re.search(r"\b[\w.-]{2,8}:/\s", raw) and re.search(r"[\u4e00-\u9fff#＃]", raw):
        return True
    return False


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise UnsupportedURLError("请先粘贴一条视频链接。")
    extracted = _first_http_url(raw)
    if extracted:
        raw = extracted
    elif looks_like_douyin_share_text(raw):
        raise UnsupportedURLError(
            "这是抖音分享口令，不是网页链接。请打开视频 → 分享 → 复制链接，粘贴带 v.douyin.com 的那条。"
        )
    if raw.startswith("//"):
        raw = "https:" + raw
    guessed = urlparse(raw)
    if guessed.scheme and guessed.scheme.lower() not in ("http", "https", ""):
        raise UnsupportedURLError("只接受 http 或 https 链接。")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise UnsupportedURLError("只接受 http 或 https 链接。")
    if not parsed.hostname:
        raise UnsupportedURLError("链接格式不正确。")
    path = (parsed.path or "").lower()
    host = _strip_www(parsed.hostname or "")
    if host.endswith("douyin.com") and ("/search/" in path or path.endswith("/search")):
        raise UnsupportedURLError(
            "这是抖音搜索页，不是单条视频。请打开视频 → 分享 → 复制链接。"
        )
    return raw


def classify_url(url: str) -> str:
    """Return platform id. Raises UnsupportedURLError for anything else."""
    raw = normalize_url(url)
    host = _strip_www(urlparse(raw).hostname or "")
    platform = _HOST_MAP.get(host)
    if platform:
        return platform
    # Subdomain fallback: foo.youtube.com, xxx.bilibili.com
    for suffix, plat in (
        (".youtube.com", "youtube"),
        (".bilibili.com", "bilibili"),
        (".douyin.com", "douyin"),
        (".tiktok.com", "tiktok"),
        (".twitter.com", "twitter"),
        (".x.com", "twitter"),
    ):
        if host.endswith(suffix):
            return plat
    raise UnsupportedURLError()


def sanitize_title(title: str | None) -> str:
    text = (title or "video").strip() or "video"
    text = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > 80:
        text = text[:80].rstrip()
    return text or "video"


def _format_bytes(n: int | float | None) -> str:
    if not n:
        return "—"
    n = float(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def _format_speed(speed: float | None) -> str | None:
    if not speed:
        return None
    return _format_bytes(speed) + "/s"


def _format_eta(eta: int | float | None) -> str | None:
    if eta is None:
        return None
    try:
        sec = int(eta)
    except (TypeError, ValueError):
        return None
    if sec < 0:
        return None
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _format_duration(sec: int | float | None) -> str | None:
    if sec is None:
        return None
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return None
    if sec < 0:
        return None
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def map_ytdlp_error(exc: BaseException) -> str:
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()

    if "ffmpeg" in low or "ffprobe" in low:
        return "未检测到 ffmpeg，无法合并视频与音频。请安装 ffmpeg 后重试，或改选「仅音频」。"
    if "private" in low or "this video is private" in low:
        return "该视频为私密内容，无法下载。"
    if (
        "fresh cookies" in low
        or "not necessarily logged in" in low
        or "s_v_web_id" in low
        or ("[douyin]" in low and "cookie" in low)
    ):
        return (
            "抖音要一份浏览器 cookies 才能解析，不用登录账号。"
            "请打开「登录态」，用 Get cookies.txt LOCALLY 导出后点导入（Chrome 可以开着）。"
        )
    if "could not copy" in low and "cookie" in low:
        return (
            "读不到 Chrome cookies。新版 Chrome 经常锁住数据库，关掉窗口也不一定行。"
            "请改用「登录态」里的导入 cookies.txt：Chrome 开着打开 douyin.com，"
            "用扩展 Get cookies.txt LOCALLY 导出后导入。不用关浏览器。"
        )
    age_needles = (
        "age-restricted",
        "age restricted",
        "age restriction",
        "agerestricted",
        "sign in to confirm",
        "login required",
        "confirm your age",
        "inappropriate for some users",
        "this video may be inappropriate",
        "please sign in",
        "sign in to youtube",
        "requires login",
    )
    if any(n in low for n in age_needles):
        return "该视频需要你已登录且已确认年龄的 YouTube 账号。请在右上角导入 cookies 或读取本机浏览器登录态后重试。"
    if "geo" in low or "not available in your" in low or "not made this video available in your country" in low:
        return "该视频因地区限制无法访问。"
    if "copyright" in low:
        return "该视频因版权原因无法下载。"
    if "timed out" in low or "timeout" in low or "timedout" in low:
        return "网络超时，请检查网络后重试。"
    if "unsupported url" in low or "no video formats" in low or "requested format is not available" in low:
        return "无法解析该链接，或所选清晰度不存在。"
    if "live" in low and ("not supported" in low or "offline" in low or "is live" in low):
        return "暂不支持直播或未开始的预约直播。"
    if "http error 404" in low or "video unavailable" in low or "removed" in low:
        return "视频不存在或已被删除。"
    if "http error 403" in low or "forbidden" in low:
        return "访问被拒绝（403）。该平台可能限制了当前网络环境。"
    if "unable to download webpage" in low or "network" in low or "connection" in low:
        return "网络连接失败，请稍后重试。"
    # Keep a short, readable remainder — never dump cookies/tokens.
    cleaned = re.sub(r"(cookie|authorization|token|session)[^\s]{0,40}", "[已隐藏]", msg, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:180] + "…"
    return f"下载失败：{cleaned}" if cleaned else "下载失败，请稍后重试。"


def _is_browser_cookie_database_error(exc: BaseException) -> bool:
    """Return True for a locked/unreadable browser cookie database."""
    low = str(exc).lower()
    return (
        "could not copy" in low and "cookie" in low
    ) or (
        "cookie database" in low
    )


def _run_with_browser_cookie_fallback(
    opts: dict[str, Any], operation: Callable[[Any], Any]
) -> Any:
    """Run a yt-dlp operation, retrying once without browser cookies.

    Chrome can lock its SQLite cookie database on Windows.  That local
    failure should not prevent downloading a publicly accessible video that
    does not need cookies.  Explicit cookies.txt is never silently removed;
    only the browser-cookie option is eligible for this fallback.
    """
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return operation(ydl)
    except (DownloadError, ExtractorError) as exc:
        if "cookiesfrombrowser" not in opts or not _is_browser_cookie_database_error(exc):
            raise
        retry_opts = dict(opts)
        retry_opts.pop("cookiesfrombrowser", None)
        with yt_dlp.YoutubeDL(retry_opts) as ydl:
            return operation(ydl)


def _extractor_allowed(info: dict[str, Any]) -> bool:
    key = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if not key:
        return True
    return any(n in key for n in _ALLOWED_EXTRACTOR_NEEDLES)


def _base_opts() -> dict[str, Any]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 1,
        "overwrites": False,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "http_headers": {
            "User-Agent": DESKTOP_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        "usenetrc": False,
        "no_check_certificates": False,
        # Documented yt-dlp extractor_args (robustness, not an age-gate bypass).
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "tv"],
            }
        },
    }
    apply_cookie_opts(opts)
    ff = ffmpeg_path()
    if ff:
        opts["ffmpeg_location"] = ff
    return opts


def _unique_stem(title: str) -> str:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    base = sanitize_title(title)
    stem = base
    n = 1
    while True:
        exists = any(DOWNLOAD_DIR.glob(f"{stem}.*")) and not any(
            p.name.endswith(".part") for p in DOWNLOAD_DIR.glob(f"{stem}.*")
        )
        # Treat any non-temp file with this stem as a clash.
        clash = False
        for p in DOWNLOAD_DIR.glob(f"{stem}.*"):
            if p.suffix in {".part", ".ytdl", ".temp"} or p.name.endswith(".part"):
                continue
            clash = True
            break
        if not clash:
            return stem
        n += 1
        stem = f"{base}_{n}"


def _available_qualities(info: dict[str, Any]) -> list[str]:
    formats = info.get("formats") or []
    heights: list[int] = []
    has_video = False
    has_audio = False
    for f in formats:
        if not isinstance(f, dict):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        h = f.get("height")
        if vcodec != "none":
            has_video = True
            if isinstance(h, (int, float)) and h:
                heights.append(int(h))
        if acodec != "none":
            has_audio = True
    # Some extractors only expose a combined format without a list.
    if not formats:
        if info.get("vcodec") and info.get("vcodec") != "none":
            has_video = True
        if info.get("acodec") and info.get("acodec") != "none":
            has_audio = True
        h = info.get("height")
        if isinstance(h, (int, float)) and h:
            heights.append(int(h))

    options: list[str] = []
    if has_video:
        options.append("best")
        max_h = max(heights) if heights else 0
        # Offer a rung if the source has at least that resolution,
        # or if we have video but no height metadata (let yt-dlp pick).
        for label, need in (("1080p", 1080), ("720p", 720), ("480p", 480)):
            if max_h >= need or (has_video and not heights):
                options.append(label)
    if has_audio or has_video:
        options.append("audio")
    if not options:
        options = ["best", "audio"]
    return options


def _reject_live_or_too_long(info: dict[str, Any]) -> None:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise AppError("这是直播或预约直播，暂不支持下载。请等待结束后再试。", code="live")
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > MAX_DURATION_SEC:
        hours = MAX_DURATION_SEC // 3600
        raise AppError(
            f"视频时长超过 {hours} 小时，已拒绝下载，以免占用过多磁盘与时间。",
            code="too_long",
        )


def extract_info(url: str) -> dict[str, Any]:
    """Fetch metadata only. Never downloads the media."""
    platform = classify_url(url)
    url = normalize_url(url)
    browser_media = None
    # yt-dlp's Douyin extractor can be stopped by a browser-generated
    # verification cookie.  A temporary local Chromium page is the normal
    # path for Douyin; other platforms keep the existing yt-dlp path.
    if platform == "douyin":
        now = time.time()
        with _douyin_media_cache_lock:
            cached = _douyin_media_cache.get(url)
            if cached and now - cached[0] < DOUYIN_MEDIA_CACHE_SEC:
                browser_media = cached[1]
            elif cached:
                _douyin_media_cache.pop(url, None)
        try:
            if browser_media is None:
                browser_media = _run_with_timeout(
                    lambda: extract_douyin(url), min(float(INFO_TIMEOUT_SEC), 30.0)
                )
                with _douyin_media_cache_lock:
                    _douyin_media_cache[url] = (time.time(), browser_media)
        except DouyinBrowserUnavailable:
            # Keep a useful yt-dlp fallback if the user has no Chromium browser
            # but has configured an explicit cookies.txt file.
            browser_media = None
        except DouyinBrowserBlocked as e:
            raise AppError(str(e), code="douyin_blocked") from e
        except DouyinBrowserError as e:
            raise AppError(str(e), code="douyin_browser") from e
        except TimeoutError:
            raise AppError("抖音页面加载超时，请稍后重试。", code="timeout") from None

    if browser_media is not None:
        info = {
            "webpage_url": browser_media.webpage_url,
            "title": browser_media.title,
            "uploader": browser_media.uploader,
            "duration": browser_media.duration,
            "thumbnail": browser_media.thumbnail,
            "formats": [
                {
                    "format_id": "browser-direct",
                    "url": browser_media.media_url,
                    "height": None,
                    "vcodec": "unknown",
                    "acodec": "unknown",
                    "ext": browser_media.media_ext,
                }
            ],
            "extractor_key": "DouyinBrowser",
            "_direct_url": browser_media.media_url,
            "_direct_audio_url": browser_media.audio_url,
            "_direct_ext": browser_media.media_ext,
            "_direct_audio_ext": browser_media.audio_ext,
        }
    else:
        opts = _base_opts()
        opts.update(
            {
                "skip_download": True,
                "extract_flat": False,
            }
        )
        if platform == "bilibili":
            opts["http_headers"]["Referer"] = "https://www.bilibili.com"

        def _run() -> dict[str, Any]:
            def _operation(ydl: Any) -> dict[str, Any]:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    raise AppError("未能解析该链接。")
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise AppError("暂不支持播放列表，请粘贴单条视频链接。", code="playlist")
                    info = entries[0]
                return info

            return _run_with_browser_cookie_fallback(opts, _operation)

        try:
            info = _run_with_timeout(_run, INFO_TIMEOUT_SEC)
        except TimeoutError:
            raise AppError("解析超时，请检查网络后重试。", code="timeout") from None
        except AppError:
            raise
        except (DownloadError, ExtractorError) as e:
            raise AppError(map_ytdlp_error(e)) from e
        except Exception as e:
            raise AppError(map_ytdlp_error(e)) from e

    if not _extractor_allowed(info):
        raise UnsupportedURLError()
    _reject_live_or_too_long(info)

    title = info.get("title") or "未命名视频"
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or ""
    duration = info.get("duration")
    thumbnail = info.get("thumbnail")
    if not thumbnail:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumbnail = thumbs[-1].get("url")

    return {
        "url": info.get("webpage_url") or url,
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "duration_string": info.get("duration_string") or _format_duration(duration),
        "thumbnail": thumbnail,
        "qualities": _available_qualities(info),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "has_ffmpeg": has_ffmpeg(),
        # Present only for the local browser fallback.  These values are
        # signed media URLs and are never written to auth/status responses.
        "direct_url": info.get("_direct_url"),
        "direct_audio_url": info.get("_direct_audio_url"),
        "direct_ext": info.get("_direct_ext"),
        "direct_audio_ext": info.get("_direct_audio_ext"),
    }


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _inner() -> None:
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — funnel into caller
            error["err"] = e

    t = threading.Thread(target=_inner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError()
    if "err" in error:
        raise error["err"]
    return result.get("value")


@dataclass
class Job:
    id: str
    url: str
    platform: str
    quality: str
    status: str = "queued"
    title: str | None = None
    percent: float = 0.0
    speed: str | None = None
    eta: str | None = None
    error: str | None = None
    output_path: str | None = None
    filename: str | None = None
    filesize: int | None = None
    filesize_label: str | None = None
    thumbnail: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["platform_label"] = PLATFORM_LABELS.get(self.platform, self.platform)
        d["status_label"] = {
            "queued": "排队中",
            "extracting": "解析中",
            "downloading": "下载中",
            "merging": "合并中",
            "finished": "已完成",
            "error": "失败",
        }.get(self.status, self.status)
        return d


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="download-worker")
        self._worker.start()

    def submit(self, url: str, quality: str) -> Job:
        platform = classify_url(url)
        url = normalize_url(url)
        if quality not in QUALITY_PRESETS:
            raise AppError("不支持的清晰度选项。", code="quality")
        job = Job(
            id=uuid.uuid4().hex[:12],
            url=url,
            platform=platform,
            quality=quality,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            extra = len(self._order) - MAX_RECENT_JOBS
            if extra > 0:
                for old_id in self._order[:extra]:
                    old = self._jobs.get(old_id)
                    if old and old.status in {"finished", "error"}:
                        self._jobs.pop(old_id, None)
                self._order = [i for i in self._order if i in self._jobs]
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self) -> list[Job]:
        with self._lock:
            jobs = [self._jobs[i] for i in self._order if i in self._jobs]
        jobs.reverse()
        return jobs

    def _update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for k, v in kwargs.items():
                setattr(job, k, v)
            job.updated_at = time.time()

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._run_download(job_id)
            except AppError as e:
                self._update(job_id, status="error", error=e.message, percent=0)
            except Exception as e:  # noqa: BLE001
                self._update(job_id, status="error", error=map_ytdlp_error(e), percent=0)
            finally:
                self._queue.task_done()

    def _progress_hook(self, job_id: str, d: dict[str, Any]) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            percent = 0.0
            if total:
                percent = max(0.0, min(99.0, done * 100.0 / total))
            self._update(
                job_id,
                status="downloading",
                percent=round(percent, 1),
                speed=_format_speed(d.get("speed")),
                eta=_format_eta(d.get("eta")),
            )
        elif status == "finished":
            self._update(job_id, status="merging", percent=99.0, speed=None, eta=None)
        elif status == "error":
            self._update(job_id, status="error", error="下载过程中出错。")

    def _pp_hook(self, job_id: str, d: dict[str, Any]) -> None:
        if d.get("status") == "started":
            self._update(job_id, status="merging", percent=99.0)
        elif d.get("status") == "finished":
            self._update(job_id, percent=99.5)

    def _run_download(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        self._update(job_id, status="extracting", percent=1)

        try:
            meta = extract_info(job.url)
        except AppError as e:
            self._update(job_id, status="error", error=e.message)
            return

        title = meta.get("title") or "video"
        qualities = meta.get("qualities") or []
        if job.quality not in qualities and job.quality != "best":
            # Fall back to best rather than failing a queued job if the
            # requested rung disappeared; still error if audio-only asked
            # of a video-less source with no audio option.
            if "best" in qualities:
                pass
            else:
                self._update(job_id, status="error", error="所选清晰度在该视频上不可用。")
                return

        self._update(
            job_id,
            title=title,
            thumbnail=meta.get("thumbnail"),
            status="downloading",
            percent=2,
        )

        stem = _unique_stem(title)

        direct_url = meta.get("direct_url")
        if direct_url:
            # Browser-discovered Douyin media is already a signed, muxed file;
            # download it directly instead of asking yt-dlp to re-extract the
            # page and hitting the same verification wall again.
            if job.quality == "audio":
                direct_url = meta.get("direct_audio_url")
                direct_ext = meta.get("direct_audio_ext") or "mp3"
                if not direct_url:
                    self._update(
                        job_id,
                        status="error",
                        error="该页面没有返回独立音频流，请选择「最佳」下载视频。",
                        percent=0,
                    )
                    return
            else:
                direct_ext = meta.get("direct_ext") or "mp4"
            direct_path = DOWNLOAD_DIR / f"{stem}.{direct_ext}"

            def _direct_progress(done: int, total: int | None) -> None:
                percent = 2.0
                if total:
                    percent = min(99.0, max(2.0, done * 100.0 / total))
                self._update(
                    job_id,
                    status="downloading",
                    percent=round(percent, 1),
                    speed=None,
                    eta=None,
                )

            try:
                size = download_media(
                    direct_url,
                    direct_path,
                    referer=meta.get("url") or job.url,
                    progress=_direct_progress,
                    max_bytes=MAX_FILESIZE,
                )
            except DouyinBrowserError as e:
                with _douyin_media_cache_lock:
                    _douyin_media_cache.pop(job.url, None)
                raise AppError(str(e)) from e
            self._update(
                job_id,
                status="finished",
                percent=100,
                speed=None,
                eta=None,
                output_path=str(direct_path.resolve()),
                filename=direct_path.name,
                filesize=size,
                filesize_label=_format_bytes(size),
                title=title,
            )
            return

        outtmpl = str(DOWNLOAD_DIR / f"{stem}.%(ext)s")
        use_ffmpeg = has_ffmpeg()
        fmt_map = QUALITY_FORMATS if use_ffmpeg else QUALITY_FORMATS_NO_FFMPEG
        fmt = fmt_map.get(job.quality, fmt_map["best"])

        opts = _base_opts()
        opts.update(
            {
                "format": fmt,
                "outtmpl": outtmpl,
                "max_filesize": MAX_FILESIZE,
                "progress_hooks": [lambda d, jid=job_id: self._progress_hook(jid, d)],
                "postprocessor_hooks": [lambda d, jid=job_id: self._pp_hook(jid, d)],
            }
        )
        if job.platform == "bilibili":
            opts["http_headers"]["Referer"] = "https://www.bilibili.com"

        if job.quality == "audio":
            if use_ffmpeg:
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                        "preferredquality": "192",
                    }
                ]
            opts["prefer_free_formats"] = False
        elif use_ffmpeg:
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ]

        final_path: Path | None = None

        def _hook_filename(d: dict[str, Any]) -> None:
            nonlocal final_path
            fn = d.get("filename") or d.get("info_dict", {}).get("_filename")
            if fn:
                p = Path(fn)
                if p.suffix not in {".part", ".ytdl"}:
                    final_path = p

        orig_progress = opts["progress_hooks"][0]

        def combined(d: dict[str, Any]) -> None:
            orig_progress(d)
            _hook_filename(d)

        opts["progress_hooks"] = [combined]

        try:
            def _operation(ydl: Any) -> None:
                nonlocal final_path
                info = ydl.extract_info(job.url, download=True)
                if info is None:
                    raise AppError("下载未返回文件。")
                prepared = ydl.prepare_filename(info)
                if prepared:
                    final_path = Path(prepared)
                    # Remux / audio extract may change extension.
                    if job.quality == "audio" and use_ffmpeg:
                        candidate = final_path.with_suffix(".m4a")
                        if candidate.exists():
                            final_path = candidate
                    elif use_ffmpeg:
                        mp4 = final_path.with_suffix(".mp4")
                        if mp4.exists():
                            final_path = mp4

            _run_with_browser_cookie_fallback(opts, _operation)
        except AppError:
            raise
        except (DownloadError, ExtractorError) as e:
            raise AppError(map_ytdlp_error(e)) from e
        except Exception as e:
            raise AppError(map_ytdlp_error(e)) from e

        if final_path is None or not final_path.exists():
            # Last resort: newest file with this stem.
            matches = sorted(DOWNLOAD_DIR.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            matches = [p for p in matches if p.suffix not in {".part", ".ytdl", ".temp"}]
            if matches:
                final_path = matches[0]
            else:
                raise AppError("下载完成但未找到输出文件。")

        size = final_path.stat().st_size if final_path.exists() else 0
        self._update(
            job_id,
            status="finished",
            percent=100,
            speed=None,
            eta=None,
            output_path=str(final_path.resolve()),
            filename=final_path.name,
            filesize=size,
            filesize_label=_format_bytes(size),
            title=title,
        )


manager = JobManager()
