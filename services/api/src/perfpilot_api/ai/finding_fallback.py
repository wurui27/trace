"""Build a deterministic Chinese synthesis when the configured AI is unusable."""

from __future__ import annotations

import re
from typing import Mapping

from perfpilot_api.ai.synthesis import (
    AISynthesisOutput,
    _TECHNICAL_IDENTIFIER_WITH_DIGIT,
    _TECHNICAL_VERSION_WITH_DIGIT,
    _contains_free_written_number,
    validate_synthesis_output,
)
from perfpilot_api.reports.projection import AIProjection


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_NUMERIC_LITERAL = (
    r"[+-]?(?:[0-9０-９]+(?:[.．][0-9０-９]+)?|[.．][0-9０-９]+)"
    r"(?:[eE][+-]?[0-9０-９]+)?"
)
_MEASUREMENT_UNIT = (
    r"(?:毫秒|微秒|纳秒|分钟|小时|帧每秒|"
    r"(?:千|兆|吉|太)?字节每秒|(?:千|兆|吉|太)?字节|"
    r"(?:千|兆|吉)?赫兹|(?:兆|吉)赫|"
    r"[kmgt]i?b(?:/s)?|b(?:/s)?|[kmg]?hz|fps|msec|sec|ms|us|µs|μs|ns|s|%|％|秒)"
)
_CHINESE_INTEGER = r"[零〇一二三四五六七八九十百千万萬亿億两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+"
_CHINESE_NUMERAL = rf"{_CHINESE_INTEGER}(?:点{_CHINESE_INTEGER})?"
_CHINESE_COUNT_UNIT = r"(?:次|条|个|项|帧|轮|倍|段|处|种|份|组|类|台|核|页|行)"
_LEXICAL_ONE_COUNT_GUARD = (
    rf"(?!一次性)(?!(?:(?<=每)|(?<=逐)|(?<=同)|(?<=另)|"
    rf"(?<=上)|(?<=下)|(?<=前)|(?<=后)|(?<=这)|(?<=某)|(?<=任)|(?<=新))"
    rf"一\s*{_CHINESE_COUNT_UNIT})"
)
_LEXICAL_ONE_MEASUREMENT_GUARD = (
    rf"(?!(?:(?<=统)|(?<=逐)|(?<=每)|(?<=同)|(?<=另)|"
    rf"(?<=上)|(?<=下)|(?<=前)|(?<=后)|(?<=这)|(?<=某)|(?<=任)|(?<=新))"
    rf"一\s*{_MEASUREMENT_UNIT})"
)
_STANDALONE_QUANTITY_PREFIX = (
    r"(?:性能|耗时|时长|延迟|占比|比例|问题数量|数量|排名|收益|吞吐|"
    r"内存|帧率|频率|CPU(?:占用)?)\s*"
    r"(?:提升|降低|增加|减少|上升|下降|为|达到|达|是)"
)
_STANDALONE_CHINESE_QUANTITY = (
    rf"{_STANDALONE_QUANTITY_PREFIX}\s*{_CHINESE_NUMERAL}(?:成)?"
    r"(?=[\s，。；！？,.!?;:]|$)"
)
_ASCII_SUFFIXED_MEASUREMENT = (
    rf"(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}"
    r"(?:x|milliseconds?|microseconds?|nanoseconds?|seconds?|percent)"
    r"(?![A-Za-z0-9_])"
)
_TOP_RANK = rf"(?i:top)\s*{_NUMERIC_LITERAL}(?![A-Za-z0-9_])"
_FREE_WRITTEN_NUMBER = re.compile(
    rf"(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}\s*{_MEASUREMENT_UNIT}(?![A-Za-z0-9_])"
    rf"|(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}(?![A-Za-z0-9_])"
    rf"|{_LEXICAL_ONE_MEASUREMENT_GUARD}{_CHINESE_NUMERAL}\s*{_MEASUREMENT_UNIT}"
    rf"(?![A-Za-z0-9_])"
    rf"|百分之{_CHINESE_NUMERAL}"
    rf"|{_LEXICAL_ONE_COUNT_GUARD}{_CHINESE_NUMERAL}\s*{_CHINESE_COUNT_UNIT}"
    rf"|{_STANDALONE_CHINESE_QUANTITY}"
    rf"|{_ASCII_SUFFIXED_MEASUREMENT}"
    rf"|{_TOP_RANK}",
    re.IGNORECASE,
)
_FREE_NUMBER_WITH_UNIT = re.compile(
    rf"(?:(?:约|近|超过|至少|至多|正|负)?\s*"
    rf"(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}\s*"
    rf"(?:{_MEASUREMENT_UNIT}|{_CHINESE_COUNT_UNIT})"
    r"(?![A-Za-z0-9_])"
    rf"|(?<![A-Za-z0-9_]){_NUMERIC_LITERAL}(?![A-Za-z0-9_])"
    rf"|(?:约|近|超过|至少|至多|正|负)?\s*"
    rf"{_LEXICAL_ONE_MEASUREMENT_GUARD}{_CHINESE_NUMERAL}\s*{_MEASUREMENT_UNIT}"
    rf"(?![A-Za-z0-9_])"
    rf"|百分之{_CHINESE_NUMERAL}"
    rf"|{_LEXICAL_ONE_COUNT_GUARD}{_CHINESE_NUMERAL}\s*{_CHINESE_COUNT_UNIT}"
    rf"|{_STANDALONE_CHINESE_QUANTITY}"
    rf"|{_ASCII_SUFFIXED_MEASUREMENT}"
    rf"|{_TOP_RANK})",
    re.IGNORECASE,
)
_INTERNAL_REFERENCE = re.compile(
    r"(?<![a-z0-9_.-])(?:art[-_]?[0-9]+|art_[a-z0-9_]+|sr[0-9]+|execute_sql:[0-9]+)"
    r"(?![a-z0-9_.-])",
    re.IGNORECASE,
)
_NUMBER_WORD_REWRITES = (
    ("第一帧", "首帧"),
    ("第一屏", "首屏"),
)
_THIRD_PARTY_DEPENDENCY = re.compile(
    r"(?:第三方|三方)(?=\s*(?:SDK|库|组件|依赖|服务|框架|模块|插件))",
    re.IGNORECASE,
)
_REMOVED_VALUE = "\ufff0"
_VALUE_LABEL_BEFORE_REMOVED = re.compile(
    rf"(?:占比|比例|累计值|总计|共计|数值|次数|数量|时长|耗时|累计)"
    rf"\s*(?:为|约|达|是)?\s*{_REMOVED_VALUE}"
)
_NUMBERED_ITEM = re.compile(
    rf"第(?:{_NUMERIC_LITERAL}|{_CHINESE_NUMERAL})\s*"
    rf"(?P<unit>{_CHINESE_COUNT_UNIT}|屏)"
)


def _rewrite_written_count_phrases(text: str) -> str:
    text = re.sub(
        rf"发现\s*{_CHINESE_NUMERAL}\s*条\s*长耗时",
        "发现多处长耗时问题",
        text,
    )
    text = re.sub(
        rf"出现\s*{_CHINESE_NUMERAL}\s*条\s*长耗时",
        "出现多处长耗时问题",
        text,
    )
    return re.sub(
        rf"首帧有\s*{_CHINESE_NUMERAL}\s*帧",
        "首帧帧数存在异常",
        text,
    )


def _without_free_numbers(text: str) -> str:
    for original, replacement in _NUMBER_WORD_REWRITES:
        text = text.replace(original, replacement)
    text = _THIRD_PARTY_DEPENDENCY.sub("外部", text)
    text = _rewrite_written_count_phrases(text)

    def rewrite_numbered_item(match: re.Match[str]) -> str:
        unit = match.group("unit")
        if unit in {"帧", "屏"}:
            return f"后续{unit}"
        if unit in {"轮", "次"}:
            return "后续"
        return "相关"

    text = _NUMBERED_ITEM.sub(rewrite_numbered_item, text)
    text = re.sub(
        r"(?:\r?\n|\\n)\s*-\s*(?:证据|证据类型)[：:].*?(?=(?:\r?\n|\\n)\s*-\s*|$)",
        "",
        text,
        flags=re.DOTALL,
    )
    text = text.replace("`", "").replace("*", "")
    text = _INTERNAL_REFERENCE.sub("", text)
    protected_identifiers: dict[str, str] = {}

    def protect_identifier(match: re.Match[str]) -> str:
        placeholder = chr(0xE100 + len(protected_identifiers))
        protected_identifiers[placeholder] = match.group(0)
        return placeholder

    text = _TECHNICAL_VERSION_WITH_DIGIT.sub(protect_identifier, text)
    text = _TECHNICAL_IDENTIFIER_WITH_DIGIT.sub(protect_identifier, text)
    removed_measurement = _FREE_NUMBER_WITH_UNIT.search(text) is not None
    text = _FREE_NUMBER_WITH_UNIT.sub(_REMOVED_VALUE, text)
    text = _VALUE_LABEL_BEFORE_REMOVED.sub(_REMOVED_VALUE, text)
    text = re.sub(
        rf"{_REMOVED_VALUE}(?:\s*[\uff0f/|]\s*{_REMOVED_VALUE})+",
        _REMOVED_VALUE,
        text,
    )
    text = text.replace(_REMOVED_VALUE, "")
    text = re.sub(r"\s+[／/|]\s+", "，", text)
    text = re.sub(
        r"(?:(?<=\s)|(?<=[\u3400-\u9fff]))[／/|]+"
        r"(?=\s|[\u3400-\u9fff]|[，。；：！？])",
        "，",
        text,
    )
    text = re.sub(r"\s*中约\s*是\s*", " 中存在 ", text)
    text = re.sub(r"\s*中\s+是\s*", " 中存在 ", text)
    text = re.sub(r"(?:\r?\n|\\n)\s*-\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，。；：！？、,.!?;:])", r"\1", text)
    text = re.sub(r"([（(])[\s，,；;：:/／|]+", r"\1", text)
    text = re.sub(r"(?:详见|参考)\s*(?:与|和|、|及|\s)*[。；，]", "", text)
    text = re.sub(r"[（(][\s，,；;：:/／|]*[）)]", "", text)
    text = re.sub(
        r"[（(]\s*(?:wall|running|self|累计|占比|比例)\s*[）)]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[（(]\s*\.so\s*[）)]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[（(][^）)]*(?:占|累计)[^）)]*[）)]", "", text)
    text = re.sub(r"[（(]\s*归因\s*[）)]", "", text)
    text = re.sub(r"\s+[）)]", lambda match: match.group(0).lstrip(), text)
    text = re.sub(r"([，；：])(?=[。！？]|$)", "", text)
    text = re.sub(r"([，。；：！？])\1+", r"\1", text)
    text = re.sub(r"\s*[—–-]\s*self\s*$", "", text, flags=re.IGNORECASE)
    for placeholder, identifier in protected_identifiers.items():
        text = text.replace(placeholder, identifier)
    text = text.strip(" ，,；;：:")
    if removed_measurement and re.search(
        r"^(?:建议)?(?:降低|增加|减少|变化为|提升|下降|耗时|占比|"
        r"累计|超过|至少|至多|约|近|为|达|是)。?$",
        text,
    ):
        return ""
    return text


