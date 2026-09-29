"""FTB Quests SNBT 引擎。

职责:解析 en_us.snbt -> 提取可翻译字段 -> 占位符保护 -> 交给翻译函数 ->
还原占位符 -> 译后校验(不合格回退原文) -> 写回 zh_cn.snbt。

本模块不发起任何网络/API 调用,翻译函数由调用方注入(mock_translate 为占位实现)。
"""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable, Iterator

import ftb_snbt_lib as slib
from ftb_snbt_lib.tag import Compound, List as TagList, String

__all__ = [
    "TRANSLATABLE_KEYS",
    "FieldRef",
    "TranslationReport",
    "iter_translatable_fields",
    "extract_translatable_fields",
    "is_translatable_key",
    "is_already_chinese",
    "should_skip",
    "mask_placeholders",
    "unmask_placeholders",
    "missing_placeholders",
    "validate_translation",
    "mock_translate",
    "prepare_field",
    "apply_translation",
    "translate_field",
    "translate_tag",
    "translate_snbt_text",
    "translate_snbt_file",
]

# ── 可翻译字段 ────────────────────────────────────────────────────────────────
# title/subtitle/text/description 是任务结构里的字段名;
# quest_desc/quest_subtitle 是 FTB Quests 语言文件(flat "quest.<id>.xxx")里的后缀。
TRANSLATABLE_KEYS = frozenset(
    {"title", "subtitle", "text", "description", "quest_desc", "quest_subtitle"}
)

# ── 占位符 ────────────────────────────────────────────────────────────────────
PLACEHOLDER_TEMPLATE = "[#{i}#]"
PLACEHOLDER_RE = re.compile(r"\[\s*#\s*(\d+)\s*#\s*\]")

# 保护顺序即优先级:越"具体"的形态越靠前,避免被后面的通用规则切碎。
# 每条 = (名字, 正则, 替换模板)。模板里的 {token} 会被真正的占位符替换。
# 注意 md_link 用 (?<=\]) 环视,只吃掉 (url) 而把 ] 留在正文里,保证括号配对。
PROTECT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # FTB 内联指令 {@pagebreak} / {@image:...}
    ("directive", re.compile(r"\{@[^{}]*\}"), "{token}"),
    # markdown 图片整块保护 ![alt](path)
    ("md_image", re.compile(r"!\[[^\]]*\]\([^)]*\)"), "{token}"),
    # markdown 链接 [文字](url) —— 用环视只锁 URL,括号与文字都留在正文里
    ("md_link", re.compile(r"(?<=\]\()[^()\s]*(?=\))"), "{token}"),
    # 尖括号自动链接 <https://...>
    ("autolink", re.compile(r"<(?:https?|ftp)://[^>\s]*>"), "{token}"),
    # 裸 URL
    ("url", re.compile(r"(?:https?|ftp)://[^\s\)\]\}>\"']+"), "{token}"),
    # &#RRGGBB / §#RRGGBB 十六进制颜色(必须早于 legacy_color)
    ("hex_color", re.compile(r"[&§]#[0-9a-fA-F]{6}"), "{token}"),
    # 传统颜色/格式码 &a §l &r
    ("legacy_color", re.compile(r"[&§][0-9a-fk-orA-FK-OR]"), "{token}"),
    # printf 变量 %s %d %1$s %.2f %%
    ("printf", re.compile(r"%[0-9.,]*\$?[A-Za-z%]"), "{token}"),
    # {...} 变量 {0} {player}
    ("brace_var", re.compile(r"\{[^{}]*\}"), "{token}"),
    # namespace:id,如 minecraft:zombie / thermal:redstone_furnace
    # (早于 image_path,免得 minecraft:textures/x.png 被切成两半)
    ("resource_id", re.compile(r"\b[a-z][a-z0-9_.-]*:[a-z0-9_./-]+\b"), "{token}"),
    # 图片/资源路径
    (
        "image_path",
        re.compile(
            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:png|jpe?g|gif|webp|tga)", re.IGNORECASE
        ),
        "{token}",
    ),
    ("image_file", re.compile(r"[A-Za-z0-9_.-]+\.(?:png|jpe?g|gif|webp|tga)\b", re.IGNORECASE), "{token}"),
    # 值里的字面转义 \n \r \t(SNBT 里是反斜杠+字母两个字符)
    ("escape_seq", re.compile(r"\\[nrt]"), "{token}"),
)

