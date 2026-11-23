"""G1 报名问卷筛选：H1–H4 硬门槛 + 配额 8/6/6 + 三档分流 6/7/7。

用法：
    python scripts/g1_screen.py --input var/g1/responses.csv
    python scripts/g1_screen.py --input 报名.csv --mapping var/g1/mapping.json
    python scripts/g1_screen.py --input 报名.csv --dry-run --limit 30

输出：
    var/g1/screened.csv   原列 + verdict/group/quota_slot/plan/reason
    var/g1/summary.json   总数/合格/待定/不合格/各组/各档/一线新一线

口径来源：
    docs/用户验证包-v0.1.md §1（H1–H4、排除项、配额 8/6/6、一线新一线 ≤12）
    docs/招募帖与问卷-v0.1.md §2（12 题、判定伪代码）
    docs/用户验证包-v0.1.md §5.1（A 6 / B 7 / C 7）

文档内部冲突的显式处理（不静默降门槛）：
    §1.1/§2 的 H3 要求「同住老人或儿童」，而 §1.3 的配额表又要求「纯危险设备组：
    无老人儿童，但 ≥2 个危险设备」。两者字面不可同时成立。本脚本默认把
    「Q6 仅选只有成年人 + 至少 1 个危险设备」视为纯危险设备组的 H3 等价来源，
    再按「不足 2 个危险设备 → 待定（备选池）」处理（与 §2 伪代码的执行顺序一致）。
    H1 / H2 / H4 不放宽。需要严格按 §1.1 字面口径时加 --strict-h3。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "var" / "g1" / "responses.csv"
DEFAULT_OUT_DIR = ROOT / "var" / "g1"

VERDICT_OK = "合格"
VERDICT_PENDING = "待定（备选池）"
VERDICT_REJECT = "不合格"

GROUPS = ("老人", "儿童", "纯危险设备")
GROUP_QUOTA = {"老人": 8, "儿童": 6, "纯危险设备": 6}
PLAN_TARGET = {"A": 6, "B": 7, "C": 7}
PLAN_PRICE = {"A": "¥99/年", "B": "¥199/年", "C": "¥299 买断"}
FIRST_TIER_LIMIT = 12
FIRST_TIER_CITIES = {"一线", "新一线"}
DANGER_DEVICES = ("门锁", "燃气", "总电", "大功率")
HA_FORMS = {"HA OS", "HA Green", "Supervised", "Docker"}
DURATION_OK = {"3–12 月", ">12 月"}
ELDER = "65+ 老人"
CHILD_LABELS = {"3–6 岁儿童", "6–12 岁儿童", "儿童（年龄未注明）"}
MEMBER_TARGETS = {ELDER, *CHILD_LABELS}
OUTPUT_COLUMNS = ("verdict", "group", "quota_slot", "plan", "reason")

PENDING_REASON_LABELS = {
    "group_quota": "组配额已满",
    "first_tier": "一线/新一线超限",
    "device_count": "危险设备不足 2 个",
}
REJECT_REASON_LABELS = {
    "excluded": "命中排除项",
    "h1": "H1 未过",
    "h2": "H2 未过",
    "h3": "H3 未过",
    "h4": "H4 未过",
}

QUESTION_HINTS: dict[str, tuple[str, ...]] = {
    "Q1": ("城市级别", "常住城市"),
    "Q2": ("用 home assistant 多久", "使用 home assistant", "ha 多久", "用了多久"),
    "Q3": ("安装形态", "部署形态", "安装方式"),
    "Q4": ("实体数", "可控实体", "设备数量"),
    "Q5": ("哪些设备已接入", "已接入 ha", "接入的设备"),
    "Q6": ("同住成员", "家庭成员", "同住"),
    "Q7": ("老人白天", "独自在家"),
    "Q8": ("孩子会自己用语音", "按开关", "孩子"),
    "Q9": ("近 3 个月", "近三个月", "发生过的异常"),
    "Q10": ("不敢让家人用语音", "怕出事", "担心"),
    "Q11": ("语音入口", "现在的语音"),
    "Q12": ("访谈与录音", "录音意愿", "录音"),
}

_SPECIAL_ALIASES = {
    "q12_contact": "contact",
    "contact": "contact",
    "联系方式": "contact",
    "微信": "contact",
    "手机号": "contact",
    "exclude": "exclude",
    "excluded": "exclude",
    "排除项": "exclude",
    "人工核对": "exclude",
}

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
_QNUM_RE = re.compile(r"^(?:q|第)?\s*0?(\d{1,2})(?:\s*题)?\s*[^0-9]?", re.IGNORECASE)
_MULTI_SEP = re.compile(r"[;|,]")


def normalize_text(value: object) -> str:
    """统一大小写、破折号与中文标点，供子串匹配使用（不改原始输出）。"""
    if value is None:
        return ""
    text = str(value).strip().lower().translate(_DASHES)
    return re.sub(r"\s+", " ", text)


def split_multi(value: object) -> list[str]:
    """多选拆分：兼容 ; ； | ｜ , ， 、 与换行。"""
    parts = [part.strip() for part in _MULTI_SEP.split(normalize_text(value))]
    return [part for part in parts if part]


def question_from_header(header: str) -> str | None:
    text = normalize_text(header)
    match = _QNUM_RE.match(text)
    if match:
        number = int(match.group(1))
        if 1 <= number <= 12:
            return f"Q{number}"
    for question, hints in QUESTION_HINTS.items():
        if any(hint in text for hint in hints):
            return question
    return None


def _canon_key(key: str) -> str | None:
    text = normalize_text(key)
    match = re.fullmatch(r"q?\s*(1[0-2]|[1-9])", text)
    if match:
        return f"Q{match.group(1)}"
    return _SPECIAL_ALIASES.get(text)


def _mapping_pairs(mapping: Mapping[str, str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for key, value in mapping.items():
        canon_key = _canon_key(str(key))
        if canon_key:
            pairs[canon_key] = str(value)
            continue
        canon_value = _canon_key(str(value))
        if canon_value:
            pairs[canon_value] = str(key)
            continue
        raise SystemExit(
            f"--mapping 无法识别：{key!r} → {value!r}；"
            "键应为 Q1..Q12 / contact / exclude（或把列名作为键、题号作为值）"
        )
    return pairs


def resolve_columns(
    headers: Sequence[str], mapping: Mapping[str, str] | None = None
) -> dict[str, str]:
    """把 Q1..Q12 映射到实际列名。支持 --mapping，也支持题号/中文题干模糊匹配。"""
    known = set(headers)
    columns: dict[str, str] = {}
    if mapping:
        for question, column in _mapping_pairs(mapping).items():
            if column not in known:
                raise SystemExit(
                    f"--mapping 指向的列不存在：{column!r}；实际表头：{list(headers)}"
                )
            columns[question] = column

    used = set(columns.values())
    for question in (f"Q{i}" for i in range(1, 13)):
        if question in columns:
            continue
        for header in headers:
            if header in used:
                continue
            if question_from_header(header) == question:
                columns[question] = header
                used.add(header)
                break

    if "contact" not in columns:
        q12 = columns.get("Q12")
        if q12 and any(key in normalize_text(q12) for key in ("联系方式", "微信", "手机")):
            columns["contact"] = q12
        else:
            for header in headers:
                if header in used:
                    continue
                text = normalize_text(header)
                if any(key in text for key in ("联系方式", "微信", "手机号", "联系电话")):
                    columns["contact"] = header
                    used.add(header)
                    break

    if "exclude" not in columns:
        for header in headers:
            if header in used:
                continue
            text = normalize_text(header)
            if any(key in text for key in ("排除项", "人工核对", "从业者")):
                columns["exclude"] = header
                break

    return columns


def parse_city(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    if "新一线" in text:
        return "新一线"
    if "一线" in text:
        return "一线"
    if "二线" in text:
        return "二线"
    if any(key in text for key in ("三线", "四线", "以下")):
        return "三线及以下"
    return None


def parse_ha_duration(value: object) -> tuple[str | None, str]:
    text = normalize_text(value).replace("至", "-")
    if not text:
        return None, "缺失"
    if any(key in text for key in ("记不清", "不确定", "忘了")):
        return None, "记不清"
    if re.search(r"<\s*1", text) or any(
        key in text for key in ("小于1", "小于 1", "不到1", "不到 1", "1个月以内", "1 个月以内")
    ):
        return "<1 月", "<1 月"
    if ("大于" in text or "超过" in text or ">" in text) and "12" in text:
        return ">12 月", ">12 月"
    if any(key in text for key in ("12个月以上", "12 个月以上", "一年以上")):
        return ">12 月", ">12 月"
    if "3-12" in text or ("三个月" in text and "一年" in text):
        return "3–12 月", "3–12 月"
    if "1-3" in text:
        return "1–3 月", "1–3 月"
    return None, f"无法识别（{value}）"


def parse_ha_form(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    if "green" in text:
        return "HA Green"
    if "haos" in text or "ha os" in text or "assistant os" in text:
        return "HA OS"
    if "supervised" in text or "监督" in text:
        return "Supervised"
    if "docker" in text or "容器" in text:
        return "Docker"
    if any(key in text for key in ("记不清", "不确定", "忘了")):
        return "记不清"
    if "其他" in text or "其它" in text:
        return "其他"
    return None


def parse_entity_count(value: object) -> tuple[int | None, str]:
    """返回实体数下界（用于 >=10 判定）与可读标签。"""
    text = normalize_text(value)
    if not text:
        return None, "缺失"
    if any(
        key in text
        for key in (">80", "大于80", "大于 80", "超过80", "超过 80", "80以上", "80 以上")
    ):
        return 81, ">80"
    match = re.search(r"(\d+)\s*-\s*(\d+)", text)
    if match:
        low = int(match.group(1))
        return low, f"{low}–{int(match.group(2))}"
    match = re.search(r"(\d+)", text)
    if match:
        number = int(match.group(1))
        return number, str(number)
    return None, f"无法识别（{value}）"


def parse_devices(value: object) -> tuple[set[str], bool]:
    """返回（危险/其他设备标签集合，是否选择都没有）。"""
    labels: set[str] = set()
    none_selected = False
    for part in split_multi(value):
        if "都没有" in part or part in {"无", "没有"}:
            none_selected = True
            continue
        if "燃气" in part:
            labels.add("燃气")
        elif any(key in part for key in ("总闸", "空开", "总电")):
            labels.add("总电")
        elif any(
            key in part
            for key in ("大功率", "热水器", "烘干机", "充电桩", "电暖", "烤箱")
        ):
            labels.add("大功率")
        elif "门锁" in part or "智能锁" in part:
            labels.add("门锁")
        elif "安防" in part or "报警器" in part:
            labels.add("安防")
        else:
            labels.add(part)
    return labels, none_selected


def parse_members(value: object) -> tuple[set[str], bool]:
    """返回（老人/儿童标签集合，是否仅选只有成年人）。"""
    labels: set[str] = set()
    adults_only = False
    for part in split_multi(value):
        if "成年人" in part:
            adults_only = True
            continue
        if "3-6" in part:
            labels.add("3–6 岁儿童")
        elif "6-12" in part:
            labels.add("6–12 岁儿童")
        elif "老人" in part or "65" in part:
            labels.add(ELDER)
        elif "儿童" in part or "孩子" in part:
            labels.add("儿童（年龄未注明）")
        else:
            labels.add(part)
    return labels, adults_only


def parse_abnormal(value: object) -> list[str]:
    incidents: list[str] = []
    for part in split_multi(value):
        if "都没有" in part or part in {"无", "没有", "none", "n/a"}:
            continue
        incidents.append(part)
    return incidents


def parse_level(value: object) -> int | None:
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"(?<!\d)([1-5])(?!\d)", text)
    return int(match.group(1)) if match else None


def parse_recording(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    if "笔记" in text:
        return "只接受笔记"
    if "不接受" in text and "接受录音" not in text:
        return "不接受"
    if "接受" in text or "可以" in text:
        return "接受录音"
    return None


def parse_yes(value: object) -> bool:
    return normalize_text(value) in {"是", "会", "yes", "y", "1", "有"}


_FALSE_MARKERS = {
    "",
    "无",
    "否",
    "n",
    "no",
    "0",
    "false",
    "-",
    "—",
    "none",
    "未命中",
    "未核对",
}


def parse_exclusion(value: object) -> bool:
    """人工核对的排除项列：空/无/否 视为未命中，其余文本视为命中。"""
    return normalize_text(value) not in _FALSE_MARKERS


@dataclass
class Answers:
    raw: dict[str, str]
    city: str | None = None
    duration: str | None = None
    duration_note: str = ""
    form: str | None = None
    entities: int | None = None
    entities_note: str = ""
    devices: set[str] = field(default_factory=set)
    devices_none: bool = False
    members: set[str] = field(default_factory=set)
    adults_only: bool = False
    abnormal: list[str] = field(default_factory=list)
    pain: int | None = None
    recording: str | None = None
    exclusion_hit: bool = False
    contact: str = ""
    elderly_alone: bool = False
    child_uses: bool = False


@dataclass
class ScreenResult:
    verdict: str
    group: str
    quota_slot: str
    plan: str
    reason: str
    reason_code: str = ""
    row_index: int = -1


@dataclass
class QualifiedRecord:
    row_index: int
    group: str
    city: str
    answers: Answers
    result: ScreenResult


def _cell(row: Mapping[str, str], columns: Mapping[str, str], key: str) -> str:
    column = columns.get(key)
    if not column:
        return ""
    return (row.get(column) or "").strip()


def build_answers(row: Mapping[str, str], columns: Mapping[str, str]) -> Answers:
    duration, duration_note = parse_ha_duration(_cell(row, columns, "Q2"))
    entities, entities_note = parse_entity_count(_cell(row, columns, "Q4"))
    devices, devices_none = parse_devices(_cell(row, columns, "Q5"))
    members, adults_only = parse_members(_cell(row, columns, "Q6"))
    return Answers(
        raw={column: (row.get(column) or "") for column in columns.values()},
        city=parse_city(_cell(row, columns, "Q1")),
        duration=duration,
        duration_note=duration_note,
        form=parse_ha_form(_cell(row, columns, "Q3")),
        entities=entities,
        entities_note=entities_note,
        devices=devices,
        devices_none=devices_none,
        members=members,
        adults_only=adults_only,
        abnormal=parse_abnormal(_cell(row, columns, "Q9")),
        pain=parse_level(_cell(row, columns, "Q10")),
        recording=parse_recording(_cell(row, columns, "Q12")),
        exclusion_hit=parse_exclusion(_cell(row, columns, "exclude")),
        contact=_cell(row, columns, "contact"),
        elderly_alone=parse_yes(_cell(row, columns, "Q7")),
        child_uses=parse_yes(_cell(row, columns, "Q8")),
    )


def danger_count(answers: Answers) -> int:
    return len(answers.devices & set(DANGER_DEVICES))


def infer_group(answers: Answers) -> str:
    if ELDER in answers.members:
        return "老人"
    if answers.members & CHILD_LABELS:
        return "儿童"
    if answers.adults_only:
        return "纯危险设备"
    return ""


def hard_gate_failures(
    answers: Answers, *, strict_h3: bool = False
) -> tuple[list[str], str]:
    """返回（失败原因列表，主要失败类别的 reason_code）。"""
    excluded: list[str] = []
    h1: list[str] = []
    h2: list[str] = []
    h3: list[str] = []
    h4: list[str] = []

    if answers.recording == "不接受":
        excluded.append("Q12 选择不接受录音")
    elif answers.recording is None:
        excluded.append("Q12 缺失或无法解析（必填项）")
    if answers.form == "记不清":
        excluded.append("Q3 选择记不清")
    if answers.exclusion_hit:
        excluded.append("人工核对命中排除项（从业者/亲友同事/非自住/已用同类产品）")
    if answers.devices_none and not answers.devices:
        excluded.append("Q5 仅选都没有")
    if strict_h3 and answers.adults_only and not (answers.members & MEMBER_TARGETS):
        excluded.append("Q6 仅选只有成年人（--strict-h3 下按排除项处理）")

    if answers.duration not in DURATION_OK:
        h1.append(f"Q2={answers.duration or answers.duration_note or '未填'}（需 ≥3 个月）")
    if answers.form not in HA_FORMS:
        h1.append(f"Q3={answers.form or '未填'}（需 HA OS/Green/Supervised/Docker）")
    if answers.entities is None or answers.entities < 10:
        h1.append(f"Q4={answers.entities_note or '未填'}（需 ≥10 个可控实体）")

    if not danger_count(answers):
        h2.append("Q5 未选门锁/燃气阀/总闸空开/大功率插座")

    has_elder_or_child = bool(answers.members & MEMBER_TARGETS)
    device_only_h3 = (
        not strict_h3 and answers.adults_only and danger_count(answers) >= 1
    )
    if not (has_elder_or_child or device_only_h3):
        h3.append(
            "Q6 既无老人/儿童，也不满足纯危险设备组"
            "（仅成年人 + 已接入危险设备）"
        )

    if not answers.abnormal and (answers.pain is None or answers.pain < 4):
        pain_text = answers.pain if answers.pain is not None else "未填"
        h4.append(f"Q9 无异常且 Q10={pain_text}（需 ≥4）")

    failures = list(excluded)
    if h1:
        failures.append("H1 未过：" + "；".join(h1))
    if h2:
        failures.append("H2 未过：" + "；".join(h2))
    if h3:
        failures.append("H3 未过：" + "；".join(h3))
    if h4:
        failures.append("H4 未过：" + "；".join(h4))

    if excluded:
        code = "excluded"
    elif h1:
        code = "h1"
    elif h2:
        code = "h2"
    elif h3:
        code = "h3"
    elif h4:
        code = "h4"
    else:
        code = "ok"
    return failures, code


def plan_targets(count: int) -> dict[str, int]:
    """合格户的三档目标：20 户为 6/7/7；不足 20 按比例（最大余数法）。"""
    if count <= 0:
        return {"A": 0, "B": 0, "C": 0}
    total = sum(PLAN_TARGET.values())
    raw = {key: count * value / total for key, value in PLAN_TARGET.items()}
    base = {key: int(value) for key, value in raw.items()}
    remaining = count - sum(base.values())
    ranked = sorted(
        raw, key=lambda key: (-(raw[key] - base[key]), "ABC".index(key))
    )
    for key in ranked[:remaining]:
        base[key] += 1
    return base


def allocate_plans(count: int) -> list[str]:
    """按报名顺序 A→B→C 轮转，满额跳到下一档；最终计数等于 plan_targets。"""
    targets = plan_targets(count)
    used = {"A": 0, "B": 0, "C": 0}
    order = ["A", "B", "C"]
    index = 0
    plans: list[str] = []
    for _ in range(count):
        assigned = None
        for step in range(3):
            candidate = order[(index + step) % 3]
            if used[candidate] < targets[candidate]:
                used[candidate] += 1
                index = (index + step + 1) % 3
                assigned = candidate
                break
        if assigned is None:  # pragma: no cover - targets 之和恒等于 count
            assigned = order[index]
            used[assigned] += 1
            index = (index + 1) % 3
        plans.append(assigned)
    return plans


@dataclass
class Screener:
    strict_h3: bool = False
    quotas: dict[str, int] = field(default_factory=lambda: dict(GROUP_QUOTA))
    first_tier_limit: int = FIRST_TIER_LIMIT
    group_used: Counter = field(default_factory=Counter)
    first_tier_used: int = 0
    qualified: list[QualifiedRecord] = field(default_factory=list)

    def decide(self, answers: Answers, row_index: int = -1) -> ScreenResult:
        failures, code = hard_gate_failures(answers, strict_h3=self.strict_h3)
        group = infer_group(answers)
        if failures:
            return ScreenResult(
                VERDICT_REJECT, group, "-", "", "不合格：" + "；".join(failures), code, row_index
            )

        danger = danger_count(answers)
        if group == "纯危险设备" and danger < 2:
            return ScreenResult(
                VERDICT_PENDING,
                group,
                "-",
                "",
                f"H1–H4 已通过；纯危险设备组危险设备仅 {danger} 个（需 ≥2），进入备选池",
                "device_count",
                row_index,
            )

        quota = self.quotas[group]
        if self.group_used[group] >= quota:
            return ScreenResult(
                VERDICT_PENDING,
                group,
                f"{group} {self.group_used[group]}/{quota}",
                "",
                f"H1–H4 已通过；{group}组配额已满（{self.group_used[group]}/{quota}），进入备选池",
                "group_quota",
                row_index,
            )

        if answers.city in FIRST_TIER_CITIES and self.first_tier_used >= self.first_tier_limit:
            return ScreenResult(
                VERDICT_PENDING,
                group,
                "-",
                "",
                f"H1–H4 已通过；一线/新一线已达上限"
                f"（{self.first_tier_used}/{self.first_tier_limit}），进入备选池",
                "first_tier",
                row_index,
            )

        self.group_used[group] += 1
        if answers.city in FIRST_TIER_CITIES:
            self.first_tier_used += 1
        result = ScreenResult(
            VERDICT_OK,
            group,
            f"{group} {self.group_used[group]}/{quota}",
            "",
            "",
            "ok",
            row_index,
        )
        self.qualified.append(QualifiedRecord(row_index, group, answers.city or "", answers, result))
        return result


def _assign_plans(qualified: Sequence[QualifiedRecord]) -> None:
    plans = allocate_plans(len(qualified))
    for record, plan in zip(qualified, plans):
        record.result.plan = plan
        record.result.reason = (
            f"H1–H4 全过；{record.result.quota_slot}；"
            f"城市 {record.city or '未填'}；分档 {plan}（{PLAN_PRICE[plan]}）"
        )


def _bonus_checks(qualified: Sequence[QualifiedRecord]) -> dict[str, dict]:
    elderly = [record for record in qualified if record.group == "老人"]
    children = [record for record in qualified if record.group == "儿童"]
    elderly_alone = sum(1 for record in elderly if record.answers.elderly_alone)
    elderly_incident = sum(
        1
        for record in elderly
        if any(
            key in incident
            for incident in record.answers.abnormal
            for key in ("误断总电", "误开门锁")
        )
    )
    young_children = sum(
        1 for record in children if "3–6 岁儿童" in record.answers.members
    )
    child_voice = sum(1 for record in children if record.answers.child_uses)
    return {
        "老人组_独处≥4h": {
            "actual": elderly_alone,
            "target": 4,
            "ok": elderly_alone >= 4,
        },
        "老人组_误断总电或误开门锁": {
            "actual": elderly_incident,
            "target": 3,
            "ok": elderly_incident >= 3,
        },
        "儿童组_≤6岁": {
            "actual": young_children,
            "target": 3,
            "ok": young_children >= 3,
        },
        "儿童组_会自己用语音或按开关（Q8 代理）": {
            "actual": child_voice,
            "target": 2,
            "ok": child_voice >= 2,
            "note": "问卷未直接询问『碰过危险开关/说过危险指令』，此处用 Q8=会 作代理，需访谈确认",
        },
    }


def _build_summary(
    answers_list: Sequence[Answers],
    results: Sequence[ScreenResult],
    screener: Screener,
    *,
    source: str = "",
) -> dict:
    qualified = sum(1 for result in results if result.verdict == VERDICT_OK)
    pending = sum(1 for result in results if result.verdict == VERDICT_PENDING)
    rejected = sum(1 for result in results if result.verdict == VERDICT_REJECT)

    by_group = {
        group: sum(
            1
            for result in results
            if result.verdict == VERDICT_OK and result.group == group
        )
        for group in GROUPS
    }
    by_plan = {
        plan: sum(
            1
            for result in results
            if result.verdict == VERDICT_OK and result.plan == plan
        )
        for plan in ("A", "B", "C")
    }

    pending_counter = Counter(
        result.reason_code for result in results if result.verdict == VERDICT_PENDING
    )
    reject_counter = Counter(
        result.reason_code for result in results if result.verdict == VERDICT_REJECT
    )
    pending_by_group = {
        group: sum(
            1
            for result in results
            if result.verdict == VERDICT_PENDING and result.group == group
        )
        for group in GROUPS
    }

    return {
        "source": source or None,
        "total": len(results),
        "qualified": qualified,
        "pending": pending,
        "rejected": rejected,
        "by_group": by_group,
        "group_quota": dict(GROUP_QUOTA),
        "by_plan": by_plan,
        "plan_target": plan_targets(qualified),
        "first_tier_count": screener.first_tier_used,
        "first_tier_limit": FIRST_TIER_LIMIT,
        "first_tier_total": sum(
            1 for answers in answers_list if answers.city in FIRST_TIER_CITIES
        ),
        "pending_by_group": pending_by_group,
        "pending_by_reason": {
            key: pending_counter[key] for key in PENDING_REASON_LABELS
        },
        "rejected_by_reason": {
            key: reject_counter[key] for key in REJECT_REASON_LABELS
        },
        "bonus_checks": _bonus_checks(screener.qualified),
        "policy_notes": [
            "H1–H4 与排除项依据 docs/招募帖与问卷-v0.1.md §2；"
            "配额与分流依据 docs/用户验证包-v0.1.md §1.3、§5.1。",
            "文档 §1.1/§2 要求 H3=同住老人或儿童，但 §1.3 配额含『纯危险设备组"
            "（无老人儿童）』；默认把『只有成年人 + 已接入危险设备』视为该组的 "
            "H3 等价来源，再按 ≥2 个危险设备判定（不足则待定）。"
            "--strict-h3 可恢复 §1.1 字面口径。",
            "H1/H2/H4 未放宽；待定户不自动转合格，也不占用合格配额。",
            "纯危险设备组的『本人是主要使用者』与儿童组『碰过危险开关/说过危险指令』"
            "问卷无直接题：前者以『仅成年人 + 已接入危险设备』近似，后者以 Q8=会 "
            "代理，均需访谈人工确认（见 bonus_checks.note）。",
        ],
    }


def read_survey_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
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


def screen_rows(
    headers: Sequence[str],
    rows: Sequence[dict[str, str]],
    *,
    mapping: Mapping[str, str] | None = None,
    strict_h3: bool = False,
    source: str = "",
) -> tuple[list[ScreenResult], dict]:
    """纯函数入口：测试与 CLI 共用。"""
    columns = resolve_columns(headers, mapping)
    answers_list = [build_answers(row, columns) for row in rows]
    screener = Screener(strict_h3=strict_h3)
    results = [
        screener.decide(answers, index)
        for index, answers in enumerate(answers_list)
    ]
    _assign_plans(screener.qualified)
    summary = _build_summary(answers_list, results, screener, source=source)
    return results, summary


def write_screened_csv(
    path: Path,
    headers: Sequence[str],
    rows: Sequence[dict[str, str]],
    results: Sequence[ScreenResult],
) -> None:
    output_headers = [
        header for header in headers if header not in OUTPUT_COLUMNS
    ] + list(OUTPUT_COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=output_headers, extrasaction="ignore"
        )
        writer.writeheader()
        for row, result in zip(rows, results):
            output = {
                header: row.get(header, "")
                for header in headers
                if header not in OUTPUT_COLUMNS
            }
            output.update(
                verdict=result.verdict,
                group=result.group,
                quota_slot=result.quota_slot,
                plan=result.plan,
                reason=result.reason,
            )
            writer.writerow(output)


def format_summary(
    summary: Mapping[str, object], *, dry_run: bool = False, out_dir: Path | None = None
) -> str:
    lines = [
        f"G1 筛选完成：共 {summary['total']} 份报名",
        f"  合格 {summary['qualified']} ｜ 待定（备选池）{summary['pending']}"
        f" ｜ 不合格 {summary['rejected']}",
        "  组别（合格）："
        + "、".join(
            f"{group} {summary['by_group'][group]}/{summary['group_quota'][group]}"
            for group in GROUPS
        ),
        "  三档（合格）："
        + "、".join(
            f"{plan} {summary['by_plan'][plan]}/{summary['plan_target'][plan]}"
            for plan in ("A", "B", "C")
        ),
        f"  一线/新一线：合格 {summary['first_tier_count']}"
        f"/{summary['first_tier_limit']}，报名中 {summary['first_tier_total']} 户",
        "  待定原因："
        + _format_reason_counts(summary["pending_by_reason"], PENDING_REASON_LABELS),
        "  不合格原因："
        + _format_reason_counts(summary["rejected_by_reason"], REJECT_REASON_LABELS),
    ]
    if dry_run:
        lines.append("  （dry-run：未写文件）")
    elif out_dir is not None:
        lines.append(f"  明细：{out_dir / 'screened.csv'}")
        lines.append(f"  摘要：{out_dir / 'summary.json'}")
    return "\n".join(lines)


def _format_reason_counts(
    counts: Mapping[str, int], labels: Mapping[str, str]
) -> str:
    parts = [
        f"{labels.get(key, key)}：{value} 户"
        for key, value in counts.items()
        if value
    ]
    return "、".join(parts) if parts else "无"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G1 报名问卷筛选（H1–H4 + 配额 8/6/6 + 三档 6/7/7）"
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="问卷导出 CSV")
    parser.add_argument(
        "--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录（默认 var/g1）"
    )
    parser.add_argument("--mapping", help="可选：列名映射 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只打印摘要，不写文件")
    parser.add_argument(
        "--limit", type=int, help="只处理前 N 份报名（按 CSV 顺序）"
    )
    parser.add_argument(
        "--strict-h3",
        action="store_true",
        help="严格按 §1.1 字面口径（H3 只认老人/儿童，关闭纯危险设备组）",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"找不到问卷 CSV：{input_path}")

    mapping = None
    if args.mapping:
        mapping_path = Path(args.mapping)
        if not mapping_path.is_file():
            raise SystemExit(f"找不到 --mapping 文件：{mapping_path}")
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"--mapping 不是合法 JSON：{error}") from error
        if not isinstance(mapping, dict):
            raise SystemExit("--mapping 顶层必须是对象：{\"Q1\": \"列名\", ...}")

    headers, rows = read_survey_csv(input_path)
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit 必须是正整数")
        rows = rows[: args.limit]

    results, summary = screen_rows(
        headers,
        rows,
        mapping=mapping,
        strict_h3=args.strict_h3,
        source=str(input_path),
    )

    out_dir = Path(args.out_dir)
    print(format_summary(summary, dry_run=args.dry_run, out_dir=out_dir))

    if args.dry_run:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    write_screened_csv(out_dir / "screened.csv", headers, rows, results)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())