def _cause_fragment(value: object) -> object:
    if not isinstance(value, str):
        return value
    match = re.search(
        r"(?:根因链|原因链)[：:]\s*(.*?)(?=(?:\r?\n|\\n)\s*-\s*|$)",
        value,
        flags=re.DOTALL,
    )
    if match is not None:
        return match.group(1)
    return re.split(r"(?:\r?\n|\\n)\s*-\s*", value, maxsplit=1)[0]


def _finding_text(finding: Mapping[str, object]) -> str:
    return "\n".join(
        str(finding.get(field, ""))
        for field in ("title", "problem", "root_cause", "engine_recommendation")
    ).casefold()


def _primary_finding_text(finding: Mapping[str, object]) -> str:
    return str(finding.get("title", "")).casefold()


def _primary_category(finding: Mapping[str, object]) -> str | None:
    text = _primary_finding_text(finding)
    if "binder" in text or "同步 ipc" in text:
        return "binder"
    if "sdk" in text or any(
        marker in text for marker in ("qqmusicsdkadapter", "xeaglebtadapter")
    ):
        return "sdk"
    if any(marker in text for marker in ("首帧", "doframe", "compose", "measure")):
        return "frame"
    if any(marker in text for marker in ("锁竞争", "monitor")):
        return "lock"
    if any(marker in text for marker in ("native", "dlopen", ".so")):
        return "native"
    if any(marker in text for marker in ("jit", "baseline profile")):
        return "jit"
    if "fork" in text or "并发进程" in text or "并发应用" in text:
        return "fork"
    if "调度延迟" in text or "runnable" in text:
        return "schedule"
    return None


