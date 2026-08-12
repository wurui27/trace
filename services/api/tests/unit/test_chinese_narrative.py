from __future__ import annotations

import pytest

from perfpilot_api.ai.chinese_narrative import (
    ChineseNarrativeError,
    validate_simplified_chinese_narrative,
)


def _document(summary: str) -> dict[str, object]:
    return {
        "verdict": summary,
        "executive_summary": summary,
        "top_findings": [{"user_impact": "用户会更晚看到首屏。", "finding_id": "technical-id"}],
        "recommendations": [{
            "title": "延迟初始化",
            "action": "把 MainActivity.onCreate 的 SQLite 初始化移到后台线程。",
            "expected_effect": "缩短 TTID。",
        }],
        "source_fixes": [{
            "diagnosis": "MainActivity.onCreate 包含同步读取。",
            "retest_target": "再次采集 Perfetto Trace 并比较 TTID。",
            "relative_path": "app/src/MainActivity.kt",
            "diff": "diff --git a/MainActivity.kt b/MainActivity.kt\n",
        }],
        "retest_plan": [{"steps": "重复五次冷启动。", "metric_ids": ["startup.ttid_ms"]}],
        "limitations": [{"summary": "当前 Trace 不含网络耗时。"}],
    }


def test_chinese_narrative_accepts_chinese_with_technical_terms() -> None:
    validate_simplified_chinese_narrative(
        _document("主线程在启动阶段连续执行磁盘读取，直接拉长 TTID。")
    )


def test_chinese_narrative_rejects_english_user_facing_paragraph_without_echo() -> None:
    text = "The main thread is blocked by synchronous disk reads."
    with pytest.raises(ChineseNarrativeError, match="^ai_narrative_language_invalid$") as error:
        validate_simplified_chinese_narrative(_document(text))
    assert text not in str(error.value)


def test_validator_ignores_paths_ids_metrics_code_and_diff() -> None:
    validate_simplified_chinese_narrative(_document("启动阶段存在同步磁盘读取。"))
