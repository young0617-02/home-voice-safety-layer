"""G1 数据工具测试。

所有报名/访谈数据均为 **synthetic**（合成），不代表任何真实用户；
测试只写 pytest 的 tmp_path，不触碰仓库 var/。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g1_analyze  # noqa: E402
import g1_screen  # noqa: E402

SYNTHETIC = "synthetic"

CITY = "Q1 常住城市级别"
DURATION = "Q2 用 Home Assistant 多久"
FORM = "Q3 HA 安装形态"
ENTITIES = "Q4 已接入且可控的实体数"
DEVICES = "Q5 哪些设备已接入 HA"
MEMBERS = "Q6 同住成员"
ELDERLY_ALONE = "Q7 老人白天是否常独自在家（≥4h）"
CHILD_USES = "Q8 孩子会自己用语音或按开关吗"
ABNORMAL = "Q9 近 3 个月发生过的异常"
PAIN = "Q10 怕出事不敢让家人用语音的程度"
ENTRY = "Q11 现在的语音入口"
RECORDING = "Q12 接受 45′ 访谈与录音吗"
CONTACT = "联系方式"
EXCLUDE = "人工核对排除项"

SURVEY_HEADERS = [
    "报名编号",
    CITY,
    DURATION,
    FORM,
    ENTITIES,
    DEVICES,
    MEMBERS,
    ELDERLY_ALONE,
    CHILD_USES,
    ABNORMAL,
    PAIN,
    ENTRY,
    RECORDING,
    CONTACT,
    EXCLUDE,
]

SURVEY_ALIASES = {
    "id": "报名编号",
    "city": CITY,
    "q2": DURATION,
    "q3": FORM,
    "q4": ENTITIES,
    "q5": DEVICES,
    "q6": MEMBERS,
    "q7": ELDERLY_ALONE,
    "q8": CHILD_USES,
    "q9": ABNORMAL,
    "q10": PAIN,
    "q11": ENTRY,
    "q12": RECORDING,
    "contact": CONTACT,
    "exclude": EXCLUDE,
}


def survey_row(**overrides: str) -> dict[str, str]:
    row = {
        "报名编号": "SYN-P00",
        CITY: "二线",
        DURATION: "3–12 月",
        FORM: "Docker",
        ENTITIES: "10–30",
        DEVICES: "门锁;燃气阀或燃气报警联动",
        MEMBERS: "65+ 老人",
        ELDERLY_ALONE: "是",
        CHILD_USES: "不适用",
        ABNORMAL: "误开门锁",
        PAIN: "4",
        ENTRY: "手机 HA App",
        RECORDING: "接受录音",
        CONTACT: f"{SYNTHETIC}-contact",
        EXCLUDE: "无",
    }
    for key, value in overrides.items():
        row[SURVEY_ALIASES.get(key, key)] = value
    return row


def build_survey_rows() -> list[dict[str, str]]:
    """30 条合成报名：20 合格 + 6 待定 + 4 不合格。"""
    rows: list[dict[str, str]] = []

    def add(**spec: str) -> None:
        row = survey_row(**spec)
        row["报名编号"] = f"SYN-P{len(rows) + 1:02d}"
        rows.append(row)

    # 老人组：8 合格（含 H1/Q9-Q10 边界、多选分隔符）+ 2 配额溢出
    for spec in (
        {"city": "一线", "q5": "门锁;燃气阀或燃气报警联动", "q9": "误开门锁", "q10": "4", "q7": "是"},
        {"city": "一线", "q2": ">12 月", "q5": "门锁|大功率插座", "q9": "误断总电", "q10": "5", "q7": "是"},
        {"city": "新一线", "q5": "门锁,总闸或空开", "q9": "误动燃气", "q10": "3", "q7": "是"},
        {"city": "二线", "q4": "31–80", "q5": "门锁、燃气阀", "q9": "误开门锁；误断总电", "q10": "2", "q7": "是"},
        {"city": "一线", "q5": "总闸或空开", "q9": "都没有", "q10": "4", "q7": "否"},
        {"city": "一线", "q5": "燃气阀或燃气报警联动;大功率插座", "q9": "都没有", "q10": "5", "q7": "否"},
        {"city": "一线", "q5": "门锁;总闸或空开", "q9": "设备自己乱动", "q10": "3", "q7": "否"},
        {"city": "二线", "q5": "门锁", "q9": "误开误关灯", "q10": "2", "q7": "否"},
    ):
        add(**spec)
    add(city="一线", q5="门锁;燃气阀")  # SYN-P09：老人配额满
    add(city="新一线", q5="门锁")  # SYN-P10：老人配额满

    # 儿童组：6 合格 + 2 配额溢出
    for spec in (
        {"city": "一线", "q6": "3–6 岁儿童", "q5": "门锁;大功率插座", "q9": "误开门锁", "q10": "4", "q8": "会"},
        {"city": "一线", "q6": "3～6 岁儿童", "q5": "总闸或空开", "q9": "误断总电", "q10": "4", "q8": "会"},
        {"city": "新一线", "q6": "3–6 岁儿童", "q5": "门锁|燃气阀", "q9": "误动燃气", "q10": "3", "q8": "不会"},
        {"city": "一线", "q6": "6–12 岁儿童", "q5": "大功率插座", "q9": "设备自己乱动", "q10": "4", "q8": "不会"},
        {"city": "二线", "q6": "6–12 岁儿童", "q5": "燃气阀;总闸或空开", "q9": "都没有", "q10": "4", "q8": "不会"},
        {"city": "一线", "q6": "6-12 岁儿童", "q5": "门锁;总闸或空开", "q9": "误开误关灯", "q10": "5", "q8": "不会"},
    ):
        add(**spec)
    add(city="一线", q6="6–12 岁儿童", q5="门锁;燃气阀")  # SYN-P17：儿童配额满
    add(city="二线", q6="3–6 岁儿童", q5="门锁")  # SYN-P18：儿童配额满

    # 纯危险设备组：6 合格 + 1 城市超限 + 1 危险设备不足
    for spec in (
        {"city": "一线", "q6": "只有成年人", "q5": "门锁;总闸或空开"},  # P19：一线第 12
        {"city": "一线", "q6": "只有成年人", "q5": "门锁|燃气阀或燃气报警联动"},  # P20：城市超限
        {"city": "二线", "q6": "只有成年人", "q5": "门锁"},  # P21：仅 1 个危险设备
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;大功率插座"},
        {"city": "三线", "q6": "只有成年人", "q5": "总闸或空开;大功率插座"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;燃气阀;总闸或空开"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁|大功率插座;安防报警器"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;燃气阀"},
    ):
        add(**spec)

    # 不合格：H1×2、排除项×2
    add(q2="<1 月")  # P27
    add(q4="0–9")  # P28
    add(q5="都没有")  # P29
    add(q3="记不清")  # P30
    assert len(rows) == 30
    return rows


def write_csv(
    path: Path, headers: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        writer.writeheader()
        writer.writerows(rows)


def run_screen(
    rows: Sequence[Mapping[str, str]], **kwargs: object
) -> tuple[list[g1_screen.ScreenResult], dict]:
    return g1_screen.screen_rows(SURVEY_HEADERS, rows, **kwargs)


def test_survey_verdicts_quota_and_plans() -> None:
    rows = build_survey_rows()
    results, summary = run_screen(rows)
    by_id = {
        row["报名编号"]: result for row, result in zip(rows, results)
    }

    for number in range(1, 9):
        assert by_id[f"SYN-P{number:02d}"].verdict == "合格"
        assert by_id[f"SYN-P{number:02d}"].group == "老人"
    for number in (9, 10):
        assert by_id[f"SYN-P{number:02d}"].verdict == "待定（备选池）"
        assert "配额已满" in by_id[f"SYN-P{number:02d}"].reason

    for number in range(11, 17):
        assert by_id[f"SYN-P{number:02d}"].verdict == "合格"
        assert by_id[f"SYN-P{number:02d}"].group == "儿童"
    for number in (17, 18):
        assert by_id[f"SYN-P{number:02d}"].verdict == "待定（备选池）"
        assert "配额已满" in by_id[f"SYN-P{number:02d}"].reason

    assert by_id["SYN-P19"].verdict == "合格"
    assert by_id["SYN-P19"].group == "纯危险设备"
    assert by_id["SYN-P20"].verdict == "待定（备选池）"
    assert "一线/新一线" in by_id["SYN-P20"].reason
    assert by_id["SYN-P21"].verdict == "待定（备选池）"
    assert "≥2" in by_id["SYN-P21"].reason
    for number in range(22, 27):
        assert by_id[f"SYN-P{number:02d}"].verdict == "合格"
        assert by_id[f"SYN-P{number:02d}"].group == "纯危险设备"

    assert by_id["SYN-P27"].verdict == "不合格"
    assert "H1" in by_id["SYN-P27"].reason
    assert by_id["SYN-P28"].verdict == "不合格"
    assert "H1" in by_id["SYN-P28"].reason
    assert by_id["SYN-P29"].verdict == "不合格"
    assert "Q5" in by_id["SYN-P29"].reason
    assert by_id["SYN-P30"].verdict == "不合格"
    assert "Q3" in by_id["SYN-P30"].reason

    assert summary["total"] == 30
    assert summary["qualified"] == 20
    assert summary["pending"] == 6
    assert summary["rejected"] == 4
    assert summary["by_group"] == {"老人": 8, "儿童": 6, "纯危险设备": 6}
    assert summary["by_plan"] == {"A": 6, "B": 7, "C": 7}
    assert summary["first_tier_count"] == 12
    assert summary["first_tier_total"] == 16
    assert summary["pending_by_reason"] == {
        "group_quota": 4,
        "first_tier": 1,
        "device_count": 1,
    }
    assert summary["rejected_by_reason"]["h1"] == 2
    assert summary["rejected_by_reason"]["excluded"] == 2


def test_survey_plan_rotation_exact_counts() -> None:
    rows = build_survey_rows()
    results, _ = run_screen(rows)
    by_id = {row["报名编号"]: result for row, result in zip(rows, results)}
    assert by_id["SYN-P01"].plan == "A"
    assert by_id["SYN-P02"].plan == "B"
    assert by_id["SYN-P03"].plan == "C"
    assert by_id["SYN-P19"].plan == "C"
    assert by_id["SYN-P25"].plan == "B"
    assert by_id["SYN-P26"].plan == "C"
    assert by_id["SYN-P09"].plan == ""


def test_survey_bonus_checks() -> None:
    rows = build_survey_rows()
    _, summary = run_screen(rows)
    bonus = summary["bonus_checks"]
    assert bonus["老人组_独处≥4h"]["actual"] == 4
    assert bonus["老人组_独处≥4h"]["ok"] is True
    assert bonus["老人组_误断总电或误开门锁"]["actual"] == 3
    assert bonus["老人组_误断总电或误开门锁"]["ok"] is True
    assert bonus["儿童组_≤6岁"]["actual"] == 3
    assert bonus["儿童组_会自己用语音或按开关（Q8 代理）"]["actual"] == 2


@pytest.mark.parametrize(
    ("overrides", "verdict", "reason_keyword"),
    [
        ({"q9": "都没有", "q10": "4"}, "合格", None),
        ({"q9": "都没有", "q10": "3"}, "不合格", "H4"),
        ({"q9": "误开门锁", "q10": "1"}, "合格", None),
        ({"q12": "不接受"}, "不合格", "Q12"),
        ({"q12": ""}, "不合格", "Q12"),
        ({"q12": "只接受笔记"}, "合格", None),
        ({"exclude": "亲友同事"}, "不合格", "人工核对"),
        ({"q6": "只有成年人", "q5": "门锁"}, "待定（备选池）", "≥2"),
        ({"q6": "只有成年人", "q5": "门锁;总闸或空开"}, "合格", None),
        ({"q6": ""}, "不合格", "H3"),
        ({"q3": "其他"}, "不合格", "H1"),
        ({"q2": "1–3 月"}, "不合格", "H1"),
        ({"q4": "0–9"}, "不合格", "H1"),
        ({"q5": "安防报警器"}, "不合格", "H2"),
        ({"q5": "燃气报警联动"}, "合格", None),
        ({"q6": "3～6 岁儿童"}, "合格", None),
        ({"q2": "3至12个月"}, "合格", None),
        ({"q4": "24"}, "合格", None),
        ({"q5": "门锁；大功率插座"}, "合格", None),
        ({"q3": "Home Assistant OS"}, "合格", None),
        ({"q1": "三线及以下"}, "合格", None),
    ],
)
def test_single_row_boundaries(
    overrides: dict[str, str], verdict: str, reason_keyword: str | None
) -> None:
    results, _ = run_screen([survey_row(**overrides)])
    assert results[0].verdict == verdict
    if reason_keyword:
        assert reason_keyword in results[0].reason


def test_strict_h3_closes_device_only_group() -> None:
    rows = [survey_row(q6="只有成年人", q5="门锁;总闸或空开")]
    results, summary = run_screen(rows, strict_h3=True)
    assert results[0].verdict == "不合格"
    assert "strict-h3" in results[0].reason
    assert summary["qualified"] == 0


def test_limit_truncates_by_signup_order() -> None:
    rows = build_survey_rows()[:9]
    _, summary = run_screen(rows)
    assert summary["total"] == 9
    assert summary["qualified"] == 8
    assert summary["pending"] == 1
    assert summary["rejected"] == 0
    assert sum(summary["by_plan"].values()) == 8
    assert summary["by_group"]["老人"] == 8


def test_mapping_supports_alias_and_reverse_direction() -> None:
    headers = [
        "城市级别",
        "HA 使用时长",
        "安装方式",
        "实体数量",
        "接入设备",
        "家庭成员",
        "老人独处",
        "孩子语音",
        "异常记录",
        "担心程度",
        "语音入口",
        "录音意愿",
        "联系微信",
        "人工核对",
    ]
    mapping = {
        "Q1": "城市级别",
        "Q2": "HA 使用时长",
        "Q3": "安装方式",
        "Q4": "实体数量",
        "Q5": "接入设备",
        "Q6": "家庭成员",
        "Q7": "老人独处",
        "Q8": "孩子语音",
        "Q9": "异常记录",
        "Q10": "担心程度",
        "Q11": "语音入口",
        "Q12": "录音意愿",
        "contact": "联系微信",
        "exclude": "人工核对",
    }
    assert g1_screen.resolve_columns(headers, mapping) == mapping

    reverse = {value: key for key, value in mapping.items() if key.startswith("Q")}
    columns = g1_screen.resolve_columns(headers, reverse)
    assert columns["Q5"] == "接入设备"
    assert columns["Q12"] == "录音意愿"


def test_mapping_to_missing_column_fails() -> None:
    with pytest.raises(SystemExit):
        g1_screen.resolve_columns(SURVEY_HEADERS, {"Q1": "不存在的列"})


def test_survey_cli_writes_screened_csv_and_summary(tmp_path: Path) -> None:
    input_path = tmp_path / "responses.csv"
    write_csv(input_path, SURVEY_HEADERS, build_survey_rows())
    out_dir = tmp_path / "out"

    assert g1_screen.main(
        ["--input", str(input_path), "--out-dir", str(out_dir)]
    ) == 0

    with (out_dir / "screened.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert len(rows) == 30
    for column in ("verdict", "group", "quota_slot", "plan", "reason"):
        assert column in reader.fieldnames
    assert rows[0]["verdict"] == "合格"
    assert rows[0]["plan"] == "A"
    assert rows[0]["quota_slot"] == "老人 1/8"

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["qualified"] == 20
    assert summary["by_plan"] == {"A": 6, "B": 7, "C": 7}


def test_survey_cli_dry_run_and_limit_write_nothing(tmp_path: Path) -> None:
    input_path = tmp_path / "responses.csv"
    write_csv(input_path, SURVEY_HEADERS, build_survey_rows())
    out_dir = tmp_path / "dry"

    assert g1_screen.main(
        [
            "--input",
            str(input_path),
            "--out-dir",
            str(out_dir),
            "--dry-run",
            "--limit",
            "9",
        ]
    ) == 0
    assert not out_dir.exists()


INTERVIEW_HEADERS = [
    "户号",
    "组别",
    "城市",
    "HA形态/实体数",
    "危险设备",
    "关键场景（1句）",
    "当前做法",
    "痛点1-5",
    "价格形式偏好",
    "NPS",
    "关键原话",
    "愿内测",
    "备注",
]

INTERVIEW_DATA = [
    ("P01", "老人", "孩子对门锁喊“开门”真的开了", 5, 10),
    ("P02", "老人", "老人误开门锁，差点让陌生人进来", 4, 9),
    ("P03", "老人", "把燃气关了被听成打开", 5, 9),
    ("P04", "老人", "孩子误断总电，全屋黑了 10 分钟", 5, 8),
    ("P05", "老人", "热水器被语音打开，烧了一下午", 4, 7),
    ("P06", "老人", "误撤防后报警器没响", 3, 6),
    ("P07", "老人", "老人把燃气关阀说成开阀", 4, 10),
    ("P08", "老人", "孩子按门锁开关，门开了", 3, 9),
    ("P09", "儿童", "儿童误断总电", 4, 8),
    ("P10", "儿童", "孩子把充电桩大功率插座打开", 3, 7),
    ("P11", "儿童", "儿童对门锁说开门", 4, 5),
    ("P12", "儿童", "电视里一句打开空调，空调自己开了", 3, 10),
    ("P13", "儿童", "无", 2, 6),
    ("P14", "儿童", "记不清", 2, 5),
    ("P15", "纯危险设备", "-", 2, 7),
    ("P16", "纯危险设备", "", 2, 8),
    ("P17", "纯危险设备", "没有发生过", 1, 10),
    ("P18", "纯危险设备", "想不起来", 1, 4),
    ("P19", "", "", None, None),
    ("P20", "", "", None, None),
]


def build_interview_rows(
    scenario_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for household, group, scenario, pain, nps in INTERVIEW_DATA:
        completed = household not in {"P19", "P20"}
        rows.append(
            {
                "户号": household,
                "组别": group,
                "城市": "杭州" if completed else "",
                "HA形态/实体数": "Docker/24" if completed else "",
                "危险设备": "门锁+燃气" if completed else "",
                "关键场景（1句）": (
                    scenario_overrides.get(household, scenario)
                    if scenario_overrides
                    else scenario
                ),
                "当前做法": "限制语音权限" if completed else "",
                "痛点1-5": str(pain) if pain is not None else "",
                "价格形式偏好": "年费" if completed else "",
                "NPS": str(nps) if nps is not None else "",
                "关键原话": "后怕" if completed else "",
                "愿内测": "Y" if completed else "",
                "备注": "",
            }
        )
    return rows


def run_interview(
    rows: Sequence[Mapping[str, str]], **kwargs: object
) -> g1_analyze.Analysis:
    return g1_analyze.analyze(
        INTERVIEW_HEADERS, rows, source=f"{SYNTHETIC}.csv", **kwargs
    )


def test_interview_analysis_numbers_and_g1_judgement() -> None:
    analysis = run_interview(build_interview_rows())
    assert analysis.valid_count == 18
    assert analysis.scenario_ok_count == 12
    assert analysis.scenario_pass is True
    assert analysis.group_counts == {"老人": 8, "儿童": 6, "纯危险设备": 4}

    assert analysis.pain_dist == {5: 3, 4: 5, 3: 4, 2: 4, 1: 2}
    assert analysis.pain_missing == 0
    assert analysis.pain_mean == pytest.approx(3.17)
    assert analysis.pain4_count == 8

    assert analysis.nps["n"] == 18
    assert analysis.nps["mean"] == pytest.approx(7.67)
    assert analysis.nps["promoters"] == 7
    assert analysis.nps["passives"] == 6
    assert analysis.nps["detractors"] == 5
    assert analysis.nps["nps"] == pytest.approx(11.1)

    assert len(analysis.gaps) == 1
    assert "18/20" in analysis.gaps[0]
    assert len(analysis.followups) == 8


def test_interview_scenario_keyword_categories() -> None:
    analysis = run_interview(build_interview_rows())
    assert analysis.category_counts["门锁"] == ["P01", "P02", "P08", "P11"]
    assert analysis.category_counts["燃气"] == ["P03", "P07"]
    assert analysis.category_counts["总电"] == ["P04", "P09"]
    assert analysis.category_counts["大功率"] == ["P05", "P10"]
    assert analysis.category_counts["撤防"] == ["P06"]
    assert analysis.category_counts["误触"] == ["P02", "P04", "P06", "P09"]
    assert analysis.category_counts["儿童"] == ["P01", "P04", "P08", "P09", "P10", "P11"]
    assert analysis.category_counts["老人"] == ["P02", "P07"]
    assert analysis.category_counts["其他"] == ["P12"]


def test_interview_threshold_boundary_fails_at_eleven() -> None:
    rows = build_interview_rows({"P12": "记不清"})
    analysis = run_interview(rows)
    assert analysis.scenario_ok_count == 11
    assert analysis.scenario_pass is False
    assert any("未达 ≥12/20" in gap for gap in analysis.gaps)
    assert len(analysis.followups) == 9


def test_interview_report_is_explainable() -> None:
    analysis = run_interview(build_interview_rows())
    markdown = g1_analyze.render_report(analysis)
    for marker in (
        "12/20",
        "8/20",
        "NPS",
        "11.1",
        "门锁",
        "scenario_ok",
        "P13",
        "G1 场景判据结论：通过",
    ):
        assert marker in markdown
    assert "未调用大模型" in markdown


def test_interview_columns_are_fuzzy() -> None:
    headers = [
        "户号编号",
        "组别",
        "城市",
        "HA形态/实体数",
        "危险设备",
        "关键事件记录",
        "当前做法",
        "痛点（1-5）",
        "价格形式偏好",
        "NPS得分",
        "关键原话",
        "愿内测",
        "备注",
    ]
    columns = g1_analyze.resolve_columns(headers)
    assert columns["household"] == "户号编号"
    assert columns["scenario"] == "关键事件记录"
    assert columns["pain"] == "痛点（1-5）"
    assert columns["nps"] == "NPS得分"


def test_interview_missing_required_column_fails() -> None:
    with pytest.raises(SystemExit):
        g1_analyze.resolve_columns(["组别", "城市"])


def test_interview_cli_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path = tmp_path / "interviews.csv"
    write_csv(input_path, INTERVIEW_HEADERS, build_interview_rows())
    out_path = tmp_path / "g1_report.md"

    assert g1_analyze.main(
        ["--input", str(input_path), "--out", str(out_path), "--dry-run"]
    ) == 0
    assert not out_path.exists()
    assert "G1 访谈分析" in capsys.readouterr().out


def test_interview_cli_writes_report(tmp_path: Path) -> None:
    input_path = tmp_path / "interviews.csv"
    write_csv(input_path, INTERVIEW_HEADERS, build_interview_rows())
    out_path = tmp_path / "nested" / "g1_report.md"

    assert g1_analyze.main(
        ["--input", str(input_path), "--out", str(out_path)]
    ) == 0
    assert out_path.is_file()
    text = out_path.read_text(encoding="utf-8")
    assert text.startswith("# G1 访谈分析报告")
    assert "12/20" in text