def _specific_problem(finding: Mapping[str, object]) -> str | None:
    category = _primary_category(finding)
    title = _primary_finding_text(finding)
    low_confidence = (
        finding.get("status") != "confirmed"
        or finding.get("confidence_ceiling") in {"low", "none"}
    )
    needs_summary = (
        (low_confidence and category in {"native", "jit", "fork", "schedule"})
        or (
            category == "binder"
            and "ipc" in title
            and "阻塞" in title
            and "self" in title
        )
        or (
            category == "frame"
            and "doframe" in title
            and re.search(r"cpu\s*占", title) is not None
        )
        or (
            category == "sdk"
            and "xeaglebtadapter.start" in title
            and "sleeping" in title
        )
        or (
            category == "lock"
            and "主线程锁竞争" in title
            and (
                _INTERNAL_REFERENCE.search(title) is not None
                or re.search(r"[0-9０-９]", title) is not None
            )
        )
    )
    if not needs_summary:
        return None
    if category == "binder":
        return "冷启动主线程同步 Binder 调用阻塞关键路径。"
    if category == "sdk":
        text = _primary_finding_text(finding)
        if "xeaglebtadapter.start" in text:
            return "XeagleBtAdapter.start 在冷启动主线程同步执行。"
        if "qqmusicsdkadapter.doinit" in text:
            return "QQMusicSdkAdapter.doInit 在冷启动主线程同步执行。"
        return "外部 SDK 在冷启动主线程同步初始化。"
    if category == "frame":
        return "首帧渲染阶段主线程工作量过重。"
    if category == "lock":
        return "冷启动主线程存在锁竞争。"
    if category == "native":
        return "启动阶段存在 Native 库加载活动，实际关键路径影响仍需核验。"
    if category == "jit":
        return "冷启动期间存在 JIT 编译活动，是否缺少 Baseline Profile 尚未证实。"
    if category == "fork":
        return "测试窗口内存在并发进程活动，可能与调度等待相关。"
    if category == "schedule":
        return "冷启动主线程存在调度等待现象，具体影响仍需对照复测。"
    return None


def _category_is_negated(text: str, markers: tuple[str, ...]) -> bool:
    clauses = re.split(r"[，。；！？\n]", text)
    for clause in clauses:
        matching = tuple(marker for marker in markers if marker in clause)
        if not matching:
            continue
        for marker in matching:
            escaped = re.escape(marker)
            marker_start = clause.find(marker)
            before = clause[:marker_start]
            after = clause[marker_start + len(marker) :]
            blocked_exclusion = re.search(
                r"(?:未发现|没有证据|无证据|无法|不能).{0,16}排除",
                before,
            )
            if (
                blocked_exclusion is None
                and re.search(rf"(?:已|明确)?排除.{{0,8}}{escaped}", clause)
            ):
                return True
            if re.search(rf"不涉及.{{0,8}}{escaped}", clause):
                return True
            absent = re.search(
                rf"(?:未发现|未观察到|没有|不存在)(.{{0,10}}){escaped}",
                clause,
            )
            if (
                absent is not None
                and all(
                    token not in absent.group(1) for token in ("证据", "排除")
                )
                and (
                    not after.strip()
                    or re.search(
                        r"(?:编译|存在|活动|影响|问题|异常|原因|根因|瓶颈)",
                        after,
                    )
                )
            ):
                return True
            if re.search(
                r"(?:未发现|未观察到|没有).{0,8}"
                r"(?:编译|存在|活动|影响|问题|异常|原因|根因|瓶颈)",
                after,
            ):
                return True
            if re.search(
                r"(?:编译|存在|活动|影响|问题|异常|原因|根因|瓶颈)"
                r".{0,8}(?:未发现|未观察到|没有|不存在)",
                after,
            ):
                return True
            if (
                re.search(
                    r".{0,8}(?:不是|并非|不属于).{0,8}"
                    r"(?:原因|根因|问题|影响|瓶颈|因素)",
                    after,
                )
                and not re.search(r"(?:不是唯一|并非无关)", clause)
            ):
                return True
    return False


_CATEGORY_MARKER_GROUPS = (
    ("binder", "同步 ipc"),
    ("sdk",),
    ("native", "dlopen", ".so"),
    ("jit", "baseline profile"),
    ("fork", "调度延迟"),
    ("首帧", "doframe", "compose", "measure"),
    ("system_server", "surfaceflinger", "系统层", "系统侧", "框架侧"),
)


def _category_is_uncertain(text: str, markers: tuple[str, ...]) -> bool:
    return any(
        any(marker in clause for marker in markers)
        and (
            re.search(
                r"(?:没有|无|缺少).{0,8}证据.{0,12}(?:表明|证明|支持|确认)",
                clause,
            )
            or re.search(r"(?:无法|不能|尚未|未能).{0,8}(?:确认|证明|判断)", clause)
            or re.search(
                r"(?:无法|不能|尚不能|难以|不足以|没有证据|无证据|"
                r"不代表(?:可以)?).{0,12}排除",
                clause,
            )
        )
        for clause in re.split(r"[，。；！？\n]", text)
    )


