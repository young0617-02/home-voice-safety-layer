"""G1 一键流水线：问卷筛选 → 访谈分析 → dashboard.html。

用法：
    # 真实（用户提供的）CSV
    python scripts/g1_pipeline.py \
        --responses var/g1/responses.csv \
        --interviews var/g1/interviews.csv \
        --out-dir var/g1

    # 合成数据端到端演示（写入 <out-dir>/demo/in 与 <out-dir>/demo/out）
    python scripts/g1_pipeline.py --demo --out-dir var/g1

    # 严格 §1.1 字面 H3（关闭纯危险设备组）
    python scripts/g1_pipeline.py --demo --strict-h3

复用：
    scripts/g1_screen.py 的 read_survey_csv / screen_rows / write_screened_csv
    scripts/g1_analyze.py 的 read_interview_csv / analyze / render_report
    本脚本只编排与渲染，不复制、不修改任何判定逻辑。

合成数据边界：
    --demo 的内置 30 条报名 + 20 条访谈全部为 synthetic，仅用于演示页面与
    流水线格式；产物页首/CSV 列/JSON 字段均标注 SYNTHETIC DEMO，不得作为
    用户证据、go/no-go 结论或“已验证”表述。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import g1_analyze  # noqa: E402
import g1_screen  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "var" / "g1"
DEFAULT_RESPONSES = DEFAULT_OUT_DIR / "responses.csv"
DEFAULT_INTERVIEWS = DEFAULT_OUT_DIR / "interviews.csv"

SYNTHETIC_NOTICE = "SYNTHETIC DEMO — 不是真实用户证据"
SYNTHETIC_MARKDOWN_BANNER = (
    f"> ⚠️ **{SYNTHETIC_NOTICE}**\n"
    ">\n"
    "> 本报告使用合成数据，仅用于演示流水线与报告格式；"
    "不得作为用户证据或 go/no-go 结论。\n\n"
)
PLAN_SUCCESS_TEXT = "三档分流目标 6/7/7"

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
DATA_SOURCE = "数据来源"

DEMO_SURVEY_HEADERS = [
    DATA_SOURCE,
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

DEMO_INTERVIEW_HEADERS = [
    DATA_SOURCE,
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

_SURVEY_ALIASES = {
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


def _demo_survey_row(**overrides: str) -> dict[str, str]:
    """一条合成报名（synthetic）。默认值是一条合格的老人组报名。"""
    row = {
        DATA_SOURCE: SYNTHETIC_NOTICE,
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
        CONTACT: "synthetic-contact",
        EXCLUDE: "无",
    }
    for key, value in overrides.items():
        row[_SURVEY_ALIASES.get(key, key)] = value
    return row


def build_demo_responses() -> list[dict[str, str]]:
    """30 条 synthetic 报名：20 合格 + 6 待定 + 4 不合格。"""
    rows: list[dict[str, str]] = []

    def add(**spec: str) -> None:
        row = _demo_survey_row(**spec)
        row["报名编号"] = f"SYN-P{len(rows) + 1:02d}"
        rows.append(row)

    # 老人组：8 合格（覆盖 H1/Q9-Q10 边界、多选分隔符）+ 2 配额溢出
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
        {"city": "一线", "q6": "只有成年人", "q5": "门锁;总闸或空开"},
        {"city": "一线", "q6": "只有成年人", "q5": "门锁|燃气阀或燃气报警联动"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;大功率插座"},
        {"city": "三线", "q6": "只有成年人", "q5": "总闸或空开;大功率插座"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;燃气阀;总闸或空开"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁|大功率插座;安防报警器"},
        {"city": "二线", "q6": "只有成年人", "q5": "门锁;燃气阀"},
    ):
        add(**spec)

    # 不合格：H1×2、排除项×2
    add(q2="<1 月")
    add(q4="0–9")
    add(q5="都没有")
    add(q3="记不清")
    assert len(rows) == 30
    return rows


_DEMO_INTERVIEW_DATA = [
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


def build_demo_interviews() -> list[dict[str, str]]:
    """20 条 synthetic 访谈：18 有效（12 户有具体场景）+ 2 户未完成。"""
    rows: list[dict[str, str]] = []
    for household, group, scenario, pain, nps in _DEMO_INTERVIEW_DATA:
        completed = household not in {"P19", "P20"}
        rows.append(
            {
                DATA_SOURCE: SYNTHETIC_NOTICE,
                "户号": household,
                "组别": group,
                "城市": "杭州" if completed else "",
                "HA形态/实体数": "Docker/24" if completed else "",
                "危险设备": "门锁+燃气" if completed else "",
                "关键场景（1句）": scenario,
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


def _write_csv(
    path: Path, headers: Sequence[str], rows: Sequence[dict[str, str]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        writer.writeheader()
        writer.writerows(rows)


def write_demo_inputs(in_dir: Path) -> tuple[Path, Path]:
    """把内置 synthetic 数据写入 demo/in，返回两个 CSV 路径。"""
    responses = in_dir / "responses.csv"
    interviews = in_dir / "interviews.csv"
    _write_csv(responses, DEMO_SURVEY_HEADERS, build_demo_responses())
    _write_csv(interviews, DEMO_INTERVIEW_HEADERS, build_demo_interviews())
    return responses, interviews


def run_pipeline(
    responses_path: Path,
    interviews_path: Path,
    out_dir: Path,
    *,
    synthetic: bool = False,
    strict_h3: bool = False,
) -> tuple[dict, g1_analyze.Analysis]:
    """screen → analyze → dashboard，全部复用既有脚本的公开函数。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    headers, rows = g1_screen.read_survey_csv(responses_path)
    results, summary = g1_screen.screen_rows(
        headers,
        rows,
        strict_h3=strict_h3,
        source=str(responses_path),
    )
    g1_screen.write_screened_csv(
        out_dir / "screened.csv", headers, rows, results
    )

    summary = dict(summary)
    summary["synthetic_demo"] = synthetic
    summary["notice"] = (
        SYNTHETIC_NOTICE
        if synthetic
        else "数据来自用户提供的 CSV；请核对样本来源后再引用"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    interview_headers, interview_rows = g1_analyze.read_interview_csv(
        interviews_path
    )
    analysis = g1_analyze.analyze(
        interview_headers, interview_rows, source=str(interviews_path)
    )
    report = g1_analyze.render_report(analysis)
    if synthetic:
        report = SYNTHETIC_MARKDOWN_BANNER + report
    (out_dir / "g1_report.md").write_text(report, encoding="utf-8")

    (out_dir / "dashboard.html").write_text(
        render_dashboard(
            summary,
            analysis,
            synthetic=synthetic,
            source_label=(
                "内置合成数据（SYNTHETIC DEMO）"
                if synthetic
                else f"{responses_path.name} + {interviews_path.name}"
            ),
        ),
        encoding="utf-8",
    )
    return summary, analysis


def format_pipeline_summary(
    summary: dict,
    analysis: g1_analyze.Analysis,
    *,
    synthetic: bool,
    out_dir: Path,
) -> str:
    nps = analysis.nps
    nps_value = (
        f"{nps['nps']:.1f}" if nps["nps"] is not None else "-"
    )
    headline = "G1 流水线完成（SYNTHETIC DEMO）" if synthetic else "G1 流水线完成"
    lines = [
        headline,
        f"  输出目录：{out_dir}",
        f"  报名：{summary['total']} → 合格 {summary['qualified']}"
        f" / 待定 {summary['pending']} / 不合格 {summary['rejected']}",
        "  组别："
        + "、".join(
            f"{group} {summary['by_group'][group]}/{summary['group_quota'][group]}"
            for group in g1_screen.GROUPS
        ),
        "  三档："
        + "、".join(
            f"{plan} {summary['by_plan'][plan]}/{summary['plan_target'][plan]}"
            for plan in ("A", "B", "C")
        )
        + f"（{PLAN_SUCCESS_TEXT}）",
        f"  一线/新一线：{summary['first_tier_count']}"
        f"/{summary['first_tier_limit']}",
        f"  G1 场景：{analysis.scenario_ok_count}/{analysis.target}"
        f"（{'通过' if analysis.scenario_pass else '未通过'}）；"
        f"痛点≥4 {analysis.pain4_count}/{analysis.target}；"
        f"NPS {nps_value}（n={nps['n']}）",
        f"  补访：{len(analysis.followups)} 户",
    ]
    if synthetic:
        lines.append(f"  ⚠️ {SYNTHETIC_NOTICE}")
    return "\n".join(lines)


DASHBOARD_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  color: #1b1b1f;
  background: #f5f6f8;
}
.banner { padding: 14px 20px; font-weight: 700; letter-spacing: .02em; text-align: center; }
.banner-demo { background: #b00020; color: #fff; }
.banner-real { background: #0b5cad; color: #fff; }
main { max-width: 1040px; margin: 0 auto; padding: 20px; }
h1 { font-size: 24px; margin: 12px 0 4px; }
h2 { font-size: 17px; margin: 24px 0 10px; border-left: 4px solid #b00020; padding-left: 8px; }
.meta { color: #5a5a66; font-size: 13px; margin: 0 0 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.card { background: #fff; border: 1px solid #e3e4e8; border-radius: 8px; padding: 12px 14px; }
.card-label { font-size: 12px; color: #6a6a76; }
.card-value { font-size: 26px; font-weight: 700; margin: 4px 0; font-variant-numeric: tabular-nums; }
.card-note { font-size: 12px; color: #6a6a76; }
.bar-row { display: flex; align-items: center; gap: 10px; margin: 8px 0; font-size: 14px; }
.bar-label { width: 110px; flex: 0 0 110px; }
.bar-track { flex: 1; height: 10px; background: #e6e7eb; border-radius: 5px; overflow: hidden; }
.bar-fill { display: block; height: 100%; background: #2f6f4f; }
.bar-value { width: 86px; text-align: right; font-variant-numeric: tabular-nums; color: #4a4a55; }
table { width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; }
th, td { border: 1px solid #e3e4e8; padding: 8px; text-align: left; vertical-align: top; }
th { background: #eef0f3; }
.judgement { background: #fff; border: 1px solid #e3e4e8; border-radius: 8px; padding: 10px 14px; }
.pass { color: #1f7a4d; font-weight: 700; }
.fail { color: #b00020; font-weight: 700; }
footer { margin-top: 28px; font-size: 12px; color: #6a6a76; border-top: 1px solid #e3e4e8; padding-top: 12px; }
.warn { color: #b00020; font-weight: 600; }
@media (max-width: 640px) { .bar-label { width: 80px; flex-basis: 80px; } }
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _short(text: object, limit: int = 80) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _card(label: str, value: object, note: object = "") -> str:
    return (
        '<div class="card">'
        f'<div class="card-label">{_esc(label)}</div>'
        f'<div class="card-value">{_esc(value)}</div>'
        f'<div class="card-note">{_esc(note)}</div>'
        "</div>"
    )


def _bar_row(label: str, value: int, total: int, note: str = "") -> str:
    pct = 0.0 if total <= 0 else min(100.0, value / total * 100)
    return (
        '<div class="bar-row">'
        f'<span class="bar-label">{_esc(label)}</span>'
        '<span class="bar-track">'
        f'<span class="bar-fill" style="width:{pct:.1f}%"></span>'
        "</span>"
        f'<span class="bar-value">{value}/{total}{_esc(note)}</span>'
        "</div>"
    )


def render_dashboard(
    summary: dict,
    analysis: g1_analyze.Analysis,
    *,
    synthetic: bool,
    source_label: str,
) -> str:
    """单文件 HTML：内嵌 CSS、无外部资源、file:// 可直接打开。"""
    total = int(summary.get("total", 0))
    qualified = int(summary.get("qualified", 0))
    pending = int(summary.get("pending", 0))
    rejected = int(summary.get("rejected", 0))

    nps = analysis.nps
    nps_value = f"{nps['nps']:.1f}" if nps["nps"] is not None else "-"
    nps_mean = f"{nps['mean']:.2f}" if nps["mean"] is not None else "-"
    scenario_state = "通过" if analysis.scenario_pass else "未通过"
    scenario_class = "pass" if analysis.scenario_pass else "fail"
    threshold = analysis.threshold
    target = analysis.target

    title = (
        "G1 看板 · SYNTHETIC DEMO" if synthetic else "G1 看板 · 访谈筛选与判据"
    )
    banner_class = "banner banner-demo" if synthetic else "banner banner-real"
    banner_text = (
        SYNTHETIC_NOTICE
        if synthetic
        else "数据来自受访者 CSV · 请核对样本与来源后再引用"
    )

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f"<style>{DASHBOARD_CSS}</style>",
        "</head>",
        "<body>",
        f'<div class="{banner_class}">{_esc(banner_text)}</div>',
        "<main>",
        f"<h1>{_esc(title)}</h1>",
        f'<p class="meta">数据来源：{_esc(source_label)} ｜ '
        "由 <code>scripts/g1_pipeline.py</code> 生成，数字均由 CSV 计算</p>",
        "<section>",
        "<h2>报名漏斗</h2>",
        '<div class="cards">',
        _card("报名总数", total),
        _card("合格（进名单）", qualified),
        _card("待定（备选池）", pending),
        _card("不合格", rejected),
        "</div>",
        _bar_row("合格", qualified, total),
        _bar_row("待定", pending, total),
        _bar_row("不合格", rejected, total),
        "</section>",
        "<section>",
        "<h2>组别配额（8/6/6）</h2>",
    ]
    for group in g1_screen.GROUPS:
        used = int(summary.get("by_group", {}).get(group, 0))
        quota = int(summary.get("group_quota", {}).get(group, 0))
        parts.append(_bar_row(group, used, quota))
    group_summary = " ｜ ".join(
        f"{group} {int(summary.get('by_group', {}).get(group, 0))}"
        f"/{int(summary.get('group_quota', {}).get(group, 0))}"
        for group in g1_screen.GROUPS
    )
    parts.append(f'<p class="judgement">{_esc(group_summary)}</p>')
    parts += [
        "</section>",
        "<section>",
        "<h2>三档分流（目标 6/7/7）</h2>",
        f'<p class="meta">{PLAN_SUCCESS_TEXT}：每户固定一档，禁止切换比价。</p>',
    ]
    for plan in ("A", "B", "C"):
        used = int(summary.get("by_plan", {}).get(plan, 0))
        plan_target = int(summary.get("plan_target", {}).get(plan, 0))
        parts.append(_bar_row(f"{plan} 臂", used, plan_target))
    plan_summary = " ｜ ".join(
        f"{plan} {int(summary.get('by_plan', {}).get(plan, 0))}"
        f"/{int(summary.get('plan_target', {}).get(plan, 0))}"
        for plan in ("A", "B", "C")
    )
    parts.append(f'<p class="judgement">{_esc(plan_summary)}</p>')
    parts += [
        "</section>",
        "<section>",
        "<h2>城市配额</h2>",
        f'<p class="judgement">一线/新一线：'
        f"<strong>{int(summary.get('first_tier_count', 0))}"
        f"/{int(summary.get('first_tier_limit', 0))}</strong>"
        f"（报名中 {int(summary.get('first_tier_total', 0))} 户）。</p>",
        "</section>",
        "<section>",
        "<h2>G1 判据</h2>",
        '<div class="cards">',
        _card(
            "G1① 关键场景",
            f"{analysis.scenario_ok_count}/{target}",
            scenario_state,
        ),
        _card(
            "痛点 ≥4",
            f"{analysis.pain4_count}/{target}",
            f"目标 ≥{g1_analyze.PAIN4_THRESHOLD}/{target}",
        ),
        _card("NPS", nps_value, f"n={nps['n']}，均值 {nps_mean}"),
        _card("有效访谈", f"{analysis.valid_count}/{target}", ""),
        "</div>",
        f'<p class="judgement">G1① 结论：'
        f'<span class="{scenario_class}">{_esc(scenario_state)}</span>'
        f"（{analysis.scenario_ok_count}/{target} "
        f"{'≥' if analysis.scenario_pass else '<'} "
        f"{threshold}/{target}，门槛 {threshold} 户）。</p>",
        "</section>",
        "<section>",
        f"<h2>建议补访（前 10 / 共 {len(analysis.followups)} 户）</h2>",
    ]
    followups = analysis.followups[:10]
    if followups:
        parts += [
            "<table>",
            "<thead><tr><th>户号</th><th>问题</th><th>建议动作</th></tr></thead>",
            "<tbody>",
        ]
        for item in followups:
            parts.append(
                "<tr>"
                f"<td>{_esc(item.get('household', ''))}</td>"
                f"<td>{_esc(_short(item.get('issue', ''), 60))}</td>"
                f"<td>{_esc(_short(item.get('action', ''), 100))}</td>"
                "</tr>"
            )
        parts += ["</tbody>", "</table>"]
    else:
        parts.append("<p>无。</p>")

    parts += [
        "</section>",
        "<footer>",
        "<p>口径：H1–H4 + 配额 8/6/6 + A/B/C 6/7/7 + G1① ≥12/20；"
        "依据 <code>docs/用户验证包-v0.1.md</code> 与 "
        "<code>docs/招募帖与问卷-v0.1.md</code>。</p>",
        "<p>本页由 CSV 计算生成，仅描述样本，不做统计显著性检验，"
        "不构成验证结论。</p>",
    ]
    if synthetic:
        parts.append(
            f'<p class="warn">⚠️ {_esc(SYNTHETIC_NOTICE)}：'
            "本页所有数字来自合成数据，仅用于演示页面格式，"
            "不得作为用户证据或 go/no-go 判断依据。</p>"
        )
    parts += [
        "</footer>",
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G1 一键流水线：问卷筛选 + 访谈分析 + dashboard.html"
    )
    parser.add_argument(
        "--responses", default=str(DEFAULT_RESPONSES), help="问卷导出 CSV"
    )
    parser.add_argument(
        "--interviews", default=str(DEFAULT_INTERVIEWS), help="访谈记录 CSV"
    )
    parser.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), help="输出根目录（默认 var/g1）"
    )
    parser.add_argument(
        "--demo", action="store_true", help="用内置 synthetic 数据跑端到端演示"
    )
    parser.add_argument(
        "--strict-h3",
        action="store_true",
        help="严格按 §1.1 字面口径（H3 只认老人/儿童）",
    )
    args = parser.parse_args(argv)

    out_root = Path(args.out_dir)
    if args.demo:
        responses, interviews = write_demo_inputs(out_root / "demo" / "in")
        out_dir = out_root / "demo" / "out"
        synthetic = True
    else:
        responses = Path(args.responses)
        interviews = Path(args.interviews)
        out_dir = out_root
        synthetic = False
        for path in (responses, interviews):
            if not path.is_file():
                raise SystemExit(f"找不到输入 CSV：{path}")

    summary, analysis = run_pipeline(
        responses,
        interviews,
        out_dir,
        synthetic=synthetic,
        strict_h3=args.strict_h3,
    )
    print(
        format_pipeline_summary(
            summary, analysis, synthetic=synthetic, out_dir=out_dir
        )
    )
    print(f"  看板：{out_dir / 'dashboard.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())