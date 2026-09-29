#!/usr/bin/env python
"""M1/M4 集成入口:CF 链接 → quests 源文件(lang 单文件 / 硬编码多文件)→ zh_cn 产物 → index.json。

把三个模块串起来(fetch → translate → index_store)::

    main.py <CF链接>:
      1. fetch.parse_cf_link / resolve_cf_link  解析链接 → (source, projectId, fileId)
      2. index_store.find_entry                 已 done → 打印「该整合包已提供汉化」并退出
      3. fetch.extract_quest_sources            抽取 quests 源:mode + {相对路径: SNBT 文本}
         · mode=lang      单文件(lang/en_us.snbt)
         · mode=hardcoded 多文件(chapters/*.snbt、data.snbt、reward_tables/*.snbt …)
         · mode=none      记 no_ftbq 退出
      4. translate.translate_sources            所有文件合并分批翻译(mask → 翻译 → 校验回退)
      5. 产出到 packs/{source}/{projectId}/{fileId}/ 下保持相对路径
         (lang → lang/zh_cn.snbt;hardcoded → chapters/*.snbt 原路径,玩家整个 quests 目录覆盖回去)
      6. index_store.update_status/save_index   记 done/failed + stats(汇总所有文件);done 时补 meta.json

用法::

    python main.py https://www.curseforge.com/minecraft/modpacks/<slug>/files/7166087
    python main.py --pack-id 125 --version-id 12629          # modpacks.ch 原生包(免 CF key)

常用选项::

    --force            已 done 的包也重新跑一遍
    --allow-mock       没有 GEMINI_API_KEY 时放行 mock 模式(产出=原文副本,仅联调用)
    --target zh_cn     目标语言(默认 zh_cn)
    --timeout 600      单次网络操作超时秒数

退出码:0=成功(done 或已 done);2=无 FTB Quests/无可翻内容(no_ftbq);1=失败(failed);
        3=环境/参数未就绪(未配置 key 且未加 --allow-mock、链接无法解析、
          fetch.extract_quest_sources 未落地等,未写任何产物)。
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import fetch
import index_store
import translate

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_FTBQ = 2
EXIT_NOT_READY = 3


def _log(msg: str) -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_NOT_READY, f"{self.prog}: 参数错误:{message}\n")


_progress_state = {"pct": -99}


def _progress(pct: int, msg: str) -> None:
    """下载进度节流:新阶段(pct 回落)重置,同一阶段每 5% 打一行。"""
    if pct < _progress_state["pct"]:
        _progress_state["pct"] = -99
    if pct != 100 and pct - _progress_state["pct"] < 5:
        return
    _progress_state["pct"] = pct
    _log(f"[{pct:3d}%] {msg}")


def _warn(msg: str) -> None:
    _err(f"[warn] {msg}")


def _parse_args(argv=None) -> argparse.Namespace:
    parser = _Parser(prog="main.py", description="抽取 FTB Quests 源文件 → 翻译 → 更新 index.json")
    parser.add_argument("url", nargs="?", help="CurseForge 整合包链接(与 --pack-id/--version-id 二选一)")
    parser.add_argument("--pack-id", type=int, default=None, help="modpacks.ch 原生包 ID")
    parser.add_argument("--version-id", type=int, default=None, help="modpacks.ch 原生包版本 ID")
    parser.add_argument("--target", default="zh_cn", help="目标语言(默认 zh_cn)")
    parser.add_argument("--timeout", type=float, default=600.0, help="单次网络操作超时秒数(默认 600)")
    parser.add_argument("--allow-mock", action="store_true",
                        help="没有 GEMINI_API_KEY 时放行 mock 模式(产出是原文副本,仅联调用)")
    parser.add_argument("--force", action="store_true", help="包已 done 也重新翻译")
    args = parser.parse_args(argv)

    if args.pack_id or args.version_id:
        if not (args.pack_id and args.version_id):
            parser.error("--pack-id 与 --version-id 必须同时给出")
        if args.url:
            parser.error("链接与 --pack-id/--version-id 只能二选一")
    elif not args.url:
        parser.error("需要一个 CurseForge 链接,或 --pack-id + --version-id")
    return args


def _record(index: dict, source: str, project_id, file_id, status: str, *,
            name=None, stats=None) -> dict:
    """更新状态并落盘 index.json;done 时额外写 meta.json 快照。

    先 upsert 一条占位再 update_status:index_store.build_entry 建新条目时
    不会自动补 translatedAt,只有 update 路径的 _apply_status 会补。
    """
    index_store.upsert_entry(index, source, project_id, file_id)
    entry = index_store.update_status(index, source, project_id, file_id, status,
                                      name=name, stats=stats)
    index_store.save_index(index)
    if status == "done":
        index_store.write_meta(entry)
    return entry


def _pack_name(source: str, project_id, file_id) -> str:
    """索引里的包名,取不到就算了:cf 走 curseforge 索引(文件名式名称,如 "The CUBE-2.0.3"),
    modpacks 走原生索引(清单里给的只是版本名,得问包里)。"""
    try:
        if source == "cf":
            manifest = fetch.modpacks_version(project_id, file_id, curseforge=True, timeout=30)
        else:
            manifest = fetch.modpacks_pack(project_id, timeout=30)
        return str(manifest.get("name") or "")
    except fetch.FetchError:
        return ""


def _resolve_cf_target(url: str, timeout: float) -> tuple[tuple[int, int] | None, int]:
    """解析 CF 链接。返回 ((project_id, file_id), 退出码);解析失败时前半段为 None。"""
    try:
        ref = fetch.resolve_cf_link(url, timeout=min(timeout, 30))
    except fetch.MissingCurseForgeKey:
        _err("链接是 slug 形式,反查 projectID 需要 CURSEFORGE_API_KEY(当前未配置)。")
        _err("  改用带数字 ID 的链接,例如 …/minecraft/modpacks/<slug>/files/7166087,")
        _err("  或从 curseforge.com 页面 URL 里取 /files/<fileID> 形式的链接。")
        return None, EXIT_NOT_READY
    except (fetch.CFLinkError, ValueError) as e:
        _err(f"链接无法解析:{e}")
        return None, EXIT_NOT_READY

    if ref.project_id is None:
        _err(f"链接里没有 projectID(也没有可反查的 slug):{ref.url}")
        return None, EXIT_NOT_READY
    if ref.file_id is None:
        _err(f"链接里没有 fileID,无法确定要处理哪个版本:{ref.url}")
        _err("  请用 …/files/<fileID> 形式的链接(或带 ?fileId= 查询参数)。")
        return None, EXIT_NOT_READY
    return (int(ref.project_id), int(ref.file_id)), EXIT_OK


def _sget(sources: object, key: str, default=None):
    """取抽取结果里的字段:约定是 dict,但兼容带同名属性的数据类。"""
    if isinstance(sources, dict):
        return sources.get(key, default)
    return getattr(sources, key, default)


_QUESTS_MARKER = "ftbquests/quests/"
_QUESTS_PREFIXES = ("config/ftbquests/quests/", "quests/")

PATCH_ZIP_NAME = "patch.zip"
# 玩家把 patch.zip 解压到整合包根目录即可覆盖:zip 内路径 = config/ftbquests/quests/<产物相对路径>
_INPACK_PREFIX = "config/ftbquests/quests/"
_ZIP_MTIME = (1980, 1, 1, 0, 0, 0)  # 固定时间戳,内容不变则 zip 字节不变(避免仓库无谓 diff)


def write_patch_zip(out_dir: Path, rel_to_text: "dict[str, str]") -> str:
    """把产物打成 patch.zip(确定性:固定 mtime + DEFLATED),玩家解压到包根即覆盖。

    rel_to_text: {quests 相对产物路径: 文本}。返回 zip 文件名。
    """
    zip_path = out_dir / PATCH_ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for out_rel in sorted(rel_to_text):
            info = zipfile.ZipInfo(f"{_INPACK_PREFIX}{out_rel}", date_time=_ZIP_MTIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, rel_to_text[out_rel].encode("utf-8"))
    return PATCH_ZIP_NAME



def _quest_relpath(key: object) -> str:
    """把抽取层给的 key 归一化成 quests 目录内的相对路径。

    兼容三种给法:quests 相对(chapters/x.snbt)、包根相对(config/ftbquests/quests/…)、
    zip 根相对(overrides/config/ftbquests/quests/…)。
    """
    rel = str(key).replace("\\", "/").lstrip("/")
    idx = rel.find(_QUESTS_MARKER)
    if idx >= 0:
        return rel[idx + len(_QUESTS_MARKER):]
    for prefix in _QUESTS_PREFIXES:
        if rel.startswith(prefix):
            return rel[len(prefix):]
    return rel


def _output_relpath(key: object, target: str) -> PurePosixPath:
    """产出相对路径:保持相对路径,lang 的 en_us.snbt 改名为 <target>.snbt;越界即报错。"""
    rel = _quest_relpath(key)
    out = PurePosixPath(rel)
    if not rel or out.is_absolute() or ".." in out.parts or ":" in out.parts[0]:
        raise ValueError(f"无法安全落盘的相对路径:{key!r} → {rel!r}")
    if out.name == "en_us.snbt":
        out = out.with_name(f"{target}.snbt")
    return out


def _extract(source: str, project_id, file_id, args) -> tuple[object | None, str, int]:
    """调 fetch.extract_quest_sources 拿 quests 源。返回 (sources, name, exit_code);
    sources 为 None 时 exit_code 即最终退出码(no_ftbq/failed/not_ready)。"""
    index = index_store.load_index()

    existing = index_store.find_entry(index, source, project_id, file_id)
    if existing and existing.get("status") == "done" and not args.force:
        _log(f"该整合包已提供汉化:{existing.get('name') or '(未命名)'}({existing['path']})")
        return None, "", EXIT_OK

    extract = getattr(fetch, "extract_quest_sources", None)
    if extract is None:
        _err("fetch.extract_quest_sources 还没实现(M4/R1 待落地),无法抽取 quests 源文件。")
        return None, "", EXIT_NOT_READY

    name = ""
    try:
        if source == "modpacks":
            sources = extract(
                f"native:{int(project_id)}/{int(file_id)}",
                timeout=args.timeout, progress_cb=_progress, on_warning=_warn,
            )
        else:
            sources = extract(
                args.url, projectId=int(project_id), fileId=int(file_id),
                timeout=args.timeout, progress_cb=_progress, on_warning=_warn,
            )
        name = _pack_name(source, project_id, file_id)
    except fetch.QuestsNotFound as e:
        status = "no_ftbq" if e.definitive else "failed"
        _record(index, source, project_id, file_id, status, name=name, stats={"error": str(e)[:400]})
        _err(f"抽取终止({status}):{e}")
        return None, name, EXIT_NO_FTBQ if e.definitive else EXIT_FAILED
    except fetch.FetchError as e:
        _record(index, source, project_id, file_id, "failed", name=name, stats={"error": str(e)[:400]})
        _err(f"下载/解析失败:{e}")
        return None, name, EXIT_FAILED

    mode = str(_sget(sources, "mode") or "none")
    files = _sget(sources, "files") or {}
    if mode == "none" or not files:
        _record(index, source, project_id, file_id, "no_ftbq", name=name,
                stats={"mode": mode,
                       "reason": str(_sget(sources, "note") or _sget(sources, "source")
                                     or "没有 lang/en_us.snbt,quests/ 下也没有硬编码 SNBT")[:400]})
        _log(f"该整合包没有可翻译的 FTB Quests 内容(mode={mode})→ 已记 no_ftbq")
        return None, name, EXIT_NO_FTBQ

    _log(f"抽取到 {len(files)} 个 quests 源文件(mode={mode})")
    return sources, name, EXIT_OK


def _translate(sources: object, source: str, project_id, file_id, name: str, args) -> int:
    index = index_store.load_index()
    mode = str(_sget(sources, "mode") or "")
    files = {str(k): str(v) for k, v in dict(_sget(sources, "files") or {}).items()}

    # 先校验所有落盘路径,坏 key 要在花掉翻译额度之前就失败
    try:
        plan = [(relpath, str(_output_relpath(relpath, args.target))) for relpath in sorted(files)]
    except ValueError as e:
        _record(index, source, project_id, file_id, "failed", name=name, stats={"error": str(e)[:400]})
        _err(f"抽取结果里有无法安全落盘的路径:{e}")
        return EXIT_FAILED

    warnings: list[str] = []
    try:
        outputs, report, per_file = translate.translate_sources(
            files, target=args.target, on_warning=warnings.append
        )
    except translate.TranslateConfigError as e:
        _record(index, source, project_id, file_id, "failed", name=name, stats={"error": str(e)[:400]})
        _err(f"翻译配置错误:{e}")
        return EXIT_FAILED

    out_dir = index_store.artifact_dir(source, project_id, file_id)
    written: list[str] = []
    rel_to_text: dict[str, str] = {}
    for relpath, out_rel in plan:
        dst = out_dir / out_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(outputs[relpath], encoding="utf-8", newline="\n")
        written.append(out_rel)
        rel_to_text[out_rel] = outputs[relpath]

    archive = write_patch_zip(out_dir, rel_to_text)

    stats = report.as_stats()
    stats.update({
        "mode": mode,
        "fileCount": len(written),
        "fileList": written,
        "archive": archive,
        "perFile": {out_rel: per_file[relpath] for relpath, out_rel in plan},
    })
    src_tag = _sget(sources, "source")
    if src_tag:
        stats["extractSource"] = str(src_tag)
    if warnings:
        stats["warnings"] = warnings[:20]

    entry = _record(index, source, project_id, file_id, "done", name=name, stats=stats)
    _log(f"产出 {len(written)} 个文件 → {entry['path']}/")
    for out_rel in written:
        _log(f"  - {out_rel}")
    _log(f"  · {archive}(玩家解压到整合包根目录即覆盖)")
    _log(report.summary())
    return EXIT_OK


def main(argv=None) -> int:
    args = _parse_args(argv)
    start = time.monotonic()

    if translate.is_mock() and not args.allow_mock:
        _err("未配置 GEMINI_API_KEY:当前会退化成 mock,产出是原文副本,不能当正式汉化。")
        _err("  联调请加 --allow-mock;正式翻译请先设置 GEMINI_API_KEY 再运行。")
        return EXIT_NOT_READY
    if translate.is_mock():
        _log("[mock] 未配置 GEMINI_API_KEY,翻译阶段直接返回原文(仅联调)")
    else:
        _log(f"翻译模型:{translate.model_name()} → {args.target}")
    if args.target != "zh_cn":
        _warn(f"--target {args.target}:lang 模式产物会命名成 {args.target}.snbt,与站点默认的 zh_cn 约定不一致")

    if args.pack_id and args.version_id:
        source, project_id, file_id = "modpacks", int(args.pack_id), int(args.version_id)
        _log(f"目标:modpacks.ch 原生包 {project_id} v{file_id}")
    else:
        source = "cf"
        target, code = _resolve_cf_target(args.url, args.timeout)
        if target is None:
            return code
        project_id, file_id = target
        _log(f"目标:CurseForge {project_id} / {file_id}")

    sources, name, code = _extract(source, project_id, file_id, args)
    if sources is None:
        return code

    code = _translate(sources, source, project_id, file_id, name, args)
    if code == EXIT_OK:
        _log(f"完成,用时 {time.monotonic() - start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
