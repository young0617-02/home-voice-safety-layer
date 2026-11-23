"""G1 一键流水线测试。

--demo 使用脚本内置的 synthetic 数据；所有输出写到 pytest tmp_path，
不触碰仓库 var/。测试只验证流水线编排与页面格式，不验证任何真实用户结论。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g1_pipeline  # noqa: E402

DEMO_INPUTS = ("responses.csv", "interviews.csv")
DEMO_OUTPUTS = ("screened.csv", "summary.json", "g1_report.md", "dashboard.html")


def run_demo(tmp_path: Path) -> tuple[Path, Path]:
    assert g1_pipeline.main(["--demo", "--out-dir", str(tmp_path)]) == 0
    return tmp_path / "demo" / "in", tmp_path / "demo" / "out"


def test_demo_pipeline_creates_all_artifacts(tmp_path: Path) -> None:
    demo_in, demo_out = run_demo(tmp_path)
    for name in DEMO_INPUTS:
        assert (demo_in / name).is_file(), f"缺少 demo/in/{name}"
    for name in DEMO_OUTPUTS:
        assert (demo_out / name).is_file(), f"缺少 demo/out/{name}"
    for path in [*(demo_in / name for name in DEMO_INPUTS), *(demo_out / name for name in DEMO_OUTPUTS)]:
        assert path.stat().st_size > 0


def test_demo_inputs_and_outputs_are_marked_synthetic(tmp_path: Path) -> None:
    demo_in, demo_out = run_demo(tmp_path)

    for name in DEMO_INPUTS:
        text = (demo_in / name).read_text(encoding="utf-8-sig")
        assert "SYNTHETIC DEMO" in text
        assert "不是真实用户证据" in text

    screened_text = (demo_out / "screened.csv").read_text(encoding="utf-8-sig")
    assert "SYNTHETIC DEMO" in screened_text

    summary = json.loads((demo_out / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is True
    assert "SYNTHETIC DEMO" in summary["notice"]

    report = (demo_out / "g1_report.md").read_text(encoding="utf-8")
    assert report.startswith("> ⚠️ **SYNTHETIC DEMO")
    assert "不是真实用户证据" in report
    assert "12/20" in report


def test_demo_summary_numbers(tmp_path: Path) -> None:
    _, demo_out = run_demo(tmp_path)
    summary = json.loads((demo_out / "summary.json").read_text(encoding="utf-8"))

    assert summary["total"] == 30
    assert summary["qualified"] == 20
    assert summary["pending"] == 6
    assert summary["rejected"] == 4
    assert summary["by_group"] == {"老人": 8, "儿童": 6, "纯危险设备": 6}
    assert summary["by_plan"] == {"A": 6, "B": 7, "C": 7}
    assert summary["plan_target"] == {"A": 6, "B": 7, "C": 7}
    assert summary["first_tier_count"] == 12
    assert summary["first_tier_limit"] == 12

    with (demo_out / "screened.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert len(rows) == 30
    assert "verdict" in reader.fieldnames
    assert rows[0]["verdict"] == "合格"
    assert rows[0]["plan"] == "A"


def test_dashboard_key_numbers_and_self_contained(tmp_path: Path) -> None:
    _, demo_out = run_demo(tmp_path)
    page = (demo_out / "dashboard.html").read_text(encoding="utf-8")

    for marker in (
        "SYNTHETIC",
        "不是真实用户证据",
        "12/20",
        "6/7/7",
        "A 6/6",
        "B 7/7",
        "C 7/7",
        "8/20",
        "11.1",
        "12/12",
        "老人 8/8",
        "儿童 6/6",
        "纯危险设备 6/6",
        "P13",
    ):
        assert marker in page, f"dashboard 缺少关键数字/标注：{marker}"

    # 合成数据不得写成已验证；页面必须单文件、无外部资源。
    assert "已验证" not in page
    assert "<style>" in page
    assert "<script src" not in page
    assert "https://" not in page
    assert "http://" not in page


def test_demo_stdout_summary_mentions_synthetic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_demo(tmp_path)
    output = capsys.readouterr().out
    assert "SYNTHETIC DEMO" in output
    assert "合格 20" in output
    assert "12/20" in output
    assert "6/7/7" in output


def test_non_demo_pipeline_uses_supplied_csv(tmp_path: Path) -> None:
    demo_in, _ = run_demo(tmp_path / "demo_root")
    out_dir = tmp_path / "custom_out"

    assert g1_pipeline.main(
        [
            "--responses",
            str(demo_in / "responses.csv"),
            "--interviews",
            str(demo_in / "interviews.csv"),
            "--out-dir",
            str(out_dir),
        ]
    ) == 0

    assert (out_dir / "dashboard.html").is_file()
    assert (out_dir / "g1_report.md").is_file()
    page = (out_dir / "dashboard.html").read_text(encoding="utf-8")
    assert "SYNTHETIC DEMO" not in page
    assert "已验证" not in page
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is False


def test_missing_inputs_fail(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        g1_pipeline.main(
            [
                "--responses",
                str(tmp_path / "missing-responses.csv"),
                "--interviews",
                str(tmp_path / "missing-interviews.csv"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )