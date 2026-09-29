"""R1 下载检测 — CurseForge 链接解析 / FTB Quests 检测 / quests SNBT 抽取。

对应 TODO.md M1 [R1] 三项:
  1. parse_cf_link(url)              CF 整合包链接 → projectID / fileID
  2. detect_ftbq(projectID, fileID)  Range 只读包 zip 前 512KB,手走 ZIP 本地头找 manifest.json,
                                     判断依赖里有无 FTB Quests(project id 289412 / 438496)
  3. extract_quests_snbt(...)        取 config/ftbquests/quests/lang/en_us.snbt
       · downloadUrl 为 null 时按 edge/mediafilez.forgecdn.net/files/{id//1000}/{id%1000}/{name} 重建
       · 或走 api.modpacks.ch(native / curseforge)列文件、stream overrides zip

依赖:仅标准库。CURSEFORGE_API_KEY 存在时用官方 API 取文件信息;
不存在时自动降级到免密钥的 api.modpacks.ch 路径(FTB 自家索引,会给出真实的 CF 直链)。

自测:
    python src/fetch.py --selftest            # 离线:链接解析 / ZIP 字节走查 / 本地 HTTP 端到端
    python src/fetch.py --selftest --online   # 追加真实网络:modpacks.ch 列表 + 真实包 Range 检测
    python src/fetch.py --selftest --online --full   # 再追加真实整包下载抽取(约 170MB)
"""

from __future__ import annotations

import argparse
import http.client
import io
import json
import os
import re
import socket
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

__all__ = [
    "CFLinkRef",
    "FTBQReport",
    "QuestsSNBT",
    "FetchError",
    "MissingCurseForgeKey",
    "CFLinkError",
    "QuestsNotFound",
    "FTB_QUESTS_PROJECT_IDS",
    "CF_CDN_HOSTS",
    "DEFAULT_PREFIX_BYTES",
    "parse_cf_link",
    "resolve_cf_link",
    "resolve_project_id",
    "rebuild_cdn_url",
    "cdn_url_candidates",
    "resolve_download_urls",
    "detect_ftbq",
    "extract_quests_snbt",
    "extract_file_name_from_url",
    "pick_quest_lang_entry",
    "iter_zip_local_entries",
    "read_zip_entry",
    "find_zip_entry",
    "find_file_in_zip_bytes",
    "ZipLocalEntry",
    "cf_api_key",
    "cf_api_get",
    "get_cf_file",
    "find_project_id_by_slug",
    "modpacks_pack",
    "modpacks_version",
    "modpacks_latest_version_id",
    "modpacks_find_override_zip",
    "modpacks_find_quest_lang_files",
    "modpacks_has_ftbquests_snbt",
]

# ── 常量 ──────────────────────────────────────────────────────────────────────
CF_API_BASE = "https://api.curseforge.com/v1"
MODPACKS_NATIVE_API = "https://api.modpacks.ch/public/modpack"
MODPACKS_CF_API = "https://api.modpacks.ch/public/curseforge"
# 同一份文件在两家 CDN 上都有;实测 edge 对部分文件 404、mediafilez 可用,故两个都作为候选
CF_CDN_HOSTS = ("edge.forgecdn.net", "mediafilez.forgecdn.net")
FTB_QUESTS_PROJECT_IDS = (289412, 438496)  # Forge / Fabric
DEFAULT_PREFIX_BYTES = 512 * 1024
QUEST_LANG_NAME = "en_us.snbt"
USER_AGENT = "mc-web-ftbq/0.1"

ProgressCB = Callable[[int, str], None]
WarnCB = Callable[[str], None]

_ZIP_LOCAL_SIG = b"PK\x03\x04"
_ZIP_DESCRIPTOR_SIG = b"PK\x07\x08"
_ZIP_LOCAL_STRUCT = struct.Struct("<4sHHHHHIIIHH")  # sig/ver/flags/method/time/date/crc/csize/usize/flen/elen
_ZIP_LOCAL_FIXED = 30
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


# ── 异常 ──────────────────────────────────────────────────────────────────────
class FetchError(RuntimeError):
    """网络/解析层面的可预期失败。"""


class MissingCurseForgeKey(FetchError):
    """需要官方 API 但环境变量 CURSEFORGE_API_KEY 未设置。"""


class CFLinkError(ValueError):
    """链接无法解析成 projectID/fileID。"""


class QuestsNotFound(FetchError):
    """解析流程正常,但这个包里确实没有 quests 语言文件。

    definitive=True 表示已拿到确定结论(例如 manifest 里根本没有 FTB Quests),
    调用方不必再换别的路径重试。
    """

    def __init__(self, message: str, *, definitive: bool = False):
        super().__init__(message)
        self.definitive = definitive


class _HTTPStatusError(FetchError):
    """带状态码的 HTTP 失败,便于调用方决定要不要换路径重试。"""

    def __init__(self, code: int, reason: str, url: str):
        super().__init__(f"HTTP {code} {reason}: {url}")
        self.code = code
        self.url = url


# ── HTTP 基础 ─────────────────────────────────────────────────────────────────
def _emit(cb: Optional[ProgressCB], pct: int, msg: str) -> None:
    if cb is None:
        return
    try:
        cb(max(0, min(100, int(pct))), msg)
    except Exception:
        pass


def _warn(cb: Optional[WarnCB], msg: str) -> None:
    if cb is None:
        return
    try:
        cb(msg)
    except Exception:
        pass