def _category_state(text: str, markers: tuple[str, ...]) -> str:
    if _category_is_uncertain(text, markers):
        return "uncertain"
    if _category_is_negated(text, markers):
        return "excluded"
    return "present"


def _finding_category_resolution(finding: Mapping[str, object]) -> str | None:
    text = _finding_text(finding)
    states = [
        _category_state(text, markers)
        for markers in _CATEGORY_MARKER_GROUPS
        if any(marker in text for marker in markers)
    ]
    if not states or "present" in states:
        return None
    return "uncertain" if "uncertain" in states else "excluded"


def _hypothesis_cause(finding: Mapping[str, object]) -> str | None:
    text = _finding_text(finding)
    scenario = str(finding.get("scenario_type", "other"))
    if finding.get("status") == "confirmed" and finding.get("confidence_ceiling") not in {
        "low",
        "none",
    }:
        return None
    if (
        scenario == "startup"
        and _category_state(text, ("jit", "baseline profile")) != "excluded"
        and ("jit" in text or "baseline profile" in text)
    ):
        return (
            "Trace 仅显示测试窗口内存在 JIT 编译活动，这与代码未充分预编译相符，"
            "但不足以证明 Baseline Profile 缺失。"
        )
    if (
        scenario == "startup"
        and _category_state(text, ("fork", "调度延迟")) != "excluded"
        and ("fork" in text or "调度延迟" in text)
    ):
        return (
            "Trace 显示其他进程活动与主线程调度等待出现在相近窗口；当前只有时间相关性，"
            "不能据此证明因果。"
        )
    if (
        scenario == "startup"
        and _category_state(text, ("native", "dlopen", ".so")) != "excluded"
        and ("native" in text or "dlopen" in text or ".so" in text)
    ):
        return (
            "Trace 显示启动阶段存在 Native 库加载；聚合口径可能包含嵌套、并行或跨窗口片段，"
            "不能把聚合值直接当作主线程阻塞时长。"
        )
    return "Trace 证据提示该机制可能影响测试场景，当前只有相关性，仍需复测。"


def _specific_cause(finding: Mapping[str, object]) -> str | None:
    text = _finding_text(finding)
    category = _primary_category(finding)
    category_resolution = _finding_category_resolution(finding)
    if category_resolution == "excluded":
        return "关联 Trace 证据已排除该候选机制，应继续核对当前场景中的其他证据。"
    low_confidence = (
        finding.get("status") != "confirmed"
        or finding.get("confidence_ceiling") in {"low", "none"}
    )
    if category_resolution == "uncertain" and not (
        low_confidence and category in {"native", "jit", "fork", "schedule"}
    ):
        return "当前 Trace 证据不足以确认或排除该候选机制，需要补充验证。"
    hypothesis = _hypothesis_cause(finding)
    if hypothesis is not None and category in {"native", "jit", "fork", "schedule"}:
        return hypothesis
    if (
        finding.get("scenario_type") == "startup"
        and not _category_is_negated(text, ("sdk",))
        and "sdk" in text
        and "qqmusicsdkadapter.doinit" in text
        and "xeaglebtadapter.start" in text
    ):
        return (
            "Trace 显示 QQMusicSdkAdapter.doInit 与 XeagleBtAdapter.start 都占用启动主线程；"
            "相关调用的生命周期入口必须分别定位，不能集中归入 Application.onCreate 或 bindApplication。"
        )
    if category == "sdk":
        primary = _primary_finding_text(finding)
        if "qqmusicsdkadapter.doinit" in primary:
            return (
                "Trace 显示 QQMusicSdkAdapter.doInit 占用冷启动主线程；"
                "具体生命周期入口仍需结合源码确认。"
            )
        if "xeaglebtadapter.start" in primary:
            return (
                "Trace 显示 XeagleBtAdapter.start 占用冷启动主线程；"
                "具体生命周期入口仍需结合源码确认。"
            )
        return "Trace 显示外部 SDK 初始化占用冷启动主线程；具体生命周期入口仍需结合源码确认。"
    needs_summary = _specific_problem(finding) is not None
    if needs_summary and category == "binder":
        return "冷启动主线程发起同步 Binder 调用并等待 system_server 返回，直接占用关键路径。"
    if needs_summary and category == "frame":
        return "首帧 doFrame 内同时出现 Compose 重组与 View 测量，主线程呈 CPU 密集工作。"
    if needs_summary and category == "lock":
        return "Trace 显示冷启动主线程存在 ClassLinker 与应用锁竞争；具体调用点仍需源码定位。"
    if category in {"native", "jit", "fork", "schedule"}:
        return _hypothesis_cause(finding)
    return hypothesis


