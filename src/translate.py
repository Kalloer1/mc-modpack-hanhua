"""R3 翻译:把 snbt.py 打码后的文本分批交给 Gemini,再走 snbt.py 的还原 + 校验。

职责边界(避免和 R2 重复实现):
- 本模块只负责"送出去翻译"这一段:拼提示词、分批、限速、退避重试、解析回包。
- 占位符保护、跳过判定、译后校验、SNBT 读写全部复用 snbt.py。

无 Key 行为:没有 GEMINI_API_KEY 时自动退化成 mock(原样返回并打日志),
所以本地和 CI 不配密钥也能跑通整条管线。

用法::

    from translate import translate_snbt_file, translate_sources
    report = translate_snbt_file("en_us.snbt", "zh_cn.snbt")      # 单文件(lang 模式)
    outputs, report, per_file = translate_sources(                 # 多文件(硬编码格式)
        {"chapters/a.snbt": "...", "chapters/b.snbt": "..."}
    )
    print(report.summary())
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence

import ftb_snbt_lib as slib

try:
    import snbt
except ImportError:  # 方便从别处 import 本模块时也能找到同目录的 snbt.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import snbt

from snbt import TranslationReport

__all__ = [
    "BATCH_SIZE",
    "INTER_BATCH_DELAY",
    "MAX_RETRIES",
    "DEFAULT_MODEL",
    "TARGET_LANGUAGES",
    "api_key",
    "is_mock",
    "model_name",
    "build_prompt",
    "parse_response",
    "chunked",
    "TranslateConfigError",
    "is_retryable",
    "is_fatal",
    "retry_delay",
    "translate_batch",
    "translate_lines",
    "translate_snbt_file",
    "translate_sources",
]

log = logging.getLogger(__name__)

# ── 可调参数 ──────────────────────────────────────────────────────────────────
API_KEY_ENV = "GEMINI_API_KEY"
MODEL_ENV = "GEMINI_MODEL"

BATCH_SIZE = 50
INTER_BATCH_DELAY = 4.5  # 免费档 15 RPM → 每批至少隔 4s,留点余量
MAX_RETRIES = 6
RETRY_BASE_DELAY = 8.0  # 退避 8s / 16s / 32s ...,命中 retryDelay 时以服务端为准
DEFAULT_MODEL = "gemini-2.0-flash"

TARGET_LANGUAGES = {
    "zh_cn": "Simplified Chinese (简体中文, zh_CN)",
    "zh_tw": "Traditional Chinese (繁體中文, zh_TW)",
    "ja_jp": "Japanese (日本語)",
    "ko_kr": "Korean (한국어)",
    "ru_ru": "Russian (Русский)",
    "es_es": "Spanish (Español)",
    "fr_fr": "French (Français)",
    "de_de": "German (Deutsch)",
    "pt_br": "Portuguese (Português)",
    "it_it": "Italian (Italiano)",
    "pl_pl": "Polish (Polski)",
}

# ── 提示词 ────────────────────────────────────────────────────────────────────
PROMPT_TEMPLATE = """You are an EXPERT translator for FTB Quests Minecraft quest book text.

Translate every input line into {language}.

ABSOLUTE RULES - ONE VIOLATION BREAKS THE QUEST BOOK:

1. **[#n#] TOKENS ARE SACRED.** Each one stands for a color code, a variable, an item ID,
   an image path or a link. They must survive untouched.
   - Keep every token EXACTLY as written: same spelling, same number, same order.
   - Never translate, split, renumber, merge, add, duplicate or drop a token.
   - Do NOT put spaces inside a token. `[#12#]` is correct, `[ # 12 # ]` is NOT.
   - Do NOT move a token to a different position relative to the text around it.
2. Translate ALL natural-language text, including words glued to a token:
   `[#3#]Ancient[#4#] Ruins` -> `[#3#]远古[#4#] 遗迹`
3. Never return an empty line for a non-empty input. If a line is already in the target
   language, echo it back unchanged. If a line is pure punctuation/tokens, echo it unchanged.
4. Use natural punctuation for the target language, but do not invent or remove markup.

RESPONSE FORMAT:
Return EXACTLY one line per input line, in this form, with no extra text,
no explanations, no markdown code fences:

LINE_<index>|||<translated text>

EXAMPLE INPUT (the example uses indices 9000+, never the ones you must answer):
LINE_9000|||[#2#]Kill a [#5#] with [#7#] damage.
LINE_9001|||Craft a [#1#] and place it.

EXAMPLE OUTPUT:
LINE_9000|||[#2#]击杀一只[#5#],造成[#7#]点伤害。
LINE_9001|||合成一个[#1#]并放置它。

TEXT TO TRANSLATE:
{body}

Return ONLY the LINE_<index>|||... lines, one per input line."""


def build_prompt(batch: Sequence[str], target: str) -> str:
    """拼一批的提示词。batch 里的文本应当已经由 snbt.mask_placeholders 打过码。"""
    language = TARGET_LANGUAGES.get(target, target)
    body = "\n".join(f"LINE_{i}|||{line}" for i, line in enumerate(batch))
    return PROMPT_TEMPLATE.format(language=language, body=body)


# ── 回包解析 ──────────────────────────────────────────────────────────────────
LINE_RE = re.compile(r"^LINE_(\d+)\|\|\|(.*)$")


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def parse_response(raw: str, originals: Sequence[str]) -> list[str]:
    """把模型的 LINE_x||| 回包解析成与 originals 等长的列表。

    模型漏掉或写坏的行保留原文——宁可少翻一行,也不能把文件写坏。
    """
    result = list(originals)
    for line in _strip_code_fence(raw).splitlines():
        match = LINE_RE.match(line.strip())
        if not match:
            continue
        index = int(match.group(1))
        if 0 <= index < len(result):
            result[index] = match.group(2)
    return result


# ── 分批 ──────────────────────────────────────────────────────────────────────
def chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    if size < 1:
        raise ValueError("batch size 必须 >= 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


# ── 退避 ──────────────────────────────────────────────────────────────────────
RETRYABLE_MARKERS = ("429", "503", "resource_exhausted", "unavailable", "overloaded", "rate limit")


class TranslateConfigError(RuntimeError):
    """配置/鉴权问题(缺 SDK、Key 无效)。

    这类错误不能像 429 那样静默回退原文——否则 CI 会心安理得地产出
    一份"一句没翻"的成品并标记 done。必须炸出来让人看见。
    """


FATAL_MARKERS = (
    "api key not valid",
    "api_key_invalid",
    "unauthenticated",
    "permission denied",
    "401",
    "403",
)


def is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in RETRYABLE_MARKERS)


def is_fatal(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in FATAL_MARKERS)


def retry_delay(exc: Exception, attempt: int) -> float:
    """优先用服务端给的 retryDelay,没有就指数退避 8/16/32..."""
    match = re.search(r"retryDelay['\":\s]+(\d+(?:\.\d+)?)s", str(exc))
    if match:
        return float(match.group(1)) + 2.0
    return RETRY_BASE_DELAY * (2**attempt)


# ── Gemini 调用 ───────────────────────────────────────────────────────────────
_client = None


def api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip()


def is_mock() -> bool:
    """没有 API Key 即 mock 模式。"""
    return not api_key()


def model_name() -> str:
    return os.getenv(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _get_client():
    """惰性建客户端:没装 google-genai 时不 import,无 Key 也能整模块可用。"""
    global _client
    if _client is None:
        try:
            from google import genai
        except ImportError as exc:
            raise TranslateConfigError(
                f"检测到 {API_KEY_ENV} 但没装 google-genai,请先执行:"
                "uv pip install --python .venv google-genai"
            ) from exc
        _client = genai.Client(api_key=api_key())
    return _client


def _call_gemini(prompt: str) -> str:
    response = _get_client().models.generate_content(model=model_name(), contents=prompt)
    return getattr(response, "text", "") or ""


CallFn = Callable[[str], str]


def translate_batch(batch: Sequence[str], target: str, *, call: CallFn | None = None) -> list[str]:
    """翻一批。

    429/503 指数退避重试;重试用尽仍失败就整批回退原文,不往上抛。
    只有配置/鉴权错误(TranslateConfigError)会穿过这一层上抛。
    """
    call = call or _call_gemini
    prompt = build_prompt(batch, target)
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            return parse_response(call(prompt), batch)
        except TranslateConfigError:
            raise  # 配置问题直接上抛,不掩盖
        except Exception as exc:  # noqa: BLE001 - 其余异常都降级成回退原文
            if is_fatal(exc):
                raise TranslateConfigError(
                    f"鉴权/配置错误,检查 {API_KEY_ENV} 与 {MODEL_ENV}: {exc}"
                ) from exc
            last_error = exc
            if not is_retryable(exc):
                log.warning("批量翻译失败且不可重试,本批回退原文: %s", exc)
                break
            delay = retry_delay(exc, attempt)
            log.warning(
                "批量翻译第 %d/%d 次重试,%.1fs 后再试: %s", attempt + 1, MAX_RETRIES, delay, exc
            )
            time.sleep(delay)

    log.error("批量翻译放弃(%d 行回退原文): %s", len(batch), last_error)
    return list(batch)


def translate_lines(
    lines: Iterable[str],
    target: str = "zh_cn",
    *,
    call: CallFn | None = None,
    batch_size: int | None = None,
    inter_batch_delay: float | None = None,
) -> list[str]:
    """分批翻译一批文本,返回与入参等长、同序的结果。

    call / batch_size / inter_batch_delay 是给自测和调用方留的注入点。
    """
    items = [line if isinstance(line, str) else str(line) for line in lines]
    if not items:
        return []

    if call is None and is_mock():
        log.warning("未设置 %s,进入 mock 模式:原样返回 %d 行", API_KEY_ENV, len(items))
        return items

    size = BATCH_SIZE if batch_size is None else batch_size
    delay = INTER_BATCH_DELAY if inter_batch_delay is None else inter_batch_delay

    batches = list(chunked(items, size))
    results: list[str] = []
    for index, batch in enumerate(batches):
        if index:
            time.sleep(delay)  # 批间限速,首批不睡
        log.info("批次 %d/%d(%d 行)", index + 1, len(batches), len(batch))
        results.extend(translate_batch(batch, target, call=call))
    return results


# ── 完整管线 ──────────────────────────────────────────────────────────────────
def translate_snbt_file(
    in_path: str | Path,
    out_path: str | Path,
    target: str = "zh_cn",
    *,
    call: CallFn | None = None,
    batch_size: int | None = None,
    inter_batch_delay: float | None = None,
) -> TranslationReport:
    """en_us.snbt -> 打码 -> 分批翻译 -> 还原 + 校验 -> zh_cn.snbt。

    每个字段独立走 snbt 的保护/校验:占位符丢了或结构被改坏的字段单独回退原文,
    不影响同批其它字段。返回统计报告(可用 report.as_stats() 喂给 index_store)。
    """
    src_path = Path(in_path)
    dst_path = Path(out_path)

    tag = slib.loads(src_path.read_text(encoding="utf-8"))
    report = TranslationReport()

    pending: list[tuple[snbt.FieldRef, str, dict[str, str]]] = []
    masked_lines: list[str] = []

    for ref in snbt.iter_translatable_fields(tag):
        raw = ref.text
        masked, mapping, skip_reason = snbt.prepare_field(raw)
        if skip_reason:
            report.record(skip_reason)
            continue
        pending.append((ref, raw, mapping))
        masked_lines.append(masked)

    translations = (
        translate_lines(
            masked_lines,
            target,
            call=call,
            batch_size=batch_size,
            inter_batch_delay=inter_batch_delay,
        )
        if masked_lines
        else []
    )

    for (ref, raw, mapping), translated in zip(pending, translations):
        text, status = snbt.apply_translation(raw, mapping, translated)
        if status == "translated":
            ref.set_text(text)
        report.record(status)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_text(slib.dumps(tag), encoding="utf-8", newline="\n")
    return report


def translate_sources(
    files: Mapping[str, str],
    target: str = "zh_cn",
    *,
    call: CallFn | None = None,
    batch_size: int | None = None,
    inter_batch_delay: float | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], TranslationReport, dict[str, dict[str, object]]]:
    """一批 relpath -> SNBT 文本进,一批 relpath -> 译文文本出(硬编码多文件/语言文件通用)。

    与 translate_snbt_file 的差别:所有文件的待翻译字段合并成一次分批翻译,
    避免每个文件各起一批、把批间限速放大成文件数倍。字段级保护/校验与单文件一致
    (占位符丢失或结构被改坏的字段单独回退原文)。
    解析失败的文件原样透传并告警——宁可不翻,也不能把整包弄丢。

    返回 (输出文本 {relpath: text}, 汇总统计, 每文件统计 {relpath: 字段统计或 {"error": …}})。
    """
    warn = on_warning or (lambda msg: log.warning("%s", msg))
    report = TranslationReport()
    per_file: dict[str, TranslationReport] = {}
    errors: dict[str, str] = {}
    parsed: dict[str, object] = {}
    pending: list[tuple[str, snbt.FieldRef, str, dict[str, str]]] = []
    masked_lines: list[str] = []

    def record(relpath: str, status: str) -> None:
        report.record(status)
        per_file[relpath].record(status)

    for relpath in sorted(files):  # 固定文件顺序,保证分批边界可复现
        text = files[relpath]
        try:
            tag = slib.loads(text)
        except Exception as exc:  # noqa: BLE001 - 单文件坏掉不该拖垮整包
            message = f"{type(exc).__name__}: {exc}"
            warn(f"{relpath}: SNBT 解析失败,原样保留({message})")
            errors[relpath] = message
            continue
        parsed[relpath] = tag
        per_file[relpath] = TranslationReport()
        for ref in snbt.iter_translatable_fields(tag):
            masked, mapping, skip_reason = snbt.prepare_field(ref.text)
            if skip_reason:
                record(relpath, skip_reason)
                continue
            pending.append((relpath, ref, ref.text, mapping))
            masked_lines.append(masked)

    translations = (
        translate_lines(
            masked_lines,
            target,
            call=call,
            batch_size=batch_size,
            inter_batch_delay=inter_batch_delay,
        )
        if masked_lines
        else []
    )

    for (relpath, ref, raw, mapping), translated in zip(pending, translations):
        text, status = snbt.apply_translation(raw, mapping, translated)
        if status == "translated":
            ref.set_text(text)
        record(relpath, status)

    outputs: dict[str, str] = {}
    for relpath in files:
        tag = parsed.get(relpath)
        outputs[relpath] = slib.dumps(tag) if tag is not None else files[relpath]

    per_file_stats: dict[str, dict[str, object]] = {
        relpath: (per_file[relpath].as_stats() if relpath in per_file else {"error": errors[relpath]})
        for relpath in files
    }
    return outputs, report, per_file_stats


# ── 自测 ──────────────────────────────────────────────────────────────────────
SAMPLE_SNBT = r'''{
	chapter.0A1B2C.title: "&6Getting Started"
	quest.0A1B2C.quest_desc: [
		"Kill a minecraft:zombie with &c{0}&r damage."
		""
		"Reward: %s x %d, see [the wiki](https://example.com/guide)."
		"{@pagebreak}"
		"已翻译的中文行，不应再送翻译。"
	]
	quest.0099AA.title: "Use §bRF§r energy"
}
'''

_FAKE_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _fake_gemini(prompt: str) -> str:
    """自测用假模型:照 LINE_x||| 协议回包,给拉丁词套层中文(占位符原样不动)。"""
    out: list[str] = []
    for line in prompt.splitlines():
        match = LINE_RE.match(line.strip())
        if match:
            index, text = match.group(1), match.group(2)
            out.append(f"LINE_{index}|||{_FAKE_WORD_RE.sub(lambda m: '译' + m.group(0), text)}")
    return "\n".join(out)


class _CountingCall:
    """记录每批行数的假模型。"""

    def __init__(self) -> None:
        self.sizes: list[int] = []

    def __call__(self, prompt: str) -> str:
        # 只数 "TEXT TO TRANSLATE:" 之后的正文,别把提示词里的示例行也算进去
        body = prompt.split("TEXT TO TRANSLATE:", 1)[-1]
        self.sizes.append(len(re.findall(r"^LINE_\d+\|\|\|", body, re.MULTILINE)))
        return _fake_gemini(prompt)


class _FlakyCall:
    """前 failures 次抛 429,之后成功——用来验证退避重试。"""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return _fake_gemini(prompt)


def _self_test() -> None:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK,中文会乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="    [log] %(levelname)s %(message)s")

    print("=" * 72)
    print("翻译引擎自测")
    print("=" * 72)

    # 1. 提示词
    print("\n[1] 提示词:")
    prompt = build_prompt(["[#0#]Ancient Ruins", "Craft a [#1#]"], "zh_cn")
    assert "LINE_0|||[#0#]Ancient Ruins" in prompt
    assert "LINE_1|||Craft a [#1#]" in prompt
    assert "Simplified Chinese" in prompt
    assert "[#n#] TOKENS ARE SACRED" in prompt
    print("    含语言名、全部行、[#n#] 保护规则: OK")

    # 2. 回包解析
    print("\n[2] 回包解析:")
    originals = ["one", "two", "three"]
    assert parse_response("LINE_0|||一\nLINE_1|||二\nLINE_2|||三", originals) == ["一", "二", "三"]
    # 模型漏行 -> 漏掉的保留原文
    assert parse_response("LINE_0|||一\nLINE_2|||三", originals) == ["一", "two", "三"]
    # 模型多话 / 带 markdown 围栏 / 序号越界 -> 都要能扛住
    noisy = "Sure! Here you go:\n```\nLINE_0|||一\nLINE_9|||乱来\njunk line\n```\nHope it helps!"
    assert parse_response(noisy, originals) == ["一", "two", "three"]
    print("    正常 / 漏行回退 / 围栏与废话 / 越界序号: OK")

    # 3. 分批
    print("\n[3] 分批:")
    chunks = list(chunked(list(range(120)), 50))
    assert [len(c) for c in chunks] == [50, 50, 20], [len(c) for c in chunks]
    assert list(chunked([], 50)) == []
    print("    120 行按 50 切 -> [50, 50, 20],空输入 -> []: OK")

    # 4. 无 Key = mock
    print("\n[4] 无 Key 退化成 mock:")
    assert is_mock(), "自测环境不带 GEMINI_API_KEY,应处于 mock 模式"
    mock_out = translate_lines(["Hello &6world", "第二行"], "zh_cn")
    assert mock_out == ["Hello &6world", "第二行"], mock_out
    print("    mock 原样返回、不炸: OK")

    # 5. 分批实际生效
    print("\n[5] 分批调用:")
    counting = _CountingCall()
    lines = [f"Line number {i}" for i in range(120)]
    result = translate_lines(lines, "zh_cn", call=counting, batch_size=50, inter_batch_delay=0)
    assert len(result) == 120, len(result)
    assert counting.sizes == [50, 50, 20], counting.sizes
    assert result[0].startswith("译"), result[0]
    print(f"    3 次调用,每批行数 {counting.sizes},结果 120 行且已翻: OK")

    # 6. 429 指数退避
    print("\n[6] 429 退避重试:")
    saved_base = globals()["RETRY_BASE_DELAY"]
    saved_retries = globals()["MAX_RETRIES"]
    globals()["RETRY_BASE_DELAY"] = 0.0  # 自测不真等
    try:
        flaky = _FlakyCall(failures=2)
        ok_result = translate_lines(["Take 3 &adamage"], "zh_cn", call=flaky, inter_batch_delay=0)
        assert flaky.calls == 3, f"应重试到第 3 次才成功,实际 {flaky.calls}"
        assert ok_result[0].startswith("译"), ok_result[0]
        print(f"    前 2 次 429 后第 3 次成功(共 {flaky.calls} 次调用): OK")

        # 一直 429 -> 用尽重试后整批回退原文,不抛异常
        globals()["MAX_RETRIES"] = 3
        always_down = _FlakyCall(failures=99)
        fallback_result = translate_lines(["Take 3 &adamage"], "zh_cn", call=always_down, inter_batch_delay=0)
        assert always_down.calls == 3, always_down.calls
        assert fallback_result == ["Take 3 &adamage"], fallback_result
        print(f"    持续 429 用尽 {always_down.calls} 次后回退原文、不抛异常: OK")

        # 非重试类错误立刻放弃,不浪费配额
        def _boom(_prompt: str) -> str:
            raise ValueError("400 INVALID_ARGUMENT: bad request")

        assert translate_lines(["x"], "zh_cn", call=_boom, inter_batch_delay=0) == ["x"]
        print("    非 429/503 错误立即回退、不重试: OK")

        # 但鉴权出错必须上抛:否则 CI 会产出"一句没翻"的成品还标记 done
        def _bad_key(_prompt: str) -> str:
            raise RuntimeError("400 API key not valid. Please pass a valid API key.")

        try:
            translate_lines(["x"], "zh_cn", call=_bad_key, inter_batch_delay=0)
            raise AssertionError("鉴权错误应当上抛 TranslateConfigError")
        except TranslateConfigError as exc:
            print(f"    Key 无效上抛 TranslateConfigError: OK ({str(exc)[:44]}...)")

        # 缺 SDK 同理
        def _no_sdk(_prompt: str) -> str:
            raise TranslateConfigError("没装 google-genai")

        try:
            translate_lines(["x"], "zh_cn", call=_no_sdk, inter_batch_delay=0)
            raise AssertionError("缺 SDK 应当上抛")
        except TranslateConfigError:
            print("    缺 SDK 上抛 TranslateConfigError: OK")
    finally:
        globals()["RETRY_BASE_DELAY"] = saved_base
        globals()["MAX_RETRIES"] = saved_retries

    # 7. 端到端管线
    print("\n[7] en_us.snbt -> zh_cn.snbt 全管线:")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "en_us.snbt"
        dst = Path(tmp) / "zh_cn.snbt"
        src.write_text(SAMPLE_SNBT, encoding="utf-8")

        counting = _CountingCall()
        report = translate_snbt_file(src, dst, "zh_cn", call=counting, inter_batch_delay=0)
        print("   ", report.summary())
        assert report.fallback == 0, f"不该有回退: {report.fallback_reasons}"
        assert report.skip_reasons.get("already_chinese") == 1, report.skip_reasons
        assert report.skip_reasons.get("empty") == 1, report.skip_reasons
        assert report.skip_reasons.get("no_text") == 1, report.skip_reasons  # {@pagebreak}

        out_tag = slib.loads(dst.read_text(encoding="utf-8"))
        desc = out_tag["quest.0A1B2C.quest_desc"]
        assert len(desc) == 5, "数组元素个数不能变"

        assert desc[0].startswith("译Kill"), desc[0]
        assert "&c{0}&r" in desc[0], f"颜色码/变量必须还原: {desc[0]}"
        assert "minecraft:zombie" in desc[0], f"资源 ID 必须还原: {desc[0]}"
        assert "%s" in desc[2] and "%d" in desc[2], desc[2]
        assert "https://example.com/guide" in desc[2], f"链接必须还原: {desc[2]}"
        assert "[译the 译wiki](https://example.com/guide)" in desc[2], f"链接文字应可翻: {desc[2]}"
        assert desc[3] == "{@pagebreak}", desc[3]
        assert desc[4] == "已翻译的中文行，不应再送翻译。", desc[4]
        assert out_tag["quest.0099AA.title"] == "译Use §b译RF§r 译energy", out_tag["quest.0099AA.title"]
        print("    结构、颜色码、§b、{0}、%s、%d、资源 ID、链接、@pagebreak、中文行: 全部正确")

        # 8. 无 Key 走 mock 的端到端:内容不变但仍产出合法 SNBT
        print("\n[8] 无 Key mock 端到端:")
        src2 = Path(tmp) / "en_us2.snbt"
        dst2 = Path(tmp) / "zh_cn2.snbt"
        src2.write_text(SAMPLE_SNBT, encoding="utf-8")
        report2 = translate_snbt_file(src2, dst2)
        print("   ", report2.summary())
        assert report2.translated == 4, report2.translated
        out2 = slib.loads(dst2.read_text(encoding="utf-8"))
        assert out2["quest.0099AA.title"] == "Use §bRF§r energy"
        assert out2["quest.0A1B2C.quest_desc"][0] == slib.loads(SAMPLE_SNBT)["quest.0A1B2C.quest_desc"][0]
        print("    mock 下文本等于原文、产出仍是合法 SNBT: OK")

    # 9. index_store 对接
    print("\n[9] 报告 -> index_store.stats:")
    stats = report.as_stats()
    assert stats["fields"] == report.total and stats["translated"] == report.translated
    print("   ", stats)
    print("    as_stats() 字段: OK")

    # 10. 多文件(硬编码章节格式):合并成一次分批,坏文件透传,统计汇总
    print("\n[10] 一批 relpath->text(硬编码章节格式):")
    chapter_a = (
        '{\n\tid: "AAAA"\n\ttitle: "&6Getting Started"\n\tquests: [\n\t\t{\n\t\t\tid: "Q1"\n'
        '\t\t\ttitle: "Craft a Stone Pickaxe"\n\t\t\tdescription: [\n'
        '\t\t\t\t"Kill a minecraft:zombie with &c{0}&r damage."\n\t\t\t\t""\n\t\t\t]\n'
        '\t\t\ttasks: [\n\t\t\t\t{\n\t\t\t\t\tcount: 1L\n\t\t\t\t\tid: "T1"\n'
        '\t\t\t\t\titem: "minecraft:stone_pickaxe"\n\t\t\t\t\ttitle: "Pickaxe"\n'
        '\t\t\t\t\ttype: "item"\n\t\t\t\t}\n\t\t\t]\n\t\t\tx: -17.5d\n\t\t}\n\t]\n}\n'
    )
    chapter_b = '{\n\tid: "BBBB"\n\tquests: []\n\ttitle: "Alarm"\n}\n'
    data_snbt = '{\n\tdefault_autoclaim_rewards: "disabled"\n}\n'
    broken = "this is { not snbt"
    files = {
        "chapters/a.snbt": chapter_a,
        "chapters/b.snbt": chapter_b,
        "data.snbt": data_snbt,
        "broken.snbt": broken,
    }
    counting = _CountingCall()
    outs, report_multi, per_file = translate_sources(files, "zh_cn", call=counting, inter_batch_delay=0)
    print("   ", report_multi.summary())
    print("    每文件:", {k: v.get("fields", v) for k, v in per_file.items()})

    assert set(outs) == set(files), "输出键必须与输入一致"
    assert len(counting.sizes) == 1, f"所有文件应合并成一次分批,实际 {counting.sizes}"
    assert report_multi.translated == 5 and report_multi.skipped == 1, report_multi.as_stats()
    assert sum(int(v["fields"]) for v in per_file.values() if "fields" in v) == report_multi.total, \
        "每文件统计之和应等于汇总"

    assert outs["broken.snbt"] == broken, "解析失败的文件必须原样透传"
    assert "error" in per_file["broken.snbt"], per_file["broken.snbt"]
    assert outs["data.snbt"] == data_snbt, "无可翻字段的文件应原样输出"
    assert per_file["data.snbt"]["fields"] == 0, per_file["data.snbt"]

    tag_a = slib.loads(outs["chapters/a.snbt"])
    src_a = slib.loads(chapter_a)
    assert tag_a["id"] == "AAAA" and "译" in tag_a["title"], tag_a["title"]
    assert tag_a["quests"][0]["tasks"][0]["title"].startswith("译"), tag_a["quests"][0]["tasks"][0]["title"]
    assert tag_a["quests"][0]["tasks"][0]["count"] == src_a["quests"][0]["tasks"][0]["count"]
    assert tag_a["quests"][0]["x"] == src_a["quests"][0]["x"]
    assert "minecraft:zombie" in tag_a["quests"][0]["description"][0]
    assert "&c{0}&r" in tag_a["quests"][0]["description"][0], tag_a["quests"][0]["description"][0]
    assert tag_a["quests"][0]["description"][1] == ""
    assert slib.loads(outs["chapters/b.snbt"])["title"].startswith("译")
    print("    合并单批、坏文件透传、数值/占位符/空行保持、每文件统计: OK")

    # 11. mock 模式多文件:内容等价原文,不炸
    print("\n[11] 无 Key mock 多文件:")
    mock_outs, mock_report, _ = translate_sources(files, "zh_cn")
    print("   ", mock_report.summary())
    assert slib.loads(mock_outs["chapters/a.snbt"]) == src_a, "mock 下内容应等价原文"
    assert mock_outs["broken.snbt"] == broken
    assert mock_report.fallback == 0
    print("    mock 等价原文、坏文件透传、无回退: OK")

    print("\n" + "=" * 72)
    print("全部自测通过")
    print("=" * 72)


if __name__ == "__main__":
    _self_test()
