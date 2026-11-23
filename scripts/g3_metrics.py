"""G3 内测指标：周活 / 确认完成率 / 误报率 / 越权下发 / 确认 P90。

用法：
    python scripts/g3_metrics.py --events var/g3/events.csv --out-dir var/g3
    python scripts/g3_metrics.py --demo --out-dir var/g3
    python scripts/g3_metrics.py --events 事件.csv --cohort-size 10 --weeks 2 --attempt 1

口径（**docs/用户验证包-v0.1.md §7 G3**，不使用总纲 §6 的早期粗阈值）：
    10 户 × 2 周；
    ① 周活 ≥60%（6/10）；② 确认完成率 ≥80%；③ 误报率 ≤15%；
    ④ 越权下发 = 0；⑤ 确认时长 P90 ≤20s。
    任一不过 → 修 2 周复测一次；第 2 轮仍不过 → 转 B2B 或暂停。

输入事件字段：
    household_id,date,active,danger_commands,confirm_started,confirm_completed,
    confirm_seconds,false_positive,unauthorized_dispatch,note,synthetic

边界：
    - 同户同日重复行**合并**：数值列求和、active 取 OR、confirm_seconds 逐条保留；
      合并后再次校验 completed ≤ started、false_positive ≤ started。
    - P90 使用 nearest-rank：升序后取第 ceil(0.9 × n) 个（1-based），不插值；
      只统计 confirm_seconds > 0 的样本。
    - 缺失项（started=0、无 P90 样本）标“未采集”，**不得算通过**。
    - --from-audit **未实现**：hvsl audit 表没有 household_id / false_positive，
      越权需跨行窗口推断，无法与 CSV 同口径；请先导出事件 CSV。
    - 不接真实数据；--demo 全部标 SYNTHETIC DEMO，不得当作真实内测结论。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "var" / "g3"
DEFAULT_EVENTS = DEFAULT_DIR / "events.csv"

EVENT_FIELDS = [
    "household_id",
    "date",
    "active",
    "danger_commands",
    "confirm_started",
    "confirm_completed",
    "confirm_seconds",
    "false_positive",
    "unauthorized_dispatch",
    "note",
    "synthetic",
]

TARGET_COHORT = 10
TARGET_WEEKS = 2
WAU_THRESHOLD = 0.60
COMPLETION_THRESHOLD = 0.80
FALSE_POSITIVE_THRESHOLD = 0.15
UNAUTHORIZED_THRESHOLD = 0
P90_THRESHOLD = 20.0
P90_FRACTION = 0.9
P90_METHOD = "nearest-rank：升序后取第 ceil(0.9×n) 个（1-based），不插值"

SYNTHETIC_NOTICE = "SYNTHETIC DEMO — 不是真实内测数据"
SYNTHETIC_MARKDOWN_BANNER = (
    f"> ⚠️ **{SYNTHETIC_NOTICE}**\n"
    ">\n"
    "> 本报告使用合成事件，仅用于演示指标计算与页面格式；"
    "不得作为真实内测结论或 go/no-go 依据。\n\n"
)


class MetricsError(Exception):
    """参数/文件/格式错误 → 退出码 2。"""


@dataclass
class ValidationIssue:
    row: int
    household_id: str
    field: str
    message: str

    def as_text(self) -> str:
        return (
            f"第 {self.row} 行 {self.household_id or '（无户号）'}："
            f"{self.field}：{self.message}"
        )


@dataclass
class DayEvent:
    household_id: str
    day: date
    active: bool
    danger_commands: int = 0
    confirm_started: int = 0
    confirm_completed: int = 0
    confirm_seconds: list[float] = field(default_factory=list)
    false_positive: int = 0
    unauthorized_dispatch: int = 0
    note: str = ""
    synthetic: bool = False
    row: int = 0


def normalize_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_bool(value: object) -> bool | None:
    text = normalize_text(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def parse_count(value: object, field_name: str) -> int:
    text = normalize_text(value)
    if not text:
        return 0
    try:
        number = int(text)
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是非负整数：{value!r}") from error
    if number < 0:
        raise ValueError(f"{field_name} 不能为负：{number}")
    return number


def parse_seconds(value: object) -> float:
    text = normalize_text(value)
    if not text:
        return 0.0
    try:
        number = float(text)
    except ValueError as error:
        raise ValueError(f"confirm_seconds 必须是数字：{value!r}") from error
    if number < 0:
        raise ValueError(f"confirm_seconds 不能为负：{number}")
    return number


def parse_day(value: object) -> date:
    text = normalize_text(value)
    if not text:
        raise ValueError("date 不能为空")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as error:
        raise ValueError(f"date 应为 ISO 8601 日期：{value!r}") from error


def read_event_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise MetricsError(f"找不到事件 CSV：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise MetricsError(f"事件 CSV 缺少表头：{path}")
        missing = [field_name for field_name in EVENT_FIELDS if field_name not in reader.fieldnames]
        if missing:
            raise MetricsError(f"事件 CSV 缺少字段：{missing}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            values = {field_name: normalize_text(raw.get(field_name)) for field_name in EVENT_FIELDS}
            if not any(values.values()):
                continue
            rows.append(values)
    return rows


def build_events(
    rows: Sequence[Mapping[str, str]],
) -> tuple[list[DayEvent], list[ValidationIssue], int]:
    """逐行校验 + 同户同日合并。返回（合并后事件，问题，合并次数）。"""
    issues: list[ValidationIssue] = []
    parsed: list[DayEvent] = []

    for index, row in enumerate(rows, start=1):
        household = normalize_text(row.get("household_id"))
        if not household:
            issues.append(ValidationIssue(index, "", "household_id", "户号不能为空"))

        try:
            day = parse_day(row.get("date"))
        except ValueError as error:
            issues.append(ValidationIssue(index, household, "date", str(error)))
            day = None

        active = parse_bool(row.get("active"))
        if active is None:
            issues.append(
                ValidationIssue(
                    index,
                    household,
                    "active",
                    f"布尔值只能是 true/false/1/0/yes/no，实际 {row.get('active')!r}",
                )
            )

        values: dict[str, int] = {}
        for field_name in (
            "danger_commands",
            "confirm_started",
            "confirm_completed",
            "false_positive",
            "unauthorized_dispatch",
        ):
            try:
                values[field_name] = parse_count(row.get(field_name), field_name)
            except ValueError as error:
                issues.append(ValidationIssue(index, household, field_name, str(error)))
                values[field_name] = -1

        try:
            seconds = parse_seconds(row.get("confirm_seconds"))
        except ValueError as error:
            issues.append(ValidationIssue(index, household, "confirm_seconds", str(error)))
            seconds = -1.0

        synthetic = parse_bool(row.get("synthetic"))
        if synthetic is None:
            issues.append(
                ValidationIssue(
                    index,
                    household,
                    "synthetic",
                    f"布尔值只能是 true/false/1/0/yes/no，实际 {row.get('synthetic')!r}",
                )
            )
            synthetic = False

        if values["confirm_completed"] > values["confirm_started"] >= 0:
            issues.append(
                ValidationIssue(
                    index,
                    household,
                    "confirm_completed",
                    f"完成数 {values['confirm_completed']} 不能大于发起数 {values['confirm_started']}",
                )
            )
        if values["false_positive"] > values["confirm_started"] >= 0:
            issues.append(
                ValidationIssue(
                    index,
                    household,
                    "false_positive",
                    f"误报数 {values['false_positive']} 不能大于发起数 {values['confirm_started']}",
                )
            )

        if (
            not household
            or day is None
            or active is None
            or any(value < 0 for value in values.values())
            or seconds < 0
        ):
            continue

        parsed.append(
            DayEvent(
                household_id=household,
                day=day,
                active=active,
                danger_commands=values["danger_commands"],
                confirm_started=values["confirm_started"],
                confirm_completed=values["confirm_completed"],
                confirm_seconds=[seconds] if seconds > 0 else [],
                false_positive=values["false_positive"],
                unauthorized_dispatch=values["unauthorized_dispatch"],
                note=normalize_text(row.get("note")),
                synthetic=bool(synthetic),
                row=index,
            )
        )

    merged: dict[tuple[str, date], DayEvent] = {}
    duplicates = 0
    for event in parsed:
        key = (event.household_id, event.day)
        base = merged.get(key)
        if base is None:
            merged[key] = event
            continue
        duplicates += 1
        base.active = base.active or event.active
        base.danger_commands += event.danger_commands
        base.confirm_started += event.confirm_started
        base.confirm_completed += event.confirm_completed
        base.confirm_seconds.extend(event.confirm_seconds)
        base.false_positive += event.false_positive
        base.unauthorized_dispatch += event.unauthorized_dispatch
        base.synthetic = base.synthetic or event.synthetic
        notes = [note for note in (base.note, event.note) if note]
        base.note = "；".join(dict.fromkeys(notes))

    merged_events = sorted(merged.values(), key=lambda item: (item.household_id, item.day))
    for event in merged_events:
        if event.confirm_completed > event.confirm_started:
            issues.append(
                ValidationIssue(
                    event.row,
                    event.household_id,
                    "confirm_completed",
                    f"合并后完成数 {event.confirm_completed} 大于发起数 {event.confirm_started}",
                )
            )
        if event.false_positive > event.confirm_started:
            issues.append(
                ValidationIssue(
                    event.row,
                    event.household_id,
                    "false_positive",
                    f"合并后误报数 {event.false_positive} 大于发起数 {event.confirm_started}",
                )
            )
    return merged_events, issues, duplicates


def percentile_nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    """nearest-rank 分位数：升序第 ceil(fraction × n) 个（1-based），不插值。"""
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil(fraction * len(ordered)) - 1
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def iso_week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def iso_week_label(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _criterion(
    label: str, value: float | None, threshold: float, comparator: str
) -> dict:
    if value is None:
        return {
            "label": label,
            "value": None,
            "threshold": threshold,
            "comparator": comparator,
            "collected": False,
            "pass": False,
            "status": "未采集",
        }
    passed = value >= threshold if comparator == ">=" else value <= threshold
    return {
        "label": label,
        "value": value,
        "threshold": threshold,
        "comparator": comparator,
        "collected": True,
        "pass": passed,
        "status": "pass" if passed else "fail",
    }


def _seconds_of(events: Sequence[DayEvent]) -> list[float]:
    return [seconds for event in events for seconds in event.confirm_seconds]


def _week_bucket(events: Sequence[DayEvent], cohort_size: int) -> dict:
    week_start = iso_week_start(events[0].day)
    active_households = len({event.household_id for event in events if event.active})
    started = sum(event.confirm_started for event in events)
    completed = sum(event.confirm_completed for event in events)
    false_positive = sum(event.false_positive for event in events)
    unauthorized = sum(event.unauthorized_dispatch for event in events)
    seconds = _seconds_of(events)
    return {
        "week_start": week_start.isoformat(),
        "iso_week": iso_week_label(events[0].day),
        "households": len({event.household_id for event in events}),
        "active_households": active_households,
        "wau_rate": _rate(active_households, cohort_size),
        "confirm_started": started,
        "confirm_completed": completed,
        "confirm_completion_rate": _rate(completed, started),
        "false_positive": false_positive,
        "false_positive_rate": _rate(false_positive, started),
        "unauthorized_dispatch": unauthorized,
        "confirm_samples": len(seconds),
        "confirm_p90_seconds": percentile_nearest_rank(seconds, P90_FRACTION),
    }


def _household_bucket(events: Sequence[DayEvent]) -> dict:
    started = sum(event.confirm_started for event in events)
    completed = sum(event.confirm_completed for event in events)
    false_positive = sum(event.false_positive for event in events)
    unauthorized = sum(event.unauthorized_dispatch for event in events)
    seconds = _seconds_of(events)
    return {
        "active_days": sum(1 for event in events if event.active),
        "active": any(event.active for event in events),
        "danger_commands": sum(event.danger_commands for event in events),
        "confirm_started": started,
        "confirm_completed": completed,
        "confirm_completion_rate": _rate(completed, started),
        "false_positive": false_positive,
        "false_positive_rate": _rate(false_positive, started),
        "unauthorized_dispatch": unauthorized,
        "confirm_samples": len(seconds),
        "confirm_p90_seconds": percentile_nearest_rank(seconds, P90_FRACTION),
    }


def compute_metrics(
    events: Sequence[DayEvent],
    *,
    source: str = "",
    cohort_size: int = TARGET_COHORT,
    weeks: int = TARGET_WEEKS,
    attempt: int = 1,
    synthetic: bool | None = None,
    duplicates_merged: int = 0,
) -> dict:
    households = sorted({event.household_id for event in events})
    days = sorted({event.day for event in events})
    active_households = len({event.household_id for event in events if event.active})
    started = sum(event.confirm_started for event in events)
    completed = sum(event.confirm_completed for event in events)
    false_positive = sum(event.false_positive for event in events)
    unauthorized = sum(event.unauthorized_dispatch for event in events)
    seconds = _seconds_of(events)
    p90 = percentile_nearest_rank(seconds, P90_FRACTION)

    observed_days = len(days)
    expected_days = weeks * 7
    sample_complete = (
        len(households) >= cohort_size and observed_days >= expected_days
    )

    by_week_map: dict[date, list[DayEvent]] = defaultdict(list)
    by_household_map: dict[str, list[DayEvent]] = defaultdict(list)
    for event in events:
        by_week_map[iso_week_start(event.day)].append(event)
        by_household_map[event.household_id].append(event)

    by_week = [
        _week_bucket(by_week_map[key], cohort_size) for key in sorted(by_week_map)
    ]
    by_household = {
        household: _household_bucket(by_household_map[household])
        for household in households
    }

    criteria = {
        "wau": _criterion(
            "周活 ≥60%（6/10）",
            _rate(active_households, cohort_size),
            WAU_THRESHOLD,
            ">=",
        ),
        "confirm_completion": _criterion(
            "确认完成率 ≥80%",
            _rate(completed, started),
            COMPLETION_THRESHOLD,
            ">=",
        ),
        "false_positive": _criterion(
            "误报率 ≤15%",
            _rate(false_positive, started),
            FALSE_POSITIVE_THRESHOLD,
            "<=",
        ),
        "unauthorized_dispatch": _criterion(
            "越权下发 = 0", unauthorized, UNAUTHORIZED_THRESHOLD, "<="
        ),
        "confirm_p90": _criterion(
            "确认时长 P90 ≤20s", p90, P90_THRESHOLD, "<="
        ),
    }
    g3_pass = all(item["pass"] for item in criteria.values())

    if not sample_complete:
        action = "修 2 周复测"
        action_reason = (
            f"样本未满（{len(households)}/{cohort_size} 户，"
            f"{observed_days}/{expected_days} 天）：阶段性结论，不足以 go"
        )
    elif g3_pass:
        action = "go"
        action_reason = "五项判据全过：进入 G4 付费/留存观察"
    elif attempt >= 2:
        action = "转 B2B 或暂停"
        action_reason = "第 2 轮复测仍不过：按 §7 G3 转 B2B 或暂停"
    else:
        action = "修 2 周复测"
        action_reason = "第 1 轮未过：修 2 周后复测一次"

    gaps: list[str] = []
    for item in criteria.values():
        if item["pass"]:
            continue
        if not item["collected"]:
            gaps.append(f"{item['label']}：未采集（不得算通过）")
        else:
            gaps.append(
                f"{item['label']}：实际 {item['value']}，"
                f"阈值 {item['comparator']} {item['threshold']}"
            )
    if not sample_complete:
        gaps.append(
            f"样本覆盖 {len(households)}/{cohort_size} 户、"
            f"{observed_days}/{expected_days} 天"
        )

    if synthetic is None:
        synthetic = any(event.synthetic for event in events)

    return {
        "source": source or None,
        "synthetic_demo": bool(synthetic),
        "notice": (
            SYNTHETIC_NOTICE
            if synthetic
            else "真实内测事件；不含原句、设备名与个人信息"
        ),
        "cohort_size": cohort_size,
        "weeks": weeks,
        "attempt": attempt,
        "observed_households": len(households),
        "observed_days": observed_days,
        "expected_days": expected_days,
        "sample_complete": sample_complete,
        "duplicates_merged": duplicates_merged,
        "active_households": active_households,
        "wau_rate": _rate(active_households, cohort_size),
        "confirm_started": started,
        "confirm_completed": completed,
        "confirm_completion_rate": _rate(completed, started),
        "false_positive": false_positive,
        "false_positive_rate": _rate(false_positive, started),
        "unauthorized_dispatch_total": unauthorized,
        "confirm_samples": len(seconds),
        "confirm_p90_seconds": p90,
        "p90_method": P90_METHOD,
        "by_week": by_week,
        "by_household": by_household,
        "criteria": criteria,
        "g3_pass": g3_pass,
        "g3_action": action,
        "g3_action_reason": action_reason,
        "gaps": gaps,
    }

def _pct(value: object) -> str:
    if value is None:
        return "未采集"
    return f"{float(value) * 100:.1f}%"


def _secs(value: object) -> str:
    if value is None:
        return "未采集"
    return f"{float(value):.1f}s"


def _status_text(item: Mapping[str, object]) -> str:
    return {
        "pass": "通过",
        "fail": "未通过",
        "未采集": "未采集",
    }.get(str(item.get("status")), "未采集")


def _criterion_actual(
    key: str, item: Mapping[str, object], summary: Mapping[str, object]
) -> str:
    if not item.get("collected"):
        return "未采集"
    value = item.get("value")
    if key == "wau":
        return (
            f"{_pct(value)}（{summary.get('active_households')}"
            f"/{summary.get('cohort_size')}）"
        )
    if key == "confirm_completion":
        return (
            f"{_pct(value)}（{summary.get('confirm_completed')}"
            f"/{summary.get('confirm_started')}）"
        )
    if key == "false_positive":
        return (
            f"{_pct(value)}（{summary.get('false_positive')}"
            f"/{summary.get('confirm_started')}）"
        )
    if key == "unauthorized_dispatch":
        return str(value)
    return f"{_secs(value)}（n={summary.get('confirm_samples')}）"


def render_report(summary: Mapping[str, object]) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    criteria = summary.get("criteria") or {}
    by_week = list(summary.get("by_week") or [])
    by_household = summary.get("by_household") or {}
    gaps = list(summary.get("gaps") or [])

    lines: list[str] = []
    if synthetic:
        lines.append(SYNTHETIC_MARKDOWN_BANNER)
    lines += [
        "# G3 内测指标报告",
        "",
        f"> 数据来源：`{summary.get('source') or '（未提供来源）'}`",
        "> 口径：`docs/用户验证包-v0.1.md` §7 G3（10 户 × 2 周，五项判据）",
        "",
        "## 一、五项判据",
        "",
        "| 判据 | 实际 | 门槛 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    thresholds = {
        "wau": "≥60%（6/10）",
        "confirm_completion": "≥80%",
        "false_positive": "≤15%",
        "unauthorized_dispatch": "=0",
        "confirm_p90": "≤20s",
    }
    for key in (
        "wau",
        "confirm_completion",
        "false_positive",
        "unauthorized_dispatch",
        "confirm_p90",
    ):
        item = criteria.get(key, {})
        lines.append(
            f"| {item.get('label', key)} | "
            f"{_criterion_actual(key, item, summary)} | "
            f"{thresholds[key]} | {_status_text(item)} |"
        )

    lines += [
        "",
        f"**G3 结论：{'通过' if summary.get('g3_pass') else '未通过'} "
        f"→ 当前动作：{summary.get('g3_action')}。**",
        "",
        f"> {summary.get('g3_action_reason')}",
    ]
    if not summary.get("sample_complete"):
        lines += [
            "",
            f"> **阶段性结论**：实际覆盖 {summary.get('observed_households')}"
            f"/{summary.get('cohort_size')} 户、"
            f"{summary.get('observed_days')}/{summary.get('expected_days')} 天，"
            "样本不足时不判 go，缺失项不得算通过。",
        ]

    lines += ["", "## 二、缺口", ""]
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("无（五项判据均满足且样本覆盖完整）。")

    lines += [
        "",
        "## 三、样本概览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 计划样本 | {summary.get('cohort_size')} 户 × "
        f"{summary.get('weeks')} 周（{summary.get('expected_days')} 天） |",
        f"| 实际覆盖 | {summary.get('observed_households')} 户、"
        f"{summary.get('observed_days')} 天 |",
        f"| 同户同日重复合并 | {summary.get('duplicates_merged')} 行 |",
        f"| 确认时长样本数 | {summary.get('confirm_samples')} 条 |",
        f"| 当前轮次 | 第 {summary.get('attempt')} 轮 |",
        "",
        "## 四、周趋势（ISO 周，周一为周首）",
        "",
        "| 周 | 活跃户 | WAU | 完成率 | 误报率 | 越权 | P90 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for week in by_week:
        lines.append(
            f"| {week.get('iso_week')}（{week.get('week_start')}） | "
            f"{week.get('active_households')}/{summary.get('cohort_size')} | "
            f"{_pct(week.get('wau_rate'))} | "
            f"{_pct(week.get('confirm_completion_rate'))} | "
            f"{_pct(week.get('false_positive_rate'))} | "
            f"{week.get('unauthorized_dispatch')} | "
            f"{_secs(week.get('confirm_p90_seconds'))} |"
        )

    lines += [
        "",
        "## 五、按户明细",
        "",
        "| 户号 | 活跃天 | 完成率 | 误报率 | 越权 | P90 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for household, bucket in by_household.items():
        lines.append(
            f"| {household} | {bucket.get('active_days')} | "
            f"{_pct(bucket.get('confirm_completion_rate'))} | "
            f"{_pct(bucket.get('false_positive_rate'))} | "
            f"{bucket.get('unauthorized_dispatch')} | "
            f"{_secs(bucket.get('confirm_p90_seconds'))} |"
        )

    lines += [
        "",
        "## 六、口径与限制",
        "",
        "- **周活（WAU）**：整体口径 = 观测期内至少 1 天 `active=true` 的去重户数 / "
        "`cohort_size`（默认 10）；`by_week` 另给每周口径。",
        "- **确认完成率** = Σ`confirm_completed` / Σ`confirm_started`；"
        "**误报率** = Σ`false_positive` / Σ`confirm_started`；"
        "分母为 0 时标“未采集”，不得算通过。",
        "- **越权下发**：绝对计数，阈值 0；任一 >0 即判失败，不做比例稀释。",
        f"- **确认 P90**：{P90_METHOD}；仅统计 `confirm_seconds > 0` 的样本。",
        "- **同户同日重复**：数值列求和合并、`active` 取 OR、"
        "`confirm_seconds` 逐条保留；合并后再次校验完成数/误报数不超过发起数。",
        "- **未采集不等于通过**：started=0 或无 P90 样本时，该项判 `未采集`，"
        "并使 `g3_pass=false`。",
        "- **样本不足**：实际户数/天数低于计划时标“阶段性结论”，不判 go。",
        "- **--from-audit 未实现**：hvsl `audit` 表没有 `household_id` / "
        "`false_positive`，越权也需跨行窗口推断，无法与事件 CSV 同口径；"
        "本工具仅接受 CSV 输入。",
        "- 不接真实数据；合成 demo 的数字不得当作真实内测结论。",
        "",
    ]
    return "\n".join(lines)


def format_summary(
    summary: Mapping[str, object], *, out_dir: Path | None = None
) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    criteria = summary.get("criteria") or {}

    def value_of(key: str, formatter) -> str:
        item = criteria.get(key, {})
        return formatter(item.get("value")) if item.get("collected") else "未采集"

    lines = [
        "G3 指标报告" + ("（SYNTHETIC DEMO）" if synthetic else ""),
        f"  周活 {value_of('wau', _pct)}"
        f"（{summary.get('active_households')}/{summary.get('cohort_size')}）；"
        f"完成率 {value_of('confirm_completion', _pct)}；"
        f"误报率 {value_of('false_positive', _pct)}；"
        f"越权 {summary.get('unauthorized_dispatch_total')}；"
        f"P90 {value_of('confirm_p90', _secs)}",
        f"  样本：{summary.get('observed_households')}/{summary.get('cohort_size')} 户、"
        f"{summary.get('observed_days')}/{summary.get('expected_days')} 天；"
        f"重复合并 {summary.get('duplicates_merged')} 行；"
        f"确认样本 {summary.get('confirm_samples')} 条",
        f"  G3：{'通过' if summary.get('g3_pass') else '未通过'}"
        f" → {summary.get('g3_action')}",
        "  缺口：" + ("；".join(summary.get("gaps") or []) or "无"),
    ]
    if out_dir is not None:
        lines.append(f"  产物：{out_dir}")
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
main { max-width: 1080px; margin: 0 auto; padding: 20px; }
h1 { font-size: 24px; margin: 12px 0 4px; }
h2 { font-size: 17px; margin: 24px 0 10px; border-left: 4px solid #b00020; padding-left: 8px; }
.meta { color: #5a5a66; font-size: 13px; margin: 0 0 8px; }
.criteria { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
.criterion { background: #fff; border: 1px solid #e3e4e8; border-radius: 8px; padding: 12px 14px; border-top-width: 4px; }
.criterion.pass { border-top-color: #1f7a4d; }
.criterion.fail { border-top-color: #b00020; }
.criterion.missing { border-top-color: #b26a00; }
.criterion-label { font-size: 12px; color: #6a6a76; }
.criterion-value { font-size: 26px; font-weight: 700; margin: 4px 0; font-variant-numeric: tabular-nums; }
.criterion-note { font-size: 12px; color: #6a6a76; }
table { width: 100%; border-collapse: collapse; background: #fff; font-size: 13px; }
th, td { border: 1px solid #e3e4e8; padding: 8px; text-align: left; vertical-align: top; }
th { background: #eef0f3; }
.judgement { background: #fff; border: 1px solid #e3e4e8; border-radius: 8px; padding: 10px 14px; }
.pass-text { color: #1f7a4d; font-weight: 700; }
.fail-text { color: #b00020; font-weight: 700; }
footer { margin-top: 28px; font-size: 12px; color: #6a6a76; border-top: 1px solid #e3e4e8; padding-top: 12px; }
.warn { color: #b00020; font-weight: 600; }
@media (max-width: 640px) { table { font-size: 12px; } }
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _card(
    label: str,
    value: str,
    note: str,
    status: object,
) -> str:
    status_text = str(status)
    status_class = {
        "pass": "pass",
        "fail": "fail",
        "未采集": "missing",
    }.get(status_text, "missing")
    return (
        f'<div class="criterion {status_class}">'
        f'<div class="criterion-label">{_esc(label)}</div>'
        f'<div class="criterion-value">{_esc(value)}</div>'
        f'<div class="criterion-note">{_esc(note)}</div>'
        "</div>"
    )


def render_dashboard(summary: Mapping[str, object]) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    criteria = summary.get("criteria") or {}
    by_week = list(summary.get("by_week") or [])
    by_household = summary.get("by_household") or {}

    title = "G3 看板 · SYNTHETIC DEMO" if synthetic else "G3 看板 · 内测五项判据"
    banner_class = "banner banner-demo" if synthetic else "banner banner-real"
    banner_text = (
        SYNTHETIC_NOTICE
        if synthetic
        else "真实内测事件 · 请核对样本覆盖与来源后再引用"
    )
    state = "通过" if summary.get("g3_pass") else "未通过"
    state_class = "pass-text" if summary.get("g3_pass") else "fail-text"

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
        f'<p class="meta">数据来源：{_esc(summary.get("source") or "（未提供来源）")}'
        " ｜ 由 <code>scripts/g3_metrics.py</code> 生成，数字均由事件 CSV 计算</p>",
        "<section>",
        "<h2>五项判据</h2>",
        '<div class="criteria">',
    ]

    card_specs = (
        ("wau", "周活", "≥60%", f"{summary.get('active_households')}/{summary.get('cohort_size')} 户"),
        (
            "confirm_completion",
            "确认完成率",
            "≥80%",
            f"{summary.get('confirm_completed')}/{summary.get('confirm_started')} 次",
        ),
        (
            "false_positive",
            "误报率",
            "≤15%",
            f"{summary.get('false_positive')}/{summary.get('confirm_started')} 次",
        ),
        (
            "unauthorized_dispatch",
            "越权下发",
            "=0",
            f"总 {summary.get('unauthorized_dispatch_total')} 次",
        ),
        (
            "confirm_p90",
            "确认 P90",
            "≤20s",
            f"n={summary.get('confirm_samples')}",
        ),
    )
    for key, label, threshold, note in card_specs:
        item = criteria.get(key, {})
        if not item.get("collected"):
            display = "未采集"
        elif key in {"wau", "confirm_completion", "false_positive"}:
            display = _pct(item.get("value"))
        elif key == "confirm_p90":
            display = _secs(item.get("value"))
        else:
            display = str(item.get("value"))
        parts.append(
            _card(
                label,
                display,
                f"{threshold} · {note} · {_status_text(item)}",
                item.get("status"),
            )
        )

    parts += [
        "</div>",
        f'<p class="judgement">G3 结论：'
        f'<span class="{state_class}">{_esc(state)}</span>'
        f" → 动作：{_esc(summary.get('g3_action'))}。"
        f" {_esc(summary.get('g3_action_reason'))}</p>",
        "<section>",
        "<h2>周趋势（ISO 周）</h2>",
        "<table>",
        "<thead><tr><th>周</th><th>活跃户</th><th>WAU</th>"
        "<th>完成率</th><th>误报率</th><th>越权</th><th>P90</th></tr></thead>",
        "<tbody>",
    ]
    for week in by_week:
        parts.append(
            "<tr>"
            f"<td>{_esc(week.get('iso_week'))}<br>"
            f"<small>{_esc(week.get('week_start'))}</small></td>"
            f"<td>{_esc(week.get('active_households'))}"
            f"/{_esc(summary.get('cohort_size'))}</td>"
            f"<td>{_esc(_pct(week.get('wau_rate')))}</td>"
            f"<td>{_esc(_pct(week.get('confirm_completion_rate')))}</td>"
            f"<td>{_esc(_pct(week.get('false_positive_rate')))}</td>"
            f"<td>{_esc(week.get('unauthorized_dispatch'))}</td>"
            f"<td>{_esc(_secs(week.get('confirm_p90_seconds')))}</td>"
            "</tr>"
        )
    parts += [
        "</tbody>",
        "</table>",
        "</section>",
        "<section>",
        "<h2>按户明细</h2>",
        "<table>",
        "<thead><tr><th>户号</th><th>活跃天</th><th>完成率</th>"
        "<th>误报率</th><th>越权</th><th>P90</th></tr></thead>",
        "<tbody>",
    ]
    for household, bucket in by_household.items():
        parts.append(
            "<tr>"
            f"<td>{_esc(household)}</td>"
            f"<td>{_esc(bucket.get('active_days'))}</td>"
            f"<td>{_esc(_pct(bucket.get('confirm_completion_rate')))}</td>"
            f"<td>{_esc(_pct(bucket.get('false_positive_rate')))}</td>"
            f"<td>{_esc(bucket.get('unauthorized_dispatch'))}</td>"
            f"<td>{_esc(_secs(bucket.get('confirm_p90_seconds')))}</td>"
            "</tr>"
        )
    parts += [
        "</tbody>",
        "</table>",
        "</section>",
        "</section>",
        "<footer>",
        "<p>口径：10 户 × 2 周；周活 ≥60%、确认完成率 ≥80%、误报率 ≤15%、"
        "越权下发 0、确认 P90 ≤20s（<code>docs/用户验证包-v0.1.md</code> §7 G3）。</p>",
        f"<p>P90 方法：{_esc(P90_METHOD)}；样本不足时标“阶段性结论”，"
        "缺失项不得算通过。</p>",
        "<p>本页由事件 CSV 计算生成，仅描述样本，不做统计显著性检验，"
        "不构成验证结论。</p>",
    ]
    if synthetic:
        parts.append(
            f'<p class="warn">⚠️ {_esc(SYNTHETIC_NOTICE)}：'
            "本页所有数字来自合成事件，仅用于演示页面格式，"
            "不得作为真实内测结论或 go/no-go 判断依据。</p>"
        )
    parts += [
        "</footer>",
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)

DEMO_START = date(2026, 11, 2)
DEMO_HOUSEHOLDS = [f"SYN-HH-{index:02d}" for index in range(1, 11)]


def build_demo_rows() -> list[dict[str, str]]:
    """10 户 × 14 天 synthetic：7/10 周活、85% 完成、10% 误报、0 越权、P90 18s。"""
    rows: list[dict[str, str]] = []
    for household_index, household in enumerate(DEMO_HOUSEHOLDS, start=1):
        household_active = household_index <= 7
        for day_index in range(14):
            day = DEMO_START + timedelta(days=day_index)
            if household_active:
                slow = (household_index <= 4 and day_index == 0) or (
                    household_index in (5, 6, 7) and day_index == 7
                )
                rows.append(
                    {
                        "household_id": household,
                        "date": day.isoformat(),
                        "active": "true",
                        "danger_commands": "5",
                        "confirm_started": "20",
                        "confirm_completed": "17",
                        "confirm_seconds": "30" if slow else "18",
                        "false_positive": "2",
                        "unauthorized_dispatch": "0",
                        "note": SYNTHETIC_NOTICE,
                        "synthetic": "true",
                    }
                )
            else:
                rows.append(
                    {
                        "household_id": household,
                        "date": day.isoformat(),
                        "active": "false",
                        "danger_commands": "0",
                        "confirm_started": "0",
                        "confirm_completed": "0",
                        "confirm_seconds": "0",
                        "false_positive": "0",
                        "unauthorized_dispatch": "0",
                        "note": f"{SYNTHETIC_NOTICE}（未活跃户）",
                        "synthetic": "true",
                    }
                )
    return rows


def write_events(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=EVENT_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field_name: row.get(field_name, "") for field_name in EVENT_FIELDS})


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_outputs(summary: Mapping[str, object], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "g3_report.md").write_text(
        render_report(summary), encoding="utf-8"
    )
    (out_dir / "dashboard.html").write_text(
        render_dashboard(summary), encoding="utf-8"
    )


def _print_issues(prefix: str, issues: Sequence[ValidationIssue]) -> None:
    print(f"{prefix}：{len(issues)} 个问题")
    for issue in issues:
        print("  - " + issue.as_text())


def run_csv(
    events_path: Path,
    out_dir: Path,
    *,
    cohort_size: int,
    weeks: int,
    attempt: int,
) -> int:
    rows = read_event_csv(events_path)
    events, issues, duplicates = build_events(rows)
    if issues:
        _print_issues("数据校验失败，未生成报告", issues)
        return 1

    summary = compute_metrics(
        events,
        source=str(events_path),
        cohort_size=cohort_size,
        weeks=weeks,
        attempt=attempt,
        duplicates_merged=duplicates,
    )
    _write_outputs(summary, out_dir)
    print(f"  事件：{events_path}")
    print(format_summary(summary, out_dir=out_dir))
    return 0


def run_demo(
    out_root: Path, *, cohort_size: int, weeks: int, attempt: int
) -> int:
    out_dir = out_root / "demo" / "out"
    rows = build_demo_rows()
    events_path = out_dir / "events.csv"
    write_events(events_path, rows)

    events, issues, duplicates = build_events(rows)
    if issues:
        raise MetricsError(
            "内置合成事件校验失败："
            + "；".join(issue.as_text() for issue in issues)
        )

    summary = compute_metrics(
        events,
        source=str(events_path),
        cohort_size=cohort_size,
        weeks=weeks,
        attempt=attempt,
        synthetic=True,
        duplicates_merged=duplicates,
    )
    _write_outputs(summary, out_dir)
    print(f"  事件：{events_path}")
    print(format_summary(summary, out_dir=out_dir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G3 内测指标（周活/完成率/误报率/越权/P90 + 五项判据）"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--events", help="事件 CSV")
    group.add_argument(
        "--demo", action="store_true", help="内置 10 户×14 天 synthetic 事件"
    )
    group.add_argument(
        "--from-audit",
        metavar="DB",
        help="从 hvsl SQLite audit 表聚合（未实现，仅 CSV 输入）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--cohort-size", type=int, default=TARGET_COHORT, help="计划户数（默认 10）"
    )
    parser.add_argument(
        "--weeks", type=int, default=TARGET_WEEKS, help="计划周数（默认 2）"
    )
    parser.add_argument(
        "--attempt",
        type=int,
        choices=(1, 2),
        default=1,
        help="复测轮次：1=首轮，2=修 2 周后的复测",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cohort_size <= 0:
            raise MetricsError("--cohort-size 必须是正整数")
        if args.weeks <= 0:
            raise MetricsError("--weeks 必须是正整数")
        if args.from_audit:
            print(
                "未实现 --from-audit：hvsl audit 表没有 household_id / "
                "false_positive，越权下发也需跨行窗口推断，无法与事件 CSV "
                "同口径。请先导出事件 CSV 后使用 --events。",
                file=sys.stderr,
            )
            return 2
        if args.demo:
            return run_demo(
                Path(args.out_dir),
                cohort_size=args.cohort_size,
                weeks=args.weeks,
                attempt=args.attempt,
            )
        if not args.events:
            raise MetricsError("请使用 --events、--demo 或 --from-audit")
        return run_csv(
            Path(args.events),
            Path(args.out_dir),
            cohort_size=args.cohort_size,
            weeks=args.weeks,
            attempt=args.attempt,
        )
    except MetricsError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())