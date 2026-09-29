#!/usr/bin/env python
"""M1 集成入口:CF 链接 → en_us.snbt → zh_cn.snbt → index.json。

把三个模块串起来(fetch → index_store → translate)::

    main.py <CF链接>:
      1. fetch.parse_cf_link / resolve_cf_link  解析链接 → (source, projectId, fileId)
      2. index_store.find_entry                 已 done → 打印「该整合包已提供汉化」并退出
      3. fetch.detect_ftbq                      无 FTB Quests → 记 no_ftbq 退出
      4. fetch.extract_quests_snbt              抽取 en_us.snbt(落临时文件)
      5. translate.translate_snbt_file          mask → 批量翻译 → unmask → 校验回退
      6. index_store.update_status/save_index   记 done/failed + stats;done 时补 meta.json

用法::

    python main.py https://www.curseforge.com/minecraft/modpacks/<slug>/files/7166087
    python main.py --pack-id 125 --version-id 12629          # modpacks.ch 原生包(免 CF key)

常用选项::

    --force            已 done 的包也重新跑一遍
    --allow-mock       没有 GEMINI_API_KEY 时放行 mock 模式(产出=原文副本,仅联调用)
    --target zh_cn     目标语言(默认 zh_cn)
    --timeout 600      单次网络操作超时秒数

退出码:0=成功(done 或已 done);2=无 FTB Quests(no_ftbq);1=失败(failed);
        3=环境/参数未就绪(未配置 key 且未加 --allow-mock、链接无法解析等,未写任何产物)。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

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
    parser = _Parser(prog="main.py", description="抽取 FTB Quests 语言文件 → 翻译 → 更新 index.json")
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


def _native_pack_name(pack_id: int) -> str:
    """native 路径下 QuestsSNBT.pack_name 是版本名(如 "1.3.0"),换成真正的包名。"""
    try:
        return str(fetch.modpacks_pack(pack_id, timeout=30).get("name") or "")
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


def _extract(source: str, project_id, file_id, args) -> tuple[fetch.QuestsSNBT | None, str, int]:
    """按 检测 → 抽取 的流程拿 en_us.snbt。返回 (quests, name, exit_code);
    quests 为 None 时 exit_code 即最终退出码(no_ftbq/failed)。"""
    index = index_store.load_index()

    existing = index_store.find_entry(index, source, project_id, file_id)
    if existing and existing.get("status") == "done" and not args.force:
        _log(f"该整合包已提供汉化:{existing.get('name') or '(未命名)'}({existing['path']})")
        return None, "", EXIT_OK

    name = ""
    try:
        if source == "modpacks":
            # 原生包没有整包 zip 可做 Range 预检,直接抽;没有会抛 definitive QuestsNotFound
            quests = fetch.extract_quests_snbt(
                pack_id=project_id, version_id=file_id, pack_type="native",
                timeout=args.timeout, progress_cb=_progress, on_warning=_warn,
            )
            name = _native_pack_name(project_id) or quests.pack_name or ""
        else:
            report = fetch.detect_ftbq(project_id, file_id, timeout=args.timeout, on_warning=_warn)
            name = report.pack_name or ""
            _log(f"FTB Quests 预检:{report.has_ftb_quests}(source={report.source}, 包名={report.pack_name or '?'})")
            if report.has_ftb_quests is False:
                _record(index, source, project_id, file_id, "no_ftbq", name=name,
                        stats={"reason": report.note or report.source})
                _log(f"该整合包不依赖 FTB Quests(source={report.source})→ 已记 no_ftbq")
                return None, name, EXIT_NO_FTBQ
            if report.has_ftb_quests is None:
                _warn(f"FTB Quests 预检无结论({report.note or '前 512KB 里没有 manifest'}),继续尝试抽取")
            quests = fetch.extract_quests_snbt(
                project_id=project_id, file_id=file_id, pack_type="auto",
                timeout=args.timeout, progress_cb=_progress, on_warning=_warn,
            )
    except fetch.QuestsNotFound as e:
        status = "no_ftbq" if e.definitive else "failed"
        _record(index, source, project_id, file_id, status, name=name, stats={"error": str(e)[:400]})
        _err(f"抽取终止({status}):{e}")
        return None, name, EXIT_NO_FTBQ if e.definitive else EXIT_FAILED
    except fetch.FetchError as e:
        _record(index, source, project_id, file_id, "failed", name=name, stats={"error": str(e)[:400]})
        _err(f"下载/解析失败:{e}")
        return None, name, EXIT_FAILED

    return quests, name or quests.pack_name or "", EXIT_OK


def _translate(quests: fetch.QuestsSNBT, source: str, project_id, file_id, name: str, args) -> int:
    index = index_store.load_index()
    out_path = index_store.artifact_path(source, project_id, file_id)
    try:
        with tempfile.TemporaryDirectory(prefix="mcweb_m1_") as tmp:
            in_path = Path(tmp) / "en_us.snbt"
            in_path.write_text(quests.content, encoding="utf-8", newline="\n")
            report = translate.translate_snbt_file(in_path, out_path, target=args.target)
    except translate.TranslateConfigError as e:
        _record(index, source, project_id, file_id, "failed", name=name, stats={"error": str(e)[:400]})
        _err(f"翻译配置错误:{e}")
        return EXIT_FAILED

    stats = report.as_stats()
    stats["extractSource"] = quests.source
    entry = _record(index, source, project_id, file_id, "done", name=name, stats=stats)
    _log(f"产出:{entry['path']}/zh_cn.snbt(条目名 {quests.entry_name},抽取方式 {quests.source})")
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
        _warn(f"--target {args.target}:产物文件名仍由 index_store 约定为 zh_cn.snbt")

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

    quests, name, code = _extract(source, project_id, file_id, args)
    if quests is None:
        return code

    _log(f"抽取成功:{quests.entry_name}({len(quests.content)} 字符)")
    code = _translate(quests, source, project_id, file_id, name, args)
    if code == EXIT_OK:
        _log(f"完成,用时 {time.monotonic() - start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
