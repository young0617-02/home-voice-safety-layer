"""G1 访谈记录分析：场景归类 / 痛点分布 / NPS / G1 判据 / 补访建议。

用法：
    python scripts/g1_analyze.py --input var/g1/interviews.csv
    python scripts/g1_analyze.py --input 访谈.csv --out var/g1/g1_report.md
    python scripts/g1_analyze.py --input 访谈.csv --dry-run

输入：`docs/访谈记录模板.csv` 的 13 列（允许列名微调）：
    户号 / 组别 / 城市 / HA形态/实体数 / 危险设备 / 关键场景（1句）/
    当前做法 / 痛点1-5 / 价格形式偏好 / NPS / 关键原话 / 愿内测 / 备注

输出：`var/g1/g1_report.md`，含逐户 scenario_ok 与理由（规则判定，不调用大模型）。

口径来源：
    docs/用户验证包-v0.1.md §2.7（记录模板）、§7 G1 判据 ① ≥12/20 户能讲出具体事件
    docs/用户验证包-v0.1.md §2（痛点 1–5 口径）、§6（NPS 公式）
    docs/招募帖与问卷-v0.1.md §5（硬规则：不降标准、如实记录实际户数）
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "var" / "g1" / "interviews.csv"
DEFAULT_OUT = ROOT / "var" / "g1" / "g1_report.md"

TARGET_HOUSEHOLDS = 20
SCENARIO_THRESHOLD = 12
PAIN4_THRESHOLD = 8

GROUP_ORDER = ("老人", "儿童", "纯危险设备", "未填")

PAIN_LABELS = {
    5: "已禁用或出过事",
    4: "明确担心并加过限制",
    3: "想过没做",
    2: "听说但不担心",
    1: "无感",
}

SCENARIO_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("门锁", ("门锁", "开门", "解锁", "智能锁")),
    ("燃气", ("燃气", "煤气", "关阀", "开阀")),
    ("总电", ("总电", "总闸", "空开", "断电", "跳闸", "全屋断电")),
    ("大功率", ("大功率", "热水器", "烘干机", "充电桩", "电暖", "烤箱")),
    ("撤防", ("撤防", "布防", "安防", "报警器")),
    ("误触", ("误触", "误碰", "误操作", "误开", "误关", "误动", "误断", "误撤", "乱按", "碰过")),
    ("儿童", ("孩子", "儿童", "小孩", "儿子", "女儿", "宝宝", "娃")),
    ("老人", ("老人", "父母", "奶奶", "爷爷", "外公", "外婆", "老爸", "老妈")),
)
OTHER_CATEGORY = "其他"

_NULL_SCENARIOS = {
    "无",
    "没有",
    "没有发生",
    "没发生",
    "没有发生过",
    "没发生过",
    "从未发生",
    "没有遇到过",
    "暂无",
    "否",
    "none",
    "n/a",
    "na",
    "-",
    "--",
}
_UNCLEAR_MARKERS = (
    "记不清",
    "不记得",
    "想不起来",
    "忘了",
    "不清楚",
    "说不清",
)

COLUMN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("household", ("户号", "编号", "household", "house id")),
    ("group", ("组别", "分组")),
    ("city", ("城市",)),
    ("device", ("危险设备",)),
    ("scenario", ("关键场景", "场景", "关键事件")),
    ("current_practice", ("当前做法", "现状做法")),
    ("pain", ("痛点",)),
    ("price_form", ("价格形式", "价格偏好")),
    ("nps", ("nps", "推荐意愿")),
    ("quote", ("关键原话", "原话")),
    ("willing_test", ("愿内测", "内测")),
    ("note", ("备注",)),
)

_DASHES = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "~": "-",
        "～": "-",
        "／": "/",
        "｜": "|",
        "；": ";",
        "，": ",",
        "、": ",",
    }
)


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().translate(_DASHES)
    return re.sub(r"\s+", " ", text)


def resolve_columns(headers: Sequence[str]) -> dict[str, str]:
    """按中文列名/英文别名模糊匹配；列名微调不影响分析。"""
    columns: dict[str, str] = {}
    used: set[str] = set()
    for key, hints in COLUMN_HINTS:
        for header in headers:
            if header in used:
                continue
            text = normalize_text(header)
            if any(hint in text for hint in hints):
                columns[key] = header
                used.add(header)
                break
    missing = [key for key in ("household", "scenario") if key not in columns]
    if missing:
        raise SystemExit(
            f"访谈 CSV 缺少必需列：{missing}；实际表头：{list(headers)}"
        )
    return columns


def _cell(row: Mapping[str, str], columns: Mapping[str, str], key: str) -> str:
    column = columns.get(key)
    if not column:
        return ""
    return (row.get(column) or "").strip()


def parse_score(value: object, low: int, high: int) -> int | None:
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
    if not match:
        return None
    score = int(match.group(1))
    return score if low <= score <= high else None


def evaluate_scenario(value: object) -> tuple[bool, str]:
    """关键场景是否可用于 G1 判据：非空、非"无/没有"、非"记不清"、非过短。"""
    original = str(value or "").strip()
    text = normalize_text(original).strip(" .;,")
    if not text:
        return False, "关键场景为空（未填）"
    if text in _NULL_SCENARIOS:
        return False, f"关键场景为“{original}”（视为无，不计入判据）"
    if any(marker in text for marker in _UNCLEAR_MARKERS):
        return False, f"关键场景为“{original}”（记不清，不计入判据）"
    compact = re.sub(r"[\s\W_]+", "", text)
    if len(compact) < 4:
        return False, f"关键场景“{original}”过于简略（<4 字），需补问细节"
    return True, "关键场景有具体描述"


def classify_scenario(value: object) -> list[str]:
    """关键词归类；命中多个类别时全部保留，无命中归入"其他"。"""
    text = normalize_text(value)
    if not text:
        return []
    hits = [
        name
        for name, keywords in SCENARIO_KEYWORDS
        if any(keyword in text for keyword in keywords)
    ]
    return hits or [OTHER_CATEGORY]


@dataclass
class InterviewRecord:
    household: str
    group: str
    city: str
    device: str
    scenario: str
    current_practice: str
    pain_raw: str
    price_form: str
    nps_raw: str
    quote: str
    willing_test: str
    note: str
    scenario_ok: bool
    scenario_reason: str
    pain: int | None
    nps: int | None
    categories: list[str] = field(default_factory=list)
    valid: bool = False


@dataclass
class Analysis:
    source: str
    target: int
    threshold: int
    records: list[InterviewRecord]
    valid_records: list[InterviewRecord]
    valid_count: int
    scenario_ok_records: list[InterviewRecord]
    scenario_ok_count: int
    scenario_pass: bool
    group_counts: dict[str, int]
    category_counts: dict[str, list[str]]
    pain_dist: dict[int, int]
    pain_missing: int
    pain_mean: float | None
    pain4_count: int
    nps: dict[str, object]
    gaps: list[str]
    followups: list[dict[str, str]]


def build_record(row: Mapping[str, str], columns: Mapping[str, str]) -> InterviewRecord:
    scenario = _cell(row, columns, "scenario")
    pain_raw = _cell(row, columns, "pain")
    nps_raw = _cell(row, columns, "nps")
    household = _cell(row, columns, "household")
    scenario_ok, scenario_reason = evaluate_scenario(scenario)
    pain = parse_score(pain_raw, 1, 5)
    nps = parse_score(nps_raw, 0, 10)
    valid = bool(household) and any(
        value.strip() for value in (scenario, pain_raw, nps_raw)
    )
    return InterviewRecord(
        household=household,
        group=_cell(row, columns, "group"),
        city=_cell(row, columns, "city"),
        device=_cell(row, columns, "device"),
        scenario=scenario,
        current_practice=_cell(row, columns, "current_practice"),
        pain_raw=pain_raw,
        price_form=_cell(row, columns, "price_form"),
        nps_raw=nps_raw,
        quote=_cell(row, columns, "quote"),
        willing_test=_cell(row, columns, "willing_test"),
        note=_cell(row, columns, "note"),
        scenario_ok=scenario_ok,
        scenario_reason=scenario_reason,
        pain=pain,
        nps=nps,
        categories=classify_scenario(scenario),
        valid=valid,
    )


def nps_stats(scores: Sequence[int]) -> dict[str, object]:
    count = len(scores)
    if count == 0:
        return {
            "n": 0,
            "mean": None,
            "promoters": 0,
            "passives": 0,
            "detractors": 0,
            "nps": None,
        }
    promoters = sum(1 for score in scores if score >= 9)
    detractors = sum(1 for score in scores if score <= 6)
    return {
        "n": count,
        "mean": round(sum(scores) / count, 2),
        "promoters": promoters,
        "passives": count - promoters - detractors,
        "detractors": detractors,
        "nps": round((promoters - detractors) / count * 100, 1),
    }


def analyze(
    headers: Sequence[str],
    rows: Sequence[dict[str, str]],
    *,
    source: str = "",
    target: int = TARGET_HOUSEHOLDS,
    threshold: int = SCENARIO_THRESHOLD,
) -> Analysis:
    columns = resolve_columns(headers)
    records = [build_record(row, columns) for row in rows]
    valid = [record for record in records if record.valid]
    scenario_ok = [record for record in valid if record.scenario_ok]

    group_counter = Counter(record.group or "未填" for record in valid)
    group_counts = {
        group: group_counter.get(group, 0)
        for group in GROUP_ORDER
        if group_counter.get(group, 0)
    }

    category_counts = {name: [] for name, _ in SCENARIO_KEYWORDS}
    category_counts[OTHER_CATEGORY] = []
    for record in scenario_ok:
        for category in record.categories:
            category_counts.setdefault(category, []).append(record.household)

    pain_scores = [record.pain for record in valid if record.pain is not None]
    pain_dist = {level: 0 for level in range(5, 0, -1)}
    for score in pain_scores:
        pain_dist[score] += 1
    pain4_count = sum(1 for score in pain_scores if score >= 4)
    pain_mean = (
        round(sum(pain_scores) / len(pain_scores), 2) if pain_scores else None
    )

    nps_scores = [record.nps for record in valid if record.nps is not None]
    nps = nps_stats(nps_scores)  # type: ignore[arg-type]
    scenario_pass = len(scenario_ok) >= threshold

    gaps: list[str] = []
    if len(valid) < target:
        gaps.append(
            f"有效访谈户数 {len(valid)}/{target}，缺 {target - len(valid)} 户"
        )
    if len(scenario_ok) < threshold:
        gaps.append(
            f"关键场景可讲述户数 {len(scenario_ok)}/{target}，未达 ≥{threshold}/{target}"
        )
    if pain4_count < PAIN4_THRESHOLD:
        gaps.append(
            f"痛点 ≥4 户数 {pain4_count}/{target}，未达 ≥{PAIN4_THRESHOLD}/{target}"
        )

    followups: list[dict[str, str]] = []
    for record in valid:
        problems: list[str] = []
        actions: list[str] = []
        if not record.scenario_ok:
            problems.append(record.scenario_reason)
            actions.append(
                "按 §2.2 关键事件法重问：最近一次“差点出事/幸好没动”的时间、"
                "在场人、原话与设备动作"
            )
        if record.pain is None:
            problems.append("痛点 1–5 未填或无法解析")
            actions.append("补问痛点并按 1–5 口径回填")
        if record.nps is None:
            problems.append("NPS 0–10 未填或无法解析")
            actions.append("补问推荐意愿 0–10 并回填")
        if problems:
            followups.append(
                {
                    "household": record.household,
                    "issue": "；".join(problems),
                    "action": "；".join(actions),
                }
            )
    for record in records:
        if record.valid:
            continue
        followups.append(
            {
                "household": record.household or "（空户号）",
                "issue": "访谈记录为空（未完成）",
                "action": "按 45′ 提纲补访并回填 13 列",
            }
        )

    return Analysis(
        source=source,
        target=target,
        threshold=threshold,
        records=records,
        valid_records=valid,
        valid_count=len(valid),
        scenario_ok_records=scenario_ok,
        scenario_ok_count=len(scenario_ok),
        scenario_pass=scenario_pass,
        group_counts=group_counts,
        category_counts=category_counts,
        pain_dist=pain_dist,
        pain_missing=len(valid) - len(pain_scores),
        pain_mean=pain_mean,
        pain4_count=pain4_count,
        nps=nps,
        gaps=gaps,
        followups=followups,
    )


def _md_cell(value: object, empty: str = "（空）") -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    return text.replace("|", "\\|").replace("\n", " ")


def _pct(part: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{part / total * 100:.1f}%"


def render_report(analysis: Analysis) -> str:
    lines: list[str] = [
        "# G1 访谈分析报告",
        "",
        f"> 数据来源：`{analysis.source or '（未提供来源）'}`",
        "> 生成方式：`scripts/g1_analyze.py`，**规则判定，未调用大模型**",
        "> 口径：`docs/用户验证包-v0.1.md` §2.7 / §7-G1；"
        "`docs/招募帖与问卷-v0.1.md` §5",
        "",
        "## 一、样本概览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 登记户数 | {len(analysis.records)} |",
        f"| 有效户数 | {analysis.valid_count} |",
        f"| 目标户数 | {analysis.target} |",
        f"| 有具体关键场景户数 | {analysis.scenario_ok_count} |",
        f"| 痛点 ≥4 户数 | {analysis.pain4_count} |",
        "",
        "组别分布（有效户）：",
        "",
        "| 组别 | 户数 |",
        "| --- | --- |",
    ]
    for group, count in analysis.group_counts.items():
        lines.append(f"| {group} | {count} |")

    lines += [
        "",
        "## 二、场景关键词归类",
        "",
        f"> 分母为「关键场景可讲述」的 {analysis.scenario_ok_count} 户；"
        "一户可命中多个类别，因此占比之和不等于 100%。",
        "",
        "| 类别 | 命中户数 | 占可讲述户 | 户号 |",
        "| --- | --- | --- | --- |",
    ]
    for name, _ in SCENARIO_KEYWORDS:
        households = analysis.category_counts.get(name, [])
        lines.append(
            f"| {name} | {len(households)} | "
            f"{_pct(len(households), analysis.scenario_ok_count)} | "
            f"{'、'.join(households) or '—'} |"
        )
    others = analysis.category_counts.get(OTHER_CATEGORY, [])
    lines.append(
        f"| {OTHER_CATEGORY} | {len(others)} | "
        f"{_pct(len(others), analysis.scenario_ok_count)} | "
        f"{'、'.join(others) or '—'} |"
    )

    lines += [
        "",
        "## 三、痛点分布（1–5）",
        "",
        "| 分值 | 含义 | 户数 |",
        "| --- | --- | --- |",
    ]
    for level in range(5, 0, -1):
        lines.append(
            f"| {level} | {PAIN_LABELS[level]} | {analysis.pain_dist[level]} |"
        )
    lines.append(f"| 未填 | — | {analysis.pain_missing} |")
    lines.append(
        f"\n有效痛点回答 {analysis.valid_count - analysis.pain_missing} 户，"
        f"均值 {analysis.pain_mean if analysis.pain_mean is not None else '-'}；"
        f"痛点 ≥4 共 **{analysis.pain4_count}** 户。"
    )

    nps = analysis.nps
    lines += [
        "",
        "## 四、NPS（0–10）",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 有效回答 | {nps['n']} |",
        f"| 均值 | {nps['mean'] if nps['mean'] is not None else '-'} |",
        f"| 推荐者（9–10） | {nps['promoters']} |",
        f"| 被动者（7–8） | {nps['passives']} |",
        f"| 贬损者（0–6） | {nps['detractors']} |",
        f"| **NPS** | **{nps['nps'] if nps['nps'] is not None else '-'}** |",
        "",
        "> NPS = (推荐者 − 贬损者) / 有效回答 × 100，按 `用户验证包 §6` 指标 9。",
    ]

    scenario_mark = "通过" if analysis.scenario_pass else "未通过"
    valid_mark = "已满" if analysis.valid_count >= analysis.target else "未满"
    lines += [
        "",
        "## 五、G1 判据",
        "",
        "| 判据 | 实际 | 目标 | 结论 |",
        "| --- | --- | --- | --- |",
        f"| ① 关键场景非空且非“无/记不清” | {analysis.scenario_ok_count}/{analysis.target} | "
        f"≥{analysis.threshold}/{analysis.target}（60%） | {scenario_mark} |",
        f"| ② 痛点 ≥4 | {analysis.pain4_count}/{analysis.target} | "
        f"≥{PAIN4_THRESHOLD}/{analysis.target} | "
        f"{'通过' if analysis.pain4_count >= PAIN4_THRESHOLD else '未通过'} |",
        f"| ③ 明确“愿意为确认+审计付费”（口头） | 未采集 | ≥6/{analysis.target} | "
        "未计入（访谈模板无付费意愿列） |",
        f"| 有效访谈户数 | {analysis.valid_count}/{analysis.target} | "
        f"{analysis.target} | {valid_mark} |",
        "",
        f"**G1 场景判据结论：{scenario_mark}"
        f"（{analysis.scenario_ok_count}/{analysis.target} "
        f"{'≥' if analysis.scenario_pass else '<'} {analysis.threshold}/{analysis.target}）。**"
        + (
            f" 样本未满（有效 {analysis.valid_count}/{analysis.target}），"
            "该结论为阶段性结论，补访后需重算。"
            if analysis.valid_count < analysis.target
            else ""
        ),
    ]

    lines += [
        "",
        "## 六、逐户场景判定（可解释）",
        "",
        "| 户号 | 组别 | 关键场景 | scenario_ok | 理由 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in analysis.valid_records:
        lines.append(
            f"| {record.household} | {_md_cell(record.group, '—')} | "
            f"{_md_cell(record.scenario)} | "
            f"{'是' if record.scenario_ok else '否'} | "
            f"{_md_cell(record.scenario_reason, '—')} |"
        )

    lines += [
        "",
        "## 七、未达标项与建议补访",
        "",
        "### 未达标项",
        "",
    ]
    if analysis.gaps:
        lines.extend(f"- {gap}" for gap in analysis.gaps)
    else:
        lines.append("无。")

    lines += [
        "",
        "### 建议补访名单",
        "",
    ]
    if analysis.followups:
        lines += [
            "| 户号 | 问题 | 建议动作 |",
            "| --- | --- | --- |",
        ]
        for item in analysis.followups:
            lines.append(
                f"| {_md_cell(item['household'])} | {_md_cell(item['issue'])} | "
                f"{_md_cell(item['action'])} |"
            )
    else:
        lines.append("无。")

    lines += [
        "",
        "## 附：口径与限制",
        "",
        "- **判据分母固定为计划样本 20 户**（`--target-households` 可覆盖）；"
        "有效户不足 20 时如实标注“阶段性结论”，不把缺失户当通过。",
        "- **关键场景判定是关键词/精确匹配规则**：非空、非“无/没有/记不清”、"
        "去标点后 ≥4 字。规则可解释但可能漏判，逐户原文与理由均已保留，供人工复核。",
        "- **场景归类是多标签子串匹配**，一户可命中多个类别；"
        "未命中任何关键词的记入“其他”，不强行编造类别。",
        f"- **NPS** 按 0–10 分计算：{analysis.nps['promoters']} 推荐 / "
        f"{analysis.nps['passives']} 被动 / {analysis.nps['detractors']} 贬损；"
        "缺失回答不计入分母。",
        "- **付费意愿未采集**：访谈模板无“是否愿意付费”列，G1 判据③不臆造、不推断。",
        "- 本报告不使用 LLM，不需要联网。",
        "",
    ]
    return "\n".join(lines)


def format_summary(
    analysis: Analysis, *, dry_run: bool = False, out_path: Path | None = None
) -> str:
    categories = "、".join(
        f"{name} {len(analysis.category_counts.get(name, []))}"
        for name in [*(name for name, _ in SCENARIO_KEYWORDS), OTHER_CATEGORY]
        if analysis.category_counts.get(name)
    )
    lines = [
        f"G1 访谈分析：有效户 {analysis.valid_count}/{analysis.target}",
        f"  场景可讲述 {analysis.scenario_ok_count}/{analysis.target}"
        f"（判据 ≥{analysis.threshold}）→ "
        f"{'通过' if analysis.scenario_pass else '未通过'}",
        f"  痛点≥4 {analysis.pain4_count}/{analysis.target}；"
        f"NPS {analysis.nps['nps'] if analysis.nps['nps'] is not None else '-'}"
        f"（n={analysis.nps['n']}，均值 "
        f"{analysis.nps['mean'] if analysis.nps['mean'] is not None else '-'}）",
        f"  场景类别：{categories or '无'}",
        f"  建议补访：{len(analysis.followups)} 户"
        f"（未达标项 {len(analysis.gaps)} 条）",
    ]
    if dry_run:
        lines.append("  （dry-run：未写文件）")
    elif out_path is not None:
        lines.append(f"  报告：{out_path}")
    return "\n".join(lines)


def read_interview_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"CSV 缺少表头：{path}")
        headers = [header for header in reader.fieldnames if header is not None]
        rows: list[dict[str, str]] = []
        for raw in reader:
            values = {
                key: (value or "").strip()
                for key, value in raw.items()
                if key is not None
            }
            if not any(values.values()):
                continue
            rows.append(values)
    return headers, rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G1 访谈记录分析（场景/痛点/NPS/G1 判据/补访）"
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="访谈记录 CSV")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印摘要，不写文件")
    parser.add_argument(
        "--target-households",
        type=int,
        default=TARGET_HOUSEHOLDS,
        help="计划样本户数（判据分母，默认 20）",
    )
    parser.add_argument(
        "--scenario-threshold",
        type=int,
        default=SCENARIO_THRESHOLD,
        help="G1① 关键场景户数阈值（默认 12）",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"找不到访谈记录 CSV：{input_path}")
    if args.target_households <= 0:
        parser.error("--target-households 必须是正整数")
    if args.scenario_threshold <= 0:
        parser.error("--scenario-threshold 必须是正整数")

    headers, rows = read_interview_csv(input_path)
    analysis = analyze(
        headers,
        rows,
        source=str(input_path),
        target=args.target_households,
        threshold=args.scenario_threshold,
    )

    out_path = Path(args.out)
    print(format_summary(analysis, dry_run=args.dry_run, out_path=out_path))
    if args.dry_run:
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(analysis), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())