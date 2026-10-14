"""
Terabox link resolver + downloader.

Terabox changes its web API often, so ALL site-specific logic lives in this file.
Resolver order:
  1. Official share API with your (throwaway) account cookie  -> TERABOX_COOKIE
  2. Optional third-party resolver API                        -> TERABOX_API_URL
"""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

import aiofiles
import aiohttp

from config import TERABOX_API_URL, TERABOX_COOKIE


class TeraboxError(Exception):
    pass


@dataclass
class TeraFile:
    name: str
    size: int
    dlink: str
    thumb: Optional[str] = None


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE = "https://www.terabox.com"
_DOMAINS = (
    "terabox|teraboxapp|1024terabox|1024tera|4funbox|mirrobox|nephobox|momerybox|"
    "teraboxlink|terafileshare|freeterabox|terasharefile|terasharelink|tibibox|gibibox"
)
LINK_RE = re.compile(rf"https?://(?:[\w-]+\.)*(?:{_DOMAINS})\.[a-z]{{2,4}}/\S+", re.I)
_COOKIE_HOSTS = ("terabox", "dubox", "1024tera", "4funbox", "teraboxcdn")


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip()[:120] or "file"


def get_surl(url: str) -> Optional[str]:
    p = urlparse(url)
    q = parse_qs(p.query)
    if "surl" in q:
        return q["surl"][0]
    m = re.search(r"/s/([\w-]+)", p.path)
    if m:
        s = m.group(1)
        return s[1:] if s.startswith("1") else s
    return None


def _headers(with_cookie: bool = True) -> dict:
    h = {"User-Agent": UA, "Accept": "*/*", "Referer": BASE + "/"}
    if with_cookie and TERABOX_COOKIE:
        h["Cookie"] = TERABOX_COOKIE
    return h


# ---------- 1) official share API ----------
async def _official(surl: str, s: aiohttp.ClientSession):
    base = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0"}
    out: list[TeraFile] = []

    async def list_dir(path=None):
        params = {**base, "shorturl": surl, "page": "1", "num": "100", "by": "name", "order": "asc"}
        params.update({"root": "1"} if path is None else {"dir": path, "root": "0"})
        async with s.get(f"{BASE}/share/list", params=params) as r:
            data = await r.json(content_type=None)
        if data.get("errno") not in (0, None):
            raise TeraboxError(f"Terabox error {data.get('errno')} (link expired/private or cookie invalid)")
        return data.get("list") or []

    async def walk(items, depth=0):
        for it in items:
            if len(out) >= 50:
                return
            if str(it.get("isdir")) == "1":
                if depth < 3:
                    await walk(await list_dir(it.get("path")), depth + 1)
            elif it.get("dlink"):
                thumbs = it.get("thumbs") or {}
                out.append(
                    TeraFile(
                        name=it.get("server_filename") or "terabox_file",
                        size=int(it.get("size") or 0),
                        dlink=it["dlink"],
                        thumb=thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1"),
                    )
                )

    await walk(await list_dir())
    return out


# ---------- 2) third-party resolver API ----------
_LINK_KEYS = ("dlink", "download_link", "direct_link", "downloadUrl", "download_url")


def _walk_json(obj) -> list[TeraFile]:
    files: list[TeraFile] = []
    if isinstance(obj, dict):
        link = next((obj[k] for k in _LINK_KEYS if isinstance(obj.get(k), str) and obj[k].startswith("http")), None)
        if link:
            try:
                size = int(obj.get("size") or obj.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            name = obj.get("server_filename") or obj.get("file_name") or obj.get("filename") or obj.get("name")
            thumb = obj.get("thumbnail") or obj.get("thumb")
            files.append(TeraFile(str(name or "terabox_video.mp4"), size, link, thumb if isinstance(thumb, str) else None))
        else:
            for v in obj.values():
                files += _walk_json(v)
    elif isinstance(obj, list):
        for v in obj:
            files += _walk_json(v)
    return files


async def _custom(url: str) -> list[TeraFile]:
    # separate session: never send the Terabox cookie to a third-party API
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=45)) as s:
        async with s.get(TERABOX_API_URL.format(url=quote(url, safe=""))) as r:
            data = await r.json(content_type=None)
    return _walk_json(data)


async def resolve(url: str) -> list[TeraFile]:
    surl = get_surl(url)
    errors: list[str] = []
    if surl and TERABOX_COOKIE:
        try:
            async with aiohttp.ClientSession(headers=_headers(), timeout=aiohttp.ClientTimeout(total=30)) as s:
                files = await _official(surl, s)
            if files:
                return files
            errors.append("official: no files")
        except Exception as ex:
            errors.append(f"official: {ex}")
    if TERABOX_API_URL:
        try:
            files = await _custom(url)
            if files:
                return files
            errors.append("api: no files")
        except Exception as ex:
            errors.append(f"api: {ex}")
    if not (TERABOX_COOKIE or TERABOX_API_URL):
        raise TeraboxError("Bot is not configured (TERABOX_COOKIE / TERABOX_API_URL missing).")
    raise TeraboxError("Could not fetch this link. It may be expired, private or blocked. " + " | ".join(errors)[:200])


# ---------- download ----------
async def download(f: TeraFile, dest: str, on_progress=None):
    host = urlparse(f.dlink).netloc.lower()
    headers = _headers(with_cookie=any(k in host for k in _COOKIE_HOSTS))
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=90)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(f.dlink, allow_redirects=True) as r:
            if r.status >= 400:
                raise TeraboxError(f"Download failed (HTTP {r.status}). Link may have expired.")
            ctype = r.headers.get("Content-Type", "")
            if ctype.startswith("text/") or "json" in ctype:
                raise TeraboxError("Terabox returned an error page instead of the file.")
            total = int(r.headers.get("Content-Length") or f.size or 0)
            done = 0
            async with aiofiles.open(dest, "wb") as out:
                async for chunk in r.content.iter_chunked(1 << 20):
                    await out.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        await on_progress(done, total)
    if done == 0:
        raise TeraboxError("Downloaded file is empty.")


async def fetch_thumb(url: str, dest: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession(headers=_headers(False), timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                async with aiofiles.open(dest, "wb") as out:
                    await out.write(await r.read())
        return dest
    except Exception:
        return None
