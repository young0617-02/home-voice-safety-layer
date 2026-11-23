"""scripts/demo_showcase.py 的端到端测试：真实引擎 + simulated 后端 + 临时目录。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_showcase  # noqa: E402
from hvsl.audit import AUDIT_CSV_COLUMNS  # noqa: E402
from hvsl.catalog import load_entities  # noqa: E402

#: confirmation token 是 secrets.token_hex(5) -> 10 位十六进制。
HEX10 = re.compile(r"(?<![0-9a-f])[0-9a-f]{10}(?![0-9a-f])")


@pytest.fixture()
def demo(tmp_path):
    """完整跑一次演示；临时目录由脚本自己清理。"""
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    return demo_showcase.run_demo(temp_root=temp_parent), temp_parent


@pytest.fixture()
def retained(tmp_path):
    """审计导出到保留目录，便于检查 CSV/JSON 内容。"""
    temp_parent = tmp_path / "tmp"
    temp_parent.mkdir()
    audit_dir = tmp_path / "retained-audit"
    result = demo_showcase.run_demo(temp_root=temp_parent, audit_dir=audit_dir)
    return result, audit_dir, temp_parent


def _steps(result) -> dict[int, dict]:
    return {step["index"]: step for step in result.summary["steps"]}


# ------------------------------------------------------------------ 5 步结果


def test_live_demo_reports_five_passing_steps(demo):
    result, _ = demo
    steps = result.summary["steps"]

    assert [s["index"] for s in steps] == [1, 2, 3, 4, 5]
    assert all(s["passed"] for s in steps), [s["failures"] for s in steps]
    assert result.summary["passed"] is True
    assert result.summary["mode"] == "live"
    assert result.summary["backend"] == "simulated"
    assert "不是 Home Assistant 真机证据" in result.summary["boundary"]


def test_low_risk_executes_exactly_once(demo):
    result, _ = demo
    step = _steps(result)[1]

    assert step["kind"] == "executed"
    assert step["dispatch_count"] == 1
    assert step["outcomes"] == [
        {
            "tool": "hvs.light.turn_on",
            "targets": ["light.living_room_main"],
            "state": "executed",
            "dispatched": True,
        }
    ]


def test_danger_is_parked_with_zero_dispatch(demo):
    result, _ = demo
    step = _steps(result)[2]

    assert step["kind"] == "awaiting_confirmation"
    assert step["dispatch_count"] == 0
    assert step["outcomes"] == []
    assert result.confirmation_token


def test_confirm_dispatches_exactly_once(demo):
    result, _ = demo
    step = _steps(result)[3]

    assert step["kind"] == "executed"
    assert step["dispatch_count"] == 1
    assert [o["tool"] for o in step["outcomes"]] == ["hvs.lock.unlock"]
    assert [o["targets"] for o in step["outcomes"]] == [["lock.front_door"]]


def test_repeated_confirm_never_redispatches(demo):
    result, _ = demo
    step = _steps(result)[4]

    assert step["kind"] in {"rejected", "deduplicated"}
    assert step["dispatch_count"] == 0
    assert all(not outcome["dispatched"] for outcome in step["outcomes"])
    # 只有第 1 步（灯）和第 3 步（门）两次真实下发。
    assert result.summary["backend_dispatches"] == 2


# -------------------------------------------------------------------- 令牌


def test_token_never_appears_in_spoken_message_or_summary(demo):
    result, _ = demo
    token = result.confirmation_token

    assert token
    for step in result.summary["steps"]:
        assert token not in step["spoken_message"]
        assert token not in step["message"]
    assert token not in json.dumps(result.summary, ensure_ascii=False)
    assert "confirmation_token" not in json.dumps(result.summary, ensure_ascii=False)


def test_token_never_appears_in_audit_files(retained):
    result, audit_dir, _ = retained
    token = result.confirmation_token

    assert token
    for name in ("audit.csv", "audit.json"):
        text = (audit_dir / name).read_text(encoding="utf-8")
        assert token not in text
        assert token not in text.lower()


# -------------------------------------------------------------------- 审计


def test_audit_export_has_three_key_decisions(retained):
    result, audit_dir, _ = retained
    audit = result.summary["audit"]

    assert audit["key_decisions"] == 3
    assert audit["record_count"] >= 3
    assert audit["token_leaked"] is False

    csv_lines = (audit_dir / "audit.csv").read_text(encoding="utf-8").splitlines()
    assert csv_lines[0] == ",".join(AUDIT_CSV_COLUMNS)
    assert len(csv_lines) == audit["record_count"] + 1

    payload = json.loads((audit_dir / "audit.json").read_text(encoding="utf-8"))
    assert len(payload) == audit["record_count"]
    assert audit["json_record_count"] == audit["record_count"]

    # 关键决策可逐条核对：两次已执行下发 + 一次待确认挂起。
    executed = [row for row in payload if row["stage"] == "dispatch" and row["dispatch_state"] == "executed"]
    parked = [row for row in payload if row["stage"] == "confirm" and row["confirmation_state"] == "pending"]
    assert len(executed) == 2
    assert len(parked) == 1


# ------------------------------------------------------------------ 目录清理


def test_temp_dir_is_cleaned_up_after_live_run(demo):
    result, temp_parent = demo

    assert list(temp_parent.iterdir()) == []
    assert result.export_dir is not None
    assert not result.export_dir.exists()


def test_audit_dir_retains_files_while_temp_is_cleaned(retained):
    _, audit_dir, temp_parent = retained

    assert (audit_dir / "audit.csv").is_file()
    assert (audit_dir / "audit.json").is_file()
    assert list(temp_parent.iterdir()) == []


# ------------------------------------------------------------------ dry-run


def test_dry_run_creates_no_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert demo_showcase.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "[1/5]" in output and "[5/5]" in output
    assert list(tmp_path.iterdir()) == []


def test_dry_run_does_not_build_engine(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("dry-run 不应构建引擎")

    monkeypatch.setattr(demo_showcase, "build_engine", _boom)
    assert demo_showcase.main(["--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["writes_files"] is False
    assert len(payload["steps"]) == 5
    assert list(tmp_path.iterdir()) == []


# -------------------------------------------------------------------- 退出码


def test_exit_code_success_and_argument_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert demo_showcase.main(["--json"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        demo_showcase.main(["--unknown-flag"])
    assert excinfo.value.code == 2

    not_a_dir = tmp_path / "not-a-dir.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    assert demo_showcase.main(["--audit-dir", str(not_a_dir)]) == 2


def test_json_output_is_token_free(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert demo_showcase.main(["--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert len(payload["steps"]) == 5
    assert payload["passed"] is True
    assert "confirmation_token" not in output
    assert not HEX10.search(output)


# ------------------------------------------------------------ 演示指令假设


def test_low_risk_utterance_resolves_to_a_single_catalog_entity():
    """第 1 步必须唯一可解析；若 catalog 改动导致歧义，本测试先失败。"""
    entities = load_entities(ROOT / "devices" / "devices.json")
    hits = [
        entity.entity_id
        for entity in entities
        if any(
            word and word in demo_showcase.LOW_RISK_UTTERANCE
            for word in [entity.display_name, *entity.aliases]
        )
    ]

    assert hits == ["light.living_room_main"]