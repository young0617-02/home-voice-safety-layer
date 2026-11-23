"""`scripts/asr_cer.py` 的纯函数与 CLI 契约测试。

不加载模型、不碰 GPU：归一化 / 编辑距离 / CER 都是纯计算；CLI 只测参数契约。
需要 opencc 的用例在缺依赖时跳过（本机 asr extras 已装，CI 轻量环境不会红）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import asr_cer  # noqa: E402

SCRIPT = ROOT / "scripts" / "asr_cer.py"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


# ------------------------------------------------------------------ 归一化


def test_normalize_drops_spaces_and_chinese_punctuation():
    assert asr_cer.normalize_for_cer("打开，客厅 的灯！") == "打开客厅的灯"
    assert asr_cer.normalize_for_cer("锁上大门。") == "锁上大门"


def test_normalize_folds_fullwidth_and_ascii_case():
    assert asr_cer.normalize_for_cer("ＡＢＣ １２３，！") == "abc123"
    assert asr_cer.normalize_for_cer("ABC 123") == "abc123"


def test_normalize_keeps_cjk_and_digits_only():
    assert asr_cer.normalize_for_cer("调到 26 度%") == "调到26度"
    assert asr_cer.normalize_for_cer("音量+3-2") == "音量32"


def test_normalize_traditional_to_simplified():
    pytest.importorskip("opencc")
    assert asr_cer.normalize_for_cer("打開客廳的燈") == asr_cer.normalize_for_cer("打开客厅的灯")


def test_normalize_is_symmetric_for_ref_and_hyp():
    """ref 与 hyp 共用同一函数：只有标点/空格差异时归一化后必须相等。"""
    ref = "把客厅大灯调到百分之五十。"
    hyp = "把 客厅大灯，调到百分之五十！"
    assert asr_cer.normalize_for_cer(ref) == asr_cer.normalize_for_cer(hyp)


# ------------------------------------------------------------------ 编辑距离


def test_edit_distance_identical_is_zero():
    assert asr_cer.edit_distance("客厅灯", "客厅灯") == (0, 0, 0)


def test_edit_distance_counts_replace_delete_insert():
    assert asr_cer.edit_distance("客厅灯", "客厅等") == (1, 0, 0)
    assert asr_cer.edit_distance("打开客厅", "打开厅") == (0, 1, 0)
    assert asr_cer.edit_distance("打开客厅", "打开客厅的") == (0, 0, 1)


# ------------------------------------------------------------------ CER


def test_cer_identical_text_is_zero():
    result = asr_cer.cer("打开客厅的灯", "打开客厅的灯")
    assert result["cer"] == 0.0
    assert result["ref_chars"] == 6
    assert (result["subs"], result["dels"], result["ins"]) == (0, 0, 0)


def test_cer_single_substitution_is_one_over_n():
    result = asr_cer.cer("客厅灯", "客厅等")  # 3 字，1 处替换
    assert result["subs"] == 1
    assert result["dels"] == 0
    assert result["ins"] == 0
    assert result["cer"] == pytest.approx(1 / 3, abs=1e-4)


def test_cer_single_deletion_and_insertion():
    deleted = asr_cer.cer("打开客厅", "打开厅")
    inserted = asr_cer.cer("打开客厅", "打开客厅的")
    assert (deleted["dels"], deleted["subs"], deleted["ins"]) == (1, 0, 0)
    assert deleted["cer"] == pytest.approx(0.25)
    assert (inserted["ins"], inserted["subs"], inserted["dels"]) == (1, 0, 0)
    assert inserted["cer"] == pytest.approx(0.25)


def test_cer_empty_reference_does_not_divide_by_zero():
    result = asr_cer.cer("", "打开客厅")
    assert result["cer"] is None
    assert result["reason"] == "empty_reference"
    assert result["ref_chars"] == 0
    # 全是标点/空白，归一化后同样为空
    assert asr_cer.cer("，。！ ", "任意")["reason"] == "empty_reference"


# ------------------------------------------------------------------ CLI 契约


def test_cli_help_is_available():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--model" in result.stdout
    assert "--osr" in result.stdout
    assert "--refs" in result.stdout


def test_cli_osr_without_refs_exits_2_and_warns():
    result = run_cli("--osr")
    assert result.returncode == 2
    assert "人工核对" in result.stderr
    assert "循环论证" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_osr_refs_missing_wav_exits_2(tmp_path: Path):
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps({"does_not_exist.wav": "参照文本"}, ensure_ascii=False), encoding="utf-8")
    result = run_cli("--osr", "--refs", str(refs), "--osr-dir", str(tmp_path))
    assert result.returncode == 2
    assert "缺少音频文件" in result.stderr
    assert "does_not_exist.wav" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_osr_empty_refs_file_exits_2(tmp_path: Path):
    wav = tmp_path / "sample.wav"
    wav.write_bytes(b"RIFF")
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps({"sample.wav": "   "}, ensure_ascii=False), encoding="utf-8")
    result = run_cli("--osr", "--refs", str(refs), "--osr-dir", str(tmp_path))
    assert result.returncode == 2
    assert "参照文本为空" in result.stderr


def test_cli_tts_and_osr_are_mutually_exclusive():
    result = run_cli("--tts", "--osr")
    assert result.returncode == 2
    assert "not allowed" in result.stderr.lower()


def test_tts_cases_are_the_documented_ten():
    """内置 10 条短句必须写死在脚本里，报告口径 n=10 才能复算。"""
    assert len(asr_cer.TTS_CASES) == 10
    texts = [text for _, text in asr_cer.TTS_CASES]
    assert len(set(texts)) == 10
    assert "打开客厅的灯" in texts
    assert "把空调温度调到二十六度" in texts

# ------------------------------------------------------------------ 数字等价归一化


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("二十三", "23"),
        ("五十", "50"),
        ("十五", "15"),
        ("一百", "100"),
        ("两百", "200"),
        ("二十六", "26"),
        ("三十", "30"),
        ("二十六点五", "26.5"),
        ("百分之五十", "50%"),
        ("百分之百", "100%"),
        ("把空调温度调到二十六度", "把空调温度调到26度"),
        ("已经是26度", "已经是26度"),
    ],
)
def test_normalize_numbers_covers_chinese_numerals(text: str, expected: str):
    assert asr_cer.normalize_numbers(text) == expected


def test_number_normalization_is_symmetric_for_ref_and_hyp():
    """中文数字与阿拉伯数字写同一件事时，归一化后必须等同（`一下` 误伤同向发生）。"""
    assert asr_cer.normalize_for_cer("一下", numbers=True) == "1下"
    assert asr_cer.cer("一下", "1下")["cer_norm"] == 0.0
    assert asr_cer.cer("先等一下", "先等一下")["cer_norm"] == 0.0
    assert asr_cer.normalize_for_cer("一下", numbers=True) == asr_cer.normalize_for_cer("1下", numbers=True)


@pytest.mark.parametrize(
    ("ref", "hyp"),
    [
        ("把音量调到三十", "把音量调到30"),
        ("把空调温度调到二十六度", "把空调温度调到26度"),
        ("把客厅大灯调到百分之五十", "把客厅大灯调到50%"),
    ],
)
def test_cer_norm_is_not_worse_than_cer_raw(ref: str, hyp: str):
    """数字写法差异必须在归一化后消失；原样口径保留这份差异。"""
    result = asr_cer.cer(ref, hyp)
    assert result["cer_raw"] > 0
    assert result["cer_norm"] == 0.0
    assert result["cer_norm"] <= result["cer_raw"]


def test_cer_reports_both_metrics_and_both_sdi():
    result = asr_cer.cer("锁上大门", "索上大门")
    for key in (
        "cer_raw",
        "cer_norm",
        "subs_raw",
        "dels_raw",
        "ins_raw",
        "ref_chars_raw",
        "subs_norm",
        "dels_norm",
        "ins_norm",
        "ref_chars_norm",
    ):
        assert key in result
    # 纯声学错字：两个口径一致
    assert result["cer_norm"] == result["cer_raw"]
    assert result["subs_norm"] == 1


def test_cer_norm_keeps_empty_reference_behaviour():
    result = asr_cer.cer("", "五十")
    assert result["cer_raw"] is None
    assert result["cer_norm"] is None
    assert result["reason"] == "empty_reference"