def _specific_recommendation(finding: Mapping[str, object]) -> str:
    text = _finding_text(finding)
    primary_category = _primary_category(finding)
    scenario = str(finding.get("scenario_type", "other"))
    category_resolution = _finding_category_resolution(finding)
    if category_resolution == "excluded" or (
        category_resolution == "uncertain"
        and primary_category not in {"fork", "schedule"}
    ):
        return "依据关联 Trace 证据继续缩小问题范围，按相同场景复测后再调整对应实现。"
    if (
        any(marker in text for marker in ("system_server", "surfaceflinger"))
        and any(marker in text for marker in ("内部", "系统层", "系统侧", "应用不可直接"))
        and not _category_is_negated(
            text,
            ("system_server", "surfaceflinger", "系统层", "系统侧", "框架侧"),
        )
    ):
        return (
            "应用侧优先减少触发该系统路径的同步工作，并通过对照复测确认收益；"
            "系统进程内部锁不能作为应用代码已定位原因。"
        )
    low_confidence = (
        finding.get("status") != "confirmed"
        or finding.get("confidence_ceiling") in {"low", "none"}
    )
    if scenario == "startup" and primary_category in {"fork", "schedule"}:
        return (
            "固定后台负载并重复采集对照；现象稳定复现后，"
            "再调整测试期间的并发进程或服务。"
        )
    cautious_trace_category = any(
        _category_state(text, markers) == "present"
        and any(marker in text for marker in markers)
        for markers in (
            ("jit", "baseline profile"),
            ("native", "dlopen", ".so"),
            ("fork", "调度延迟"),
        )
    )
    if low_confidence and not cautious_trace_category:
        return (
            "先按相同场景补充对照证据并确认机制，再决定是否调整对应实现；"
            "当前结论不能直接作为改动依据。"
        )
    recommendation = _safe_chinese(finding.get("engine_recommendation"), "")
    if re.search(r"(?<![a-z])(?:self|wall|running|sleeping)(?![a-z])", recommendation, re.IGNORECASE):
        recommendation = ""
    if (
        not low_confidence
        and recommendation
        and not recommendation.startswith("排查并优化")
    ):
        return recommendation
    if (
        scenario == "scroll"
        and "binder" in text
        and _category_state(text, ("binder", "同步 ipc")) == "present"
    ):
        return (
            "将滑动回调中的同步 Binder 工作移出主线程关键帧，合并或缓存可复用结果，"
            "并复测滑动帧是否恢复稳定。"
        )
    if scenario == "scroll" and "sdk" in text and _category_state(text, ("sdk",)) == "present":
        return (
            "将外部 SDK 回调中的耗时工作移出滑动主线程，合并非关键回调，"
            "并复测滑动稳定性。"
        )
    if scenario == "memory_cycle":
        return (
            "依据关联 Trace 证据缩小内存增长范围，补充对象或分配证据后，"
            "再调整对应实现。"
        )
    if (
        scenario == "startup"
        and primary_category == "binder"
        and _category_state(text, ("binder", "同步 ipc")) == "present"
    ):
        return (
            "梳理冷启动主线程上的同步 Binder 调用；将非首屏必需调用延后，"
            "必须同步的结果采用合并或缓存，并复测等待是否下降。"
        )
    if scenario == "startup" and primary_category == "sdk":
        return (
            "将非首屏必需的外部 SDK 初始化延后；必须启动的初始化拆分执行，"
            "避免主线程串行等待。"
        )
    if scenario == "startup" and primary_category == "frame" and any(
        marker in text for marker in ("首帧", "doframe", "compose", "measure")
    ) and _category_state(text, ("首帧", "doframe", "compose", "measure")) == "present":
        return (
            "缩小首帧 Compose 重组与测量范围，延后非首屏组件创建，"
            "并复测首帧主线程工作量。"
        )
    if (
        scenario == "startup"
        and primary_category == "native"
        and _category_state(text, ("native", "dlopen", ".so")) == "present"
        and ("native" in text or "dlopen" in text or ".so" in text)
    ):
        return (
            "先确认首屏是否依赖相关 Native 库，只将非必需加载延后；"
            "聚合口径不能直接作为改动依据。"
        )
    if (
        scenario == "startup"
        and primary_category == "jit"
        and _category_state(text, ("jit", "baseline profile")) == "present"
        and ("jit" in text or "baseline profile" in text)
    ):
        return (
            "使用发布构建核验编译优化产物与 Baseline Profile 是否生效；"
            "证据充分后再补齐配置并对照复测。"
        )
    if (
        scenario == "startup"
        and primary_category in {"fork", "schedule"}
        and _category_state(text, ("fork", "调度延迟")) == "present"
        and ("fork" in text or "调度延迟" in text)
    ):
        return (
            "固定后台负载并重复采集对照；现象稳定复现后，"
            "再调整测试期间的并发进程或服务。"
        )
    return "依据关联 Trace 证据缩小问题范围，按相同场景复测后再调整对应实现。"


