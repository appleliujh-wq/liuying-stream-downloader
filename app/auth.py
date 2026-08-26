"""Local cookie / browser-login store for 流影.

Cookies live only on this machine. Values are never logged or returned in API JSON.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

ALLOWED_BROWSERS = ("chrome", "chromium", "firefox", "edge", "brave")
MAX_COOKIES_BYTES = 2 * 1024 * 1024

AUTH_HINT = (
    "抖音公开视频会自动用本地临时 Chrome 解析，不需要复制 cookies。"
    "YouTube 年龄限制等场景才需要已登录且已确认年龄的账号。"
    "也可导入 Netscape 格式 cookies.txt；只存在你这台电脑，不会上传。"
)

_COOKIE_LINE = re.compile(
    r"^(?:#HttpOnly_)?"
    r"(?P<domain>\.?[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]|localhost)"
    r"\t(?:TRUE|FALSE)\t"
    r"[^\t]+\t"
    r"(?:TRUE|FALSE)\t"
    r"-?\d+\t"
    r"[^\t]+\t",
    re.IGNORECASE,
)


class AuthError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def data_dir() -> Path:
    override = os.environ.get("LIUYING_DATA_DIR")
    if override:
        return Path(override)
    return ROOT / "data"


def cookies_file() -> Path:
    return data_dir() / "cookies.txt"


def auth_file() -> Path:
    return data_dir() / "auth.json"


def _ensure_data_dir() -> Path:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def looks_like_netscape(text: str) -> bool:
    """True if the text looks like a Netscape cookies.txt export."""
    if not text:
        return False
    if text.startswith("\ufeff"):
        text = text[1:]
    stripped = text.lstrip()
    if not stripped:
        return False
    # Reject obvious non-cookie payloads without scanning further.
    head = stripped[:80].lstrip().lower()
    if head.startswith("<!doctype") or head.startswith("<html") or head.startswith("{") or head.startswith("["):
        return False
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    if first_line.startswith("# Netscape") or first_line.lower().startswith("# netscape"):
        return True
    if re.match(r"#\s*HTTP Cookie File", first_line, re.I):
        return True
    for raw in stripped.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if _COOKIE_LINE.match(line):
            return True
        parts = line.split("\t")
        if len(parts) >= 7:
            domain = parts[0].removeprefix("#HttpOnly_")
            if domain.startswith(".") or ("." in domain and " " not in domain):
                flag = parts[1].upper()
                secure = parts[3].upper()
                if flag in {"TRUE", "FALSE"} and secure in {"TRUE", "FALSE"}:
                    return True
    return False


def has_cookies() -> bool:
    path = cookies_file()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def load_auth_json() -> dict[str, Any]:
    path = auth_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get_browser() -> str | None:
    browser = load_auth_json().get("cookies_from_browser")
    if isinstance(browser, str) and browser in ALLOWED_BROWSERS:
        return browser
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    _ensure_data_dir()
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_cookies_bytes(raw: bytes) -> None:
    if len(raw) > MAX_COOKIES_BYTES:
        raise AuthError("cookies 文件过大（上限 2MB）。")
    if not raw.strip():
        raise AuthError("文件是空的。请导出 Netscape 格式的 cookies.txt。")
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    else:
        raise AuthError("无法读取该文件。请导出 UTF-8 或纯文本的 Netscape cookies.txt。")
    if not looks_like_netscape(text):
        raise AuthError("这不是 Netscape cookies.txt。请用 Get cookies.txt LOCALLY 一类扩展导出。")
    _atomic_write(cookies_file(), text.encode("utf-8"))


def delete_cookies_file() -> bool:
    path = cookies_file()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        raise AuthError("无法删除 cookies 文件。") from e


def probe_browser_cookies(browser: str) -> int:
    """Try loading cookies from a local browser. Raises AuthError on failure."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "cookiesfrombrowser": (browser,),
            }
        ) as ydl:
            return len(list(ydl.cookiejar))
    except AuthError:
        raise
    except Exception as e:
        low = str(e).lower()
        if "could not copy" in low or "cookie database" in low:
            raise AuthError(
                "读不到 Chrome cookies。新版 Chrome 经常锁住数据库。"
                "请改用下面的「导入 cookies.txt」，Chrome 开着就能导出。"
            ) from e
        raise AuthError("无法读取该浏览器。请改用导入 cookies.txt。") from e


def set_browser(browser: str | None) -> None:
    data = load_auth_json()
    if browser is None or browser == "":
        data.pop("cookies_from_browser", None)
    else:
        if browser not in ALLOWED_BROWSERS:
            raise AuthError("不支持的浏览器。请选择 Chrome、Chromium、Firefox、Edge 或 Brave。")
        data["cookies_from_browser"] = browser
    path = auth_file()
    if not data:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                _atomic_write(path, b"{}")
        return
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(path, payload)


def auth_status() -> dict[str, Any]:
    browser = get_browser()
    cookies = has_cookies()
    return {
        "has_cookies": cookies,
        "browser": browser,
        "browsers_available": list(ALLOWED_BROWSERS),
        "hint": AUTH_HINT,
    }


def apply_cookie_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Wire the user's own cookies into yt-dlp opts. cookies.txt wins over browser."""
    path = cookies_file()
    try:
        if path.is_file() and path.stat().st_size > 0:
            opts["cookiefile"] = str(path)
            opts.pop("cookiesfrombrowser", None)
            return opts
    except OSError:
        pass
    browser = get_browser()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts
