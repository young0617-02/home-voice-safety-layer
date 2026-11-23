"""G4 留存/转化报告测试。

所有输入均为 synthetic；全部输出写到 pytest tmp_path，不触碰仓库 var/。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g4_report  # noqa: E402

SYNTHETIC_NOTICE = g4_report.SYNTHETIC_NOTICE


def make_g3(
    active: tuple[int, ...] = (7, 7, 6, 6),
    cohort: int = 10,
    observed: int = 10,
) -> dict:
    start = date(2026, 11, 2)
    by_week = [
        {
            "week_start": (start + timedelta(days=7 * index)).isoformat(),
            "iso_week": f"2026-W{45 + index:02d}",
            "households": cohort,
            "active_households": count,
            "wau_rate": round(count / cohort, 4),
        }
        for index, count in enumerate(active)
    ]
    return {
        "synthetic_demo": True,
        "notice": SYNTHETIC_NOTICE,
        "cohort_size": cohort,
        "observed_households": observed,
        "by_week": by_week,
        "g3_pass": True,
    }


def make_g2(
    paid: int = 4,
    refund: float = 0.0,
    refund_by_order: float | None = None,
    orders_total: int = 20,
    paid_by_cohort: dict[str, int] | None = None,
) -> dict:
    summary = {
        "synthetic_demo": True,
        "notice": SYNTHETIC_NOTICE,
        "orders_total": orders_total,
        "paid_orders": paid,
        "paid_households": paid,
        "unpaid_orders": max(orders_total - paid, 0),
        "refunded_count": 0,
        "refund_rate": refund,
        "refund_rate_by_order": refund if refund_by_order is None else refund_by_order,
        "g2_pass": paid >= 10 and refund <= 0.2,
        "gaps": [],
    }
    if paid_by_cohort is not None:
        summary["paid_households_by_cohort"] = paid_by_cohort
    return summary


def make_nps(values: list[int]) -> list[dict[str, str]]:
    return [
        {
            "household_id": f"HH-{index:02d}",
            "nps": str(value),
            "reason": "synthetic test",
            "date": "2026-11-30",
            "synthetic": "true",
        }
        for index, value in enumerate(values, start=1)
    ]


def write_json(path: Path, data: object) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_case(
    tmp_path: Path,
    g3: dict,
    g2: dict,
    nps_rows: list[dict[str, str]] | None = None,
    *,
    cohort: int = 10,
    cohort_id: str | None = None,
    out_name: str = "out",
) -> tuple[int, Path]:
    g3_path = tmp_path / "g3_summary.json"
    g2_path = tmp_path / "g2_summary.json"
    write_json(g3_path, g3)
    write_json(g2_path, g2)

    args = [
        "--g3-summary",
        str(g3_path),
        "--g2-summary",
        str(g2_path),
        "--cohort-size",
        str(cohort),
        "--out-dir",
        str(tmp_path / out_name),
    ]
    if cohort_id is not None:
        args += ["--cohort-id", cohort_id]
    if nps_rows is not None:
        nps_path = tmp_path / "nps.csv"
        g4_report.write_nps_csv(nps_path, nps_rows)
        args += ["--nps", str(nps_path)]

    return g4_report.main(args), tmp_path / out_name


def read_summary(out_dir: Path) -> dict:
    return json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))


NPS_PASS = [10, 10, 10, 10, 10, 8, 8, 8, 8, 5]  # NPS = 40


def test_retention_50_percent_passes(tmp_path: Path) -> None:
    rc, out = run_case(tmp_path, make_g3(active=(7, 7, 5, 5)), make_g2(), make_nps(NPS_PASS))
    assert rc == 0
    summary = read_summary(out)
    assert summary["week4_retention"] == pytest.approx(0.5)
    assert summary["criteria"]["week4_retention"]["pass"] is True
    assert summary["g4_pass"] is True
    assert summary["g4_action"] == "go（继续 C 端）"
    assert (out / "g4_report.md").is_file()
    assert (out / "dashboard.html").is_file()


def test_retention_49_percent_fails(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(active=(100, 100, 100, 49), cohort=100, observed=100),
        make_g2(paid=40, orders_total=100),
        make_nps(NPS_PASS),
        cohort=100,
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["week4_retention"] == pytest.approx(0.49)
    assert summary["criteria"]["week4_retention"]["pass"] is False
    assert summary["g4_pass"] is False
    assert summary["g4_action"] == "转 B2B"


def test_conversion_30_percent_passes(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(active=(60, 60, 60, 60), cohort=100, observed=100),
        make_g2(paid=30, orders_total=100),
        make_nps(NPS_PASS),
        cohort=100,
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["paid_conversion"] == pytest.approx(0.3)
    assert summary["criteria"]["paid_conversion"]["pass"] is True
    assert summary["g4_pass"] is True


def test_conversion_29_percent_fails(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(active=(60, 60, 60, 60), cohort=100, observed=100),
        make_g2(paid=29, orders_total=100),
        make_nps(NPS_PASS),
        cohort=100,
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["paid_conversion"] == pytest.approx(0.29)
    assert summary["criteria"]["paid_conversion"]["pass"] is False
    assert summary["g4_pass"] is False


def test_nps_30_passes(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(),
        make_g2(),
        make_nps([9, 9, 9, 8, 7, 7, 7, 7, 7, 7]),
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["nps"] == pytest.approx(30.0)
    assert summary["criteria"]["nps"]["pass"] is True
    assert summary["g4_pass"] is True


def test_nps_29_fails(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(),
        make_g2(),
        make_nps([10] * 29 + [8] * 71),
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["nps"] == pytest.approx(29.0)
    assert summary["criteria"]["nps"]["pass"] is False
    assert summary["g4_pass"] is False


def test_refund_10_percent_passes(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(),
        make_g2(refund=0.10, refund_by_order=0.10),
        make_nps(NPS_PASS),
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["refund_rate"] == pytest.approx(0.10)
    assert summary["criteria"]["refund_rate"]["pass"] is True
    assert summary["g4_pass"] is True


def test_refund_11_percent_fails(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path,
        make_g3(),
        make_g2(refund=0.11, refund_by_order=0.11),
        make_nps(NPS_PASS),
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["refund_rate"] == pytest.approx(0.11)
    assert summary["criteria"]["refund_rate"]["pass"] is False
    assert summary["g4_pass"] is False
    assert summary["g4_action"] == "转 B2B"


def test_less_than_four_weeks_is_not_collected(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path, make_g3(active=(7, 7, 6)), make_g2(), make_nps(NPS_PASS)
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["week4_retention"] is None
    assert summary["criteria"]["week4_retention"]["status"] == "未采集"
    assert summary["criteria"]["week4_retention"]["pass"] is False
    assert summary["g4_pass"] is False
    assert summary["g4_action"] == "补数据"


def test_nps_sample_below_five_is_not_pass(tmp_path: Path) -> None:
    rc, out = run_case(
        tmp_path, make_g3(), make_g2(), make_nps([10, 10, 10, 10])
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["nps_status"] == "样本不足"
    assert summary["criteria"]["nps"]["pass"] is False
    assert summary["g4_pass"] is False
    assert summary["g4_action"] == "补数据"


def test_missing_nps_marks_not_collected(tmp_path: Path) -> None:
    rc, out = run_case(tmp_path, make_g3(), make_g2(), nps_rows=None)
    assert rc == 0
    summary = read_summary(out)
    assert summary["nps_status"] == "未采集"
    assert summary["criteria"]["nps"]["pass"] is False
    assert summary["g4_pass"] is False
    assert summary["g4_action"] == "补数据"


def test_missing_g3_by_week_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    g3 = make_g3()
    del g3["by_week"]
    rc, _ = run_case(tmp_path, g3, make_g2(), make_nps(NPS_PASS))
    assert rc == 2
    assert "by_week" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["paid_households", "refund_rate"])
def test_missing_g2_key_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    g2 = make_g2()
    del g2[key]
    rc, _ = run_case(tmp_path, make_g3(), g2, make_nps(NPS_PASS))
    assert rc == 2
    assert key in capsys.readouterr().err


def test_duplicate_nps_uses_last_record(tmp_path: Path) -> None:
    rows = make_nps([0, 10, 10, 10, 10])
    rows[1]["household_id"] = "HH-01"  # 同户重复：后一条 10 覆盖前一条 0
    rc, out = run_case(tmp_path, make_g3(), make_g2(), rows)
    assert rc == 0
    summary = read_summary(out)
    assert summary["nps_duplicates_dropped"] == 1
    assert summary["nps_duplicate_households"] == ["HH-01"]
    assert summary["nps"] == pytest.approx(100.0)
    report = (out / "g4_report.md").read_text(encoding="utf-8")
    assert "重复户" in report


def test_invalid_nps_value_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = run_case(tmp_path, make_g3(), make_g2(), make_nps([11]))
    assert rc == 1
    assert not (out / "summary.json").exists()
    assert "NPS 数据校验失败" in capsys.readouterr().out


def test_missing_nps_file_returns_two(tmp_path: Path) -> None:
    g3_path = tmp_path / "g3_summary.json"
    g2_path = tmp_path / "g2_summary.json"
    write_json(g3_path, make_g3())
    write_json(g2_path, make_g2())
    rc = g4_report.main(
        [
            "--g3-summary",
            str(g3_path),
            "--g2-summary",
            str(g2_path),
            "--nps",
            str(tmp_path / "missing-nps.csv"),
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 2


def test_demo_artifacts_numbers_and_synthetic_markings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert g4_report.main(["--demo", "--out-dir", str(tmp_path)]) == 0
    out_dir = tmp_path / "demo" / "out"
    for name in (
        "g3_summary.json",
        "g2_summary.json",
        "nps.csv",
        "summary.json",
        "g4_report.md",
        "dashboard.html",
    ):
        assert (out_dir / name).is_file(), f"缺少 demo/out/{name}"

    for name in ("g3_summary.json", "g2_summary.json", "nps.csv"):
        text = (out_dir / name).read_text(encoding="utf-8-sig")
        assert "SYNTHETIC" in text

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is True
    assert summary["cohort_size"] == 10
    assert summary["week4_retention"] == pytest.approx(0.6)
    assert summary["week4_active_households"] == 6
    assert summary["paid_conversion"] == pytest.approx(0.4)
    assert summary["paid_conversion"] < 0.99  # 不应回退成 10/10=100%
    assert summary["paid_conversion_source"] == "cohort"
    assert summary["g2_paid_households"] == 10
    assert summary["g2_cohort_paid_households"] == 4
    assert summary["g2_orders_total"] == 20
    assert summary["nps"] == pytest.approx(40.0)
    assert summary["nps_n"] == 10
    assert summary["refund_rate"] == pytest.approx(0.0)
    assert summary["g4_pass"] is True
    assert summary["g4_action"] == "go（继续 C 端）"

    report = (out_dir / "g4_report.md").read_text(encoding="utf-8")
    assert report.startswith("> ⚠️ **SYNTHETIC DEMO")
    for marker in ("60.0%", "40.0%", "40.0（n=10）", "0.0%", "cohort=g3", "G4 结论"):
        assert marker in report

    page = (out_dir / "dashboard.html").read_text(encoding="utf-8")
    for marker in (
        "SYNTHETIC",
        "不是真实留存/收入",
        "4 周留存",
        "付费转化",
        "NPS",
        "退款率",
        "60.0%",
        "40.0%",
        "40.0",
        "0.0%",
        "cohort=g3",
    ):
        assert marker in page, f"dashboard 缺少关键数字/标注：{marker}"
    assert "已验证" not in page
    assert "<style>" in page
    assert "<script src" not in page
    assert "https://" not in page
    assert "http://" not in page

    output = capsys.readouterr().out
    assert "G4 留存/转化报告" in output
    assert "SYNTHETIC DEMO" in output
    assert "4 周留存 60.0%" in output

def test_conversion_uses_cohort_filter(tmp_path: Path) -> None:
    g2 = make_g2(paid=10, orders_total=20, paid_by_cohort={"g2": 6, "g3": 4})
    rc, out = run_case(tmp_path, make_g3(), g2, make_nps(NPS_PASS))
    assert rc == 0
    summary = read_summary(out)
    assert summary["paid_conversion"] == pytest.approx(0.4)
    assert summary["paid_conversion_source"] == "cohort"
    assert summary["g2_paid_households"] == 10
    assert summary["g2_cohort_paid_households"] == 4
    assert not any(
        "旧 G2 summary 未分 cohort" in warning
        for warning in summary["warnings"]
    )


def test_old_summary_falls_back_with_warning(tmp_path: Path) -> None:
    g2 = make_g2(paid=10, orders_total=20)  # 旧 summary：无 paid_households_by_cohort
    rc, out = run_case(tmp_path, make_g3(), g2, make_nps(NPS_PASS))
    assert rc == 0
    summary = read_summary(out)
    assert summary["paid_conversion"] == pytest.approx(1.0)
    assert summary["paid_conversion_source"] == "fallback"
    assert (
        "WARNING: 旧 G2 summary 未分 cohort，转化率可能高估"
        in summary["warnings"]
    )

    report = (out / "g4_report.md").read_text(encoding="utf-8")
    assert "WARNING: 旧 G2 summary 未分 cohort" in report
    page = (out / "dashboard.html").read_text(encoding="utf-8")
    assert "WARNING: 旧 G2 summary 未分 cohort" in page


def test_cohort_id_selects_bucket(tmp_path: Path) -> None:
    g2 = make_g2(paid=10, orders_total=20, paid_by_cohort={"g2": 2, "g3": 8})
    rc_g2, out_g2 = run_case(
        tmp_path,
        make_g3(),
        g2,
        make_nps(NPS_PASS),
        cohort_id="g2",
        out_name="out_g2",
    )
    rc_g3, out_g3 = run_case(
        tmp_path,
        make_g3(),
        g2,
        make_nps(NPS_PASS),
        cohort_id="g3",
        out_name="out_g3",
    )
    assert rc_g2 == 0 and rc_g3 == 0
    summary_g2 = read_summary(out_g2)
    summary_g3 = read_summary(out_g3)
    assert summary_g2["paid_conversion"] == pytest.approx(0.2)
    assert summary_g2["g4_pass"] is False
    assert summary_g3["paid_conversion"] == pytest.approx(0.8)
    assert summary_g3["g4_pass"] is True


def test_missing_cohort_bucket_falls_back(tmp_path: Path) -> None:
    g2 = make_g2(paid=10, orders_total=20, paid_by_cohort={"g2": 6})
    rc, out = run_case(
        tmp_path, make_g3(), g2, make_nps(NPS_PASS), cohort_id="g3"
    )
    assert rc == 0
    summary = read_summary(out)
    assert summary["paid_conversion_source"] == "fallback"
    assert summary["paid_conversion"] == pytest.approx(1.0)
    assert any(
        "缺少 cohort=g3 计数" in warning for warning in summary["warnings"]
    )


def test_empty_cohort_id_returns_two(tmp_path: Path) -> None:
    g3_path = tmp_path / "g3_summary.json"
    g2_path = tmp_path / "g2_summary.json"
    nps_path = tmp_path / "nps.csv"
    write_json(g3_path, make_g3())
    write_json(g2_path, make_g2())
    g4_report.write_nps_csv(nps_path, make_nps(NPS_PASS))
    assert (
        g4_report.main(
            [
                "--g3-summary",
                str(g3_path),
                "--g2-summary",
                str(g2_path),
                "--nps",
                str(nps_path),
                "--cohort-id",
                "",
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        == 2
    )