"""
Terabox link resolver + downloader.

Terabox changes its web API often, so ALL site-specific logic lives in this file.
Resolver order:
  1. Official share API with your (throwaway) account cookie  -> TERABOX_COOKIE
  2. Optional third-party resolver API                        -> TERABOX_API_URL
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

import aiofiles
import aiohttp

from config import TERABOX_API_URL, TERABOX_COOKIE


log = logging.getLogger("terabox")


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


# ---------- 1) official share API (scrapes jsToken from the share page) ----------
_TOKEN_PATTERNS = (
    r"fn%28%22([^%\"']+)%22%29",
    r"fn\(\"([^\"]+)\"\)",
    r"\"jsToken\"\s*:\s*\"([^\"]+)\"",
    r"jsToken\s*=\s*['\"]([^'\"]+)['\"]",
)


def _find_token(page: str) -> Optional[str]:
    for pat in _TOKEN_PATTERNS:
        m = re.search(pat, page)
        if m:
            return m.group(1)
    return None


def _why(errno, msg="") -> str:
    if errno == 400210 or "verify" in str(msg).lower():
        return "Terabox asks for verification (need verify_v2), direct access is blocked"
    return f"Terabox error {errno} {msg}".strip()


async def _get_json(s: aiohttp.ClientSession, url: str, params: dict) -> dict:
    async with s.get(url, params=params) as r:
        txt = await r.text()
        status = r.status
    try:
        data = json.loads(txt)
        return data if isinstance(data, dict) else {}
    except ValueError:
        log.warning("%s -> non-JSON response (HTTP %s): %.120r", urlparse(url).path, status, txt)
        return {}


async def _fetch_page(s: aiohttp.ClientSession, page_url: str):
    async with s.get(page_url, allow_redirects=True) as r:
        return await r.text(), r.status, str(r.url)


async def _attempt(s: aiohttp.ClientSession, base: str, page: str, surl: str) -> list[TeraFile]:
    token = _find_token(page)
    m = re.search(r"dp-logid=([^&\"'\s]+)", page)
    logid = m.group(1) if m else None
    log.info("try %s: jsToken=%s logid=%s", urlparse(base).netloc, bool(token), bool(logid))

    common = {"app_id": "250528", "web": "1", "channel": "0"}
    if token:
        common["jsToken"] = token
    if logid:
        common["dp-logid"] = logid
    list_params = {**common, "page": "1", "num": "100", "by": "name", "order": "asc", "shorturl": surl, "root": "1"}

    async def list_dir(path, extra):
        q = {**list_params, **extra}
        if "shareid" in extra:          # signed call: identify the share by shareid/uk, not shorturl
            q.pop("shorturl", None)
        if path is not None:
            q.update({"dir": path, "root": "0"})
        data = await _get_json(s, f"{base}/share/list", q)
        errno = data.get("errno", -1)
        if errno != 0:
            raise TeraboxError(_why(errno, data.get("errmsg", "")))
        return data.get("list") or []

    async def collect(extra: dict) -> list[dict]:
        out: list[dict] = []

        async def walk(items, depth=0):
            for it in items:
                if len(out) >= 50:
                    return
                if str(it.get("isdir")) == "1":
                    if depth < 3:
                        await walk(await list_dir(it.get("path"), extra), depth + 1)
                else:
                    out.append(it)

        await walk(await list_dir(None, extra))
        return out

    signed: dict = {}

    async def get_signed() -> dict:
        nonlocal signed
        if signed:
            return signed
        info = await _get_json(s, f"{base}/api/shorturlinfo", {**common, "shorturl": "1" + surl, "root": "1"})
        errno = info.get("errno", -1)
        if errno != 0:
            raise TeraboxError(_why(errno, info.get("errmsg", "")))
        signed = {k: str(info[k]) for k in ("sign", "timestamp", "shareid", "uk", "randsk") if info.get(k) is not None}
        return signed

    first_err: Optional[Exception] = None
    items: list[dict] = []
    try:
        items = await collect({})
    except TeraboxError as ex:
        first_err = ex
        log.info("share/list (unsigned) failed: %s", ex)
    if not items:
        try:
            items = await collect(await get_signed())
        except TeraboxError as ex:
            first_err = first_err or ex
            log.info("share/list (signed) failed: %s", ex)
    if not items:
        raise first_err or TeraboxError("Terabox returned an empty file list")

    out: list[TeraFile] = []
    for it in items:
        dl = it.get("dlink")
        if not dl and it.get("fs_id"):
            try:
                sg = await get_signed()
                d = await _get_json(s, f"{base}/share/download", {
                    **common, **sg, "primaryid": sg.get("shareid", ""), "product": "share",
                    "nozip": "0", "fid_list": f"[{it['fs_id']}]",
                })
                dl = d.get("dlink") or ((d.get("list") or [{}])[0].get("dlink"))
                if not dl:
                    log.info("share/download: no dlink (errno=%s)", d.get("errno"))
            except Exception as ex:
                log.info("share/download failed: %s", ex)
        if dl:
            thumbs = it.get("thumbs") or {}
            out.append(TeraFile(
                name=it.get("server_filename") or "terabox_file",
                size=int(it.get("size") or 0),
                dlink=dl,
                thumb=thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1"),
            ))
    log.info("official resolver: %d item(s) listed, %d with download link", len(items), len(out))
    return out


async def _official(url: str, s: aiohttp.ClientSession) -> list[TeraFile]:
    page, status, final_url = await _fetch_page(s, url)
    fin = urlparse(final_url)
    surl = (parse_qs(fin.query).get("surl") or [None])[0] or get_surl(final_url) or get_surl(url)
    log.info("share page: HTTP %s host=%s surl=%s cookie=%s", status, fin.netloc, bool(surl), bool(TERABOX_COOKIE))
    if not surl:
        raise TeraboxError("Could not read the share id from this link.")

    # the link may redirect to a mirror (1024tera.com ...); also try the main Terabox hosts
    base0 = f"{fin.scheme}://{fin.netloc}"
    attempts = [(base0, page)]
    for b in ("https://www.terabox.com", "https://www.terabox.app"):
        if b != base0:
            attempts.append((b, None))

    errors: list[str] = []
    for base, pg in attempts:
        host = urlparse(base).netloc
        try:
            if pg is None:
                pg, _, _ = await _fetch_page(s, f"{base}/wap/share/filelist?surl={surl}")
            files = await _attempt(s, base, pg, surl)
            if files:
                return files
            errors.append(f"{host}: no download link")
        except Exception as ex:
            log.info("host %s failed: %s", host, ex)
            errors.append(f"{host}: {ex}")
    raise TeraboxError(" ; ".join(errors)[:200])


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
    errors: list[str] = []
    if TERABOX_COOKIE:
        try:
            async with aiohttp.ClientSession(headers=_headers(), timeout=aiohttp.ClientTimeout(total=45)) as s:
                files = await _official(url, s)
            if files:
                return files
            errors.append("official: files found but no download link")
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
    log.warning("resolve failed: %s", " | ".join(errors))
    raise TeraboxError("Could not fetch this link. It may be expired, private or blocked. "
                       + " | ".join(errors)[:220])


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
