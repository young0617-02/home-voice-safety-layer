"""G2 预订/退款台账：订单校验、退款状态机、G2 判据与报告。

用法：
    python scripts/g2_orders.py new --out var/g2/orders.csv
    python scripts/g2_orders.py validate --orders var/g2/orders.csv
    python scripts/g2_orders.py refund --orders var/g2/orders.csv \
        --order-id G2-001 --status requested --at "2026-11-20 09:00:00"
    python scripts/g2_orders.py refund --orders var/g2/orders.csv \
        --order-id G2-001 --status refunded --amount 99 --at "2026-11-22 15:00:00"
    python scripts/g2_orders.py report --orders var/g2/orders.csv --out-dir var/g2
    python scripts/g2_orders.py reconcile --orders var/g2/orders.csv \
        --payments var/g2/payments.csv
    python scripts/g2_orders.py demo --out-dir var/g2

口径（docs/用户验证包-v0.1.md §7 G2）：
    ＜20 户中 ≥10 户完成真实预付（可退）且退款率 ≤20%＞ → 通过（go）；
    5–9 户 → 降价或改买断 + 缩到“门锁+燃气”再测 2 周；
    <5 户 → 转 B2B。

隐私与边界：
    本工具只记录订单号/户号/档位/金额/流水号/退款状态，**不接收、不存储**
    卡号、身份证、支付密码；不连接任何真实支付渠道。演示数据全部标注
    SYNTHETIC DEMO，不得当作真实收入。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "var" / "g2"
DEFAULT_ORDERS = DEFAULT_DIR / "orders.csv"

ORDER_FIELDS = [
    "order_id",
    "household_id",
    "plan",
    "amount",
    "currency",
    "created_at",
    "paid_at",
    "payment_method",
    "payment_ref",
    "refund_status",
    "refund_requested_at",
    "refunded_at",
    "refund_amount",
    "consent_version",
    "note",
    "synthetic",
    "cohort",
]

FIELD_DOCS: tuple[tuple[str, str], ...] = (
    ("order_id", "订单号，全局唯一；建议前缀 G2- 或渠道前缀"),
    ("household_id", "匿名户号，同一户只允许 1 个有效付费单"),
    ("plan", "档位：A=¥99/年，B=¥199/年，C=¥299 买断（一次性）"),
    ("amount", "实付金额（元），必须与档位一致：A 99 / B 199 / C 299"),
    ("currency", "币种，默认 CNY"),
    ("created_at", "下单时间，ISO 8601（YYYY-MM-DD HH:MM:SS）"),
    ("paid_at", "真实预付时间；空表示未付/过期"),
    ("payment_method", "支付方式（微信/支付宝/银行卡等，仅记录类别）"),
    ("payment_ref", "外部支付流水号，用于对账；已付订单必填"),
    ("refund_status", "none / requested / refunded / rejected"),
    ("refund_requested_at", "退款申请时间；requested/refunded/rejected 时必填"),
    ("refunded_at", "退款完成时间；仅 refunded 时填写"),
    ("refund_amount", "实际退款金额，0 ≤ 退款金额 ≤ 实付"),
    ("consent_version", "知情同意书版本，如 v0.1"),
    ("note", "备注；合成数据必须写明 SYNTHETIC DEMO"),
    ("synthetic", "是否为合成数据：true/false；示例行与 demo 必须为 true"),
    (
        "cohort",
        "可选：g2=普通 G2 预订户，g3=G3 内测户；留空按 g2。"
        "G4 付费转化只统计 cohort=g3",
    ),
)

PLAN_PRICES = {
    "A": Decimal(99),
    "B": Decimal(199),
    "C": Decimal(299),
}
PLAN_LABELS = {"A": "¥99/年", "B": "¥199/年", "C": "¥299 买断"}
PLAN_INTERVALS = {"A": "年费", "B": "年费", "C": "一次性"}
REFUND_STATUSES = ("none", "requested", "refunded", "rejected")
ALLOWED_TRANSITIONS = {
    "none": {"requested"},
    "requested": {"refunded", "rejected"},
    "refunded": set(),
    "rejected": set(),
}
SYNTHETIC_NOTICE = "SYNTHETIC DEMO — 不是真实收入"
SYNTHETIC_MARKDOWN_BANNER = (
    f"> ⚠️ **{SYNTHETIC_NOTICE}**\n"
    ">\n"
    "> 本报告使用合成订单，仅用于演示台账与判据计算；"
    "不得作为真实收入、付费意愿或 go/no-go 结论。\n\n"
)

TARGET_HOUSEHOLDS = 20
MIN_PAID_HOUSEHOLDS = 10
MAX_REFUND_RATE = Decimal("0.20")


class LedgerError(Exception):
    """参数/文件/格式错误 → 退出码 2。"""


class LedgerBusinessError(Exception):
    """业务校验失败 → 退出码 1。"""


@dataclass
class ValidationIssue:
    row: int
    order_id: str
    field: str
    message: str

    def as_text(self) -> str:
        return f"第 {self.row} 行 {self.order_id or '（无订单号）'}：{self.field}：{self.message}"


def normalize_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def parse_bool(value: object) -> bool | None:
    text = normalize_text(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", ""}:
        return False
    return None


def parse_amount(value: object) -> Decimal:
    """金额解析（元）。空值视为 0；非法/负数抛 ValueError。"""
    text = normalize_text(value).replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return Decimal(0)
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"金额不是合法数字：{value!r}") from error
    if amount < 0:
        raise ValueError(f"金额不能为负：{value!r}")
    return amount


def parse_datetime(value: object) -> datetime | None:
    """ISO 8601 → naive datetime（统一折算 UTC），空值返回 None。"""
    text = normalize_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"时间格式应为 ISO 8601：{value!r}") from error
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def read_orders(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise LedgerError(f"找不到订单 CSV：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise LedgerError(f"订单 CSV 缺少表头：{path}")
        missing = [
            field
            for field in ORDER_FIELDS
            if field not in reader.fieldnames and field != "cohort"
        ]
        if missing:
            raise LedgerError(f"订单 CSV 缺少字段：{missing}")
        rows: list[dict[str, str]] = []
        for raw in reader:
            # 兼容模板末尾的 `#` 注释段：任一列以 # 开头即视为注释行。
            # 真实订单的 order_id 不允许以 # 开头（validate 会拒绝）。
            if any(
                normalize_text(raw.get(field)).startswith("#") for field in ORDER_FIELDS
            ):
                continue
            values = {field: normalize_text(raw.get(field)) for field in ORDER_FIELDS}
            # 旧 16 列 CSV 没有 cohort 列：统一 normalize 为 g2。
            values["cohort"] = values["cohort"].lower() or "g2"
            if not any(values.values()):
                continue
            rows.append(values)
    return rows


def write_orders(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=ORDER_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            output = {field: row.get(field, "") for field in ORDER_FIELDS}
            output["cohort"] = normalize_text(output.get("cohort")).lower() or "g2"
            writer.writerow(output)


def _paid_rows(rows: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    return [row for row in rows if normalize_text(row.get("paid_at"))]


def validate_orders(
    rows: Sequence[Mapping[str, str]], *, force: bool = False
) -> list[ValidationIssue]:
    """逐行校验。force 仅放宽“同户重复有效付费单”，其他规则不放宽。"""
    issues: list[ValidationIssue] = []
    seen_order_ids: dict[str, int] = {}
    paid_by_household: dict[str, str] = {}

    for index, row in enumerate(rows, start=1):
        order_id = normalize_text(row.get("order_id"))
        household = normalize_text(row.get("household_id"))
        plan = normalize_text(row.get("plan"))

        def add(
            field: str,
            message: str,
            _index: int = index,
            _order_id: str = order_id,
        ) -> None:
            issues.append(ValidationIssue(_index, _order_id, field, message))

        if not order_id:
            add("order_id", "订单号不能为空")
        elif order_id in seen_order_ids:
            add("order_id", f"重复订单号（第 {seen_order_ids[order_id]} 行已使用）")
        else:
            seen_order_ids[order_id] = index

        if not household:
            add("household_id", "户号不能为空")

        if plan not in PLAN_PRICES:
            add("plan", f"档位必须是 A/B/C，实际 {plan or '空'}")
            amount = None
        else:
            try:
                amount = parse_amount(row.get("amount"))
            except ValueError as error:
                add("amount", str(error))
                amount = None
            if amount is not None and amount != PLAN_PRICES[plan]:
                add(
                    "amount",
                    f"{plan} 档应为 {PLAN_PRICES[plan]}，实际 {amount}",
                )
            if amount == 0:
                add("amount", "订单金额必须 >0")

        currency = normalize_text(row.get("currency")).upper() or "CNY"
        if currency != "CNY":
            add("currency", f"当前仅支持 CNY，实际 {currency}")

        cohort = normalize_text(row.get("cohort")).lower() or "g2"
        if cohort not in {"g2", "g3"}:
            add("cohort", f"cohort 只能是 g2/g3（空按 g2），实际 {cohort!r}")

        synthetic = parse_bool(row.get("synthetic"))
        if synthetic is None:
            add("synthetic", f"只能填 true/false，实际 {row.get('synthetic')!r}")

        created = paid = requested = refunded = None
        for field, value in (
            ("created_at", row.get("created_at")),
            ("paid_at", row.get("paid_at")),
            ("refund_requested_at", row.get("refund_requested_at")),
            ("refunded_at", row.get("refunded_at")),
        ):
            try:
                parsed = parse_datetime(value)
            except ValueError as error:
                add(field, str(error))
                continue
            if field == "created_at":
                created = parsed
            elif field == "paid_at":
                paid = parsed
            elif field == "refund_requested_at":
                requested = parsed
            else:
                refunded = parsed

        if created is None:
            add("created_at", "下单时间不能为空")
        if paid is not None and created is not None and paid < created:
            add("paid_at", "付费时间早于下单时间")

        if paid is not None and not normalize_text(row.get("payment_ref")):
            add("payment_ref", "已付费订单必须填写支付流水号，否则无法对账")

        try:
            refund_amount = parse_amount(row.get("refund_amount"))
        except ValueError as error:
            add("refund_amount", str(error))
            refund_amount = None

        status = normalize_text(row.get("refund_status")) or "none"
        if status not in REFUND_STATUSES:
            add("refund_status", f"必须是 {REFUND_STATUSES}，实际 {status!r}")
        else:
            if paid is None and status != "none":
                add("refund_status", "未付费订单不能有退款状态")
            if status == "none":
                if requested or refunded:
                    add("refund_requested_at", "none 状态不应有退款时间")
                if refund_amount is not None and refund_amount != 0:
                    add("refund_amount", "none 状态退款金额应为 0")
            elif status == "requested":
                if requested is None:
                    add("refund_requested_at", "requested 状态必须填写申请时间")
                if refunded is not None:
                    add("refunded_at", "requested 状态不应有退款完成时间")
                if (
                    refund_amount is not None
                    and amount is not None
                    and refund_amount > amount
                ):
                    add("refund_amount", "退款金额不能超过实付金额")
            elif status == "refunded":
                if requested is None:
                    add("refund_requested_at", "refunded 状态必须填写申请时间")
                if refunded is None:
                    add("refunded_at", "refunded 状态必须填写退款完成时间")
                if refund_amount is None or refund_amount <= 0:
                    add("refund_amount", "refunded 状态退款金额必须 >0")
                elif amount is not None and refund_amount > amount:
                    add("refund_amount", "退款金额不能超过实付金额")
                if requested and paid and requested < paid:
                    add("refund_requested_at", "退款申请时间早于付费时间")
                if refunded and requested and refunded < requested:
                    add("refunded_at", "退款完成时间早于申请时间")
            elif status == "rejected":
                if requested is None:
                    add("refund_requested_at", "rejected 状态必须填写申请时间")
                if refunded is not None:
                    add("refunded_at", "rejected 状态不应有退款完成时间")
                if refund_amount is not None and refund_amount != 0:
                    add("refund_amount", "rejected 状态退款金额应为 0")

        if paid is not None and household:
            first_order = paid_by_household.get(household)
            if first_order is not None and not force:
                add(
                    "household_id",
                    f"同户重复有效付费单（首单 {first_order}）；"
                    "确属补单请显式使用 --force",
                )
            paid_by_household.setdefault(household, order_id)

    return issues


def compute_summary(
    rows: Sequence[Mapping[str, str]],
    *,
    source: str = "",
    synthetic: bool | None = None,
) -> dict:
    """G2 指标。退款率默认按户；同时给出按单口径。"""
    paid = _paid_rows(rows)
    paid_households = {
        normalize_text(row.get("household_id")) for row in paid
    } - {""}
    refunded_orders = [
        row for row in paid if normalize_text(row.get("refund_status")) == "refunded"
    ]
    refunded_households = {
        normalize_text(row.get("household_id")) for row in refunded_orders
    } - {""}

    paid_count = len(paid_households)
    refund_count = len(refunded_households)
    rate = (
        Decimal(refund_count) / Decimal(paid_count)
        if paid_count
        else Decimal(0)
    )
    rate_by_order = (
        Decimal(len(refunded_orders)) / Decimal(len(paid))
        if paid
        else Decimal(0)
    )

    gross = sum((parse_amount(row.get("amount")) for row in paid), Decimal(0))
    refund_total = sum(
        (parse_amount(row.get("refund_amount")) for row in refunded_orders),
        Decimal(0),
    )

    by_plan: dict[str, dict] = {}
    for plan in ("A", "B", "C"):
        plan_orders = [row for row in paid if normalize_text(row.get("plan")) == plan]
        by_plan[plan] = {
            "orders": len(plan_orders),
            "households": len(
                {normalize_text(row.get("household_id")) for row in plan_orders} - {""}
            ),
            "gross_revenue": _num(
                sum(
                    (parse_amount(row.get("amount")) for row in plan_orders),
                    Decimal(0),
                )
            ),
            "refunded_orders": sum(
                1
                for row in plan_orders
                if normalize_text(row.get("refund_status")) == "refunded"
            ),
        }

    paid_orders_by_cohort: dict[str, int] = {}
    paid_households_by_cohort: dict[str, int] = {}
    refunded_by_cohort: dict[str, int] = {}
    for cohort in ("g2", "g3"):
        cohort_paid = [
            row
            for row in paid
            if (normalize_text(row.get("cohort")).lower() or "g2") == cohort
        ]
        cohort_households = {
            normalize_text(row.get("household_id")) for row in cohort_paid
        } - {""}
        cohort_refunded = {
            normalize_text(row.get("household_id"))
            for row in cohort_paid
            if normalize_text(row.get("refund_status")) == "refunded"
        } - {""}
        paid_orders_by_cohort[cohort] = len(cohort_paid)
        paid_households_by_cohort[cohort] = len(cohort_households)
        refunded_by_cohort[cohort] = len(cohort_refunded)
    refund_rate_by_cohort = {
        cohort: (
            round(
                refunded_by_cohort[cohort] / paid_households_by_cohort[cohort], 4
            )
            if paid_households_by_cohort[cohort]
            else 0.0
        )
        for cohort in ("g2", "g3")
    }

    g2_pass = paid_count >= MIN_PAID_HOUSEHOLDS and rate <= MAX_REFUND_RATE
    if g2_pass:
        action = "go"
        action_reason = (
            "≥10 户完成真实预付且退款率 ≤20%：进入 G3 内测准备"
        )
    elif paid_count >= MIN_PAID_HOUSEHOLDS:
        action = "降价或缩范围"
        action_reason = (
            "预付达标但退款率 >20%：先排查退款原因，"
            "缩到“门锁+燃气”复测 2 周，暂不 go"
        )
    elif paid_count >= 5:
        action = "降价或缩范围"
        action_reason = (
            "预付 5–9 户：降价或改买断，并缩到“门锁+燃气”再测 2 周"
        )
    else:
        action = "转 B2B"
        action_reason = "预付 <5 户：按 §7 G2 立即转 B2B"

    gaps: list[str] = []
    if paid_count < MIN_PAID_HOUSEHOLDS:
        gaps.append(
            f"预付户数 {paid_count}/{MIN_PAID_HOUSEHOLDS}，"
            f"缺 {MIN_PAID_HOUSEHOLDS - paid_count} 户"
        )
    if rate > MAX_REFUND_RATE:
        gaps.append(f"退款率 {rate * 100:.1f}% 超过 20% 上限")

    if synthetic is None:
        synthetic = any(parse_bool(row.get("synthetic")) is True for row in rows)

    return {
        "source": source or None,
        "synthetic_demo": bool(synthetic),
        "notice": (
            SYNTHETIC_NOTICE
            if synthetic
            else "真实订单数据；不含卡号/身份证等支付敏感信息"
        ),
        "orders_total": len(rows),
        "paid_orders": len(paid),
        "paid_households": paid_count,
        "paid_households_by_cohort": paid_households_by_cohort,
        "paid_orders_by_cohort": paid_orders_by_cohort,
        "refunded_by_cohort": refunded_by_cohort,
        "refund_rate_by_cohort": refund_rate_by_cohort,
        "unpaid_orders": len(rows) - len(paid),
        "refunded_orders": len(refunded_orders),
        "refunded_count": refund_count,
        "refunded_households": refund_count,
        "refund_rate": round(float(rate), 4),
        "refund_rate_by_order": round(float(rate_by_order), 4),
        "gross_revenue": _num(gross),
        "refund_amount_total": _num(refund_total),
        "net_revenue": _num(gross - refund_total),
        "by_plan": by_plan,
        "g2_pass": g2_pass,
        "g2_action": action,
        "g2_action_reason": action_reason,
        "gaps": gaps,
        "target_households": TARGET_HOUSEHOLDS,
        "min_paid_households": MIN_PAID_HOUSEHOLDS,
        "max_refund_rate": float(MAX_REFUND_RATE),
    }


def _num(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)

def mark_refund(
    rows: list[dict[str, str]],
    *,
    order_id: str,
    target_status: str,
    amount: str | None = None,
    at: datetime | None = None,
) -> list[dict[str, str]]:
    """按 none→requested→refunded/rejected 状态机就地更新一行。"""
    if target_status not in {"requested", "refunded", "rejected"}:
        raise LedgerError(
            f"--status 只能是 requested/refunded/rejected，实际 {target_status!r}"
        )
    index = next(
        (
            position
            for position, row in enumerate(rows)
            if normalize_text(row.get("order_id")) == order_id
        ),
        None,
    )
    if index is None:
        raise LedgerBusinessError(f"订单不存在：{order_id}")

    row = rows[index]
    current = normalize_text(row.get("refund_status")) or "none"
    if target_status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise LedgerBusinessError(
            f"非法退款状态转换：{current} → {target_status}"
            "（合法路径：none→requested→refunded/rejected，不可逆）"
        )
    if not normalize_text(row.get("paid_at")):
        raise LedgerBusinessError(f"未付费订单不能标记退款：{order_id}")

    try:
        order_amount = parse_amount(row.get("amount"))
    except ValueError as error:
        raise LedgerBusinessError(f"订单金额不合法：{error}") from error

    timestamp = at or datetime.now().replace(microsecond=0)
    if target_status == "requested":
        try:
            paid_at = parse_datetime(row.get("paid_at"))
        except ValueError as error:
            raise LedgerBusinessError(str(error)) from error
        if paid_at is not None and timestamp < paid_at:
            raise LedgerBusinessError("退款申请时间不能早于付费时间")
        try:
            value = parse_amount(amount) if amount is not None else Decimal(0)
        except ValueError as error:
            raise LedgerError(str(error)) from error
        if value > order_amount:
            raise LedgerBusinessError("退款金额不能超过实付金额")
        row["refund_status"] = "requested"
        row["refund_requested_at"] = format_datetime(timestamp)
        row["refunded_at"] = ""
        row["refund_amount"] = str(_num(value))
    elif target_status == "refunded":
        if amount is None:
            raise LedgerError("--status refunded 必须同时提供 --amount")
        try:
            value = parse_amount(amount)
            requested_at = parse_datetime(row.get("refund_requested_at"))
        except ValueError as error:
            raise LedgerError(str(error)) from error
        if value <= 0 or value > order_amount:
            raise LedgerBusinessError(
                f"退款金额必须满足 0 < 退款 ≤ {_num(order_amount)}，实际 {value}"
            )
        if requested_at is None:
            raise LedgerBusinessError(
                "refunded 前必须先标记 requested（缺少退款申请时间）"
            )
        if timestamp < requested_at:
            raise LedgerBusinessError("退款完成时间不能早于申请时间")
        row["refund_status"] = "refunded"
        row["refunded_at"] = format_datetime(timestamp)
        row["refund_amount"] = str(_num(value))
    else:
        row["refund_status"] = "rejected"
        row["refunded_at"] = ""
        row["refund_amount"] = "0"
    return rows


def _money(value: object) -> str:
    return f"¥{value}"


def _rate_text(rate: object) -> str:
    return f"{float(rate) * 100:.1f}%"


def render_report(
    summary: Mapping[str, object], rows: Sequence[Mapping[str, str]]
) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    paid = int(summary.get("paid_households", 0))
    paid_orders = int(summary.get("paid_orders", 0))
    refunded_count = int(summary.get("refunded_count", 0))
    refunded_orders = int(summary.get("refunded_orders", 0))
    rate = float(summary.get("refund_rate", 0.0))
    rate_by_order = float(summary.get("refund_rate_by_order", 0.0))
    paid_ok = paid >= MIN_PAID_HOUSEHOLDS
    rate_ok = rate <= float(MAX_REFUND_RATE)
    g2_pass = bool(summary.get("g2_pass"))
    action = str(summary.get("g2_action", ""))
    reason = str(summary.get("g2_action_reason", ""))
    gaps = list(summary.get("gaps") or [])

    lines: list[str] = []
    if synthetic:
        lines.append(SYNTHETIC_MARKDOWN_BANNER)
    lines += [
        "# G2 预付台账报告",
        "",
        f"> 数据来源：`{summary.get('source') or '（未提供来源）'}`",
        "> 口径：`docs/用户验证包-v0.1.md` §7 G2（≥10 户真实预付、退款率 ≤20%）",
        "",
        "## 一、G2 判据",
        "",
        "| 判据 | 实际 | 门槛 | 结论 |",
        "| --- | --- | --- | --- |",
        f"| ① 完成真实预付户数 | {paid} | ≥{MIN_PAID_HOUSEHOLDS} | "
        f"{'通过' if paid_ok else '未通过'} |",
        f"| ② 退款率（按户） | {_rate_text(rate)} | ≤20% | "
        f"{'通过' if rate_ok else '未通过'} |",
        "",
        f"**G2 结论：{'通过' if g2_pass else '未通过'} → 当前动作：{action}。**",
        "",
        f"> {reason}",
        "",
        "## 二、缺口",
        "",
    ]
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("无（两条判据均满足）。")

    lines += [
        "",
        "## 三、分档明细",
        "",
        "| 档位 | 价格 | 付费单 | 付费户 | 毛收入 | 退款单 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    by_plan = summary.get("by_plan") or {}
    for plan in ("A", "B", "C"):
        bucket = by_plan.get(plan, {})
        lines.append(
            f"| {plan} | {PLAN_LABELS[plan]}（{PLAN_INTERVALS[plan]}） | "
            f"{bucket.get('orders', 0)} | {bucket.get('households', 0)} | "
            f"{_money(bucket.get('gross_revenue', 0))} | "
            f"{bucket.get('refunded_orders', 0)} |"
        )

    lines += [
        "",
        "## 四、收入",
        "",
        "| 指标 | 金额 |",
        "| --- | --- |",
        f"| 毛收入（已付订单） | {_money(summary.get('gross_revenue', 0))} |",
        f"| 退款金额 | {_money(summary.get('refund_amount_total', 0))} |",
        f"| 净收入 | {_money(summary.get('net_revenue', 0))} |",
        "",
        f"订单总数 {summary.get('orders_total', 0)}，已付 {paid_orders} 单，"
        f"未付/过期 {summary.get('unpaid_orders', 0)} 单。",
        "",
        "## 五、退款明细",
        "",
    ]
    refunded_rows = [
        row
        for row in rows
        if normalize_text(row.get("paid_at"))
        and normalize_text(row.get("refund_status")) == "refunded"
    ]
    if refunded_rows:
        lines += [
            "| 订单号 | 户号 | 档位 | 实付 | 退款 | 退款完成时间 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in refunded_rows:
            lines.append(
                f"| {row.get('order_id')} | {row.get('household_id')} | "
                f"{row.get('plan')} | {_money(row.get('amount'))} | "
                f"{_money(row.get('refund_amount'))} | "
                f"{row.get('refunded_at') or '—'} |"
            )
    else:
        lines.append("无退款。")

    unpaid_rows = [row for row in rows if not normalize_text(row.get("paid_at"))]
    lines += [
        "",
        "## 六、未付/过期订单",
        "",
        f"共 {len(unpaid_rows)} 单。",
        "",
    ]
    if unpaid_rows:
        lines.append(
            "户号："
            + "、".join(str(row.get("household_id")) for row in unpaid_rows[:20])
            + ("…" if len(unpaid_rows) > 20 else "")
        )

    lines += [
        "",
        "## 七、口径与限制",
        "",
        "- **退款率默认按户**：分子 = 存在 `refunded` 订单的去重户数，"
        f"分母 = 有过 `paid_at` 的去重户数；本表另给按单口径 "
        f"{_rate_text(rate_by_order)}（{refunded_orders}/{paid_orders} 单）。",
        "- **预付户包含后来退款的户**：先发生真实预付，退款是后续事件；"
        "退款率作为独立次判据约束，避免用退款抹掉预付款。",
        "- **C 档为一次性买断**，不产生续费；A/B 为年费档。",
        "- **不接真实支付**：本台账只记录订单号、匿名户号、档位、金额、"
        "支付流水号与退款状态；不接收/不存储卡号、身份证、支付密码。",
        "- 报告数字全部由订单 CSV 计算，不做统计显著性检验；"
        "合成数据的数字不得当作真实收入。",
        "",
    ]
    return "\n".join(lines)


def format_summary(
    summary: Mapping[str, object], *, out_dir: Path | None = None
) -> str:
    synthetic = bool(summary.get("synthetic_demo"))
    lines = [
        "G2 台账报告" + ("（SYNTHETIC DEMO）" if synthetic else ""),
        f"  付费：{summary.get('paid_households', 0)} 户 / "
        f"{summary.get('paid_orders', 0)} 单；退款："
        f"{summary.get('refunded_count', 0)} 户 / "
        f"{summary.get('refunded_orders', 0)} 单；退款率 "
        f"{_rate_text(summary.get('refund_rate', 0))}（按户）",
        f"  收入：毛 {_money(summary.get('gross_revenue', 0))} / "
        f"退款 {_money(summary.get('refund_amount_total', 0))} / "
        f"净 {_money(summary.get('net_revenue', 0))}",
        "  分档："
        + "、".join(
            f"{plan} {int((summary.get('by_plan') or {}).get(plan, {}).get('households', 0))} 户"
            for plan in ("A", "B", "C")
        ),
        f"  G2：{'通过' if summary.get('g2_pass') else '未通过'}"
        f" → {summary.get('g2_action', '')}",
        "  缺口："
        + ("；".join(summary.get("gaps") or []) or "无"),
    ]
    if out_dir is not None:
        lines.append(f"  产物：{out_dir}")
    if synthetic:
        lines.append(f"  ⚠️ {SYNTHETIC_NOTICE}")
    return "\n".join(lines)

PAYMENT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "payment_ref",
        ("payment_ref", "流水号", "交易号", "支付单号", "transaction_id", "trade_no"),
    ),
    ("amount", ("amount", "金额", "实付金额")),
    ("currency", ("currency", "币种")),
    ("paid_at", ("paid_at", "支付时间")),
    ("household_id", ("household_id", "户号")),
    ("status", ("status", "状态")),
)

_REFUND_PAYMENT_STATUSES = {"refund", "refunded", "退款", "已退款"}


def resolve_payment_columns(headers: Sequence[str]) -> dict[str, str]:
    columns: dict[str, str] = {}
    used: set[str] = set()
    for key, hints in PAYMENT_HINTS:
        for header in headers:
            if header in used:
                continue
            text = normalize_text(header).lower()
            if any(hint.lower() in text for hint in hints):
                columns[key] = header
                used.add(header)
                break
    missing = [key for key in ("payment_ref", "amount") if key not in columns]
    if missing:
        raise LedgerError(
            f"支付流水 CSV 缺少列：{missing}；实际表头：{list(headers)}"
        )
    return columns


def read_payments(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise LedgerError(f"找不到支付流水 CSV：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise LedgerError(f"支付流水 CSV 缺少表头：{path}")
        columns = resolve_payment_columns(reader.fieldnames)
        rows: list[dict[str, str]] = []
        for raw in reader:
            values = {
                key: normalize_text(raw.get(column))
                for key, column in columns.items()
            }
            if not any(values.values()):
                continue
            status = values.get("status", "").lower()
            if status in _REFUND_PAYMENT_STATUSES:
                continue
            rows.append(
                {
                    "payment_ref": values.get("payment_ref", ""),
                    "amount": values.get("amount", ""),
                    "currency": values.get("currency", "") or "CNY",
                    "paid_at": values.get("paid_at", ""),
                    "household_id": values.get("household_id", ""),
                    "status": status,
                }
            )
    return rows


def reconcile_orders(
    orders: Sequence[Mapping[str, str]],
    payments: Sequence[Mapping[str, str]],
) -> dict:
    """外部支付流水与订单表对账；仅对比已付订单。"""
    paid = _paid_rows(orders)
    order_by_ref: dict[str, dict] = {}
    orders_without_ref: list[dict] = []
    invalid_orders: list[dict] = []
    duplicate_order_refs: list[str] = []
    for row in paid:
        reference = normalize_text(row.get("payment_ref"))
        if not reference:
            orders_without_ref.append(
                {
                    "order_id": row.get("order_id"),
                    "household_id": row.get("household_id"),
                }
            )
            continue
        try:
            amount = parse_amount(row.get("amount"))
        except ValueError as error:
            invalid_orders.append(
                {"order_id": row.get("order_id"), "error": str(error)}
            )
            continue
        if reference in order_by_ref:
            duplicate_order_refs.append(reference)
            continue
        order_by_ref[reference] = {
            "row": row,
            "amount": amount,
            "currency": normalize_text(row.get("currency")).upper() or "CNY",
        }

    payment_by_ref: dict[str, dict] = {}
    invalid_payments: list[dict] = []
    duplicate_payment_refs: list[str] = []
    payments_without_ref: list[dict] = []
    for row in payments:
        reference = normalize_text(row.get("payment_ref"))
        if not reference:
            payments_without_ref.append({"amount": row.get("amount")})
            continue
        try:
            amount = parse_amount(row.get("amount"))
        except ValueError as error:
            invalid_payments.append({"payment_ref": reference, "error": str(error)})
            continue
        if reference in payment_by_ref:
            duplicate_payment_refs.append(reference)
            continue
        payment_by_ref[reference] = {
            "row": row,
            "amount": amount,
            "currency": normalize_text(row.get("currency")).upper() or "CNY",
        }

    missing_in_orders = [
        {
            "payment_ref": reference,
            "amount": _num(bucket["amount"]),
            "household_id": bucket["row"].get("household_id"),
            "paid_at": bucket["row"].get("paid_at"),
        }
        for reference, bucket in payment_by_ref.items()
        if reference not in order_by_ref
    ]
    missing_in_payments = [
        {
            "order_id": bucket["row"].get("order_id"),
            "payment_ref": reference,
            "household_id": bucket["row"].get("household_id"),
        }
        for reference, bucket in order_by_ref.items()
        if reference not in payment_by_ref
    ]

    amount_mismatch: list[dict] = []
    currency_mismatch: list[dict] = []
    for reference in sorted(set(order_by_ref) & set(payment_by_ref)):
        order_bucket = order_by_ref[reference]
        payment_bucket = payment_by_ref[reference]
        if order_bucket["amount"] != payment_bucket["amount"]:
            amount_mismatch.append(
                {
                    "payment_ref": reference,
                    "order_id": order_bucket["row"].get("order_id"),
                    "order_amount": _num(order_bucket["amount"]),
                    "payment_amount": _num(payment_bucket["amount"]),
                }
            )
        if order_bucket["currency"] != payment_bucket["currency"]:
            currency_mismatch.append(
                {
                    "payment_ref": reference,
                    "order_currency": order_bucket["currency"],
                    "payment_currency": payment_bucket["currency"],
                }
            )

    ok = not any(
        (
            missing_in_orders,
            missing_in_payments,
            amount_mismatch,
            currency_mismatch,
            orders_without_ref,
            invalid_orders,
            invalid_payments,
            duplicate_order_refs,
            duplicate_payment_refs,
            payments_without_ref,
        )
    )
    return {
        "orders_total": len(orders),
        "paid_orders": len(paid),
        "payments_rows": len(payments),
        "matched": len(set(order_by_ref) & set(payment_by_ref)),
        "missing_in_orders": missing_in_orders,
        "missing_in_payments": missing_in_payments,
        "amount_mismatch": amount_mismatch,
        "currency_mismatch": currency_mismatch,
        "orders_without_ref": orders_without_ref,
        "invalid_orders": invalid_orders,
        "invalid_payments": invalid_payments,
        "duplicate_order_refs": duplicate_order_refs,
        "duplicate_payment_refs": duplicate_payment_refs,
        "payments_without_ref": payments_without_ref,
        "ok": ok,
    }


def _ref_preview(items: Sequence[Mapping[str, object]], key: str) -> str:
    values = [str(item.get(key, "")) for item in items[:5]]
    suffix = "…" if len(items) > 5 else ""
    return "、".join(values) + suffix


def render_reconcile(result: Mapping[str, object]) -> str:
    missing_orders = list(result.get("missing_in_orders") or [])
    missing_payments = list(result.get("missing_in_payments") or [])
    amount_mismatch = list(result.get("amount_mismatch") or [])
    currency_mismatch = list(result.get("currency_mismatch") or [])
    lines = [
        "G2 支付对账",
        f"  已付订单 {result['paid_orders']}；流水行 {result['payments_rows']}；"
        f"匹配 {result['matched']}",
        f"  {'✗' if missing_orders else '✓'} 流水有、订单无："
        f"{len(missing_orders)}"
        + (
            f"（{_ref_preview(missing_orders, 'payment_ref')}）"
            if missing_orders
            else ""
        ),
        f"  {'✗' if missing_payments else '✓'} 订单有、流水无："
        f"{len(missing_payments)}"
        + (
            f"（{_ref_preview(missing_payments, 'order_id')}）"
            if missing_payments
            else ""
        ),
        f"  {'✗' if amount_mismatch else '✓'} 金额不一致："
        f"{len(amount_mismatch)}"
        + (
            f"（{_ref_preview(amount_mismatch, 'payment_ref')}）"
            if amount_mismatch
            else ""
        ),
        f"  {'✗' if currency_mismatch else '✓'} 币种不一致："
        f"{len(currency_mismatch)}",
    ]
    if result.get("orders_without_ref"):
        lines.append(
            f"  ✗ 已付订单缺流水号：{len(result['orders_without_ref'])}"
            f"（{_ref_preview(result['orders_without_ref'], 'order_id')}）"
        )
    if result.get("invalid_orders") or result.get("invalid_payments"):
        lines.append(
            "  ✗ 金额无法解析："
            f"订单 {len(result.get('invalid_orders') or [])} / "
            f"流水 {len(result.get('invalid_payments') or [])}"
        )
    if result.get("duplicate_order_refs") or result.get("duplicate_payment_refs"):
        lines.append(
            "  ✗ 重复流水号："
            f"订单 {len(result.get('duplicate_order_refs') or [])} / "
            f"流水 {len(result.get('duplicate_payment_refs') or [])}"
        )
    lines.append(
        f"  结论：{'一致' if result.get('ok') else '存在差异，需人工核对'}"
    )
    return "\n".join(lines)


def render_fields_doc() -> str:
    lines = [
        "# G2 订单台账字段说明",
        "",
        "> 本工具**不接收、不存储卡号、身份证、支付密码**；"
        "`synthetic=true` 的行不得当作真实收入。",
        "",
        "| 字段 | 说明 |",
        "| --- | --- |",
    ]
    lines.extend(f"| `{field}` | {doc} |" for field, doc in FIELD_DOCS)
    lines += [
        "",
        "## 档位与金额",
        "",
        "| 档位 | 价格 | 应付金额 | 计费方式 |",
        "| --- | --- | --- | --- |",
        "| A | ¥99/年 | 99 | 年费 |",
        "| B | ¥199/年 | 199 | 年费 |",
        "| C | ¥299 买断 | 299 | 一次性 |",
        "",
        "## 退款状态机",
        "",
        "`none → requested → refunded` 或 `none → requested → rejected`；不可逆。",
        "",
        "- `requested`：填写 `refund_requested_at`，`refund_amount` 可为 0。",
        "- `refunded`：填写 `refunded_at` 与 `refund_amount`（0 < 退款 ≤ 实付）。",
        "- `rejected`：`refunded_at` 留空，`refund_amount=0`。",
        "",
        "## G2 判据（`用户验证包 §7`）",
        "",
        "- ≥10 户完成真实预付且退款率 ≤20% → **go**；",
        "- 5–9 户 → 降价或改买断，并缩到“门锁+燃气”再测 2 周；",
        "- <5 户 → **转 B2B**。",
        "",
    ]
    return "\n".join(lines)


def build_template_row() -> dict[str, str]:
    return {
        "order_id": "SYNTHETIC-DEMO-001",
        "household_id": "SYNTHETIC-HH-001",
        "plan": "A",
        "amount": "99",
        "currency": "CNY",
        "created_at": "2026-11-01 10:00:00",
        "paid_at": "2026-11-01 10:05:00",
        "payment_method": "synthetic",
        "payment_ref": "SYNTHETIC-REF-001",
        "refund_status": "none",
        "refund_requested_at": "",
        "refunded_at": "",
        "refund_amount": "0",
        "consent_version": "v0.1",
        "note": f"示例行：请删除后填写真实订单；{SYNTHETIC_NOTICE}",
        "synthetic": "true",
        "cohort": "g2",
    }


DEMO_PLANS = ["A", "A", "A", "B", "B", "B", "B", "C", "C", "C"]


def build_demo_rows() -> list[dict[str, str]]:
    """20 户 synthetic：10 户付费（A3/B4/C3），1 户退款（10%），10 户未付。"""
    rows: list[dict[str, str]] = []
    for index, plan in enumerate(DEMO_PLANS, start=1):
        amount = str(_num(PLAN_PRICES[plan]))
        row = {
            "order_id": f"SYN-DEMO-{index:03d}",
            "household_id": f"SYN-HH-{index:02d}",
            "plan": plan,
            "amount": amount,
            "currency": "CNY",
            "created_at": "2026-11-15 10:00:00",
            "paid_at": "2026-11-15 10:05:00",
            "payment_method": "synthetic",
            "payment_ref": f"SYN-REF-{index:03d}",
            "refund_status": "none",
            "refund_requested_at": "",
            "refunded_at": "",
            "refund_amount": "0",
            "consent_version": "v0.1",
            "note": SYNTHETIC_NOTICE,
            "synthetic": "true",
            "cohort": "g3" if index <= 4 else "g2",
        }
        if index == 3:
            row.update(
                {
                    "refund_status": "refunded",
                    "refund_requested_at": "2026-11-20 09:00:00",
                    "refunded_at": "2026-11-22 15:00:00",
                    "refund_amount": amount,
                }
            )
        rows.append(row)

    for index in range(11, 21):
        plan = ("A", "B", "C")[(index - 11) % 3]
        rows.append(
            {
                "order_id": f"SYN-DEMO-{index:03d}",
                "household_id": f"SYN-HH-{index:02d}",
                "plan": plan,
                "amount": str(_num(PLAN_PRICES[plan])),
                "currency": "CNY",
                "created_at": "2026-11-16 10:00:00",
                "paid_at": "",
                "payment_method": "",
                "payment_ref": "",
                "refund_status": "none",
                "refund_requested_at": "",
                "refunded_at": "",
                "refund_amount": "0",
                "consent_version": "v0.1",
                "note": f"未付/过期；{SYNTHETIC_NOTICE}",
                "synthetic": "true",
                "cohort": "g2",
            }
        )
    return rows


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

def _print_issues(prefix: str, issues: Sequence[ValidationIssue]) -> None:
    print(f"{prefix}：{len(issues)} 个问题")
    for issue in issues:
        print("  - " + issue.as_text())


def cmd_new(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if out.exists() and not args.force:
        raise LedgerError(f"文件已存在：{out}；覆盖请加 --force")
    write_orders(out, [build_template_row()])
    fields_path = out.with_suffix(".fields.md")
    fields_path.write_text(render_fields_doc(), encoding="utf-8")
    print(f"已生成模板：{out}")
    print(f"字段说明：{fields_path}")
    print("示例行 synthetic=true，仅用于说明格式；请删除后填写真实订单。")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    rows = read_orders(Path(args.orders))
    issues = validate_orders(rows, force=args.force)
    if issues:
        _print_issues("校验失败", issues)
        return 1
    summary = compute_summary(rows, source=str(args.orders))
    print(
        f"校验通过：{len(rows)} 行订单，"
        f"{summary['paid_households']} 户付费，"
        f"{summary['refunded_count']} 户退款"
        + ("（已 --force 放宽同户重复检查）" if args.force else "")
    )
    return 0


def cmd_refund(args: argparse.Namespace) -> int:
    rows = read_orders(Path(args.orders))
    try:
        at = (
            parse_datetime(args.at)
            if args.at
            else datetime.now().replace(microsecond=0)
        )
    except ValueError as error:
        raise LedgerError(str(error)) from error
    if at is None:
        at = datetime.now().replace(microsecond=0)

    mark_refund(
        rows,
        order_id=args.order_id,
        target_status=args.status,
        amount=args.amount,
        at=at,
    )
    issues = validate_orders(rows, force=True)
    if issues:
        _print_issues("退款后台账校验失败", issues)
        return 1

    out = Path(args.out) if args.out else Path(args.orders)
    write_orders(out, rows)
    print(
        f"已更新订单 {args.order_id} → refund_status={args.status}；"
        f"写入 {out}"
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    orders_path = Path(args.orders)
    rows = read_orders(orders_path)
    issues = validate_orders(rows)
    if issues:
        _print_issues("台账校验失败，未生成报告", issues)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = compute_summary(rows, source=str(orders_path))
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "g2_report.md").write_text(
        render_report(summary, rows), encoding="utf-8"
    )
    print(f"  台账：{orders_path}")
    print(format_summary(summary, out_dir=out_dir))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    orders = read_orders(Path(args.orders))
    payments = read_payments(Path(args.payments))
    result = reconcile_orders(orders, payments)
    print(render_reconcile(result))
    if args.out:
        _write_json(Path(args.out), result)
        print(f"  对账结果：{args.out}")
    return 0 if result["ok"] else 1


def cmd_demo(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) / "demo" / "out"
    rows = build_demo_rows()
    issues = validate_orders(rows)
    if issues:
        raise LedgerBusinessError(
            "内置合成数据校验失败：" + "；".join(issue.as_text() for issue in issues)
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    orders_path = out_dir / "orders.csv"
    write_orders(orders_path, rows)
    summary = compute_summary(rows, source=str(orders_path), synthetic=True)
    _write_json(out_dir / "summary.json", summary)
    (out_dir / "g2_report.md").write_text(
        render_report(summary, rows), encoding="utf-8"
    )
    print(f"  台账：{orders_path}")
    print(format_summary(summary, out_dir=out_dir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G2 预订/退款台账（校验、退款状态机、G2 判据、对账）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="生成空订单模板 + 字段说明")
    new_parser.add_argument("--out", default=str(DEFAULT_ORDERS))
    new_parser.add_argument("--force", action="store_true", help="覆盖已存在文件")
    new_parser.set_defaults(func=cmd_new)

    validate_parser = subparsers.add_parser("validate", help="校验订单台账")
    validate_parser.add_argument("--orders", default=str(DEFAULT_ORDERS))
    validate_parser.add_argument(
        "--force", action="store_true", help="放宽同户重复有效付费单检查"
    )
    validate_parser.set_defaults(func=cmd_validate)

    refund_parser = subparsers.add_parser("refund", help="按状态机标记退款")
    refund_parser.add_argument("--orders", default=str(DEFAULT_ORDERS))
    refund_parser.add_argument("--order-id", required=True)
    refund_parser.add_argument(
        "--status",
        required=True,
        choices=("requested", "refunded", "rejected"),
    )
    refund_parser.add_argument("--amount", help="退款金额（refunded 时必填）")
    refund_parser.add_argument("--at", help="操作时间（ISO 8601，默认当前时间）")
    refund_parser.add_argument("--out", help="输出 CSV；默认原地更新")
    refund_parser.set_defaults(func=cmd_refund)

    report_parser = subparsers.add_parser("report", help="生成 summary.json + g2_report.md")
    report_parser.add_argument("--orders", default=str(DEFAULT_ORDERS))
    report_parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    report_parser.set_defaults(func=cmd_report)

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="外部支付流水与订单表对账"
    )
    reconcile_parser.add_argument("--orders", default=str(DEFAULT_ORDERS))
    reconcile_parser.add_argument("--payments", required=True)
    reconcile_parser.add_argument("--out", help="可选：对账结果 JSON")
    reconcile_parser.set_defaults(func=cmd_reconcile)

    demo_parser = subparsers.add_parser(
        "demo", help="生成 20 户 synthetic 台账并跑完整报告"
    )
    demo_parser.add_argument("--out-dir", default=str(DEFAULT_DIR))
    demo_parser.set_defaults(func=cmd_demo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LedgerError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except LedgerBusinessError as error:
        print(f"校验失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())