def _safe_chinese(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text or len(text) > 2000 or not _CJK.search(text):
        return fallback
    sanitized = _without_free_numbers(text)
    if (
        not sanitized
        or not _CJK.search(sanitized)
        or _contains_free_written_number(sanitized)
    ):
        return fallback
    return sanitized


def _claim_refs(
    finding: Mapping[str, object],
    critical_evidence_ids: frozenset[str],
    key_metric_ids: frozenset[str],
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    for metric_id in finding["metric_ids"]:  # type: ignore[union-attr]
        if metric_id not in key_metric_ids:
            continue
        claims.append(
            {
                "claim_type": "metric_observed",
                "metric_id": metric_id,
                "evidence_id": None,
            }
        )
    for evidence_id in finding["evidence_ids"]:  # type: ignore[union-attr]
        claims.append(
            {
                "claim_type": (
                    "evidence_on_critical_path"
                    if evidence_id in critical_evidence_ids
                    else "evidence_supports_mechanism"
                ),
                "metric_id": None,
                "evidence_id": evidence_id,
            }
        )
    return claims


def _conclusion(
    finding: Mapping[str, object],
    *,
    source_matched: bool,
    critical_evidence_ids: frozenset[str],
    key_metric_ids: frozenset[str],
) -> dict[str, object]:
    status = finding["status"]
    ceiling = finding["confidence_ceiling"]
    confirmed = status == "confirmed" and ceiling not in {"low", "none"}
    recommendation = _specific_recommendation(finding)
    specific_cause = _specific_cause(finding)
    return {
        "finding_id": finding["finding_id"],
        "evidence_ids": list(finding["evidence_ids"]),  # type: ignore[arg-type]
        "source_ref_ids": (
            list(finding["source_ref_ids"])  # type: ignore[arg-type]
            if source_matched
            else []
        ),
        "claim_refs": _claim_refs(finding, critical_evidence_ids, key_metric_ids),
        "problem": _safe_chinese(
            _specific_problem(finding) or finding.get("problem"),
            _safe_chinese(
                finding.get("title"), "检测到与该 Finding 对应的性能问题。"
            ),
        ),
        "cause": _safe_chinese(
            specific_cause or _cause_fragment(finding.get("root_cause")),
            (
                "SmartPerfetto 证据支持该机制与测试场景的关键路径相关。"
                if confirmed
                else "SmartPerfetto 证据提示该机制可能影响测试场景，当前仍需复测确认。"
            ),
        ),
        "source_root_cause": (
            "强匹配源码证据支持该问题与对应实现相关。"
            if source_matched and finding["source_ref_ids"]
            else "当前没有经过验证的匹配源码，只能判断 Trace 机制，不能定位代码根因。"
        ),
        "recommendation": f"{recommendation.rstrip('。')}；修改仅供参考。",
    }


def _key_metric_ids(
    findings: list[Mapping[str, object]], primary_ids: list[str]
) -> list[str]:
    primary = set(primary_ids)
    selected: list[str] = []
    for finding in findings:
        if finding["finding_id"] not in primary:
            continue
        for metric_id in finding["metric_ids"]:  # type: ignore[union-attr]
            if metric_id not in selected:
                selected.append(metric_id)
            if len(selected) == 3:
                return selected
    return selected


def _recommendations(
    conclusions: list[dict[str, object]],
    findings_by_id: Mapping[str, Mapping[str, object]],
    primary_ids: list[str],
) -> list[dict[str, object]]:
    conclusions_by_id = {item["finding_id"]: item for item in conclusions}
    result: list[dict[str, object]] = []
    used_priorities: set[str] = set()
    for finding_id in primary_ids:
        finding = findings_by_id[finding_id]
        priority = str(finding["priority"])
        if priority not in {"p0", "p1", "p2"} or priority in used_priorities:
            continue
        conclusion = conclusions_by_id[finding_id]
        result.append(
            {
                "priority": priority,
                "title": _safe_chinese(
                    finding.get("title"), "处理已识别的性能问题"
                ),
                "action": conclusion["recommendation"],
                "expected_effect": _safe_chinese(
                    finding.get("impact"),
                    "减少已识别机制对测试场景关键路径的影响。",
                ),
                "finding_ids": [finding_id],
                "evidence_ids": list(conclusion["evidence_ids"]),  # type: ignore[arg-type]
            }
        )
        used_priorities.add(priority)
    return sorted(result, key=lambda item: ("p0", "p1", "p2").index(str(item["priority"])))


def _retest_plan(
    retest_by_finding: Mapping[str, Mapping[str, object]],
    findings_by_id: Mapping[str, Mapping[str, object]],
    primary_ids: list[str],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for finding_id in primary_ids:
        retest = retest_by_finding.get(finding_id)
        finding = findings_by_id[finding_id]
        finding_metric_ids = set(finding["metric_ids"])  # type: ignore[arg-type]
        metric_ids = (
            [
                metric_id
                for metric_id in retest["metric_ids"]  # type: ignore[index,union-attr]
                if metric_id in finding_metric_ids
            ]
            if retest
            else []
        )
        if (
            not retest
            or not metric_ids
            or retest["scenario_type"] not in {"startup", "scroll", "memory_cycle"}
        ):
            continue
        problem = _safe_chinese(finding.get("title"), "已识别的性能问题").rstrip("。")
        item = {
            "mode": "verify_metric",
            "scenario_type": retest["scenario_type"],
            "metric_ids": metric_ids,
            "limitation_ids": [],
            "steps": f"针对{problem}，按相同应用、环境、测试场景和采集方式重新执行测试。",
            "success_condition": "improve_from_baseline",
            "failure_condition": "threshold_missed",
        }
        if item not in result:
            result.append(item)
        if len(result) == 3:
            break
    return result


def build_deterministic_finding_synthesis(
    projection: AIProjection,
) -> AISynthesisOutput:
    """Return one fail-closed, deterministic synthesis for projection 2.1."""

    document = projection.document
    if document.get("schema_version") != "2.1":
        raise ValueError("finding fallback input is invalid")
    workbench = document["workbench"]
    findings = list(workbench["findings"])
    findings_by_id = {item["finding_id"]: item for item in findings}
    primary_ids = list(workbench["primary_finding_ids"])
    key_metric_ids = _key_metric_ids(findings, primary_ids)
    critical_evidence_ids = frozenset(
        evidence_id
        for segment in workbench["critical_path"]
        for evidence_id in segment["evidence_ids"]
    )
    source_matched = document["capabilities"]["source"] == "matched"
    conclusions = [
        _conclusion(
            finding,
            source_matched=source_matched,
            critical_evidence_ids=critical_evidence_ids,
            key_metric_ids=frozenset(key_metric_ids),
        )
        for finding in findings
    ]
    conclusions_by_id = {item["finding_id"]: item for item in conclusions}
    retest_by_finding = {
        item["finding_id"]: item for item in workbench["retest_plans"]
    }
    candidate = {
        "schema_version": "2.1",
        "verdict": "分析完成",
        "executive_summary": "SmartPerfetto 证据已经整理为可执行的问题与复测建议。",
        "key_metric_ids": key_metric_ids,
        "conclusions": conclusions,
        "top_findings": [
            {
                "finding_id": finding_id,
                "evidence_ids": list(conclusions_by_id[finding_id]["evidence_ids"]),  # type: ignore[arg-type]
                "user_impact": _safe_chinese(
                    findings_by_id[finding_id].get("impact"),
                    "该问题影响当前测试场景的关键性能路径。",
                ),
            }
            for finding_id in primary_ids
        ],
        "recommendations": _recommendations(
            conclusions, findings_by_id, primary_ids
        ),
        "source_fixes": [],
        "retest_plan": _retest_plan(
            retest_by_finding, findings_by_id, primary_ids
        ),
        "limitations": [
            {
                "limitation_id": limitation["limitation_id"],
                "summary": "相关证据存在限制，结论已按可用证据范围收敛。",
            }
            for limitation in [
                *document["limitations"],
                *[
                    item
                    for scenario in document["scenarios"]
                    for item in scenario["limitations"]
                ],
            ][:20]
        ],
    }
    return validate_synthesis_output(projection=projection, candidate=candidate)


__all__ = ["build_deterministic_finding_synthesis"]
