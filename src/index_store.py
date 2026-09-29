"""index.json 读写模块:仓库根目录总索引 + 单包 meta.json。

产物结构约定(相对仓库根,统一正斜杠)::

    packs/{source}/{projectId}/{fileId}/zh_cn.snbt   译后成品 SNBT
    packs/{source}/{projectId}/{fileId}/meta.json    单包元信息(entry 快照)
    index.json                                       总索引(前端直接拉取渲染)

entry 字段::

    source       来源平台,目前 "cf"(CurseForge),M5 再扩展 "modrinth"
    projectId    CF project id,统一按字符串存
    fileId       CF file id,统一按字符串存
    name         整合包显示名,取自 CF 元数据
    mcVersion    目标 MC 版本,如 "1.20.1"
    status       pending | done | failed | no_ftbq | already_localized
    path         产物目录,如 "packs/cf/123/456"(由本模块统一生成,勿手填)
    translatedAt 完成翻译的 UTC 时间(ISO-8601,done 时自动写入,其余可为 null)
    stats        统计信息,自由 dict,如 {"fields": 120, "translated": 118}

index.json 结构::

    {"version": 1, "updatedAt": "...", "entries": [entry, ...]}

本模块只依赖标准库,不发起网络请求;查询/更新都在内存 dict 上做,由调用方
决定何时 save_index()。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "INDEX_VERSION",
    "STATUSES",
    "REPO_ROOT",
    "index_path",
    "artifact_dir",
    "artifact_path",
    "meta_path",
    "entry_key",
    "empty_index",
    "load_index",
    "save_index",
    "iter_entries",
    "find_entry",
    "normalize_entry",
    "build_entry",
    "upsert_entry",
    "update_status",
    "write_meta",
]

INDEX_VERSION = 1
STATUSES = ("pending", "done", "failed", "no_ftbq", "already_localized")
DEFAULT_STATUS = "pending"

PACKS_DIRNAME = "packs"
INDEX_FILENAME = "index.json"
SNBT_FILENAME = "zh_cn.snbt"
META_FILENAME = "meta.json"

REPO_ROOT = Path(__file__).resolve().parent.parent

_SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
# 数字(CF id)与 slug(将来 Modrinth)都要放行,同时挡掉路径分隔符/穿越
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ── 路径与 key ────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm_source(source) -> str:
    text = str(source).strip().lower()
    if not _SOURCE_RE.match(text):
        raise ValueError(f"source 不合法(仅小写字母/数字/_-):{source!r}")
    return text


def _norm_segment(value, field: str) -> str:
    text = str(value).strip()
    if not _SEGMENT_RE.match(text) or text in {".", ".."}:
        raise ValueError(f"{field} 不合法(仅字母/数字/._-,且不能是 . 或 ..):{value!r}")
    return text


def index_path(root=None) -> Path:
    return (Path(root) if root is not None else REPO_ROOT) / INDEX_FILENAME


def _rel_dir(source, project_id, file_id) -> str:
    return f"{PACKS_DIRNAME}/{_norm_source(source)}/{_norm_segment(project_id, 'projectId')}/{_norm_segment(file_id, 'fileId')}"


def artifact_dir(source, project_id, file_id, root=None) -> Path:
    """产物目录 packs/{source}/{projectId}/{fileId}/。"""
    base = Path(root) if root is not None else REPO_ROOT
    return base.joinpath(*_rel_dir(source, project_id, file_id).split("/"))


def artifact_path(source, project_id, file_id, root=None) -> Path:
    """译后 SNBT 成品路径 …/zh_cn.snbt。"""
    return artifact_dir(source, project_id, file_id, root=root) / SNBT_FILENAME


def meta_path(source, project_id, file_id, root=None) -> Path:
    """单包元信息路径 …/meta.json。"""
    return artifact_dir(source, project_id, file_id, root=root) / META_FILENAME


def entry_key(source, project_id, file_id) -> str:
    """查重 key:source/projectId/fileId(各组分别归一化)。"""
    return _rel_dir(source, project_id, file_id)


# ── 索引读写 ──────────────────────────────────────────────────────────────────


def empty_index() -> dict:
    return {"version": INDEX_VERSION, "updatedAt": _now_iso(), "entries": []}


def load_index(path=None) -> dict:
    """读取 index.json;文件不存在返回空索引,存在但损坏则报错(拒绝静默覆盖)。"""
    path = Path(path) if path is not None else index_path()
    if not path.exists():
        return empty_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} 不是合法 JSON,拒绝覆盖,请人工检查:{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"{path} 结构异常(缺 entries 数组),拒绝覆盖")
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("updatedAt", _now_iso())
    for entry in data["entries"]:
        normalize_entry(entry)
    return data


def save_index(index: dict, path=None) -> Path:
    """归一化 + 排序后原子写出(先写 .tmp 再 os.replace)。"""
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise ValueError("index['entries'] 必须是列表")
    for entry in entries:
        normalize_entry(entry)
    index["version"] = INDEX_VERSION
    index["updatedAt"] = _now_iso()
    # 排序保证索引稳定,CI 提交的 diff 干净
    index["entries"] = sorted(entries, key=lambda e: entry_key(e["source"], e["projectId"], e["fileId"]))
    path = Path(path) if path is not None else index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def iter_entries(index: dict):
    yield from index.get("entries", [])


def find_entry(index: dict, source, project_id, file_id) -> dict | None:
    """按 (source, projectId, fileId) 查重,返回 entry 本体(可直接改)。"""
    key = entry_key(source, project_id, file_id)
    for entry in index.get("entries", []):
        if entry_key(entry["source"], entry["projectId"], entry["fileId"]) == key:
            return entry
    return None


# ── entry 构造与更新 ──────────────────────────────────────────────────────────


def normalize_entry(entry: dict) -> dict:
    """校验并归一化一条 entry(就地修改并返回);path 永远按 id 重算。"""
    if not isinstance(entry, dict):
        raise ValueError(f"entry 必须是 dict:{entry!r}")
    missing = [k for k in ("source", "projectId", "fileId", "status") if not entry.get(k)]
    if missing:
        raise ValueError(f"entry 缺字段 {missing}:{entry!r}")
    if entry["status"] not in STATUSES:
        raise ValueError(f"非法 status {entry['status']!r},应为 {STATUSES} 之一")
    entry["source"] = _norm_source(entry["source"])
    entry["projectId"] = _norm_segment(entry["projectId"], "projectId")
    entry["fileId"] = _norm_segment(entry["fileId"], "fileId")
    entry["name"] = str(entry.get("name") or "")
    entry["mcVersion"] = str(entry.get("mcVersion") or "")
    entry["path"] = _rel_dir(entry["source"], entry["projectId"], entry["fileId"])
    entry["translatedAt"] = entry.get("translatedAt") or None
    stats = entry.get("stats")
    entry["stats"] = dict(stats) if isinstance(stats, dict) else {}
    return entry


def build_entry(source, project_id, file_id, *, name="", mc_version="", status=DEFAULT_STATUS,
                stats=None, translated_at=None) -> dict:
    """构造一条新 entry(不落盘)。"""
    if status not in STATUSES:
        raise ValueError(f"非法 status {status!r},应为 {STATUSES} 之一")
    return normalize_entry({
        "source": source,
        "projectId": project_id,
        "fileId": file_id,
        "name": name,
        "mcVersion": mc_version,
        "status": status,
        "path": "",
        "translatedAt": translated_at,
        "stats": stats or {},
    })


def _apply_status(entry: dict, status: str, translated_at=None) -> None:
    if status not in STATUSES:
        raise ValueError(f"非法 status {status!r},应为 {STATUSES} 之一")
    entry["status"] = status
    if translated_at is not None:
        entry["translatedAt"] = translated_at
    elif status == "done" and not entry.get("translatedAt"):
        entry["translatedAt"] = _now_iso()


def upsert_entry(index: dict, source, project_id, file_id, *, name=None, mc_version=None,
                 status=None, stats=None, translated_at=None) -> tuple[dict, bool]:
    """新增或更新一条 entry,返回 (entry, 是否新建)。

    语义:查重命中已有 entry 则就地更新;参数为 None 表示保留原值;
    stats 做浅合并(方便增量追加统计);status="done" 时自动补 translatedAt。
    """
    existing = find_entry(index, source, project_id, file_id)
    if existing is None:
        entry = build_entry(source, project_id, file_id,
                            name=name or "", mc_version=mc_version or "",
                            status=status or DEFAULT_STATUS, stats=stats,
                            translated_at=translated_at)
        index.setdefault("entries", []).append(entry)
        return entry, True

    if name is not None:
        existing["name"] = str(name)
    if mc_version is not None:
        existing["mcVersion"] = str(mc_version)
    if stats:
        existing["stats"] = {**(existing.get("stats") or {}), **stats}
    if status is not None:
        _apply_status(existing, status, translated_at)
    elif translated_at is not None:
        existing["translatedAt"] = translated_at
    return existing, False


def update_status(index: dict, source, project_id, file_id, status, *, name=None, mc_version=None,
                  stats=None, translated_at=None) -> dict:
    """更新状态(entry 不存在则顺手补建),返回 entry。"""
    entry, _ = upsert_entry(index, source, project_id, file_id, name=name, mc_version=mc_version,
                            status=status, stats=stats, translated_at=translated_at)
    return entry


def write_meta(entry: dict, root=None) -> Path:
    """把 entry 快照写入 packs/{source}/{pid}/{fid}/meta.json(目录自动创建)。"""
    normalize_entry(entry)
    path = meta_path(entry["source"], entry["projectId"], entry["fileId"], root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ── 自测 / 命令行 ─────────────────────────────────────────────────────────────


def _self_test() -> int:
    checks = 0

    def check(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(msg)

    with tempfile.TemporaryDirectory(prefix="index_store_selftest_") as tmp:
        root = Path(tmp)
        idx_file = root / INDEX_FILENAME

        # 1) 空仓库:加载返回空索引
        idx = load_index(idx_file)
        check(idx["entries"] == [], "空索引应无 entries")
        check(idx["version"] == INDEX_VERSION, "version 应为 INDEX_VERSION")

        # 2) upsert 新建 pending,path 按约定生成
        entry, created = upsert_entry(idx, "cf", 123456, "7890", name="TestPack", mc_version="1.20.1")
        check(created and entry["status"] == DEFAULT_STATUS, "首次 upsert 应为 pending 新建")
        check(entry["path"] == "packs/cf/123456/7890", f"path 约定不符:{entry['path']}")
        check(entry["translatedAt"] is None, "pending 不应有 translatedAt")

        # 3) 同 (source, projectId, fileId) 再 upsert:查重命中,不新增,字段被更新
        entry2, created2 = upsert_entry(idx, "cf", "123456", "7890", name="TestPack v2",
                                        status="done", stats={"fields": 120, "translated": 118})
        check(not created2 and len(idx["entries"]) == 1, "同 key 应命中已有 entry 而非新增")
        check(entry2 is entry, "upsert 应返回原 entry 本体")
        check(entry2["name"] == "TestPack v2" and entry2["status"] == "done", "字段应被更新")
        check(bool(entry2["translatedAt"]), "done 应自动补 translatedAt")

        # 4) find_entry
        check(find_entry(idx, "cf", "123456", "7890") is entry, "find_entry 应命中")
        check(find_entry(idx, "cf", "123456", "9999") is None, "不存在的 fileId 应为 None")

        # 5) source 不同视为不同条目
        _, created3 = upsert_entry(idx, "modrinth", "123456", "7890")
        check(created3 and len(idx["entries"]) == 2, "source 不同应视为新条目")

        # 6) update_status:stats 浅合并,历史 translatedAt 不清空
        update_status(idx, "cf", "123456", "7890", "failed", stats={"error": "429"})
        check(entry["status"] == "failed", "状态应更新为 failed")
        check(entry["stats"] == {"fields": 120, "translated": 118, "error": "429"}, "stats 应浅合并")
        check(bool(entry["translatedAt"]), "failed 不应清掉历史 translatedAt")

        # 7) 非法 status 报错
        bad = False
        try:
            update_status(idx, "cf", "123456", "7890", "bogus")
        except ValueError:
            bad = True
        check(bad, "非法 status 应抛 ValueError")

        # 8) 落盘 → 重载一致,且 entries 按 key 排序
        save_index(idx, idx_file)
        check(idx_file.exists(), "save_index 应写出文件")
        check(not idx_file.with_name(idx_file.name + ".tmp").exists(), "临时文件应被 os.replace 清掉")
        reloaded = load_index(idx_file)
        check(len(reloaded["entries"]) == 2, "重载应保留 2 条")
        keys = [entry_key(e["source"], e["projectId"], e["fileId"]) for e in reloaded["entries"]]
        check(keys == sorted(keys), "entries 应按 key 排序")
        check(bool(reloaded["updatedAt"]), "updatedAt 应写入")

        # 9) 产物路径与 meta.json
        check(artifact_dir("cf", "123456", "7890", root=root) == root / PACKS_DIRNAME / "cf" / "123456" / "7890",
              "artifact_dir 约定不符")
        check(artifact_path("cf", "123456", "7890", root=root).name == SNBT_FILENAME,
              f"产物文件名应为 {SNBT_FILENAME}")
        meta = write_meta(entry, root=root)
        check(meta == artifact_dir("cf", "123456", "7890", root=root) / META_FILENAME and meta.exists(),
              "meta.json 应写入包目录")
        check(json.loads(meta.read_text(encoding="utf-8"))["fileId"] == "7890", "meta 内容应为 entry 快照")

        # 10) 非法 id 拒绝(防目录穿越)
        for bad_pid in ("..", "a/b", "", "a\\b"):
            bad = False
            try:
                artifact_dir("cf", bad_pid, "7890", root=root)
            except ValueError:
                bad = True
            check(bad, f"非法 projectId {bad_pid!r} 应被拒绝")

        # 11) 损坏的 index.json 拒绝静默覆盖
        idx_file.write_text("{ not json", encoding="utf-8")
        bad = False
        try:
            load_index(idx_file)
        except ValueError:
            bad = True
        check(bad, "损坏 JSON 应报错而非返回空索引")

    print(f"[index_store] 自测通过({checks} 项检查)")
    return 0


def _cli(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.index_store",
                                     description="index.json 读写;无子命令时跑自测")
    parser.add_argument("--index", default=None, help=f"索引文件路径(默认 {INDEX_FILENAME}@仓库根)")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("selftest", help="运行内置自测")
    sub.add_parser("list", help="列出全部条目")

    def add_common(p):
        p.add_argument("--source", default="cf")
        p.add_argument("--project-id", required=True)
        p.add_argument("--file-id", required=True)
        p.add_argument("--name", default=None)
        p.add_argument("--mc-version", default=None)
        p.add_argument("--stats", default=None, help="JSON 字符串,如 '{\"fields\": 120}'")

    p_up = sub.add_parser("upsert", help="新增或更新一条 entry")
    add_common(p_up)
    p_up.add_argument("--status", default=None, choices=STATUSES)

    p_set = sub.add_parser("set-status", help="更新状态(entry 不存在则补建)")
    add_common(p_set)
    p_set.add_argument("--status", required=True, choices=STATUSES)

    args = parser.parse_args(argv)
    if args.cmd in (None, "selftest"):
        return _self_test()

    idx_file = Path(args.index) if args.index else index_path()
    index = load_index(idx_file)

    if args.cmd == "list":
        for e in sorted(index["entries"], key=lambda x: entry_key(x["source"], x["projectId"], x["fileId"])):
            print(f"{e['status']:<18} {e['source']}/{e['projectId']}/{e['fileId']}  {e['name']}  {e['path']}")
        print(f"共 {len(index['entries'])} 条")
        return 0

    # upsert 与 set-status 只差 --status 是否必填,共用同一实现
    entry, created = upsert_entry(index, args.source, args.project_id, args.file_id,
                                  name=args.name, mc_version=args.mc_version, status=args.status,
                                  stats=json.loads(args.stats) if args.stats else None)
    save_index(index, idx_file)
    print(f"{'新建' if created else '更新'} {entry['path']} status={entry['status']}")
    write_meta(entry, root=idx_file.parent)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