def _open(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[float] = 30,
    retries: int = 3,
    backoff: float = 1.0,
):
    """urlopen + 瞬时错误重试(429/5xx/连接类)。返回 response,调用方负责 close。"""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)

    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in _RETRY_STATUS and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise _HTTPStatusError(e.code, str(e.reason), url) from e
        except (urllib.error.URLError, OSError, socket.timeout, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise FetchError(f"{type(e).__name__}: {e} ({url})") from e
    raise FetchError(f"请求失败: {url} ({last})")


def _get_bytes(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[float] = 30,
    retries: int = 3,
    backoff: float = 0.5,
) -> bytes:
    """整块读取小响应。

    重试放在这一层而不只放在连接层:响应体读到一半被截断(IncompleteRead 等
    http.client.HTTPException)也要能重来,否则 JSON 接口偶发半包就整条链路失败。
    """
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        resp = None
        try:
            resp = _open(url, headers=headers, timeout=timeout, retries=1)
            return resp.read()
        except _HTTPStatusError as e:
            last = e
            if e.code in _RETRY_STATUS and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
        except FetchError as e:
            # _open 传的是 retries=1,连接层的 FetchError 在这里由外层统一重试
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
        except (http.client.HTTPException, OSError, socket.timeout) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise FetchError(f"{type(e).__name__}: {e} ({url})") from e
        finally:
            if resp is not None:
                resp.close()
    raise FetchError(f"请求失败: {url} ({last})")


def _get_json(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[float] = 30,
    retries: int = 3,
) -> Any:
    raw = _get_bytes(url, headers=headers, timeout=timeout, retries=retries)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise FetchError(f"响应不是合法 JSON: {url}") from e


def fetch_prefix(
    url: str,
    nbytes: int = DEFAULT_PREFIX_BYTES,
    *,
    timeout: Optional[float] = 90,
    retries: int = 3,
) -> tuple[bytes, str]:
    """Range 只读前 nbytes 字节。返回 (数据, 最终 URL)。

    服务器不支持 Range(返回 200)时只读前 nbytes 就断开,不会拖整包。
    """
    hdrs = {"Range": f"bytes=0-{max(0, nbytes - 1)}"}
    resp = _open(url, headers=hdrs, timeout=timeout, retries=retries)
    try:
        data = resp.read(nbytes)
        final_url = resp.geturl()
    except (http.client.HTTPException, OSError, socket.timeout) as e:
        raise FetchError(f"读取前 {nbytes} 字节失败: {type(e).__name__}: {e} ({url})") from e
    finally:
        resp.close()
    if not data:
        raise FetchError(f"空响应: {url}")
    return data, final_url


def download_to_file(
    url: str,
    dest: Path,
    *,
    chunk_size: int = 256 * 1024,
    timeout: Optional[float] = 120,
    retries: int = 2,
    progress_cb: Optional[ProgressCB] = None,
) -> int:
    """流式下载到 dest,返回字节数。"""
    resp = _open(url, timeout=timeout, retries=retries)
    try:
        total = int(resp.headers.get("Content-Length") or 0)
        written = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                if total:
                    _emit(
                        progress_cb,
                        written * 100 // total,
                        f"下载中 {written / 1048576:.1f}/{total / 1048576:.1f} MB",
                    )
    except (http.client.HTTPException, OSError, socket.timeout) as e:
        raise FetchError(f"下载中断: {type(e).__name__}: {e} ({url})") from e
    finally:
        resp.close()
    if written == 0:
        raise FetchError(f"下载到 0 字节: {url}")
    return written


def _scale_progress(cb: Optional[ProgressCB], lo: int, hi: int) -> Optional[ProgressCB]:
    if cb is None:
        return None

    def inner(pct: int, msg: str) -> None:
        _emit(cb, lo + (hi - lo) * max(0, min(100, int(pct))) // 100, msg)

    return inner


def _host_variants(url: str) -> list[str]:
    """CF CDN 上的文件在 edge/mediafilez 两家都有,给出同路径的另一家作为备选。"""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in CF_CDN_HOSTS:
        return [url]
    out: list[str] = []
    for h in CF_CDN_HOSTS:
        variant = urllib.parse.urlunsplit((parts.scheme, h, parts.path, parts.query, parts.fragment))
        if variant not in out:
            out.append(variant)
    return out


# ── CurseForge 官方 API(可选,需要 CURSEFORGE_API_KEY)────────────────────────
def cf_api_key() -> Optional[str]:
    return (os.getenv("CURSEFORGE_API_KEY") or "").strip() or None


def _require_cf_api_key() -> str:
    key = cf_api_key()
    if not key:
        raise MissingCurseForgeKey(
            "环境变量 CURSEFORGE_API_KEY 未设置;可改用免密钥的 modpacks.ch 路径"
            "(extract_quests_snbt 的 native/curseforge 模式),或先配置 key 再解析 slug 链接"
        )
    return key


def cf_api_get(path: str, params: Optional[dict] = None, *, timeout: float = 30, retries: int = 3) -> Any:
    url = CF_API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _get_json(
        url,
        headers={"x-api-key": _require_cf_api_key(), "Accept": "application/json"},
        timeout=timeout,
        retries=retries,
    )


def get_cf_file(project_id: int, file_id: int, *, timeout: float = 30) -> dict:
    """GET /v1/mods/{modId}/files/{fileId}:含 fileName 与 downloadUrl(可能为 null)。"""
    data = cf_api_get(f"/mods/{int(project_id)}/files/{int(file_id)}", timeout=timeout)
    return (data or {}).get("data") or {}


def find_project_id_by_slug(slug: str, *, timeout: float = 30) -> Optional[int]:
    """官方 API 反查 slug → projectID(需要 key)。"""
    data = cf_api_get(
        "/mods/search",
        {"gameId": 432, "classId": 4471, "slug": slug, "pageSize": 20},
        timeout=timeout,
    )
    for mod in (data or {}).get("data", []):
        if (mod.get("slug") or "").lower() == slug.lower():
            return int(mod["id"])
    return None


# ── 链接解析 ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CFLinkRef:
    """解析出的整合包引用。project_id 为空但 slug 有值时需走官方 API 反查。"""

    url: str
    project_id: Optional[int] = None
    file_id: Optional[int] = None
    slug: Optional[str] = None
    file_name: Optional[str] = None
    kind: str = "unknown"  # modpack | cdn | api | query

    @property
    def needs_slug_lookup(self) -> bool:
        return self.project_id is None and bool(self.slug)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "projectID": self.project_id,
            "fileID": self.file_id,
            "slug": self.slug,
            "fileName": self.file_name,
            "kind": self.kind,
            "needsSlugLookup": self.needs_slug_lookup,
        }


def parse_cf_link(url: str) -> CFLinkRef:
    """从 CurseForge 整合包链接解析 projectID/fileID(纯字符串解析,不联网)。

    支持:
      · https://www.curseforge.com/minecraft/modpacks/<slug>[/files|/download/<fileID>]
      · https://legacy.curseforge.com/... 与 https://minecraft.curseforge.com/projects/<slug>/files/<fileID>
      · 数字 id 形式 /modpacks/<projectID>/files/<fileID>
      · https://www.curseforge.com/api/v1/mods/<projectID>/files/<fileID>/download
      · edge/mediafilez.forgecdn.net/files/<id//1000>/<id%1000>/<文件名>
      · 带 ?projectId=&fileId= 查询参数的链接
    """
    raw = (url or "").strip()
    if not raw:
        raise CFLinkError("空链接")

    candidate = raw if "://" in raw else "https://" + raw.lstrip("/")
    parts = urllib.parse.urlsplit(candidate)
    host = (parts.hostname or "").lower()
    path = urllib.parse.unquote(parts.path or "")
    query = urllib.parse.parse_qs(parts.query or "")

    def qint(*keys: str) -> Optional[int]:
        for k in keys:
            value = (query.get(k) or [None])[0]
            if value and value.isdigit():
                return int(value)
        return None

    q_pid = qint("projectId", "project_id", "projectID")
    q_fid = qint("fileId", "file_id", "fileID")

    def cdn_name(text: str) -> Optional[str]:
        tail = text.strip("/").rsplit("/", 1)[-1]
        return tail or None

    if host.endswith("forgecdn.net"):
        m = re.match(r"^/files/(\d+)/(\d+)/(.+)$", path)
        if m:
            file_id = int(m.group(1)) * 1000 + int(m.group(2))
            return CFLinkRef(raw, q_pid, file_id, None, cdn_name(m.group(3)), "cdn")
        m = re.match(r"^/files/(\d+)/(.+)$", path)
        if m:
            return CFLinkRef(raw, q_pid, int(m.group(1)), None, cdn_name(m.group(2)), "cdn")
        raise CFLinkError(f"无法识别的 CDN 链接: {url}")

    if host and host != "curseforge.com" and not host.endswith(".curseforge.com"):
        raise CFLinkError(f"不是 CurseForge 链接: {url}")

    m = re.search(r"/api/v\d+/mods/(\d+)/files/(\d+)(?:/download)?", path)
    if m:
        return CFLinkRef(raw, int(m.group(1)), int(m.group(2)), None, None, "api")

    m = re.search(r"/(?:modpacks|projects)/([^/]+)(?:/(?:files|download)/(\d+))?", path)
    if m:
        ident, fid = m.group(1), int(m.group(2)) if m.group(2) else None
        project_id = int(ident) if ident.isdigit() else None
        slug = None if project_id is not None else ident
        return CFLinkRef(raw, project_id or q_pid, fid or q_fid, slug, None, "modpack")

    if q_pid or q_fid:
        return CFLinkRef(raw, q_pid, q_fid, None, None, "query")

    raise CFLinkError(f"无法从链接解析出 projectID/fileID: {url}")


def resolve_project_id(ref: CFLinkRef | str, *, timeout: float = 30) -> int:
    """补全 projectID:数字链接直接返回,slug 链接走官方 API(需要 key)。"""
    if isinstance(ref, str):
        ref = parse_cf_link(ref)
    if ref.project_id is not None:
        return ref.project_id
    if ref.slug:
        found = find_project_id_by_slug(ref.slug, timeout=timeout)
        if found is None:
            raise CFLinkError(f"官方 API 查不到 slug 对应的整合包: {ref.slug}")
        return found
    raise CFLinkError(f"链接里既没有 projectID 也没有 slug: {ref.url}")


def resolve_cf_link(url: str, *, timeout: float = 30) -> CFLinkRef:
    """parse_cf_link + 必要时补全 projectID。"""
    ref = parse_cf_link(url)
    if ref.project_id is None and ref.slug:
        project_id = resolve_project_id(ref, timeout=timeout)
        ref = CFLinkRef(ref.url, project_id, ref.file_id, ref.slug, ref.file_name, ref.kind)
    return ref


# ── 下载直链重建 ──────────────────────────────────────────────────────────────
def rebuild_cdn_url(file_id: int, file_name: str, host: str = CF_CDN_HOSTS[0]) -> str:
    """downloadUrl 为 null 时重建直链:files/{id//1000}/{id%1000}/{文件名}。

    file_name 必须是未编码的原始文件名(含空格/+ 等会被正确转义)。
    """
    fid = int(file_id)
    name = (file_name or "").strip()
    if fid <= 0:
        raise ValueError(f"file_id 非法: {file_id!r}")
    if not name:
        raise ValueError("file_name 为空,无法重建直链")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    try:
        seq, idx = divmod(fid, 1000)
    except ZeroDivisionError:  # pragma: no cover - divmod(_, 1000) 不会抛
        raise ValueError(f"file_id 非法: {file_id!r}") from None
    return f"https://{host}/files/{seq}/{idx}/{urllib.parse.quote(name, safe='')}"


def cdn_url_candidates(file_id: int, file_name: str) -> list[str]:
    """两家 CDN 的重建候选(实测同一文件可能只有其中一家能下)。"""
    return [rebuild_cdn_url(file_id, file_name, host) for host in CF_CDN_HOSTS]


def extract_file_name_from_url(url: str) -> Optional[str]:
    tail = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "").strip("/").rsplit("/", 1)[-1]
    return tail or None


