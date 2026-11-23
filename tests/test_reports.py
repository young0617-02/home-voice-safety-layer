"""报告必须由 JSON 产物渲染，不允许手改数字（与 inventory 同一套防漂移思路）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import write_reports  # noqa: E402


@pytest.mark.parametrize("name", ["评测结果.json", "变异测试.json"])
def test_required_artifacts_exist(name: str):
    assert (ROOT / "reports" / name).is_file(), f"缺少 reports/{name}，请先跑对应命令"


def test_markdown_reports_match_their_json_source(tmp_path, monkeypatch):
    names = [
        name
        for name in ("评测结果.md", "评测结果-llm.md", "变异测试.md", "语音链路分段实测.md")
        if (ROOT / "reports" / name).is_file()
    ]
    if not names:
        pytest.skip("reports/*.md 尚未生成")

    # 渲染到 tmp_path：验证同一份 JSON 能重现仓库里的 md，但不改写工作树。
    # （旧版直接调用 main() 写 reports/，跑完 evals/judge.py 后会让工作树变脏。）
    monkeypatch.setattr(write_reports, "OUT", tmp_path)
    write_reports.main([])

    for name in names:
        rendered = tmp_path / name
        assert rendered.is_file(), f"{name} 未被渲染"
        assert rendered.read_text(encoding="utf-8") == (ROOT / "reports" / name).read_text(encoding="utf-8"), (
            f"reports/{name} 与 JSON 产物不一致：请运行 python scripts/write_reports.py 而不是手改"
        )


def test_eval_report_states_the_three_state_caveat():
    text = (ROOT / "reports" / "评测结果.md").read_text(encoding="utf-8")
    for marker in ("实测", "未实测", "设计值"):
        assert marker in text
    assert "越权下发" in text, "报告必须把红线指标（越权下发）与能力缺口分开写"