# ── 校验用 ────────────────────────────────────────────────────────────────────
STRUCTURAL_CHARS = ("{", "}", "[", "]")
# 交替顺序:十六进制色必须比传统色先匹配
COLOR_CODE_RE = re.compile(r"[&§]#[0-9a-fA-F]{6}|[&§][0-9a-fk-orA-FK-OR]")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


# ── 占位符 mask / unmask ──────────────────────────────────────────────────────
def mask_placeholders(text: str) -> tuple[str, dict[str, str]]:
    """把颜色码/变量/资源 ID/图片/链接等替换成 [#n#] 占位符。

    返回 (打码后的文本, {占位符: 原文片段})。
    """
    mapping: dict[str, str] = {}
    masked = text

    for _name, pattern, template in PROTECT_PATTERNS:

        def _replace(match: re.Match[str]) -> str:
            token = PLACEHOLDER_TEMPLATE.format(i=len(mapping))
            mapping[token] = match.group(0)
            return template.format(token=token)

        masked = pattern.sub(_replace, masked)

    return masked, mapping


def unmask_placeholders(text: str, mapping: dict[str, str]) -> str:
    """把译文里的 [#n#] 还原成被保护的原文片段。

    容忍模型加空格写成的 [ # n # ];未知编号原样保留。
    """
    by_index: dict[int, str] = {}
    for token, original in mapping.items():
        m = PLACEHOLDER_RE.fullmatch(token)
        if m:
            by_index[int(m.group(1))] = original

    def _replace(match: re.Match[str]) -> str:
        return by_index.get(int(match.group(1)), match.group(0))

    return PLACEHOLDER_RE.sub(_replace, text)


def missing_placeholders(text: str, mapping: dict[str, str]) -> list[int]:
    """译文里丢失/重复不足的占位符编号。"""
    present = {int(m.group(1)) for m in PLACEHOLDER_RE.finditer(text)}
    wanted = [int(PLACEHOLDER_RE.fullmatch(t).group(1)) for t in mapping if PLACEHOLDER_RE.fullmatch(t)]
    return sorted(i for i in wanted if i not in present)


# ── 译后校验 ──────────────────────────────────────────────────────────────────
def validate_translation(original: str, translated: str) -> tuple[bool, str]:
    """译后结构校验,不通过则调用方应回退原文。

    检查:括号数量、引号数量、颜色码序列(含 &#RRGGBB)。
    冒号不检查——正文里加/减冒号是正常的,键名不会被改是因为我们按字段精确回写。
    """
    for char in STRUCTURAL_CHARS:
        if original.count(char) != translated.count(char):
            return False, f"括号数量不一致: {char!r} {original.count(char)} -> {translated.count(char)}"

    if original.count('"') != translated.count('"'):
        return False, f"引号数量不一致: {original.count(chr(34))} -> {translated.count(chr(34))}"

    codes_original = COLOR_CODE_RE.findall(original)
    codes_translated = COLOR_CODE_RE.findall(translated)
    if codes_original != codes_translated:
        return False, f"颜色码序列不一致: {codes_original} -> {codes_translated}"

    return True, "OK"


# ── 跳过判定 ──────────────────────────────────────────────────────────────────
def is_already_chinese(text: str) -> bool:
    """行内已有中文且中文字数不少于拉丁字母数,视为已翻译。"""
    cjk = len(CJK_RE.findall(text))
    if cjk == 0:
        return False
    return cjk >= len(LATIN_RE.findall(text))


def should_skip(text: str, masked: str) -> str | None:
    """该字段是否不需要送翻译。返回跳过原因,None 表示需要翻译。

    masked 需是 mask_placeholders(text) 的结果——颜色码/变量都换成 [#n#] 之后,
    还能不能找到拉丁字母是判断"有没有实质内容"的好办法。
    """
    if not text.strip():
        return "empty"
    if is_already_chinese(text):
        return "already_chinese"
    if not LATIN_RE.search(masked):
        return "no_text"
    return None


# ── 字段遍历 ──────────────────────────────────────────────────────────────────
def is_translatable_key(key: object) -> bool:
    """键的最后一个点分段是否是可翻译字段名。

    'quest.0A1B.quest_desc' -> True;'quests' / 'tasks' -> False。
    """
    return str(key).rsplit(".", 1)[-1] in TRANSLATABLE_KEYS


@dataclass
class FieldRef:
    """一个可翻译字符串,以及它在 SNBT 树里回写的位置。"""

    path: str
    container: object
    slot: object  # Compound 的键,或 List 的下标
    text: str

    def set_text(self, new_text: str) -> None:
        self.container[self.slot] = String(new_text)  # type: ignore[index]
        self.text = new_text


def iter_translatable_fields(tag: object, prefix: str = "") -> Iterator[FieldRef]:
    """深度优先遍历 SNBT 树,产出所有可翻译字段。"""
    if isinstance(tag, Compound):
        for key, value in tag.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, String):
                if is_translatable_key(key):
                    yield FieldRef(path, tag, key, str(value))
            elif isinstance(value, TagList):
                if is_translatable_key(key):
                    for index, item in enumerate(value):
                        if isinstance(item, String):
                            yield FieldRef(f"{path}[{index}]", value, index, str(item))
                else:
                    for index, item in enumerate(value):
                        yield from iter_translatable_fields(item, f"{path}[{index}]")
            elif isinstance(value, Compound):
                yield from iter_translatable_fields(value, path)
    elif isinstance(tag, TagList):
        for index, item in enumerate(tag):
            yield from iter_translatable_fields(item, f"{prefix}[{index}]")


def extract_translatable_fields(tag: object) -> list[FieldRef]:
    return list(iter_translatable_fields(tag))


# ── 翻译管线 ──────────────────────────────────────────────────────────────────
TranslateFn = Callable[[str], str]


def mock_translate(text: str) -> str:
    """占位翻译器:原样返回。R3 接入 Gemini 之前用它跑通整条管线。"""
    return text


def prepare_field(raw: str) -> tuple[str, dict[str, str], str | None]:
    """翻译前的准备:打码 + 判断要不要翻。

    返回 (masked, mapping, 跳过原因);跳过原因为 None 时 masked 才需要送翻译。
    """
    masked, mapping = mask_placeholders(raw)
    return masked, mapping, should_skip(raw, masked)


def apply_translation(
    raw: str, mapping: dict[str, str], translated_masked: str
) -> tuple[str, str]:
    """翻译后的收尾:查占位符 -> 还原 -> 校验。

    返回 (最终文本, 状态)。状态不是 translated 时,最终文本就是原文。
    """
    if not isinstance(translated_masked, str) or not translated_masked.strip():
        return raw, "translate_failed"

    lost = missing_placeholders(translated_masked, mapping)
    if lost:
        return raw, f"placeholder_lost:{lost}"

    restored = unmask_placeholders(translated_masked, mapping)

    ok, detail = validate_translation(raw, restored)
    if not ok:
        return raw, f"invalid:{detail}"

    return restored, "translated"


def translate_field(raw: str, translate_fn: TranslateFn) -> tuple[str, str]:
    """单个字段:打码 -> 翻译 -> 校验 -> 还原。

    返回 (最终文本, 状态)。状态为 translated / 跳过原因 / 回退原因,
    只要不是 translated,最终文本就是原文。
    """
    masked, mapping, reason = prepare_field(raw)
    if reason:
        return raw, reason
    return apply_translation(raw, mapping, translate_fn(masked))


