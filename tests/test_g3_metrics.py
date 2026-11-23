"""G3 内测指标测试。

所有事件为 synthetic；全部输出写到 pytest tmp_path，不触碰仓库 var/。
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g3_metrics  # noqa: E402

SYNTHETIC = "synthetic"


def event_row(**overrides: str) -> dict[str, str]:
    row = {
        "household_id": "HH-01",
        "date": "2026-11-02",
        "active": "true",
        "danger_commands": "5",
        "confirm_started": "20",
        "confirm_completed": "17",
        "confirm_seconds": "18",
        "false_positive": "2",
        "unauthorized_dispatch": "0",
        "note": f"{SYNTHETIC} event",
        "synthetic": "true",
    }
    row.update(overrides)
    return row


def build_rows(
    *,
    households: int = 10,
    active_households: int = 6,
    days: int = 14,
    started: int = 100,
    completed: int = 80,
    false_positive: int = 15,
    seconds: float = 20.0,
    unauthorized: int = 0,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for household_index in range(1, households + 1):
        active = household_index <= active_households
        for day_index in range(days):
            day = date(2026, 11, 2) + timedelta(days=day_index)
            if active:
                rows.append(
                    event_row(
                        household_id=f"HH-{household_index:02d}",
                        date=day.isoformat(),
                        active="true",
                        confirm_started=str(started),
                        confirm_completed=str(completed),
                        confirm_seconds=str(seconds),
                        false_positive=str(false_positive),
                        unauthorized_dispatch=str(unauthorized),
                    )
                )
            else:
                rows.append(
                    event_row(
                        household_id=f"HH-{household_index:02d}",
                        date=day.isoformat(),
                        active="false",
                        danger_commands="0",
                        confirm_started="0",
                        confirm_completed="0",
                        confirm_seconds="0",
                        false_positive="0",
                        unauthorized_dispatch="0",
                    )
                )
    return rows


def analyze(rows: list[dict[str, str]], **kwargs: object) -> dict:
    events, issues, duplicates = g3_metrics.build_events(rows)
    assert not issues, [issue.as_text() for issue in issues]
    return g3_metrics.compute_metrics(
        events, duplicates_merged=duplicates, **kwargs
    )


def test_wau_six_of_ten_passes() -> None:
    summary = analyze(build_rows(active_households=6))
    assert summary["active_households"] == 6
    assert summary["wau_rate"] == pytest.approx(0.6)
    assert summary["criteria"]["wau"]["pass"] is True
    assert summary["g3_pass"] is True
    assert summary["g3_action"] == "go"


def test_wau_five_of_ten_fails() -> None:
    summary = analyze(build_rows(active_households=5))
    assert summary["wau_rate"] == pytest.approx(0.5)
    assert summary["criteria"]["wau"]["pass"] is False
    assert summary["g3_pass"] is False


def test_confirm_completion_80_percent_passes() -> None:
    summary = analyze(build_rows(started=100, completed=80))
    assert summary["confirm_completion_rate"] == pytest.approx(0.8)
    assert summary["criteria"]["confirm_completion"]["pass"] is True


def test_confirm_completion_79_percent_fails() -> None:
    summary = analyze(build_rows(started=100, completed=79))
    assert summary["confirm_completion_rate"] == pytest.approx(0.79)
    assert summary["criteria"]["confirm_completion"]["pass"] is False
    assert summary["g3_pass"] is False


def test_false_positive_15_percent_passes() -> None:
    summary = analyze(build_rows(started=100, false_positive=15))
    assert summary["false_positive_rate"] == pytest.approx(0.15)
    assert summary["criteria"]["false_positive"]["pass"] is True


def test_false_positive_16_percent_fails() -> None:
    summary = analyze(build_rows(started=100, false_positive=16))
    assert summary["false_positive_rate"] == pytest.approx(0.16)
    assert summary["criteria"]["false_positive"]["pass"] is False
    assert summary["g3_pass"] is False


def test_single_unauthorized_dispatch_fails_overall() -> None:
    rows = build_rows()
    rows[0]["unauthorized_dispatch"] = "1"
    summary = analyze(rows)
    assert summary["unauthorized_dispatch_total"] == 1
    assert summary["criteria"]["unauthorized_dispatch"]["pass"] is False
    assert summary["g3_pass"] is False


def test_confirm_p90_20_seconds_passes() -> None:
    summary = analyze(build_rows(seconds=20.0))
    assert summary["confirm_p90_seconds"] == pytest.approx(20.0)
    assert summary["criteria"]["confirm_p90"]["pass"] is True


def test_confirm_p90_21_seconds_fails() -> None:
    summary = analyze(build_rows(seconds=21.0))
    assert summary["confirm_p90_seconds"] == pytest.approx(21.0)
    assert summary["criteria"]["confirm_p90"]["pass"] is False
    assert summary["g3_pass"] is False


def test_p90_nearest_rank_is_reproducible() -> None:
    values = [10, 11, 12, 13, 14, 15, 16, 17, 19, 100]
    # nearest-rank: n=10 → ceil(9) = 第 9 个 → 19；插值法会得到 27.1。
    assert g3_metrics.percentile_nearest_rank(values, 0.9) == 19
    assert g3_metrics.percentile_nearest_rank([], 0.9) is None
    assert g3_metrics.percentile_nearest_rank([7], 0.9) == 7


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        (
            {"confirm_started": "10", "confirm_completed": "11"},
            "confirm_completed",
        ),
        (
            {"confirm_started": "10", "false_positive": "11"},
            "false_positive",
        ),
        (
            {"unauthorized_dispatch": "-1"},
            "unauthorized_dispatch",
        ),
        (
            {"confirm_seconds": "-1"},
            "confirm_seconds",
        ),
        (
            {"active": "maybe"},
            "active",
        ),
        (
            {"date": "2026/11/02"},
            "date",
        ),
    ],
)
def test_invalid_rows_are_rejected(
    overrides: dict[str, str], field: str
) -> None:
    _, issues, _ = g3_metrics.build_events([event_row(**overrides)])
    assert any(issue.field == field for issue in issues), [
        issue.as_text() for issue in issues
    ]


def test_duplicate_household_day_is_merged() -> None:
    rows = [
        event_row(
            date="2026-11-02",
            confirm_started="1",
            confirm_completed="1",
            confirm_seconds="10",
            false_positive="0",
        ),
        event_row(
            date="2026-11-02",
            confirm_started="1",
            confirm_completed="1",
            confirm_seconds="20",
            false_positive="1",
        ),
    ]
    events, issues, duplicates = g3_metrics.build_events(rows)
    assert issues == []
    assert duplicates == 1
    assert len(events) == 1
    event = events[0]
    assert event.confirm_started == 2
    assert event.confirm_completed == 2
    assert event.confirm_seconds == [10.0, 20.0]
    assert event.false_positive == 1

    summary = g3_metrics.compute_metrics(
        events, cohort_size=1, weeks=1, duplicates_merged=duplicates
    )
    assert summary["confirm_started"] == 2
    assert summary["confirm_samples"] == 2


def test_missing_metrics_are_not_counted_as_pass() -> None:
    rows = [
        event_row(
            confirm_started="0",
            confirm_completed="0",
            false_positive="0",
            confirm_seconds="0",
        )
    ]
    summary = analyze(rows, cohort_size=1, weeks=1)
    assert summary["confirm_completion_rate"] is None
    assert summary["false_positive_rate"] is None
    assert summary["confirm_p90_seconds"] is None
    for key in ("confirm_completion", "false_positive", "confirm_p90"):
        assert summary["criteria"][key]["status"] == "未采集"
        assert summary["criteria"][key]["pass"] is False
    assert summary["g3_pass"] is False


def test_second_attempt_failure_goes_b2b() -> None:
    summary = analyze(build_rows(active_households=5), attempt=2)
    assert summary["g3_pass"] is False
    assert summary["g3_action"] == "转 B2B 或暂停"


def test_incomplete_sample_blocks_go() -> None:
    summary = analyze(build_rows(days=3))
    assert summary["sample_complete"] is False
    assert summary["g3_action"] == "修 2 周复测"


def test_demo_artifacts_numbers_and_synthetic_markings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert g3_metrics.main(["--demo", "--out-dir", str(tmp_path)]) == 0
    out_dir = tmp_path / "demo" / "out"
    for name in ("events.csv", "summary.json", "g3_report.md", "dashboard.html"):
        assert (out_dir / name).is_file(), f"缺少 demo/out/{name}"

    events_text = (out_dir / "events.csv").read_text(encoding="utf-8-sig")
    assert "SYNTHETIC DEMO" in events_text

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is True
    assert summary["observed_households"] == 10
    assert summary["observed_days"] == 14
    assert summary["sample_complete"] is True
    assert summary["active_households"] == 7
    assert summary["wau_rate"] == pytest.approx(0.7)
    assert summary["confirm_completion_rate"] == pytest.approx(0.85)
    assert summary["false_positive_rate"] == pytest.approx(0.1)
    assert summary["unauthorized_dispatch_total"] == 0
    assert summary["confirm_p90_seconds"] == pytest.approx(18.0)
    assert summary["confirm_samples"] == 98
    assert summary["g3_pass"] is True
    assert summary["g3_action"] == "go"
    assert len(summary["by_week"]) == 2
    assert summary["by_household"]["SYN-HH-01"]["confirm_p90_seconds"] == pytest.approx(
        18.0
    )
    assert summary["by_household"]["SYN-HH-08"]["confirm_p90_seconds"] is None

    report = (out_dir / "g3_report.md").read_text(encoding="utf-8")
    assert report.startswith("> ⚠️ **SYNTHETIC DEMO")
    for marker in ("70.0%", "85.0%", "10.0%", "18.0s", "G3 结论"):
        assert marker in report

    page = (out_dir / "dashboard.html").read_text(encoding="utf-8")
    for marker in (
        "SYNTHETIC",
        "不是真实内测数据",
        "70.0%",
        "85.0%",
        "10.0%",
        "18.0s",
        "周活",
        "确认完成率",
        "误报率",
        "越权下发",
        "确认 P90",
    ):
        assert marker in page, f"dashboard 缺少关键数字/标注：{marker}"
    assert "已验证" not in page
    assert "<style>" in page
    assert "<script src" not in page
    assert "https://" not in page
    assert "http://" not in page

    output = capsys.readouterr().out
    assert "SYNTHETIC DEMO" in output
    assert "P90 18.0s" in output


def test_cli_csv_creates_all_outputs(tmp_path: Path) -> None:
    events_path = tmp_path / "events.csv"
    with events_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=g3_metrics.EVENT_FIELDS)
        writer.writeheader()
        writer.writerows(build_rows())

    out_dir = tmp_path / "out"
    assert (
        g3_metrics.main(["--events", str(events_path), "--out-dir", str(out_dir)])
        == 0
    )
    for name in ("summary.json", "g3_report.md", "dashboard.html"):
        assert (out_dir / name).is_file()
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is True
    assert summary["g3_pass"] is True


def test_cli_invalid_csv_returns_one(tmp_path: Path) -> None:
    events_path = tmp_path / "events.csv"
    with events_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=g3_metrics.EVENT_FIELDS)
        writer.writeheader()
        writer.writerow(
            event_row(confirm_started="10", confirm_completed="11")
        )

    out_dir = tmp_path / "out"
    assert (
        g3_metrics.main(["--events", str(events_path), "--out-dir", str(out_dir)])
        == 1
    )
    assert not (out_dir / "summary.json").exists()


def test_cli_missing_events_returns_two(tmp_path: Path) -> None:
    assert (
        g3_metrics.main(
            ["--events", str(tmp_path / "missing.csv"), "--out-dir", str(tmp_path)]
        )
        == 2
    )


def test_cli_invalid_cohort_size_returns_two(tmp_path: Path) -> None:
    assert (
        g3_metrics.main(
            ["--demo", "--out-dir", str(tmp_path), "--cohort-size", "0"]
        )
        == 2
    )


def test_from_audit_is_explicitly_unimplemented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        g3_metrics.main(
            ["--from-audit", str(tmp_path / "decisions.db")]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "未实现" in error
    assert "仅" in error or "CSV" in error
    assert not (tmp_path / "demo").exists()