def resolve_download_urls(
    *,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    file_name: Optional[str] = None,
    download_url: Optional[str] = None,
    timeout: float = 60,
    on_warning: Optional[WarnCB] = None,
) -> list[tuple[str, Optional[str]]]:
    """给出一串 (url, 文件名) 候选,按可用性排序。

    顺序:显式传入的 downloadUrl → 官方 API(有 key 时)→ modpacks.ch curseforge 索引(免密钥)
          → 按 file_id + file_name 重建 CDN 直链
    """
    candidates: list[tuple[str, Optional[str]]] = []
    seen: set[tuple[str, Optional[str]]] = set()

    def add(url: Optional[str], name: Optional[str]) -> None:
        if not url:
            return
        name = name or extract_file_name_from_url(url)
        for variant in _host_variants(url):
            key = (variant, name)
            if key not in seen:
                seen.add(key)
                candidates.append(key)

    add(download_url, file_name)

    if project_id and file_id and cf_api_key():
        try:
            info = get_cf_file(project_id, file_id, timeout=timeout)
            name = info.get("fileName") or file_name
            url = info.get("downloadUrl")
            if not url and name:
                url = rebuild_cdn_url(file_id, name)
            add(url, name)
        except FetchError as e:
            _warn(on_warning, f"官方 API 取文件信息失败,继续走免密钥路径: {e}")

    if project_id and file_id:
        # modpacks.ch 的 curseforge 索引里 version_id 就是 CF 的 fileID,cf-extract 项即整合包本体直链
        try:
            manifest = modpacks_version(project_id, file_id, curseforge=True, timeout=timeout)
            override_zip = modpacks_find_override_zip(manifest.get("files") or [])
            if override_zip:
                add(override_zip.get("url"), override_zip.get("name"))
        except FetchError as e:
            _warn(on_warning, f"modpacks.ch 索引查询失败: {e}")

    if file_id and file_name:
        for url in cdn_url_candidates(file_id, file_name):
            add(url, file_name)

    return candidates


# ── ZIP 本地头走查(Range 片段里没有中央目录,只能手走)────────────────────────
@dataclass(frozen=True)
class ZipLocalEntry:
    name: str
    method: int
    flags: int
    comp_size: int
    uncomp_size: int
    header_offset: int
    data_offset: int

    @property
    def uses_data_descriptor(self) -> bool:
        return bool(self.flags & 0x8)


def _inflate_raw(data: bytes, start: int, end: int) -> Optional[tuple[bytes, int]]:
    """从 start 解压 raw deflate 流,返回 (内容, 流结束偏移);不完整/损坏返回 None。"""
    if start >= end:
        return None
    engine = zlib.decompressobj(-15)
    out: list[bytes] = []
    pos = start
    while pos < end:
        chunk = data[pos : min(end, pos + 65536)]
        pos += len(chunk)
        try:
            out.append(engine.decompress(chunk))
        except zlib.error:
            return None
        if engine.eof:
            return b"".join(out), pos - len(engine.unused_data)
    return None


def iter_zip_local_entries(data: bytes) -> Iterator[ZipLocalEntry]:
    """按顺序走查 bytes 里所有 ZIP 本地文件头(截断的片段也能用)。"""
    pos = 0
    end = len(data)
    while True:
        idx = data.find(_ZIP_LOCAL_SIG, pos)
        if idx < 0 or idx + _ZIP_LOCAL_FIXED > end:
            return
        (_sig, _ver, flags, method, _tm, _dt, _crc, comp_size, uncomp_size, fname_len, extra_len) = (
            _ZIP_LOCAL_STRUCT.unpack_from(data, idx)
        )
        if method not in (0, 8) or fname_len == 0:
            pos = idx + 4
            continue
        raw_name = data[idx + _ZIP_LOCAL_FIXED : idx + _ZIP_LOCAL_FIXED + fname_len]
        name = raw_name.decode("utf-8", errors="replace")
        if not name or any(ord(ch) < 32 for ch in name):
            # 大概率是压缩数据里偶然出现的 PK\x03\x04,跳过
            pos = idx + 4
            continue

        data_offset = idx + _ZIP_LOCAL_FIXED + fname_len + extra_len
        entry = ZipLocalEntry(name, method, flags, comp_size, uncomp_size, idx, data_offset)
        yield entry

        if comp_size > 0:
            pos = data_offset + comp_size
        else:
            # 流式写入的包(bit3 data descriptor)本地头 csize=0,靠 deflate 流自己结束来找下一项
            pos = idx + 4
            if method == 8:
                inflated = _inflate_raw(data, data_offset, end)
                if inflated is not None:
                    stream_end = inflated[1]
                    if data[stream_end : stream_end + 4] == _ZIP_DESCRIPTOR_SIG:
                        stream_end += 16  # descriptor: crc(4) + csize(4) + usize(4) + 4(尾部 usize)
                    pos = stream_end
            elif name.endswith("/"):
                pos = idx + 4
        if pos <= idx:
            pos = idx + 4


def read_zip_entry(data: bytes, entry: ZipLocalEntry, *, allow_truncated: bool = False) -> Optional[bytes]:
    """解出某个条目的内容;片段里数据不全时返回 None(除非 allow_truncated)。"""
    if entry.data_offset > len(data):
        return None
    if entry.method == 0:
        if entry.comp_size == 0:
            return b""
        end = entry.data_offset + entry.comp_size
        if end <= len(data):
            return data[entry.data_offset:end]
        return data[entry.data_offset:] if allow_truncated else None
    if entry.method == 8:
        end = entry.data_offset + entry.comp_size
        if entry.comp_size > 0 and end <= len(data):
            try:
                return zlib.decompress(data[entry.data_offset:end], -15)
            except zlib.error:
                pass  # 落到流式解压
        inflated = _inflate_raw(data, entry.data_offset, len(data))
        if inflated is not None:
            return inflated[0]
        return data[entry.data_offset:] if allow_truncated else None
    return None


def find_zip_entry(data: bytes, target: str) -> Optional[ZipLocalEntry]:
    """按名称(允许目录前缀)找条目。"""
    for entry in iter_zip_local_entries(data):
        name = entry.name.lstrip("./")
        if name == target or name.endswith("/" + target):
            return entry
    return None


def find_file_in_zip_bytes(data: bytes, target: str) -> Optional[bytes]:
    """在片段里找目标文件并解压出内容,找不到/解不开返回 None。"""
    entry = find_zip_entry(data, target)
    if entry is None:
        return None
    return read_zip_entry(data, entry)


def _looks_ftbquests(name: str) -> bool:
    low = name.lower()
    return "ftb-quests" in low or "ftbquests" in low


def _looks_ftbquests_jar(name: str) -> bool:
    return name.lower().endswith(".jar") and _looks_ftbquests(name)


def _quest_lang_rank(name: str) -> Optional[int]:
    """越小越优先:标准路径 0,含 ftbquests 的 lang 1,其它 en_us.snbt 2。"""
    norm = name.replace("\\", "/").lstrip("./")
    if norm.endswith(f"ftbquests/quests/lang/{QUEST_LANG_NAME}"):
        return 0
    if "ftbquests" in norm.lower() and norm.endswith(f"lang/{QUEST_LANG_NAME}"):
        return 1
    if norm.endswith(f"lang/{QUEST_LANG_NAME}"):
        return 2
    return None


def pick_quest_lang_entry(names: Iterable[str]) -> Optional[str]:
    """从 zip 条目名里挑出 quests 的 en_us.snbt(overrides/ 前缀无所谓)。"""
    best: Optional[tuple[int, str]] = None
    for name in names:
        rank = _quest_lang_rank(name)
        if rank is None:
            continue
        if best is None or rank < best[0]:
            best = (rank, name)
            if rank == 0:
                break
    return best[1] if best else None


# ── modpacks.ch(免密钥路径)───────────────────────────────────────────────────
def _modpacks_json(url: str, *, timeout: float = 60, retries: int = 3) -> dict:
    data = _get_json(url, timeout=timeout, retries=retries)
    if isinstance(data, dict) and data.get("status") == "error":
        raise FetchError(data.get("message") or f"modpacks.ch 返回错误: {url}")
    if not isinstance(data, dict):
        raise FetchError(f"modpacks.ch 响应格式异常: {url}")
    return data


def modpacks_pack(pack_id: int, *, curseforge: bool = False, timeout: float = 60) -> dict:
    base = MODPACKS_CF_API if curseforge else MODPACKS_NATIVE_API
    return _modpacks_json(f"{base}/{int(pack_id)}", timeout=timeout)


def modpacks_version(pack_id: int, version_id: int, *, curseforge: bool = False, timeout: float = 60) -> dict:
    """取某版本的完整文件清单。

    curseforge=True 时 version_id 用 CF 的 fileID(FTB 的 curseforge 索引即按此编号)。
    """
    base = MODPACKS_CF_API if curseforge else MODPACKS_NATIVE_API
    return _modpacks_json(f"{base}/{int(pack_id)}/{int(version_id)}", timeout=timeout)


def modpacks_latest_version_id(pack_id: int, *, curseforge: bool = False, timeout: float = 60) -> Optional[int]:
    """版本列表里的最后一个(通常最新)。"""
    versions = modpacks_pack(pack_id, curseforge=curseforge, timeout=timeout).get("versions") or []
    if not versions:
        return None
    return int(versions[-1]["id"])


def _modpacks_full_name(entry: dict) -> str:
    path = (entry.get("path") or "").strip()
    name = entry.get("name") or ""
    path = path[2:] if path.startswith("./") else path
    return f"{path.rstrip('/')}/{name}" if path else name


def modpacks_find_override_zip(files: Iterable[dict]) -> Optional[dict]:
    """CF 整合包在 modpacks.ch 上的 overrides/整包 zip(type == cf-extract)。"""
    fallback: Optional[dict] = None
    for entry in files or []:
        url = entry.get("url")
        if not url:
            continue
        if entry.get("type") == "cf-extract":
            return entry
        if fallback is None and (entry.get("name") or "").lower().endswith(".zip"):
            fallback = entry
    return fallback


def modpacks_find_quest_lang_files(files: Iterable[dict]) -> list[dict]:
    """manifest 里直接列出的 quests en_us.snbt(native 包可单文件下载,省掉整包)。"""
    hits: list[tuple[int, dict]] = []
    for entry in files or []:
        rank = _quest_lang_rank(_modpacks_full_name(entry))
        if rank is not None and entry.get("url"):
            hits.append((rank, entry))
    hits.sort(key=lambda item: item[0])
    return [entry for _rank, entry in hits]


def modpacks_has_ftbquests_snbt(files: Iterable[dict]) -> bool:
    for entry in files or []:
        full = _modpacks_full_name(entry)
        if _looks_ftbquests(full) and full.endswith(".snbt"):
            return True
    return False


# ── FTB Quests 检测 ───────────────────────────────────────────────────────────
@dataclass
class FTBQReport:
    has_ftb_quests: Optional[bool] = None
    ftb_quests_project_ids: list[int] = dataclass_field(default_factory=list)
    manifest_found: bool = False
    pack_name: Optional[str] = None
    pack_version: Optional[str] = None
    mod_count: Optional[int] = None
    matched_zip_names: list[str] = dataclass_field(default_factory=list)
    download_url: Optional[str] = None
    file_name: Optional[str] = None
    bytes_read: int = 0
    source: str = "none"  # manifest | zip-names | modpacks-mods | none
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "hasFTBQuests": self.has_ftb_quests,
            "ftbQuestsProjectIDs": self.ftb_quests_project_ids,
            "manifestFound": self.manifest_found,
            "packName": self.pack_name,
            "packVersion": self.pack_version,
            "modCount": self.mod_count,
            "matchedZipNames": self.matched_zip_names,
            "downloadUrl": self.download_url,
            "fileName": self.file_name,
            "bytesRead": self.bytes_read,
            "source": self.source,
            "note": self.note,
        }


def detect_ftbq(
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    *,
    download_url: Optional[str] = None,
    file_name: Optional[str] = None,
    prefix_bytes: int = DEFAULT_PREFIX_BYTES,
    timeout: float = 90,
    on_warning: Optional[WarnCB] = None,
) -> FTBQReport:
    """对整合包 zip 发 Range 请求只读前 prefix_bytes,解析 manifest.json 判断是否依赖 FTB Quests。

    has_ftb_quests: True/False 为确定结论;None 表示取不到直链或前 512KB 里没有 manifest(→ R4 记 failed)。
    """
    report = FTBQReport()
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        _warn(on_warning, msg)

    try:
        candidates = resolve_download_urls(
            project_id=project_id,
            file_id=file_id,
            file_name=file_name,
            download_url=download_url,
            timeout=timeout,
            on_warning=warn,
        )
    except FetchError as e:
        report.note = f"解析下载直链失败: {e}"
        return report

    if not candidates:
        report.note = "无法得到下载直链(需要 fileID;若只有 slug 还需 CURSEFORGE_API_KEY)"
        return report

    errors: list[str] = []
    for url, name in candidates:
        try:
            data, final_url = fetch_prefix(url, prefix_bytes, timeout=timeout)
        except FetchError as e:
            errors.append(str(e))
            continue

        report.download_url = final_url
        report.file_name = name
        report.bytes_read = len(data)

        entries = [entry.name for entry in iter_zip_local_entries(data)]
        report.matched_zip_names = [n for n in entries if _looks_ftbquests(n)][:20]

        manifest_bytes = find_file_in_zip_bytes(data, "manifest.json")
        manifest: Optional[dict] = None
        if manifest_bytes is None:
            if find_zip_entry(data, "manifest.json") is not None:
                report.note = "manifest.json 落在前 %d KB 之外或数据被截断" % (prefix_bytes // 1024)
            else:
                report.note = "前 %d KB 内没有 manifest.json" % (prefix_bytes // 1024)
        else:
            report.manifest_found = True
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                report.note = f"manifest.json 解析失败: {type(e).__name__}"

        if isinstance(manifest, dict):
            report.pack_name = manifest.get("name")
            report.pack_version = manifest.get("version")
            files = manifest.get("files") or []
            report.mod_count = len(files)
            pids: list[int] = []
            for item in files:
                raw_pid = item.get("projectID") or item.get("project_id")
                try:
                    pids.append(int(raw_pid))
                except (TypeError, ValueError):
                    continue
            hits = sorted({pid for pid in pids if pid in FTB_QUESTS_PROJECT_IDS})
            report.ftb_quests_project_ids = hits
            report.has_ftb_quests = bool(hits)
            report.source = "manifest"
            if not hits and any(_looks_ftbquests_jar(n) for n in report.matched_zip_names):
                # 依赖清单说没有,但包体里躺着 ftb-quests 的 jar(少见但要认)
                report.has_ftb_quests = True
                report.source = "zip-names"
                report.note = "manifest 未列出 FTB Quests,但压缩包内发现 ftb-quests jar"
            return report

        if report.matched_zip_names:
            report.has_ftb_quests = True
            report.source = "zip-names"
            report.note = report.note or "未读到 manifest,按压缩包内 ftbquests 文件名判断"
            return report

        if not report.manifest_found and project_id and file_id:
            # 实测不少包的 manifest.json 不在前 512KB;退一步用 FTB 自家索引的模组清单判断
            try:
                index_doc = modpacks_version(project_id, file_id, curseforge=True, timeout=timeout)
                mods = [f for f in (index_doc.get("files") or []) if f.get("type") == "mod"]
                if mods:
                    hits = [f.get("name") or _modpacks_full_name(f) for f in mods if _looks_ftbquests(f.get("name") or "")]
                    report.mod_count = len(mods)
                    report.has_ftb_quests = bool(hits)
                    report.source = "modpacks-mods"
                    if hits:
                        report.matched_zip_names = hits[:5]
                    report.note = "前 512KB 里没有 manifest.json,按 modpacks.ch 模组清单判断"
                    return report
            except FetchError as e:
                warn(f"modpacks.ch 模组清单查询失败: {e}")

        if errors:
            report.note = report.note or errors[0]
        return report

    report.note = errors[0] if errors else "所有候选直链都不可用"
    if len(errors) > 1:
        report.note += f"(另有 {len(errors) - 1} 个候选失败)"
    return report


# ── quests SNBT 抽取 ─────────────────────────────────────────────────────────
@dataclass
class QuestsSNBT:
    content: str
    entry_name: str
    source: str  # modpacks-native | modpacks-cf-zip | cf-zip
    pack_zip_url: Optional[str] = None
    pack_name: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "entryName": self.entry_name,
            "source": self.source,
            "packZipUrl": self.pack_zip_url,
            "packName": self.pack_name,
            "chars": len(self.content),
        }


def _download_zip_and_pick(url: str, *, timeout: float, progress_cb: Optional[ProgressCB]) -> tuple[Path, bytes]:
    """下载整包 zip 到临时文件,返回 (临时路径, 全部条目名)。调用方负责删除。"""
    fd, tmp_name = tempfile.mkstemp(prefix="mcweb_pack_", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        download_to_file(url, tmp_path, timeout=timeout, progress_cb=progress_cb)
        try:
            with zipfile.ZipFile(tmp_path) as zf:
                return tmp_path, zf.namelist()
        except zipfile.BadZipFile as e:
            raise FetchError(f"下载到的不是合法 zip: {url}") from e
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _extract_from_pack_zip_url(
    url: str,
    *,
    pack_name: Optional[str],
    source: str,
    timeout: float,
    progress_cb: Optional[ProgressCB],
    on_warning: Optional[WarnCB] = None,
) -> QuestsSNBT:
    last_error: Optional[Exception] = None
    for candidate in _host_variants(url):
        tmp_path: Optional[Path] = None
        try:
            _emit(progress_cb, 1, "下载整合包 zip(只取需要的文件)…")
            tmp_path, names = _download_zip_and_pick(
                candidate, timeout=timeout, progress_cb=_scale_progress(progress_cb, 2, 90)
            )
            picked = pick_quest_lang_entry(names)
            if picked is None:
                ftbq_names = [n for n in names if _looks_ftbquests(n)][:10]
                detail = f";包内 ftbquests 相关条目示例: {ftbq_names}" if ftbq_names else ""
                raise QuestsNotFound(
                    f"zip 里没有 config/ftbquests/quests/lang/{QUEST_LANG_NAME}{detail}",
                    definitive=True,
                )
            _emit(progress_cb, 95, f"解压 {picked} …")
            with zipfile.ZipFile(tmp_path) as zf:
                content = zf.read(picked).decode("utf-8", errors="replace")
            _emit(progress_cb, 100, "抽取完成")
            return QuestsSNBT(
                content=content,
                entry_name=picked,
                source=source,
                pack_zip_url=candidate,
                pack_name=pack_name,
            )
        except QuestsNotFound:
            raise
        except FetchError as e:
            last_error = e
            _warn(on_warning, f"{candidate} 不可用: {e}")
            continue
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    raise FetchError(f"整合包 zip 下载失败: {last_error or '无可用直链'}")


def _extract_native(
    pack_id: Optional[int],
    version_id: Optional[int],
    *,
    timeout: float,
    progress_cb: Optional[ProgressCB],
    on_warning: Optional[WarnCB],
) -> QuestsSNBT:
    if not pack_id or not version_id:
        raise QuestsNotFound("native 路径需要 pack_id + version_id", definitive=False)
    _emit(progress_cb, 2, "查询 api.modpacks.ch native 文件清单…")
    manifest = modpacks_version(pack_id, version_id, curseforge=False, timeout=timeout)
    files = manifest.get("files") or []

    hit = modpacks_find_quest_lang_files(files)
    if hit:
        entry = hit[0]
        url = entry["url"]
        _emit(progress_cb, 20, f"下载 {_modpacks_full_name(entry)} …")
        content = _get_bytes(url, timeout=timeout).decode("utf-8", errors="replace")
        _emit(progress_cb, 100, "抽取完成")
        return QuestsSNBT(
            content=content,
            entry_name=_modpacks_full_name(entry),
            source="modpacks-native",
            pack_zip_url=url,
            pack_name=manifest.get("name"),
        )

    if modpacks_has_ftbquests_snbt(files):
        raise QuestsNotFound(
            f"包 {pack_id} v{version_id} 里 FTB Quests 是 1.12.2 硬编码章节格式,没有 lang/{QUEST_LANG_NAME}"
            "(见 TODO M4)",
            definitive=True,
        )
    raise QuestsNotFound(f"包 {pack_id} v{version_id} 的文件清单里没有 FTB Quests", definitive=True)


def _extract_curseforge(
    *,
    pack_id: Optional[int],
    version_id: Optional[int],
    project_id: Optional[int],
    file_id: Optional[int],
    timeout: float,
    progress_cb: Optional[ProgressCB],
    on_warning: Optional[WarnCB],
) -> QuestsSNBT:
    index_id = pack_id or project_id
    index_version = version_id or file_id
    if not index_id or not index_version:
        raise QuestsNotFound("curseforge 路径需要 pack_id/project_id + version_id/file_id", definitive=False)

    _emit(progress_cb, 2, "查询 api.modpacks.ch curseforge 清单…")
    manifest = modpacks_version(index_id, index_version, curseforge=True, timeout=timeout)
    files = manifest.get("files") or []

    hit = modpacks_find_quest_lang_files(files)
    if hit:
        entry = hit[0]
        _emit(progress_cb, 20, f"下载 {_modpacks_full_name(entry)} …")
        content = _get_bytes(entry["url"], timeout=timeout).decode("utf-8", errors="replace")
        _emit(progress_cb, 100, "抽取完成")
        return QuestsSNBT(
            content=content,
            entry_name=_modpacks_full_name(entry),
            source="modpacks-native",
            pack_zip_url=entry["url"],
            pack_name=manifest.get("name"),
        )

    override_zip = modpacks_find_override_zip(files)
    if not override_zip:
        raise QuestsNotFound(
            f"modpacks.ch 上没有 {index_id}/{index_version} 的 overrides zip(可能未被索引)",
            definitive=False,
        )
    return _extract_from_pack_zip_url(
        override_zip["url"],
        pack_name=manifest.get("name"),
        source="modpacks-cf-zip",
        timeout=timeout,
        progress_cb=progress_cb,
        on_warning=on_warning,
    )


def extract_quests_snbt(
    *,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    pack_id: Optional[int] = None,
    version_id: Optional[int] = None,
    pack_type: str = "auto",
    download_url: Optional[str] = None,
    file_name: Optional[str] = None,
    timeout: float = 600,
    progress_cb: Optional[ProgressCB] = None,
    on_warning: Optional[WarnCB] = None,
) -> QuestsSNBT:
    """抽取 config/ftbquests/quests/lang/en_us.snbt。

    pack_type:
      · "native"     — FTB 自家包:pack_id/version_id 走 api.modpacks.ch/public/modpack
      · "curseforge" — CF 包:pack_id(或 project_id)/version_id(或 file_id)走
                       api.modpacks.ch/public/curseforge,下 cf-extract zip 后抽取
      · "zip"        — 直接给 download_url(含 file_name 备用)下载整包后抽取
      · "auto"       — 按参数能走哪条走哪条;native 拿到明确结论(例如硬编码格式)就停止

    拿不到语言文件时抛 QuestsNotFound(definitive=True 表示已确认这个包没有)。
    """
    mode = (pack_type or "auto").lower()
    plan: list[str] = []
    if mode in ("auto", "native") and pack_id:
        plan.append("native")
    if mode in ("auto", "curseforge", "cf") and (pack_id or project_id):
        plan.append("curseforge")
    if mode in ("auto", "zip") and (download_url or (project_id and file_id)):
        plan.append("zip")
    if not plan:
        raise QuestsNotFound(
            "参数不足:至少给出 (pack_id+version_id) / (project_id+file_id) / download_url",
            definitive=False,
        )

    errors: list[str] = []
    for step in plan:
        try:
            if step == "native":
                return _extract_native(
                    pack_id, version_id, timeout=timeout, progress_cb=progress_cb, on_warning=on_warning
                )
            if step == "curseforge":
                return _extract_curseforge(
                    pack_id=pack_id,
                    version_id=version_id,
                    project_id=project_id,
                    file_id=file_id,
                    timeout=timeout,
                    progress_cb=progress_cb,
                    on_warning=on_warning,
                )
            urls = resolve_download_urls(
                project_id=project_id,
                file_id=file_id,
                file_name=file_name,
                download_url=download_url,
                timeout=min(timeout, 120),
                on_warning=on_warning,
            )
            if not urls:
                raise QuestsNotFound("无法解析出整合包 zip 直链", definitive=False)
            url, name = urls[0]
            return _extract_from_pack_zip_url(
                url,
                pack_name=name,
                source="cf-zip",
                timeout=timeout,
                progress_cb=progress_cb,
                on_warning=on_warning,
            )
        except QuestsNotFound as e:
            if e.definitive:
                raise
            errors.append(f"[{step}] {e}")
        except FetchError as e:
            errors.append(f"[{step}] {e}")

    raise QuestsNotFound(";".join(errors) or "所有抽取路径都失败", definitive=False)


# ══════════════════════════════════════════════════════════════════════════════
# 自测
# ══════════════════════════════════════════════════════════════════════════════
_SAMPLE_SNBT = """{
\tdefault_hide_dependency_lines: false
\tchapter "01F1B1CC0E5A1234" {
\t\ttitle: "Getting Started"
\t\tdescription: ["Welcome to the test pack"]
\t}
}"""

_MANIFEST_FTBQ = {
    "minecraft": {"version": "1.21.1"},
    "manifestType": "minecraftModpack",
    "manifestVersion": 1,
    "name": "Test Pack",
    "version": "1.0.0",
    "author": "selftest",
    "files": [
        {"projectID": 289412, "fileID": 7133722, "required": True},
        {"projectID": 238222, "fileID": 1, "required": False},
    ],
    "overrides": "overrides",
}

_MANIFEST_NO_FTBQ = {
    "minecraft": {"version": "1.20.1"},
    "manifestType": "minecraftModpack",
    "manifestVersion": 1,
    "name": "No Quest Pack",
    "version": "0.1.0",
    "files": [{"projectID": 238222, "fileID": 2, "required": True}],
    "overrides": "overrides",
}

_SAMPLE_PACK_NAME = "overrides/config/ftbquests/quests/lang/en_us.snbt"


class _Checks:
    def __init__(self, label: str):
        self.label = label
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: bool, msg: str) -> None:
        if condition:
            self.passed += 1
            print(f"  [ok] {msg}")
        else:
            self.failed.append(msg)
            print(f"  [FAIL] {msg}")

    def eq(self, actual, expected, msg: str) -> None:
        self.check(actual == expected, f"{msg} (期望 {expected!r}, 实际 {actual!r})")

    def summary(self) -> bool:
        total = self.passed + len(self.failed)
        print(f"\n[{self.label}] {self.passed}/{total} 通过")
        for item in self.failed:
            print(f"  - 未通过: {item}")
        return not self.failed


def _make_pack_zip(
    manifest: dict,
    *,
    with_lang: bool = True,
    with_mod_jar: bool = True,
    pad_before_manifest: int = 0,
    pad_after: int = 0,
    seed: int = 1234,
) -> bytes:
    import random

    rng = random.Random(seed)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if pad_before_manifest:
            # 不可压缩内容,确保 manifest.json 不落在第一个条目
            zf.writestr("overrides/README.txt", rng.randbytes(pad_before_manifest))
        zf.writestr("manifest.json", json.dumps(manifest))
        if with_mod_jar:
            zf.writestr("overrides/mods/ftb-quests-forge-2001.3.1.jar", b"MZ" + b"\x00" * 128)
        if with_lang:
            zf.writestr(_SAMPLE_PACK_NAME, _SAMPLE_SNBT)
        if pad_after:
            zf.writestr("overrides/config/zzz_pad.bin", rng.randbytes(pad_after))
    return buf.getvalue()


def _craft_descriptor_zip() -> tuple[bytes, bytes]:
    """手工造一个用 data descriptor(本地头 csize=0)的 zip,返回 (字节, 期望内容)。"""
    payload = b'{"files": [{"projectID": 438496}]}'
    name = b"manifest.json"
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw = comp.compress(payload) + comp.flush()
    crc = zlib.crc32(payload) & 0xFFFFFFFF

    out = bytearray()
    out += _ZIP_LOCAL_STRUCT.pack(_ZIP_LOCAL_SIG, 20, 0x8, 8, 0, 0, 0, 0, 0, len(name), 0)
    out += name
    out += raw
    out += _ZIP_DESCRIPTOR_SIG + struct.pack("<III", crc, len(raw), len(payload))

    second = b"overrides/config/ftbquests/quests/lang/en_us.snbt"
    second_payload = _SAMPLE_SNBT.encode()
    comp2 = zlib.compressobj(9, zlib.DEFLATED, -15)
    raw2 = comp2.compress(second_payload) + comp2.flush()
    out += _ZIP_LOCAL_STRUCT.pack(_ZIP_LOCAL_SIG, 20, 0, 8, 0, 0, 0, len(raw2), len(second_payload), len(second), 0)
    out += second + raw2
    return bytes(out), payload


def _make_range_handler():
    """极简测试服务器:支持 Range / 302 跳转 / 忽略 Range / 404。"""
    import http.server

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        routes: dict[str, bytes] = {}
        state: dict[str, int] = {}

        def log_message(self, *args) -> None:  # 静音
            pass

        def _send(self, code: int, body: bytes, extra: Optional[dict] = None) -> None:
            self.send_response(code)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端只读了前 512KB 就断开,属预期

        def _respond(self, body: bytes) -> None:
            if self.path.rstrip("/") == "/norange.zip":
                self._send(200, body)  # 模拟不支持 Range 的服务器
                return
            m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
            if not m:
                self._send(200, body)
                return
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else len(body) - 1, len(body) - 1)
            self._send(206, body[start : end + 1], {"Content-Range": f"bytes {start}-{end}/{len(body)}"})

        def do_GET(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/redirect":
                self._send(302, b"", {"Location": "/pack.zip"})
                return
            if path in ("/flaky-once", "/truncated-once"):
                # 首次访问故意失败(503 / 声明 1024 字节却只发 100 后断开),之后照常
                if self.state.get(path, 0) == 0:
                    self.state[path] = 1
                    if path == "/flaky-once":
                        self._send(503, b"busy")
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", "1024")
                        self.end_headers()
                        try:
                            self.wfile.write(b"x" * 100)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        self.close_connection = True
                    return
                self._respond(self.routes.get("/pack.zip", b""))
                return
            body = self.routes.get(path)
            if body is None:
                self._send(404, b"not found")
                return
            self._respond(body)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

    return RangeHandler


def _selftest_offline(checks: _Checks) -> None:
    print("\n== 离线:parse_cf_link ==")
    parse_cases = [
        ("https://www.curseforge.com/minecraft/modpacks/ftb-stoneblock-4/files/7166087", None, 7166087, "ftb-stoneblock-4"),
        ("https://www.curseforge.com/minecraft/modpacks/ftb-stoneblock-4/download/7166087", None, 7166087, "ftb-stoneblock-4"),
        ("https://www.curseforge.com/minecraft/modpacks/ftb-stoneblock-4", None, None, "ftb-stoneblock-4"),
        ("https://www.curseforge.com/minecraft/modpacks/1373378/files/7166087", 1373378, 7166087, None),
        ("https://legacy.curseforge.com/minecraft/modpacks/ftb-skies-2/files/7123456", None, 7123456, "ftb-skies-2"),
        ("https://minecraft.curseforge.com/projects/ftb-stoneblock-4/files/7166087", None, 7166087, "ftb-stoneblock-4"),
        ("www.curseforge.com/minecraft/modpacks/ftb-stoneblock-4/files/7166087", None, 7166087, "ftb-stoneblock-4"),
        ("https://www.curseforge.com/api/v1/mods/1373378/files/7166087/download", 1373378, 7166087, None),
        ("https://www.curseforge.com/minecraft/modpacks/stoneblock-4?projectId=1373378&fileId=7166087", 1373378, 7166087, "stoneblock-4"),
    ]
    for url, pid, fid, slug in parse_cases:
        try:
            ref = parse_cf_link(url)
            checks.check(
                ref.project_id == pid and ref.file_id == fid and ref.slug == slug,
                f"{url[:64]} → project={ref.project_id} file={ref.file_id} slug={ref.slug}",
            )
        except CFLinkError as e:
            checks.check(False, f"{url} 解析抛错: {e}")

    cdn = parse_cf_link("https://mediafilez.forgecdn.net/files/7166/87/ftb-stoneblock-4-1.0.1.zip")
    checks.eq(cdn.file_id, 7166087, "CDN 直链反推 fileID")
    checks.eq(cdn.file_name, "ftb-stoneblock-4-1.0.1.zip", "CDN 直链取文件名")

    for bad in ("https://example.com/foo/bar", "", "https://www.curseforge.com/", "not a link at all"):
        try:
            parse_cf_link(bad)
            checks.check(False, f"非法链接应报错: {bad!r}")
        except CFLinkError:
            checks.check(True, f"非法链接正确报错: {bad!r}")

    print("\n== 离线:直链重建 ==")
    checks.eq(
        rebuild_cdn_url(7166087, "ftb-stoneblock-4-1.0.1.zip"),
        "https://edge.forgecdn.net/files/7166/87/ftb-stoneblock-4-1.0.1.zip",
        "rebuild_cdn_url 标准样例",
    )
    checks.eq(
        rebuild_cdn_url(1000, "a b+c.zip"),
        "https://edge.forgecdn.net/files/1/0/a%20b%2Bc.zip",
        "rebuild_cdn_url 转义空格与加号",
    )
    checks.eq(len(cdn_url_candidates(7166087, "x.zip")), len(CF_CDN_HOSTS), "两家 CDN 候选")
    checks.check(
        rebuild_cdn_url(1000, "a b+c.zip", host=CF_CDN_HOSTS[1]).startswith("https://mediafilez.forgecdn.net/"),
        "可指定 CDN 主机",
    )
    try:
        rebuild_cdn_url(1, "")
        checks.check(False, "空文件名应报错")
    except ValueError:
        checks.check(True, "空文件名正确报错")

    print("\n== 离线:ZIP 条目走查 ==")
    normal_zip = _make_pack_zip(_MANIFEST_FTBQ, pad_before_manifest=4096, pad_after=700 * 1024)
    checks.check(len(normal_zip) > DEFAULT_PREFIX_BYTES, "测试包大于 512KB(保证截断场景)")

    names = [e.name for e in iter_zip_local_entries(normal_zip)]
    checks.check("manifest.json" in names, "走查能找到 manifest.json")
    checks.check(_SAMPLE_PACK_NAME in names, "走查能找到 lang 条目")

    prefix = normal_zip[:DEFAULT_PREFIX_BYTES]
    manifest = find_file_in_zip_bytes(prefix, "manifest.json")
    checks.check(manifest is not None, "512KB 片段里能解出 manifest.json")
    if manifest:
        parsed = json.loads(manifest)
        checks.eq(parsed.get("name"), "Test Pack", "片段 manifest.json 内容正确")
        checks.check(
            any(f.get("projectID") == 289412 for f in parsed.get("files", [])),
            "manifest 含 FTB Quests(289412)",
        )
    checks.eq(pick_quest_lang_entry(names), _SAMPLE_PACK_NAME, "pick_quest_lang_entry 选中标准路径")

    descriptor_zip, expected = _craft_descriptor_zip()
    desc_names = [e.name for e in iter_zip_local_entries(descriptor_zip)]
    checks.eq(
        desc_names,
        ["manifest.json", _SAMPLE_PACK_NAME],
        "data descriptor 风格 zip 走查不跑偏",
    )
    checks.eq(find_file_in_zip_bytes(descriptor_zip, "manifest.json"), expected, "data descriptor 条目可解压")

    picked_names = [
        "overrides/config/ftbquests/normal/chapters/x.snbt",
        "foo/bar/lang/en_us.snbt",
        _SAMPLE_PACK_NAME,
    ]
    checks.eq(pick_quest_lang_entry(picked_names), _SAMPLE_PACK_NAME, "多候选时优先标准路径")
    checks.eq(pick_quest_lang_entry(["a.snbt"]), None, "无候选返回 None")

    print("\n== 离线:本地 HTTP 端到端 ==")
    import http.server
    import threading

    no_lang_zip = _make_pack_zip(
        _MANIFEST_NO_FTBQ, with_lang=False, with_mod_jar=False, pad_after=700 * 1024
    )
    far_manifest_zip = _make_pack_zip(
        _MANIFEST_FTBQ,
        with_lang=False,
        with_mod_jar=False,
        pad_before_manifest=600 * 1024,
    )
    handler = _make_range_handler()
    handler.routes = {
        "/pack.zip": normal_zip,
        "/norange.zip": normal_zip,
        "/nolang.zip": no_lang_zip,
        "/descriptor.zip": descriptor_zip,
        "/farmanifest.zip": far_manifest_zip,
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        report = detect_ftbq(download_url=f"{base}/redirect", file_name="pack.zip", timeout=30)
        checks.eq(report.has_ftb_quests, True, "302 跳转后 Range 检测到 FTB Quests")
        checks.eq(report.ftb_quests_project_ids, [289412], "命中 project id 289412")
        checks.eq(report.source, "manifest", "结论来自 manifest")
        checks.eq(report.bytes_read, DEFAULT_PREFIX_BYTES, "只读了 512KB")

        report = detect_ftbq(download_url=f"{base}/norange.zip", timeout=30)
        checks.eq(report.has_ftb_quests, True, "服务器忽略 Range 时仍能判定")
        checks.eq(report.bytes_read, DEFAULT_PREFIX_BYTES, "忽略 Range 时也不会拖整包")

        report = detect_ftbq(download_url=f"{base}/nolang.zip", timeout=30)
        checks.eq(report.has_ftb_quests, False, "无 FTB Quests 的包判定为 False")
        checks.eq(report.source, "manifest", "否定结论同样来自 manifest")

        report = detect_ftbq(download_url=f"{base}/descriptor.zip", timeout=30)
        checks.eq(report.has_ftb_quests, True, "data descriptor 风格包也能判定")

        report = detect_ftbq(download_url=f"{base}/farmanifest.zip", timeout=30)
        checks.eq(report.has_ftb_quests, None, "manifest 落在 512KB 之外时返回 None(不误判 False)")
        checks.check("manifest" in report.note, f"说明里点出 manifest: {report.note[:60]}")

        report = detect_ftbq(download_url=f"{base}/missing.zip", timeout=30)
        checks.eq(report.has_ftb_quests, None, "404 时返回 None 而不是抛异常")
        checks.check(bool(report.note), f"404 时给出说明: {report.note[:60]}")

        try:
            result = extract_quests_snbt(
                pack_type="zip", download_url=f"{base}/pack.zip", file_name="pack.zip", timeout=30
            )
            checks.eq(result.content, _SAMPLE_SNBT, "整包下载后抽取 lang 内容一致")
            checks.check(result.entry_name.endswith(_SAMPLE_PACK_NAME), "报告了 zip 内路径")
            checks.eq(result.source, "cf-zip", "标注来源")
        except FetchError as e:
            checks.check(False, f"整包抽取失败: {e}")

        try:
            extract_quests_snbt(pack_type="zip", download_url=f"{base}/nolang.zip", timeout=30)
            checks.check(False, "没有 lang 文件时应抛 QuestsNotFound")
        except QuestsNotFound as e:
            checks.check(e.definitive, f"no-lang 结论标记为确定: {str(e)[:60]}")

        # 读阶段故障(503 / 半包 IncompleteRead)应被 _get_bytes 重试后成功
        try:
            raw = _get_bytes(f"{base}/flaky-once", timeout=30, retries=3, backoff=0.05)
            checks.check(raw == normal_zip, "503 一次后重试成功")
        except FetchError as e:
            checks.check(False, f"503 重试失败: {e}")

        try:
            raw = _get_bytes(f"{base}/truncated-once", timeout=30, retries=3, backoff=0.05)
            checks.check(raw == normal_zip, "半包(IncompleteRead)后重试成功")
        except FetchError as e:
            checks.check(False, f"半包重试失败: {e}")

        report = detect_ftbq(
            project_id=None, file_id=None, download_url=None, on_warning=lambda m: None
        )
        checks.eq(report.has_ftb_quests, None, "无任何参数时返回 None")
    finally:
        server.shutdown()
        server.server_close()


def _selftest_online(checks: _Checks, full: bool) -> None:
    print("\n== 联网:modpacks.ch native 清单(免密钥)==")
    try:
        manifest = modpacks_version(1, 158, curseforge=False, timeout=60)
        files = manifest.get("files") or []
        quests = [n for n in (_modpacks_full_name(f) for f in files) if _looks_ftbquests(n)]
        checks.check(len(quests) > 0, f"FTB Academy 1.4.0 清单里有 ftbquests 文件 ({len(quests)} 个)")
        checks.eq(
            modpacks_find_quest_lang_files(files),
            [],
            "1.12.2 老包没有 lang/en_us.snbt(符合预期)",
        )
        try:
            extract_quests_snbt(pack_id=1, version_id=158, pack_type="native", timeout=120)
            checks.check(False, "1.12.2 硬编码包应抛 QuestsNotFound")
        except QuestsNotFound as e:
            checks.check(e.definitive, f"老格式给出确定结论: {str(e)[:70]}")
    except FetchError as e:
        checks.check(False, f"modpacks.ch native 查询失败: {e}")

    print("\n== 联网:真实 CF 包 Range 检测(StoneBlock 4 / 1373378 / 7166087)==")
    try:
        report = detect_ftbq(1373378, 7166087, timeout=120)
        checks.eq(report.has_ftb_quests, True, "StoneBlock 4 检测到 FTB Quests")
        checks.check(289412 in report.ftb_quests_project_ids, f"命中 id: {report.ftb_quests_project_ids}")
        checks.eq(report.bytes_read, DEFAULT_PREFIX_BYTES, "只读了 512KB")
        checks.check("forgecdn.net" in (report.download_url or ""), f"直链来源: {report.download_url}")
        checks.check(report.mod_count and report.mod_count > 100, f"manifest 里模组数: {report.mod_count}")
        print(
            f"  · 包名 {report.pack_name!r} v{report.pack_version},"
            f" 直链 {report.download_url}"
        )
    except FetchError as e:
        checks.check(False, f"真实包检测失败: {e}")

    print("\n== 联网:native 单文件抽取(FTB Evolution / 125 / 12629)==")
    try:
        result = extract_quests_snbt(pack_id=125, version_id=12629, pack_type="native", timeout=240)
        checks.eq(result.source, "modpacks-native", "走的是清单里单文件直链,没下整包")
        checks.check(result.entry_name.endswith("ftbquests/quests/lang/en_us.snbt"), f"路径: {result.entry_name}")
        checks.check(len(result.content) > 10000, f"拿到 {len(result.content)} 字符")
        checks.check("title:" in result.content, "内容含 title: 字段(能交给 R2 解析)")
        print(f"  · zip 内路径 {result.entry_name}")
    except (FetchError, QuestsNotFound) as e:
        checks.check(False, f"native 单文件抽取失败: {e}")

    print("\n== 联网:manifest 不在前 512KB 的包(降级判定,FTB OceanBlock 2)==")
    try:
        report = detect_ftbq(1198207, 6192537, timeout=180)
        checks.eq(report.manifest_found, False, "OceanBlock 2 的 manifest 确实不在前 512KB")
        checks.eq(report.has_ftb_quests, True, "降级到 modpacks.ch 模组清单后仍能判定为 True")
        checks.eq(report.source, "modpacks-mods", "标注了降级来源")
        checks.check(any("ftb-quests" in n for n in report.matched_zip_names), "点名命中的 ftb-quests jar")
    except FetchError as e:
        checks.check(False, f"降级判定用例失败: {e}")

    print("\n== 联网:真实 CF 包直链重建一致性 ==")
    try:
        manifest = modpacks_version(1373378, 7166087, curseforge=True, timeout=60)
        zip_entry = modpacks_find_override_zip(manifest.get("files") or [])
        checks.check(zip_entry is not None, "modpacks.ch 给出 overrides/整包 zip")
        if zip_entry:
            name = zip_entry["name"]
            url = zip_entry["url"]
            checks.check(f"/{7166087 // 1000}/{7166087 % 1000}/" in url, f"直链目录与重建规则一致: {url}")
            checks.check(
                rebuild_cdn_url(7166087, "ftb-stoneblock-4-1.0.1.zip").endswith("/ftb-stoneblock-4-1.0.1.zip"),
                "重建直链文件名与官方一致",
            )
    except FetchError as e:
        checks.check(False, f"modpacks.ch curseforge 查询失败: {e}")

    if full:
        print("\n== 联网:真实整包下载抽取(约 170MB,仅 --full 时执行)==")
        try:
            # curseforge 模式 = R0 的正式链路:modpacks.ch cf-extract → 下整包 → 抽 lang
            result = extract_quests_snbt(project_id=1373378, file_id=7166087, pack_type="curseforge", timeout=1800)
            checks.check(len(result.content) > 1000, f"拿到 en_us.snbt {len(result.content)} 字符")
            checks.check("title:" in result.content or "chapter" in result.content, "内容像 FTB Quests SNBT")
            print(f"  · zip 内路径 {result.entry_name}")
            print(f"  · 前 120 字符: {result.content[:120]!r}")
        except (FetchError, QuestsNotFound) as e:
            checks.check(False, f"整包抽取失败: {e}")


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="R1 下载检测:CF 链接解析 / FTBQ 检测 / quests 抽取")
    parser.add_argument("--selftest", action="store_true", help="跑自测")
    parser.add_argument("--online", action="store_true", help="自测中加入真实网络用例")
    parser.add_argument("--full", action="store_true", help="在线自测再追加真实整包下载(约 170MB)")
    parser.add_argument("--link", metavar="URL", help="只解析一个 CF 链接")
    parser.add_argument("--detect", nargs=2, metavar=("PROJECT_ID", "FILE_ID"), help="检测某整合包是否含 FTB Quests")
    parser.add_argument("--file-name", help="配合 --detect:zip 文件名(downloadUrl 为空时用于重建直链)")
    parser.add_argument("--extract", nargs=2, metavar=("PROJECT_ID", "FILE_ID"), help="抽取 quests en_us.snbt")
    parser.add_argument("--pack-type", default="zip", help="配合 --extract:native/curseforge/zip/auto")
    parser.add_argument("--pack-id", type=int, help="配合 --extract:FTB native 包 id")
    parser.add_argument("--version-id", type=int, help="配合 --extract:FTB native 版本 id")
    parser.add_argument("--out", help="配合 --extract:输出文件路径")
    args = parser.parse_args(argv)
    try:
        return _run(args, parser)
    except (FetchError, CFLinkError, MissingCurseForgeKey) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.link:
        ref = parse_cf_link(args.link)
        print(json.dumps(ref.to_dict(), ensure_ascii=False, indent=2))
        if ref.needs_slug_lookup:
            if cf_api_key():
                print(f"projectID(API 反查)= {resolve_project_id(ref)}")
            else:
                print("需要 CURSEFORGE_API_KEY 才能把 slug 反查成 projectID")
        return 0

    if args.detect:
        report = detect_ftbq(int(args.detect[0]), int(args.detect[1]), file_name=args.file_name)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.has_ftb_quests is not None else 1

    if args.extract:
        progress = lambda pct, msg: print(f"  {pct:3d}% {msg}", file=sys.stderr)
        result = extract_quests_snbt(
            project_id=int(args.extract[0]),
            file_id=int(args.extract[1]),
            pack_id=args.pack_id,
            version_id=args.version_id,
            pack_type=args.pack_type,
            progress_cb=progress,
        )
        if args.out:
            Path(args.out).write_text(result.content, encoding="utf-8")
            print(f"已写入 {args.out}({len(result.content)} 字符,来自 {result.entry_name})")
        else:
            print(result.content)
        return 0

    if args.selftest or not any([args.link, args.detect, args.extract]):
        checks = _Checks("离线")
        _selftest_offline(checks)
        ok = checks.summary()
        if args.online or args.full:
            online = _Checks("联网")
            _selftest_online(online, full=args.full)
            ok = online.summary() and ok
        print("\n自测结果:", "全部通过" if ok else "有失败项")
        return 0 if ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
