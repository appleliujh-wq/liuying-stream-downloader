"""流影 — local streaming-video downloader API."""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError as UrllibHTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import (
    ALLOWED_BROWSERS,
    AuthError,
    auth_status,
    delete_cookies_file,
    probe_browser_cookies,
    save_cookies_bytes,
    set_browser,
)
from app.downloader import (
    DOWNLOAD_DIR,
    DESKTOP_UA,
    AppError,
    UnsupportedURLError,
    classify_url,
    extract_info,
    has_ffmpeg,
    manager,
    normalize_url,
    ytdlp_version,
)
from app.douyin_browser import chrome_path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="流影", docs_url=None, redoc_url=None)

THUMB_TIMEOUT = 8
THUMB_MAX_BYTES = 8 * 1024 * 1024
THUMB_ALLOW_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "ytimg.com",
    "ggpht.com",
    "googleusercontent.com",
    "bilibili.com",
    "hdslb.com",
    "douyin.com",
    "douyinvod.com",
    "byteimg.com",
    "ibyteimg.com",
    "iesdouyin.com",
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokv.com",
    "muscdn.com",
    "byteoversea.com",
    "ibytedtos.com",
    "twimg.com",
    "twitter.com",
    "x.com",
)


class InfoRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=8000)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=8000)
    quality: str = Field(default="best", max_length=32)


class BrowserRequest(BaseModel):
    browser: str | None = None


def _host_allowed(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for suffix in THUMB_ALLOW_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


class _AllowlistRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in ("http", "https") or not _host_allowed(parsed.hostname):
            raise UrllibHTTPError(newurl, 403, "redirect off allowlist", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@app.get("/api/health")
def health() -> dict:
    ff = has_ffmpeg()
    return {
        "ok": True,
        "app": "liuying",
        "name": "流影",
        "yt_dlp": ytdlp_version(),
        "ffmpeg": ff,
        "ffmpeg_warning": None if ff else "未检测到 ffmpeg。最佳画质的视频+音频合并将不可用，请安装后重启。",
        "douyin_browser": bool(chrome_path()),
        "douyin_browser_warning": None
        if chrome_path()
        else "未检测到 Chrome/Edge，抖音自动解析不可用。",
        "download_dir": str(DOWNLOAD_DIR.resolve()),
        "platforms": ["youtube", "bilibili", "douyin", "tiktok", "twitter"],
    }


@app.get("/api/auth")
def api_auth() -> dict:
    return auth_status()


@app.post("/api/cookies")
async def api_cookies_upload(file: UploadFile = File(...)) -> dict:
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="cookies 文件过大（上限 2MB）。")
    try:
        save_cookies_bytes(raw)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    return {"ok": True, "has_cookies": True}


@app.delete("/api/cookies")
def api_cookies_delete() -> dict:
    try:
        delete_cookies_file()
    except AuthError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    return {"ok": True, "has_cookies": False}


@app.post("/api/auth/browser")
def api_auth_browser(body: BrowserRequest) -> dict:
    browser = body.browser
    if isinstance(browser, str):
        browser = browser.strip().lower() or None
    if browser is not None and browser not in ALLOWED_BROWSERS:
        raise HTTPException(status_code=400, detail="不支持的浏览器。请选择 Chrome、Chromium、Firefox、Edge 或 Brave。")
    try:
        if browser:
            probe_browser_cookies(browser)
        # Only persist a browser after the probe succeeds.  Otherwise a
        # locked Chrome database would poison every later parse/download.
        set_browser(browser)
    except AuthError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    return auth_status()


@app.get("/api/thumb")
def api_thumb(u: str = "") -> Response:
    url = (u or "").strip()
    if not url or len(url) > 2000:
        raise HTTPException(status_code=400, detail="无效的封面地址。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="无效的封面地址。")
    if not _host_allowed(parsed.hostname):
        raise HTTPException(status_code=400, detail="该封面域名不在允许列表中。")
    req = Request(
        url,
        headers={
            "User-Agent": DESKTOP_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    opener = build_opener(_AllowlistRedirect)
    try:
        with opener.open(req, timeout=THUMB_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            final_host = urlparse(resp.geturl()).hostname
            if not _host_allowed(final_host):
                raise HTTPException(status_code=400, detail="封面跳转到了不允许的域名。")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > THUMB_MAX_BYTES:
                    raise HTTPException(status_code=400, detail="封面文件过大。")
                chunks.append(block)
            body = b"".join(chunks)
    except HTTPException:
        raise
    except UrllibHTTPError as e:
        raise HTTPException(status_code=404, detail="无法获取封面。") from e
    except (URLError, TimeoutError, OSError, ValueError) as e:
        raise HTTPException(status_code=404, detail="无法获取封面。") from e
    if not body:
        raise HTTPException(status_code=404, detail="无法获取封面。")
    if ctype and not ctype.startswith("image/") and ctype not in {"application/octet-stream", "binary/octet-stream"}:
        raise HTTPException(status_code=404, detail="封面不是图片。")
    if not ctype or not ctype.startswith("image/"):
        if body[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif body[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            ctype = "image/webp"
        elif body[:6] in {b"GIF87a", b"GIF89a"}:
            ctype = "image/gif"
        else:
            ctype = "image/jpeg"
    return Response(content=body, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/info")
def api_info(body: InfoRequest) -> dict:
    try:
        classify_url(body.url)
        return extract_info(body.url)
    except UnsupportedURLError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    except AppError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@app.post("/api/download")
def api_download(body: DownloadRequest) -> dict:
    try:
        job = manager.submit(body.url, body.quality)
        return job.to_dict()
    except UnsupportedURLError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    except AppError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到该任务。")
    return job.to_dict()


@app.get("/api/jobs")
def api_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in manager.list_recent()]}


@app.get("/api/classify")
def api_classify(url: str) -> dict:
    try:
        platform = classify_url(url)
        from app.downloader import PLATFORM_LABELS

        return {
            "ok": True,
            "url": normalize_url(url),
            "platform": platform,
            "platform_label": PLATFORM_LABELS.get(platform, platform),
        }
    except AppError as e:
        return {"ok": False, "error": e.message, "platform": None}


@app.get("/api/files/{job_id}")
def api_file(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到该任务。")
    if job.status != "finished" or not job.output_path:
        raise HTTPException(status_code=409, detail="文件尚未准备好。")
    path = Path(job.output_path).resolve()
    root = DOWNLOAD_DIR.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=403, detail="非法文件路径。") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已不在磁盘上。")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )


@app.get("/")
def index():
    index_path = STATIC / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="前端文件缺失。")
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
