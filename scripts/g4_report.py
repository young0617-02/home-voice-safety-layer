"""G4 留存/转化报告：读取 G3 summary + G2 summary + NPS CSV → 四项判据。

用法：
    python scripts/g4_report.py \
        --g3-summary var/g3/summary.json \
        --g2-summary var/g2/summary.json \
        --nps var/g4/nps.csv \
        --cohort-size 10 --cohort-id g3 --out-dir var/g4
    python scripts/g4_report.py --demo --out-dir var/g4

口径（docs/用户验证包-v0.1.md §7 G4，第 10 周末）：
    ① 4 周留存 ≥50%；② 内测户付费转化 ≥30%；
    ③ NPS ≥30；④ 退款率 ≤10%。任一不过 → 转 B2B。

字段来源（**读取真实产物，不另造字段名**）：
    - G3 summary（scripts/g3_metrics.py 产物）：
        cohort_size, observed_households,
        by_week[].{week_start, iso_week, active_households}
    - G2 summary（scripts/g2_orders.py 产物）：
        paid_households, paid_households_by_cohort,
        refund_rate, refund_rate_by_order, orders_total, g2_pass
    - NPS CSV：household_id,nps,reason,date,synthetic

口径说明：
    - 付费转化优先用 `paid_households_by_cohort[--cohort-id]`（默认 g3）
      / `--cohort-size`；旧 G2 summary 无该键或缺少该 cohort 计数时，
      回退 `paid_households` 并输出
      `WARNING: 旧 G2 summary 未分 cohort，转化率可能高估`（不静默）。
    - 付费转化分母是**内测 cohort**（--cohort-size，默认 10），不是 G2 台账总户数。
    - 4 周留存取 G3 `by_week` 排序后的第 4 个观测周的 active_households；
      不足 4 周判“未采集”，不得算通过。
    - NPS 同户重复按文件顺序取最后一条并标注；有效回答 <5 标“样本不足”。
    - 未采集 / 样本不足 ≠ 通过；缺失数据动作是“补数据”，不默认 go。
    - 合成数据全部标 SYNTHETIC DEMO；不得当作真实留存/收入。
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "var" / "g4"
DEFAULT_COHORT = 10
DEFAULT_COHORT_ID = "g3"

RETENTION_THRESHOLD = 0.50
CONVERSION_THRESHOLD = 0.30
NPS_THRESHOLD = 30.0
NPS_MIN_SAMPLE = 5
REFUND_THRESHOLD = 0.10

SYNTHETIC_NOTICE = "SYNTHETIC DEMO — 不是真实留存/收入"
SYNTHETIC_MARKDOWN_BANNER = (
    f"> ⚠️ **{SYNTHETIC_NOTICE}**\n"
    ">\n"
    "> 本报告使用合成留存/订单/NPS 数据，仅用于演示判据计算与页面格式；"
    "不得作为真实留存、收入或 go/no-go 依据。\n\n"
)

NPS_FIELDS = ["household_id", "nps", "reason", "date", "synthetic"]

NPS_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("household_id", ("household_id", "户号", "house_id")),
    ("nps", ("nps", "推荐分", "推荐意愿")),
    ("reason", ("reason", "原因", "理由")),
    ("date", ("date", "日期")),
    ("synthetic", ("synthetic", "合成")),
)


class G4Error(Exception):
    """参数/文件/schema 错误 → 退出码 2。"""


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
class NpsRecord:
    household_id: str
    nps: int
    reason: str
    day: str
    synthetic: bool
    row: int


def normalize_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_bool(value: object) -> bool | None:
    text = normalize_text(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def read_json_dict(path: Path, label: str) -> dict:
    if not path.is_file():
        raise G4Error(f"找不到 {label}：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise G4Error(f"{label} 不是合法 JSON：{path}（{error}）") from error
    if not isinstance(data, dict):
        raise G4Error(f"{label} 顶层必须是 JSON 对象：{path}")
    return data


def read_g3_summary(path: Path) -> dict:
    data = read_json_dict(path, "G3 summary")
    weeks = data.get("by_week")
    if not isinstance(weeks, list):
        raise G4Error("G3 summary 缺少 by_week 数组（g3_metrics.py 产物）")
    for index, week in enumerate(weeks):
        if not isinstance(week, dict):
            raise G4Error(f"G3 summary.by_week[{index}] 必须是对象")
        for key in ("week_start", "active_households"):
            if key not in week:
                raise G4Error(f"G3 summary.by_week[{index}] 缺少 {key}")
        try:
            date.fromisoformat(str(week["week_start"])[:10])
        except ValueError as error:
            raise G4Error(
                f"G3 summary.by_week[{index}].week_start 不是 ISO 日期："
                f"{week['week_start']!r}"
            ) from error
        active = week["active_households"]
        try:
            if int(active) < 0:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise G4Error(
                f"G3 summary.by_week[{index}].active_households 必须是非负整数："
                f"{active!r}"
            ) from error
    return data


def read_g2_summary(path: Path) -> dict:
    data = read_json_dict(path, "G2 summary")
    for key in ("paid_households", "refund_rate"):
        if key not in data:
            raise G4Error(f"G2 summary 缺少 {key}（g2_orders.py 产物）")
    paid = data["paid_households"]
    try:
        if int(paid) < 0:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise G4Error(
            f"G2 summary.paid_households 必须是非负整数：{paid!r}"
        ) from error
    for key in ("refund_rate", "refund_rate_by_order"):
        value = data.get(key)
        if value is None:
            continue
        try:
            rate = float(value)
        except (TypeError, ValueError) as error:
            raise G4Error(f"G2 summary.{key} 必须是数字：{value!r}") from error
        if not 0.0 <= rate <= 1.0:
            raise G4Error(f"G2 summary.{key} 必须在 0..1：{rate}")

    by_cohort = data.get("paid_households_by_cohort")
    if by_cohort is not None:
        if not isinstance(by_cohort, dict):
            raise G4Error(
                "G2 summary.paid_households_by_cohort 必须是对象，"
                f"实际 {type(by_cohort).__name__}"
            )
        for cohort, value in by_cohort.items():
            try:
                if int(value) < 0:
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise G4Error(
                    "G2 summary.paid_households_by_cohort"
                    f"[{cohort!r}] 必须是非负整数：{value!r}"
                ) from error
    return data


def resolve_nps_columns(headers: Sequence[str]) -> dict[str, str]:
    columns: dict[str, str] = {}
    used: set[str] = set()
    for key, hints in NPS_HINTS:
        for header in headers:
            if header in used:
                continue
            text = normalize_text(header).lower()
            if any(hint.lower() in text for hint in hints):
                columns[key] = header
                used.add(header)
                break
    missing = [key for key in ("household_id", "nps") if key not in columns]
    if missing:
        raise G4Error(
            f"NPS CSV 缺少列：{missing}；实际表头：{list(headers)}"
        )
    return columns


def read_nps_csv(
    path: Path,
) -> tuple[list[NpsRecord], list[ValidationIssue], int, list[str]]:
    """返回（去重后的记录，非法行问题，被丢弃的重复条数，重复户号列表）。"""
    if not path.is_file():
        raise G4Error(f"找不到 NPS CSV：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise G4Error(f"NPS CSV 缺少表头：{path}")
        columns = resolve_nps_columns(reader.fieldnames)
        parsed: list[NpsRecord] = []
        issues: list[ValidationIssue] = []
        for index, raw in enumerate(reader, start=1):
            values = {
                key: normalize_text(raw.get(column)) for key, column in columns.items()
            }
            household = values.get("household_id", "")
            if not household and not any(values.values()):
                continue
            if not household:
                issues.append(
                    ValidationIssue(index, "", "household_id", "户号不能为空")
                )
                continue

            nps_text = values.get("nps", "")
            try:
                nps = int(nps_text)
            except ValueError:
                issues.append(
                    ValidationIssue(index, household, "nps", f"NPS 必须是整数：{nps_text!r}")
                )
                continue
            if not 0 <= nps <= 10:
                issues.append(
                    ValidationIssue(index, household, "nps", f"NPS 必须在 0..10：{nps}")
                )
                continue

            day = values.get("date", "")
            if day:
                try:
                    date.fromisoformat(day[:10])
                except ValueError:
                    issues.append(
                        ValidationIssue(
                            index, household, "date", f"date 不是 ISO 日期：{day!r}"
                        )
                    )
                    continue

            synthetic = parse_bool(values.get("synthetic", ""))
            if synthetic is None:
                issues.append(
                    ValidationIssue(
                        index,
                        household,
                        "synthetic",
                        f"布尔值只能是 true/false/1/0/yes/no：{values.get('synthetic')!r}",
                    )
                )
                continue

            parsed.append(
                NpsRecord(
                    household_id=household,
                    nps=nps,
                    reason=values.get("reason", ""),
                    day=day,
                    synthetic=bool(synthetic),
                    row=index,
                )
            )

    by_household: dict[str, NpsRecord] = {}
    duplicates = 0
    duplicate_households: list[str] = []
    for record in parsed:
        if record.household_id in by_household:
            duplicates += 1
            duplicate_households.append(record.household_id)
        by_household[record.household_id] = record
    records = list(by_household.values())
    return records, issues, duplicates, sorted(set(duplicate_households))


def _criterion(
    label: str,
    value: float | None,
    threshold: float,
    comparator: str,
    *,
    collected: bool = True,
    status: str | None = None,
    detail: str = "",
) -> dict:
    if not collected:
        return {
            "label": label,
            "value": value,
            "threshold": threshold,
            "comparator": comparator,
            "collected": False,
            "pass": False,
            "status": status or "未采集",
            "detail": detail,
        }
    passed = value is not None and (
        value >= threshold if comparator == ">=" else value <= threshold
    )
    return {
        "label": label,
        "value": value,
        "threshold": threshold,
        "comparator": comparator,
        "collected": True,
        "pass": passed,
        "status": status or ("pass" if passed else "fail"),
        "detail": detail,
    }


def nps_stats(records: Sequence[NpsRecord]) -> dict:
    count = len(records)
    if count == 0:
        return {
            "value": None,
            "n": 0,
            "promoters": 0,
            "passives": 0,
            "detractors": 0,
            "collected": False,
            "status": "未采集",
        }
    promoters = sum(1 for record in records if record.nps >= 9)
    detractors = sum(1 for record in records if record.nps <= 6)
    passives = count - promoters - detractors
    value = round((promoters - detractors) / count * 100, 1)
    collected = count >= NPS_MIN_SAMPLE
    return {
        "value": value,
        "n": count,
        "promoters": promoters,
        "passives": passives,
        "detractors": detractors,
        "collected": collected,
        "status": "ok" if collected else "样本不足",
    }


def compute_report(
    g3: Mapping[str, object],
    g2: Mapping[str, object],
    nps_records: Sequence[NpsRecord],
    *,
    cohort_size: int,
    cohort_id: str = DEFAULT_COHORT_ID,
    duplicates_dropped: int = 0,
    duplicate_households: Sequence[str] = (),
    source_g3: str = "",
    source_g2: str = "",
    source_nps: str = "",
) -> dict:
    weeks = sorted(
        (week for week in (g3.get("by_week") or []) if isinstance(week, dict)),
        key=lambda week: str(week.get("week_start", "")),
    )
    if len(weeks) >= 4:
        week4 = weeks[3]
        week4_active = int(week4.get("active_households", 0))
        week4_value = round(week4_active / cohort_size, 4)
        week4_detail = (
            f"第 4 个观测周 {week4.get('week_start')}"
            f"（{week4.get('iso_week', '—')}）：{week4_active}/{cohort_size}"
        )
        week4_collected = True
    else:
        week4 = {}
        week4_active = None
        week4_value = None
        week4_detail = f"G3 by_week 仅 {len(weeks)} 周，不足 4 周"
        week4_collected = False

    paid = int(g2.get("paid_households", 0))
    by_cohort = g2.get("paid_households_by_cohort")
    fallback_warning = ""
    if isinstance(by_cohort, dict) and cohort_id in by_cohort:
        cohort_paid = int(by_cohort[cohort_id])
        conversion_source = "cohort"
        conversion_detail = (
            f"G2 paid_households_by_cohort[{cohort_id!r}]={cohort_paid}"
            f" / 内测 cohort={cohort_size}；G2 总 paid={paid}"
        )
    else:
        cohort_paid = paid
        conversion_source = "fallback"
        if isinstance(by_cohort, dict):
            fallback_warning = (
                f"WARNING: G2 summary 缺少 cohort={cohort_id} 计数，"
                "回退总 paid，转化率可能高估"
            )
            conversion_detail = (
                f"G2 summary.paid_households_by_cohort 无 {cohort_id!r}；"
                f"回退 paid_households={paid} / 内测 cohort={cohort_size}"
            )
        else:
            fallback_warning = (
                "WARNING: 旧 G2 summary 未分 cohort，转化率可能高估"
            )
            conversion_detail = (
                "旧 G2 summary 无 paid_households_by_cohort；"
                f"回退 paid_households={paid} / 内测 cohort={cohort_size}"
            )
    conversion_value = round(cohort_paid / cohort_size, 4)

    nps = nps_stats(nps_records)
    nps_detail = (
        f"n={nps['n']}：{nps['promoters']} 推荐 / "
        f"{nps['passives']} 被动 / {nps['detractors']} 贬损"
    )
    if nps["status"] == "未采集":
        nps_detail = "未提供 NPS CSV 或 CSV 无有效回答"

    refund_rate = g2.get("refund_rate")
    refund_rate_by_order = g2.get("refund_rate_by_order")
    refund_value = None if refund_rate is None else float(refund_rate)
    refund_by_order_value = (
        None if refund_rate_by_order is None else float(refund_rate_by_order)
    )
    refund_detail = (
        "按户 " + ("未采集" if refund_value is None else f"{refund_value * 100:.1f}%")
        + "；按单 "
        + (
            "未采集"
            if refund_by_order_value is None
            else f"{refund_by_order_value * 100:.1f}%"
        )
    )

    criteria = {
        "week4_retention": _criterion(
            "4 周留存 ≥50%",
            week4_value,
            RETENTION_THRESHOLD,
            ">=",
            collected=week4_collected,
            detail=week4_detail,
        ),
        "paid_conversion": _criterion(
            "内测户付费转化 ≥30%",
            conversion_value,
            CONVERSION_THRESHOLD,
            ">=",
            detail=conversion_detail,
        ),
        "nps": _criterion(
            "NPS ≥30",
            nps["value"] if nps["value"] is not None else None,
            NPS_THRESHOLD,
            ">=",
            collected=bool(nps["collected"]),
            status=None if nps["collected"] else nps["status"],
            detail=nps_detail,
        ),
        "refund_rate": _criterion(
            "退款率 ≤10%",
            refund_value,
            REFUND_THRESHOLD,
            "<=",
            collected=refund_value is not None,
            detail=refund_detail,
        ),
    }
    g4_pass = all(item["pass"] for item in criteria.values())
    if g4_pass:
        action = "go（继续 C 端）"
        action_reason = "四项判据全过：继续 C 端并进入下一阶段"
    elif any(not item["collected"] for item in criteria.values()):
        action = "补数据"
        action_reason = "存在未采集/样本不足项：先补齐数据再判，不能默认通过"
    else:
        action = "转 B2B"
        action_reason = "四项判据有未过项：按 §7 G4 转 B2B"

    gaps: list[str] = []
    for item in criteria.values():
        if item["pass"]:
            continue
        if not item["collected"]:
            gaps.append(f"{item['label']}：{item['status']}（{item['detail']}）")
        else:
            gaps.append(
                f"{item['label']}：实际 {item['value']}，"
                f"阈值 {item['comparator']} {item['threshold']}"
            )

    warnings: list[str] = []
    if fallback_warning:
        warnings.append(fallback_warning)
    g2_orders = g2.get("orders_total")
    if cohort_paid > cohort_size:
        warnings.append(
            f"cohort={cohort_id} 的 paid_households={cohort_paid} 大于内测 "
            f"cohort={cohort_size}：转化率会超过 100%，需核对 cohort 名单"
        )
    if conversion_source == "cohort" and paid > cohort_paid:
        warnings.append(
            f"G2 总 paid={paid} 含非 {cohort_id} 户 {paid - cohort_paid} 户；"
            f"G4 已按 cohort={cohort_id} 过滤为 {cohort_paid}，未过滤会高估"
        )
    if g2.get("g2_pass") is False:
        warnings.append(
            "G2 台账自身未通过（g2_pass=false，G2 是 "
            f"{g2.get('target_households', 20)} 户口径）；G4 只按内测 "
            f"cohort={cohort_size} 计算转化，不改变 G2 判据"
        )
    g3_cohort = g3.get("cohort_size")
    if g3_cohort is not None and int(g3_cohort) != cohort_size:
        warnings.append(
            f"G3 cohort_size={g3_cohort} 与 G4 --cohort-size={cohort_size} 不一致；"
            "以 G4 参数为准，报告中已注明"
        )
    observed = g3.get("observed_households")
    if observed is not None and int(observed) < cohort_size:
        warnings.append(
            f"G3 实际覆盖 {observed} 户 < 内测 cohort={cohort_size}："
            "留存/转化分母仍用 cohort（偏严格），样本不足需注明"
        )
    if len(weeks) > 4:
        warnings.append(
            f"G3 by_week 有 {len(weeks)} 周，仅取第 4 个观测周；后续周未纳入"
        )
    if duplicates_dropped:
        warnings.append(
            f"NPS 重复户 {duplicates_dropped} 条，按文件顺序取最后一条；"
            f"重复户：{'、'.join(duplicate_households) or '—'}"
        )

    synthetic = bool(g3.get("synthetic_demo")) or bool(g2.get("synthetic_demo"))
    synthetic = synthetic or any(record.synthetic for record in nps_records)

    return {
        "source_g3": source_g3 or None,
        "source_g2": source_g2 or None,
        "source_nps": source_nps or None,
        "synthetic_demo": synthetic,
        "notice": (
            SYNTHETIC_NOTICE
            if synthetic
            else "真实留存/订单/NPS 数据；不含原句与个人信息"
        ),
        "cohort_size": cohort_size,
        "cohort_id": cohort_id,
        "paid_conversion_source": conversion_source,
        "g2_cohort_paid_households": cohort_paid,
        "g3_cohort_size": g3_cohort,
        "g3_observed_households": observed,
        "g3_week_count": len(weeks),
        "week4_retention": week4_value,
        "week4_active_households": week4_active,
        "week4_week_start": week4.get("week_start"),
        "week4_iso_week": week4.get("iso_week"),
        "paid_conversion": conversion_value,
        "g2_paid_households": paid,
        "g2_orders_total": g2_orders,
        "g2_g2_pass": g2.get("g2_pass"),
        "nps": nps["value"],
        "nps_n": nps["n"],
        "nps_promoters": nps["promoters"],
        "nps_passives": nps["passives"],
        "nps_detractors": nps["detractors"],
        "nps_status": nps["status"],
        "nps_duplicates_dropped": duplicates_dropped,
        "nps_duplicate_households": list(duplicate_households),
        "refund_rate": refund_value,
        "refund_rate_by_order": refund_by_order_value,
        "criteria": criteria,
        "g4_pass": g4_pass,
        "g4_action": action,
        "g4_action_reason": action_reason,
        "gaps": gaps,
        "warnings": warnings,
    }

def _pct(value: object) -> str:
    if value is None:
        return "未采集"
    return f"{float(value) * 100:.1f}%"


def _status_text(item: Mapping[str, object]) -> str:
    return {
        "pass": "通过",
        "fail": "未通过",
        "未采集": "未采集",
        "样本不足": "样本不足",
    }.get(str(item.get("status")), "未采集")


def _actual_text(
    key: str, item: Mapping[str, object], summary: Mapping[str, object]
) -> str:
    if item.get("value") is None:
        return str(item.get("status"))
    if key == "week4_retention":
        return (
            f"{_pct(item.get('value'))}（{summary.get('week4_active_households')}"
            f"/{summary.get('cohort_size')}）"
        )
    if key == "paid_conversion":
        if summary.get("paid_conversion_source") == "cohort":
            return (
                f"{_pct(item.get('value'))}"
                f"（{summary.get('g2_cohort_paid_households')}"
                f"/{summary.get('cohort_size')}，"
                f"cohort={summary.get('cohort_id')}）"
            )
        return (
            f"{_pct(item.get('value'))}"
            f"（回退总 paid {summary.get('g2_paid_households')}"
            f"/{summary.get('cohort_size')}）"
        )
    if key == "nps":
        text = f"{float(item.get('value')):.1f}（n={summary.get('nps_n')}）"
        if item.get("status") == "样本不足":
            text += " · 样本不足"
        return text
    return (
        f"{_pct(item.get('value'))}（按户；按单 "
        f"{_pct(summary.get('refund_rate_by_order'))}）"
    )


def _nps_label(nps: int) -> str:
    if nps >= 9:
        return "推荐"
    if nps <= 6:
        return "贬损"
    return "被动"


def render_report(
    summary: Mapping[str, object], nps_records: Sequence[NpsRecord]
) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    criteria = summary.get("criteria") or {}
    gaps = list(summary.get("gaps") or [])
    warnings = list(summary.get("warnings") or [])

    lines: list[str] = []
    if synthetic:
        lines.append(SYNTHETIC_MARKDOWN_BANNER)
    lines += [
        "# G4 留存/转化报告",
        "",
        f"> G3 来源：`{summary.get('source_g3') or '（未提供）'}`",
        f"> G2 来源：`{summary.get('source_g2') or '（未提供）'}`",
        f"> NPS 来源：`{summary.get('source_nps') or '（未提供）'}`",
        "> 口径：`docs/用户验证包-v0.1.md` §7 G4（第 10 周末，四项判据）",
        "",
        "## 一、四项判据",
        "",
        "| 判据 | 实际 | 门槛 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    thresholds = {
        "week4_retention": "≥50%",
        "paid_conversion": "≥30%",
        "nps": "≥30",
        "refund_rate": "≤10%",
    }
    for key in ("week4_retention", "paid_conversion", "nps", "refund_rate"):
        item = criteria.get(key, {})
        lines.append(
            f"| {item.get('label', key)} | "
            f"{_actual_text(key, item, summary)} | "
            f"{thresholds[key]} | {_status_text(item)} |"
        )

    lines += [
        "",
        f"**G4 结论：{'通过' if summary.get('g4_pass') else '未通过'} "
        f"→ 当前动作：{summary.get('g4_action')}。**",
        "",
        f"> {summary.get('g4_action_reason')}",
        "",
        "## 二、缺口",
        "",
    ]
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("无（四项判据均满足，且无未采集/样本不足项）。")

    lines += [
        "",
        "## 三、数据来源与口径",
        "",
        "| 指标 | 来源产物/字段 | 计算与分母 |",
        "| --- | --- | --- |",
        f"| 4 周留存 | `g3_summary.json` → `by_week[3].active_households` | "
        f"{summary.get('week4_active_households')} / 内测 cohort "
        f"{summary.get('cohort_size')}（第 4 个观测周 "
        f"{summary.get('week4_week_start') or '—'}，"
        f"{summary.get('week4_iso_week') or '—'}） |",
        f"| 付费转化 | `g2_summary.json` → `paid_households_by_cohort` | "
        f"{summary.get('g2_cohort_paid_households')} / **内测 cohort** "
        f"{summary.get('cohort_size')}（`cohort={summary.get('cohort_id')}`）；"
        f"G2 总 paid {summary.get('g2_paid_households')}、台账总户数 "
        f"{summary.get('g2_orders_total') or '—'}，**都不是**转化分母 |",
        f"| NPS | `nps.csv` → `nps` | (推荐 {summary.get('nps_promoters')} − "
        f"贬损 {summary.get('nps_detractors')}) / 有效回答 "
        f"{summary.get('nps_n')} × 100 = "
        f"{float(summary.get('nps') or 0):.1f} |",
        f"| 退款率 | `g2_summary.json` → `refund_rate` | 按户 "
        f"{_pct(summary.get('refund_rate'))}；按单 "
        f"{_pct(summary.get('refund_rate_by_order'))} |",
        "",
        "- **4 周留存**取 G3 `by_week` 按 `week_start` 排序后的第 4 个观测周；"
        "不足 4 周判“未采集”，不得算通过。",
        "- **付费转化分母是内测 cohort**（`--cohort-size`，默认 10），"
        "不是 G2 台账总户数；G2 的 `paid_households` 可能含 G1/G2 阶段户，"
        "未按 cohort 过滤，转化率可能高估，需按内测名单复核。",
        f"- **本次转化来源**：`--cohort-id {summary.get('cohort_id')}`，"
        + (
            "读取 `paid_households_by_cohort`，只取该 cohort，未回退。"
            if summary.get("paid_conversion_source") == "cohort"
            else "旧 G2 summary 未分 cohort 或缺少该 cohort 计数，"
            "已回退总 `paid_households` 并在上方给出 WARNING。"
        ),
        "- **NPS** 按 0–10：9–10 推荐、7–8 被动、0–6 贬损；"
        "同户重复按文件顺序取最后一条；有效回答 <5 标“样本不足”。",
        "- **退款率**主判据按户（G2 `refund_rate`），同时展示按单口径，"
        "两者区分见上表。",
        "- **未采集 ≠ 通过**：缺 G3 周数据、缺 NPS 或样本不足时，"
        "该项 `pass=false`，总判据也为 false。",
        "",
        "## 四、NPS 明细",
        "",
    ]
    if nps_records:
        lines += [
            "| 户号 | NPS | 分类 | 理由 | 日期 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for record in nps_records:
            reason = record.reason.replace("|", "\\|") or "—"
            lines.append(
                f"| {record.household_id} | {record.nps} | "
                f"{_nps_label(record.nps)} | "
                f"{reason} | "
                f"{record.day or '—'} |"
            )
    else:
        lines.append("未提供有效 NPS 回答。")

    if warnings:
        lines += ["", "## 五、口径警告", ""]
        lines.extend(f"- {warning}" for warning in warnings)

    lines += [
        "",
        "## 六、口径与限制",
        "",
        "- 样本不足时改分母必须在报告中注明；本报告所有分母均已在"
        "“数据来源与口径”列明。",
        "- 未采集项不得计入通过；`g4_action` 对未采集/样本不足优先给“补数据”，"
        "而不是默认 go 或直接 B2B。",
        "- 不接真实支付与真实用户原句；NPS CSV 只含匿名户号、分值、理由与日期。",
        "- 合成 demo 的全部数字与判据仅用于演示，不得当作真实留存/收入结论。",
        "",
    ]
    return "\n".join(lines)


def format_summary(
    summary: Mapping[str, object], *, out_dir: Path | None = None
) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    nps_text = (
        "未采集"
        if summary.get("nps_status") == "未采集"
        else f"{float(summary.get('nps') or 0):.1f}（n={summary.get('nps_n')}）"
    )
    if summary.get("nps_status") == "样本不足":
        nps_text += " · 样本不足"
    lines = [
        "G4 留存/转化报告" + ("（SYNTHETIC DEMO）" if synthetic else ""),
        f"  4 周留存 {_pct(summary.get('week4_retention'))}"
        f"（{summary.get('week4_active_households')}/{summary.get('cohort_size')}）；"
        f"付费转化 {_pct(summary.get('paid_conversion'))}"
        f"（{summary.get('g2_cohort_paid_households')}"
        f"/{summary.get('cohort_size')}，"
        f"cohort={summary.get('cohort_id')}"
        f"{'，WARNING 回退总 paid' if summary.get('paid_conversion_source') == 'fallback' else ''}）；"
        f"NPS {nps_text}；退款率 {_pct(summary.get('refund_rate'))}（按户）",
        f"  G4：{'通过' if summary.get('g4_pass') else '未通过'}"
        f" → {summary.get('g4_action')}",
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
main { max-width: 1040px; margin: 0 auto; padding: 20px; }
h1 { font-size: 24px; margin: 12px 0 4px; }
h2 { font-size: 17px; margin: 24px 0 10px; border-left: 4px solid #b00020; padding-left: 8px; }
.meta { color: #5a5a66; font-size: 13px; margin: 0 0 8px; }
.criteria { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
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
.warn-list { background: #fff; border: 1px solid #e3e4e8; border-radius: 8px; padding: 10px 14px; }
footer { margin-top: 28px; font-size: 12px; color: #6a6a76; border-top: 1px solid #e3e4e8; padding-top: 12px; }
.warn { color: #b00020; font-weight: 600; }
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _card(label: str, value: str, note: str, status: object) -> str:
    status_text = str(status)
    status_class = {"pass": "pass", "fail": "fail"}.get(status_text, "missing")
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
    gaps = list(summary.get("gaps") or [])
    warnings = list(summary.get("warnings") or [])

    title = "G4 看板 · SYNTHETIC DEMO" if synthetic else "G4 看板 · 留存/转化四判据"
    banner_class = "banner banner-demo" if synthetic else "banner banner-real"
    banner_text = (
        SYNTHETIC_NOTICE
        if synthetic
        else "真实留存/订单/NPS 数据 · 请核对 cohort 与来源后再引用"
    )
    state = "通过" if summary.get("g4_pass") else "未通过"
    state_class = "pass-text" if summary.get("g4_pass") else "fail-text"

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
        '<p class="meta">由 <code>scripts/g4_report.py</code> 读取 G3/G2 真实产物与 NPS CSV 生成</p>',
        "<section>",
        "<h2>四项判据</h2>",
        '<div class="criteria">',
    ]

    card_specs = (
        (
            "week4_retention",
            "4 周留存",
            "≥50%",
            f"{summary.get('week4_active_households')}/{summary.get('cohort_size')} 户",
        ),
        (
            "paid_conversion",
            "付费转化",
            "≥30%",
            f"{summary.get('g2_cohort_paid_households')}"
            f"/{summary.get('cohort_size')} 户"
            f"（cohort={summary.get('cohort_id')}"
            f"{'，回退' if summary.get('paid_conversion_source') == 'fallback' else ''}）",
        ),
        (
            "nps",
            "NPS",
            "≥30",
            f"n={summary.get('nps_n')}",
        ),
        (
            "refund_rate",
            "退款率（按户）",
            "≤10%",
            f"按单 {_pct(summary.get('refund_rate_by_order'))}",
        ),
    )
    for key, label, threshold, note in card_specs:
        item = criteria.get(key, {})
        if key == "nps" and item.get("value") is not None:
            display = f"{float(item.get('value')):.1f}"
        elif key == "nps":
            display = "未采集"
        else:
            display = _pct(item.get("value"))
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
        f'<p class="judgement">G4 结论：'
        f'<span class="{state_class}">{_esc(state)}</span>'
        f" → 动作：{_esc(summary.get('g4_action'))}。"
        f" {_esc(summary.get('g4_action_reason'))}</p>",
        "<section>",
        "<h2>数据来源与分母</h2>",
        "<table>",
        "<thead><tr><th>指标</th><th>来源字段</th><th>计算 / 分母</th></tr></thead>",
        "<tbody>",
        "<tr><td>4 周留存</td><td>G3 summary → by_week[3].active_households</td>"
        f"<td>{_esc(summary.get('week4_active_households'))}"
        f"/{_esc(summary.get('cohort_size'))}（内测 cohort）</td></tr>",
        "<tr><td>付费转化</td><td>G2 summary → paid_households_by_cohort</td>"
        f"<td>{_esc(summary.get('g2_cohort_paid_households'))}"
        f"/{_esc(summary.get('cohort_size'))}（内测 cohort；"
        f"cohort={_esc(summary.get('cohort_id'))}；"
        f"总 paid {_esc(summary.get('g2_paid_households'))}，"
        f"台账 {_esc(summary.get('g2_orders_total') or '—')} 户不是分母）</td></tr>",
        "<tr><td>NPS</td><td>nps.csv → nps</td>"
        f"<td>推荐 {_esc(summary.get('nps_promoters'))} / 被动 "
        f"{_esc(summary.get('nps_passives'))} / 贬损 "
        f"{_esc(summary.get('nps_detractors'))}，n="
        f"{_esc(summary.get('nps_n'))}</td></tr>",
        "<tr><td>退款率</td><td>G2 summary → refund_rate</td>"
        f"<td>按户 {_esc(_pct(summary.get('refund_rate')))}；按单 "
        f"{_esc(_pct(summary.get('refund_rate_by_order')))}</td></tr>",
        "</tbody>",
        "</table>",
        "</section>",
        "<section>",
        "<h2>缺口</h2>",
    ]
    if gaps:
        parts.append('<div class="warn-list"><ul>')
        parts.extend(f"<li>{_esc(gap)}</li>" for gap in gaps)
        parts.append("</ul></div>")
    else:
        parts.append("<p>无。</p>")

    if warnings:
        parts.append("<h2>口径警告</h2>")
        parts.append('<div class="warn-list"><ul>')
        parts.extend(f"<li>{_esc(warning)}</li>" for warning in warnings)
        parts.append("</ul></div>")

    parts += [
        "</section>",
        "<footer>",
        "<p>口径：4 周留存 ≥50%、内测户付费转化 ≥30%、NPS ≥30、退款率 ≤10%"
        "（<code>docs/用户验证包-v0.1.md</code> §7 G4）。</p>",
        "<p>未采集/样本不足不得算通过；付费转化分母是内测 cohort，不是 G2 台账总户数。</p>",
        "<p>本页由真实产物计算生成，仅描述样本，不做统计显著性检验，不构成验证结论。</p>",
    ]
    if synthetic:
        parts.append(
            f'<p class="warn">⚠️ {_esc(SYNTHETIC_NOTICE)}：'
            "本页所有数字来自合成数据，仅用于演示页面格式，"
            "不得作为真实留存/收入或 go/no-go 依据。</p>"
        )
    parts += [
        "</footer>",
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)

def _demo_week(week_start: str, iso_week: str, active: int) -> dict:
    return {
        "week_start": week_start,
        "iso_week": iso_week,
        "households": 10,
        "active_households": active,
        "wau_rate": round(active / 10, 4),
        "confirm_started": 980,
        "confirm_completed": 833,
        "confirm_completion_rate": 0.85,
        "false_positive": 98,
        "false_positive_rate": 0.1,
        "unauthorized_dispatch": 0,
        "confirm_samples": 49,
        "confirm_p90_seconds": 18.0,
    }


def build_demo_g3_summary() -> dict:
    """4 周 G3 summary（第 4 周 6/10 活跃 = 60%）。"""
    return {
        "source": f"{SYNTHETIC_NOTICE}（G3 事件合成）",
        "synthetic_demo": True,
        "notice": SYNTHETIC_NOTICE,
        "cohort_size": 10,
        "weeks": 4,
        "attempt": 1,
        "observed_households": 10,
        "observed_days": 28,
        "expected_days": 28,
        "sample_complete": True,
        "duplicates_merged": 0,
        "active_households": 6,
        "wau_rate": 0.6,
        "confirm_started": 3920,
        "confirm_completed": 3332,
        "confirm_completion_rate": 0.85,
        "false_positive": 392,
        "false_positive_rate": 0.1,
        "unauthorized_dispatch_total": 0,
        "confirm_samples": 196,
        "confirm_p90_seconds": 18.0,
        "p90_method": "nearest-rank：升序后取第 ceil(0.9×n) 个（1-based），不插值",
        "by_week": [
            _demo_week("2026-11-02", "2026-W45", 7),
            _demo_week("2026-11-09", "2026-W46", 7),
            _demo_week("2026-11-16", "2026-W47", 6),
            _demo_week("2026-11-23", "2026-W48", 6),
        ],
        "by_household": {},
        "criteria": {},
        "g3_pass": True,
        "g3_action": "go",
        "g3_action_reason": "五项判据全过：进入 G4 付费/留存观察",
        "gaps": [],
    }


def build_demo_g2_summary() -> dict:
    """G2 台账 20 户：总 paid=10，其中内测 cohort g3=4 → G4 转化 4/10=40%。"""
    return {
        "source": f"{SYNTHETIC_NOTICE}（G2 订单合成）",
        "synthetic_demo": True,
        "notice": SYNTHETIC_NOTICE,
        "orders_total": 20,
        "paid_orders": 10,
        "paid_households": 10,
        "paid_households_by_cohort": {"g2": 6, "g3": 4},
        "paid_orders_by_cohort": {"g2": 6, "g3": 4},
        "refunded_by_cohort": {"g2": 0, "g3": 0},
        "refund_rate_by_cohort": {"g2": 0.0, "g3": 0.0},
        "unpaid_orders": 10,
        "refunded_orders": 0,
        "refunded_count": 0,
        "refunded_households": 0,
        "refund_rate": 0.0,
        "refund_rate_by_order": 0.0,
        "gross_revenue": 1890,
        "refund_amount_total": 0,
        "net_revenue": 1890,
        "by_plan": {
            "A": {"orders": 4, "households": 4, "gross_revenue": 396, "refunded_orders": 0},
            "B": {"orders": 3, "households": 3, "gross_revenue": 597, "refunded_orders": 0},
            "C": {"orders": 3, "households": 3, "gross_revenue": 897, "refunded_orders": 0},
        },
        "g2_pass": True,
        "g2_action": "go",
        "g2_action_reason": "≥10 户完成真实预付且退款率 ≤20%：进入 G3 内测准备",
        "gaps": [],
        "target_households": 20,
        "min_paid_households": 10,
        "max_refund_rate": 0.2,
    }


def build_demo_nps_rows() -> list[dict[str, str]]:
    """10 条 NPS：5 推荐（10）+ 4 被动（8）+ 1 贬损（5）→ NPS 40。"""
    values = [10, 10, 10, 10, 10, 8, 8, 8, 8, 5]
    return [
        {
            "household_id": f"SYN-HH-{index:02d}",
            "nps": str(value),
            "reason": SYNTHETIC_NOTICE,
            "date": "2026-11-30",
            "synthetic": "true",
        }
        for index, value in enumerate(values, start=1)
    ]


def write_nps_csv(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=NPS_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in NPS_FIELDS})


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_outputs(
    summary: Mapping[str, object], nps_records: Sequence[NpsRecord], out_dir: Path
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "g4_report.md").write_text(
        render_report(summary, nps_records), encoding="utf-8"
    )
    (out_dir / "dashboard.html").write_text(
        render_dashboard(summary), encoding="utf-8"
    )


def _print_issues(issues: Sequence[ValidationIssue]) -> None:
    print(f"NPS 数据校验失败：{len(issues)} 个问题")
    for issue in issues:
        print("  - " + issue.as_text())


def run_paths(
    g3_path: Path,
    g2_path: Path,
    nps_path: Path | None,
    out_dir: Path,
    cohort_size: int,
    cohort_id: str = DEFAULT_COHORT_ID,
) -> int:
    g3 = read_g3_summary(g3_path)
    g2 = read_g2_summary(g2_path)

    if nps_path is not None:
        records, issues, duplicates, duplicate_households = read_nps_csv(nps_path)
        if issues:
            _print_issues(issues)
            return 1
        source_nps = str(nps_path)
    else:
        records = []
        duplicates = 0
        duplicate_households = []
        source_nps = ""

    summary = compute_report(
        g3,
        g2,
        records,
        cohort_size=cohort_size,
        cohort_id=cohort_id,
        duplicates_dropped=duplicates,
        duplicate_households=duplicate_households,
        source_g3=str(g3_path),
        source_g2=str(g2_path),
        source_nps=source_nps,
    )
    _write_outputs(summary, records, out_dir)

    print(f"  G3：{g3_path}")
    print(f"  G2：{g2_path}")
    print(f"  NPS：{nps_path if nps_path is not None else '未提供（NPS 未采集）'}")
    print(format_summary(summary, out_dir=out_dir))
    return 0


def run_demo(
    out_root: Path, cohort_size: int, cohort_id: str = DEFAULT_COHORT_ID
) -> int:
    out_dir = out_root / "demo" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    g3_path = out_dir / "g3_summary.json"
    g2_path = out_dir / "g2_summary.json"
    nps_path = out_dir / "nps.csv"
    _write_json(g3_path, build_demo_g3_summary())
    _write_json(g2_path, build_demo_g2_summary())
    write_nps_csv(nps_path, build_demo_nps_rows())
    return run_paths(
        g3_path, g2_path, nps_path, out_dir, cohort_size, cohort_id
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G4 留存/转化报告（读 G3/G2 summary + NPS CSV，四项判据）"
    )
    parser.add_argument("--g3-summary", help="G3 metrics 的 summary.json")
    parser.add_argument("--g2-summary", help="G2 orders 的 summary.json")
    parser.add_argument("--nps", help="NPS CSV（可选；不提供则 NPS 未采集）")
    parser.add_argument("--cohort-size", type=int, default=DEFAULT_COHORT)
    parser.add_argument(
        "--cohort-id",
        default=DEFAULT_COHORT_ID,
        help="付费转化使用的 cohort 标识（默认 g3；读 G2 paid_households_by_cohort）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--demo", action="store_true", help="生成 10 户合成 G3/G2/NPS 并跑完整报告"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cohort_size <= 0:
            raise G4Error("--cohort-size 必须是正整数")
        if not args.cohort_id or not args.cohort_id.strip():
            raise G4Error("--cohort-id 不能为空")
        if args.demo:
            return run_demo(Path(args.out_dir), args.cohort_size, args.cohort_id)
        if not args.g3_summary or not args.g2_summary:
            parser.error("非 demo 必须提供 --g3-summary 与 --g2-summary")
        return run_paths(
            Path(args.g3_summary),
            Path(args.g2_summary),
            Path(args.nps) if args.nps else None,
            Path(args.out_dir),
            args.cohort_size,
            args.cohort_id,
        )
    except G4Error as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())