@dataclass
class TranslationReport:
    """一次运行的统计。"""

    total: int = 0
    translated: int = 0
    skipped: int = 0
    fallback: int = 0
    skip_reasons: dict[str, int] = dataclass_field(default_factory=dict)
    fallback_reasons: dict[str, int] = dataclass_field(default_factory=dict)

    def _bump(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def record(self, status: str) -> None:
        self.total += 1
        if status == "translated":
            self.translated += 1
        elif status.startswith(("placeholder_lost", "invalid", "translate_failed")):
            self.fallback += 1
            self._bump(self.fallback_reasons, status.split(":", 1)[0])
        else:
            self.skipped += 1
            self._bump(self.skip_reasons, status)

    def summary(self) -> str:
        return (
            f"字段 {self.total} 个:翻译 {self.translated}、跳过 {self.skipped}、回退 {self.fallback}"
            f" | 跳过明细 {self.skip_reasons or '-'} | 回退明细 {self.fallback_reasons or '-'}"
        )

    def as_stats(self) -> dict[str, object]:
        """给 index_store 的 meta.stats 用。"""
        return {
            "fields": self.total,
            "translated": self.translated,
            "skipped": self.skipped,
            "fallback": self.fallback,
            "skipReasons": dict(self.skip_reasons),
            "fallbackReasons": dict(self.fallback_reasons),
        }


def translate_tag(tag: object, translate_fn: TranslateFn = mock_translate) -> TranslationReport:
    """就地翻译整棵 SNBT 树。"""
    report = TranslationReport()
    for ref in iter_translatable_fields(tag):
        new_text, status = translate_field(ref.text, translate_fn)
        if status == "translated":
            ref.set_text(new_text)
        report.record(status)
    return report


def translate_snbt_text(
    content: str, translate_fn: TranslateFn = mock_translate
) -> tuple[str, TranslationReport]:
    """SNBT 文本进、SNBT 文本出(不经过临时文件)。

    语言文件(flat "quest.<id>.xxx")与硬编码章节文件(chapters/*.snbt,嵌套
    quests/tasks/reward_tables)走同一套遍历:解析 → 提取 title/subtitle/
    description/text → 打码 → 翻译 → 还原 + 校验 → 回填 → dump。
    返回 (译文文本, 统计);统计供上层汇总(例如写进 meta.json)。
    """
    tag = slib.loads(content)
    report = translate_tag(tag, translate_fn)
    return slib.dumps(tag), report


def translate_snbt_file(
    src: str | Path,
    dst: str | Path | None = None,
    translate_fn: TranslateFn = mock_translate,
) -> TranslationReport:
    """读 en_us.snbt -> 翻译 -> 写 zh_cn.snbt。dst 省略时只统计不落盘。"""
    src_path = Path(src)
    tag = slib.loads(src_path.read_text(encoding="utf-8"))
    report = translate_tag(tag, translate_fn)

    if dst is not None:
        dst_path = Path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_text(slib.dumps(tag), encoding="utf-8", newline="\n")

    return report


# ── 自测 ──────────────────────────────────────────────────────────────────────
SAMPLE_SNBT = r'''{
	chapter.0A1B2C.title: "&6Getting Started"
	chapter.0A1B2C.subtitle: ["A &onew &rbeginning"]
	quest.0A1B2C.title: "&aFree Runner&r boots"
	quest.0A1B2C.quest_subtitle: "&#FF00AAWeekly&#FFFFFF challenge"
	quest.0A1B2C.quest_desc: [
		"Kill a minecraft:zombie with &c{0}&r damage."
		""
		"Read [the wiki](https://example.com/guide) or <https://ftb.team> for help."
		"&7Reward: %s x %d"
		"Multi\nline keeps its break."
		"{@pagebreak}"
		"![icon](textures/gui/icon.png) and assets/title/banner.png"
		"已翻译的中文行，不应再送翻译。"
		"---"
	]
	task.1F2E3D.title: "Craft a %s"
	quest.0099AA.quest_desc: ["Kill %1$s zombies for %.2f points"]
	quest.0088BB.title: "Use §bRF§r energy"
}
'''


def _fake_translate(text: str) -> str:
    """自测用"翻译器":给每个拉丁词套一层中文,模拟译文但不动结构。"""
    return LATIN_WORD_RE.sub(lambda m: "译" + m.group(0), text)


def _destructive_translate(text: str) -> str:
    """自测用"坏翻译器":吃掉方括号,用来验证回退。"""
    return text.replace("[", "(")


def _self_test() -> None:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK,中文会乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 72)
    print("SNBT 引擎自测")
    print("=" * 72)

    # 1. 解析 + 提取
    tag = slib.loads(SAMPLE_SNBT)
    fields = extract_translatable_fields(tag)
    paths = [f.path for f in fields]
    print(f"\n[1] 提取可翻译字段 {len(fields)} 个:")
    for p in paths:
        print("    -", p)
    assert "chapter.0A1B2C.title" in paths
    assert "quest.0A1B2C.quest_desc[0]" in paths
    assert "quest.0A1B2C.quest_desc[4]" in paths, "数组长文本要逐元素提取"
    assert all("task" not in p or p.endswith("title") for p in paths)

    # 2. mask/unmask 恒等往返
    sample = (
        "&6Gold &#FF00AAHex &rReset §bSec kill minecraft:zombie "
        "![i](textures/gui/icon.png) [wiki](https://example.com/a) "
        "{@pagebreak} {0} %s x %d Multi\\nbreak"
    )
    masked, mapping = mask_placeholders(sample)
    print(f"\n[2] mask 得到 {len(mapping)} 个占位符:")
    print("    原文:", sample)
    print("    打码:", masked)
    assert "minecraft:zombie" not in masked, "资源 ID 必须被打码"
    assert "https://example.com/a" not in masked, "链接必须被打码"
    assert "&6" not in masked and "&#FF00AA" not in masked, "颜色码必须被打码"
    assert unmask_placeholders(masked, mapping) == sample, "mask/unmask 必须恒等往返"
    print("    unmask 往返: OK")

    # 3. 校验器:坏译文必须被拒
    print("\n[3] 校验器:")
    cases = [
        ("&aHello &rWorld", "&a你好", False, "颜色码丢失"),
        ("Say \"hi\"", "说 \"你好", False, "引号数量变化"),
        ("Use [A] here", "用 A] 这里", False, "方括号数量变化"),
        ("&6Hello &rWorld", "&6你好 &r世界", True, "结构完好"),
        ("See https://a.b/c", "见 https://a.b/c", True, "URL 不改也应放行"),
    ]
    for original, translated, expect_ok, label in cases:
        ok, detail = validate_translation(original, translated)
        print(f"    {'PASS' if ok else 'FAIL'} <- {label}: {detail}")
        assert ok is expect_ok, f"{label} 校验结果不符预期"

    # 4. 整条管线(mock 原样返回 = 不该发生任何改动)
    print("\n[4] mock_translate(原样返回)管线:")
    out_same, report = translate_snbt_text(SAMPLE_SNBT, mock_translate)
    print("   ", report.summary())
    assert not report.fallback, "mock 不应触发回退"
    assert slib.loads(out_same) is not None

    # 5. 整条管线(模拟翻译):结构必须保住,中文行必须跳过
    print("\n[5] 模拟翻译管线:")
    out, report = translate_snbt_text(SAMPLE_SNBT, _fake_translate)
    print("   ", report.summary())
    assert report.translated > 0, "应该有字段被翻译"
    assert report.skip_reasons.get("already_chinese") == 1, "中文行应被跳过"
    # '---' 无字母;'{@pagebreak}' 打码后不剩任何拉丁字母,都不该送翻译
    assert report.skip_reasons.get("no_text") == 2, "纯符号行与纯指令行应被跳过"
    assert report.fallback == 0, f"模拟翻译不该触发回退: {report.fallback_reasons}"

    out_tag = slib.loads(out)
    out_fields = {f.path: f.text for f in extract_translatable_fields(out_tag)}

    for path in (
        "chapter.0A1B2C.title",
        "quest.0A1B2C.quest_subtitle",
        "quest.0A1B2C.quest_desc[0]",
        "quest.0A1B2C.quest_desc[2]",
        "quest.0A1B2C.quest_desc[3]",
        "quest.0A1B2C.quest_desc[4]",
        "quest.0A1B2C.quest_desc[6]",
        "quest.0099AA.quest_desc[0]",
        "quest.0088BB.title",
        "task.1F2E3D.title",
    ):
        original = next(f.text for f in fields if f.path == path)
        translated = out_fields[path]
        assert translated != original, f"{path} 应该被翻译"
        ok, detail = validate_translation(original, translated)
        assert ok, f"{path} 译后校验失败: {detail}"
        for fragment in _protected_fragments(original):
            assert fragment in translated, f"{path} 丢失了 {fragment!r}: {translated!r}"
    print("    所有受保护片段(&颜色、§颜色、&#hex、{0}、%s、%1$s、%.2f、")
    print("    minecraft:zombie、图片路径、markdown 链接、<url>、{@pagebreak}、\\n)均已还原: OK")

    assert out_fields["quest.0A1B2C.quest_desc[5]"] == "{@pagebreak}", "{@pagebreak} 必须原样保留"
    assert out_fields["quest.0A1B2C.quest_desc[7]"] == "已翻译的中文行，不应再送翻译。", "中文行不能被改"
    assert out_fields["quest.0A1B2C.quest_desc[8]"] == "---", "'---' 不能被改"
    assert out_fields["quest.0A1B2C.quest_desc[1]"] == "", "空行要保持空"
    print("    已中文行 / 纯符号行 / 空行 保持原样: OK")

    # 6. 坏翻译器 -> 回退原文
    print("\n[6] 破坏性译文回退:")
    out_bad, report_bad = translate_snbt_text(SAMPLE_SNBT, _destructive_translate)
    print("   ", report_bad.summary())
    assert report_bad.fallback > 0, "坏译文必须触发回退"
    bad_tag = slib.loads(out_bad)
    assert bad_tag["chapter.0A1B2C.title"] == tag["chapter.0A1B2C.title"], "回退必须等于原文"
    print("    被破坏的字段已回退为原文: OK")

    # 7. 落盘 / 回读
    print("\n[7] 落盘 -> 回读:")
    with tempfile.TemporaryDirectory() as tmp:
        src_file = Path(tmp) / "en_us.snbt"
        dst_file = Path(tmp) / "zh_cn.snbt"
        src_file.write_text(SAMPLE_SNBT, encoding="utf-8")
        report_file = translate_snbt_file(src_file, dst_file, _fake_translate)
        print("   ", report_file.summary())
        reread = slib.loads(dst_file.read_text(encoding="utf-8"))
        assert reread["quest.0A1B2C.quest_subtitle"] == out_tag["quest.0A1B2C.quest_subtitle"]
        assert len(reread["quest.0A1B2C.quest_desc"]) == 9, "数组元素个数不能变"
        print("    写出 zh_cn.snbt 并成功回读,数组长度不变: OK")

    # 8. 嵌套结构(非 flat lang 文件也要能遍历)
    print("\n[8] 嵌套结构遍历:")
    nested = slib.loads('{\n\tquests: [\n\t\t{\n\t\t\ttitle: "&6Deep One"\n\t\t\ttasks: [\n\t\t\t\t{\n\t\t\t\t\ttitle: "Inner Task"\n\t\t\t\t}\n\t\t\t]\n\t\t}\n\t]\n}\n')
    nested_paths = [f.path for f in extract_translatable_fields(nested)]
    print("   ", nested_paths)
    assert "quests[0].title" in nested_paths
    assert "quests[0].tasks[0].title" in nested_paths
    print("    递归遍历嵌套 compound/list: OK")

    print("\n" + "=" * 72)
    print("全部自测通过")
    print("=" * 72)


def _protected_fragments(original: str) -> list[str]:
    """自测辅助:列出原文里所有应当原样出现在译文中的受保护片段(去重保序)。"""
    _, mapping = mask_placeholders(original)
    seen: list[str] = []
    for fragment in mapping.values():
        if fragment not in seen:
            seen.append(fragment)
    return seen


if __name__ == "__main__":
    _self_test()
