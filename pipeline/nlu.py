"""F-006 intent -> tool calls.

Two providers, one contract:

* :class:`RuleParser` - deterministic, offline, no model. This is what the
  evaluation suite and the tests run against, so a reviewer gets a reproducible
  result without a GPU or an API key.
* :class:`LlmParser` - Ollama (or any loopback OpenAI-compatible endpoint) with a
  hard timeout and automatic degradation to :class:`RuleParser`. A parse failure
  must never become a dispatch failure.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hvsl.config import Settings
from hvsl.models import Intent, PlannedCall
from hvsl.tools import ToolRegistry, build_default_registry

_FILLERS = (
    "请", "帮我", "我要", "我想要", "给我", "麻烦", "能不能", "可以", "把", "将", "让", "一下", "现在",
    "立刻", "马上", "顺便", "这个", "那个", "我的", "的", "了", "吧", "吗", "呢", "啊", "哦",
)
_SPLITTERS = ("然后", "之后", "接着", "最后", "并且", "并", "再", "同时", "还要", "顺便")

_ALL_WORDS = ("都", "全部", "所有", "全开", "全关")

_DOMAIN_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("alarm_control_panel", ("安防", "报警", "布防", "撤防")),
    ("lock", ("门锁", "大门锁", "入户门", "大门", "锁")),
    ("cover", ("窗帘", "卷帘", "遮阳")),
    ("vacuum", ("扫地", "扫地机", "吸尘器", "机器人")),
    ("media_player", ("电视", "音响", "播放器", "音量", "声音")),
    ("climate", ("空调", "暖气机")),
    ("fan", ("风扇", "电扇", "净化器")),
    ("switch", ("插座", "开关", "电暖器", "取暖器", "热水壶", "水壶", "加湿器", "燃气", "电磁阀",
                "灶", "总电源", "总闸", "空开")),
    ("light", ("灯", "大灯", "主灯", "台灯", "灯带", "筒灯", "射灯")),
]

_OFF_WORDS = ("关闭", "关掉", "关了", "关", "熄灭", "停止", "别开", "退出", "断电", "拉闸", "切断")
_ON_WORDS = ("打开", "开一下", "开启", "开个", "开", "启动", "点亮", "上电", "合闸")

_HVAC_MODES = {"制冷": "cool", "制热": "heat", "送风": "fan_only", "除湿": "dry", "自动": "auto", "eco": "eco"}

_CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_MEASURE_WORDS = ("度", "℃", "%", "百分", "档", "挡", "级", "音量", "声音", "亮度", "位置")

# Relative-adjust vocabulary.  Each hit means "read the current state and move by
# one step"; these phrases must never be turned into a guessed absolute value.
RELATIVE_STEP_PCT = 10
_RELATIVE_UP_WORDS = ("调大", "调高", "调亮", "亮一点", "亮一些", "大声一点", "大一点", "高一点")
_RELATIVE_DOWN_WORDS = ("调小", "调低", "调暗", "暗一点", "暗一些", "小声一点", "小一点", "低一点")
#: Spoken softeners ("一点/一些") are not numeric arguments; stripping them keeps
#: "声音大一点" from being parsed as the concrete volume 1.
_RELATIVE_FILLERS = ("一点儿", "一点", "一些")


def chinese_number(text: str) -> int | None:
    """Convert 0-99 written in Chinese or digits; anything else returns None."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = 1 if not left else _CN_DIGITS.get(left, -1)
        ones = 0 if not right else _CN_DIGITS.get(right, -1)
        if tens < 0 or ones < 0:
            return None
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _extract_number(segment: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", segment)
    if match:
        return float(match.group(1))
    for candidate in re.findall(r"[零一二两三四五六七八九十]+", segment):
        value = chinese_number(candidate)
        if value is not None:
            return float(value)
    return None


def _correct_entities(text: str, names: list[str]) -> tuple[str, list[str]]:
    """catalog-aware 同音纠错（pypinyin 可选；未安装则原样返回）。"""
    if not text or not names:
        return text, []
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return text, []

    def syllables(value: str) -> list[str]:
        return [item.lower() for item in lazy_pinyin(value, style=Style.NORMAL)]

    candidates: list[tuple[int, int, int, str, str]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen or len(name) < 2:
            continue
        seen.add(name)
        target = syllables(name)
        if len(target) < 2:
            continue
        limit = 0  # 只接受同音替换，避免把“客厅的灯”消歧成“客厅大灯”
        for start in range(len(text) - len(name) + 1):
            piece = text[start : start + len(name)]
            if piece == name:
                continue
            source = syllables(piece)
            if len(source) != len(target):
                continue
            distance = sum(1 for a, b in zip(source, target) if a != b)
            if distance <= limit:
                candidates.append((distance, start, len(name), piece, name))

    candidates.sort()
    occupied = [False] * len(text)
    corrected = text
    notes: list[str] = []
    for distance, start, length, piece, name in candidates:
        if any(occupied[start : start + length]):
            continue
        corrected = corrected[:start] + name + corrected[start + length :]
        for index in range(start, start + length):
            occupied[index] = True
        notes.append(f"同音纠错：{piece}→{name}")
    return corrected, notes


class RuleParser:
    """Keyword/regex parser. Bounded vocabulary, predictable output."""

    name = "rule"

    def __init__(
        self, registry: ToolRegistry | None = None, catalog: list[dict[str, str]] | None = None
    ) -> None:
        self.registry = registry or build_default_registry()
        self.catalog = catalog or []

    def _catalog_names(self) -> list[str]:
        return [
            str(item.get("display_name") or "")
            for item in self.catalog
            if item.get("display_name")
        ]

    def _catalog_name_set(self) -> set[str]:
        return {
            str(item.get("display_name") or "")
            for item in self.catalog
            if item.get("display_name")
        }

    def parse(self, utterance: str) -> Intent:
        segments = self._split(utterance)
        steps: list[PlannedCall] = []
        notes: list[str] = []
        for segment in segments:
            step = self._parse_segment(segment, notes)
            if step is not None:
                steps.append(step)
        return Intent(
            utterance=utterance,
            name="device_command" if steps else "unparsed",
            steps=steps,
            provider=self.name,
            notes=notes,
        )

    @staticmethod
    def _split(utterance: str) -> list[str]:
        parts = re.split(r"[，,；;。、]", utterance)
        out: list[str] = []
        for part in parts:
            piece = part
            for splitter in _SPLITTERS:
                piece = piece.replace(splitter, "\x00")
            out.extend(p for p in piece.split("\x00") if p.strip())
        return [p.strip() for p in out if p.strip()] or [utterance.strip()]

    def _parse_segment(self, segment: str, notes: list[str]) -> PlannedCall | None:
        text = segment.strip()
        explicit = self._explicit_entity(text)
        domain = self._domain_of(text)
        power = self._power_of(text)
        relative = self._relative_of(text)
        # "一点/一些" is a softener, not a measurement: strip it before looking for
        # a concrete number so "声音大一点" cannot become volume=1.
        number_text = text
        for filler in _RELATIVE_FILLERS:
            number_text = number_text.replace(filler, "")
        number = _extract_number(number_text) if self._has_measure(number_text) else None
        target = self._target_phrase(text, domain)
        if target and target not in self._catalog_name_set():
            corrected, correction_notes = _correct_entities(target, self._catalog_names())
            if corrected != target:
                notes.extend(correction_notes)
                target = corrected
        wants_all = any(word in text for word in _ALL_WORDS)

        if domain is None and explicit is None:
            return None

        tool, args = self._select_tool(domain or "", power, number, relative, text, notes)
        if tool is None:
            return None
        return PlannedCall(
            tool=tool,
            target=explicit or target,
            all=wants_all and len(args) == 0,
            args=args,
        )

    @staticmethod
    def _has_measure(text: str) -> bool:
        return any(word in text for word in _MEASURE_WORDS)

    @staticmethod
    def _relative_of(text: str) -> int | None:
        """Map relative phrases onto one ±10 step; None means "not relative"."""
        if any(word in text for word in _RELATIVE_DOWN_WORDS):
            return -RELATIVE_STEP_PCT
        if any(word in text for word in _RELATIVE_UP_WORDS):
            return RELATIVE_STEP_PCT
        return None

    @staticmethod
    def _explicit_entity(text: str) -> str:
        match = re.search(r"\b([a-z_]+\.[a-z0-9_]+)\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _domain_of(text: str) -> str | None:
        for domain, keywords in _DOMAIN_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                return domain
        return None

    @staticmethod
    def _power_of(text: str) -> str | None:
        if any(word in text for word in _OFF_WORDS):
            return "off"
        if any(word in text for word in _ON_WORDS):
            return "on"
        return None

    @staticmethod
    def _target_phrase(text: str, domain: str | None) -> str:
        cleaned = text
        for word in _FILLERS:
            cleaned = cleaned.replace(word, "")
        for splitter in _SPLITTERS:
            cleaned = cleaned.replace(splitter, "")
        for group in (
            *_OFF_WORDS,
            *_ON_WORDS,
            *_RELATIVE_UP_WORDS,
            *_RELATIVE_DOWN_WORDS,
            *_RELATIVE_FILLERS,
            "调到",
            "设定",
            "设为",
            "设置",
        ):
            cleaned = cleaned.replace(group, "")
        for keyword in _HVAC_MODES:
            cleaned = cleaned.replace(keyword, "")
        cleaned = re.sub(r"\d+(?:\.\d+)?\s*(?:度|℃|%|°C|档|级)?", "", cleaned)
        cleaned = re.sub(r"[零一二两三四五六七八九十]+(?:度|档|级|百分之|成)", "", cleaned)
        cleaned = re.sub(r"[，,。！!？?、\s]", "", cleaned)
        if domain == "alarm_control_panel":
            for word in ("安防", "报警", "布防", "撤防", "主机", "报警系统", "解除", "外出", "在家", "出门", "系统"):
                cleaned = cleaned.replace(word, "")
        return cleaned.strip()

    def _select_tool(
        self,
        domain: str,
        power: str | None,
        number: float | None,
        relative: int | None,
        text: str,
        notes: list[str],
    ) -> tuple[str | None, dict[str, Any]]:
        """Map (device domain, action word, parameter) onto a namespaced tool."""

        if domain == "lock":
            if any(word in text for word in ("解锁", "开锁", "打开门锁", "开门锁", "把门打开", "开一下门")):
                return "hvs.lock.unlock", {}
            # 「关门锁」按上锁理解：歧义时一律取安全方向。
            if any(word in text for word in ("上锁", "锁门", "锁好", "关门锁", "锁住", "锁上", "反锁", "关好门")):
                return "hvs.lock.lock", {}
            return ("hvs.lock.unlock" if power == "on" else "hvs.lock.lock"), {}

        if domain == "alarm_control_panel":
            if any(word in text for word in ("撤防", "解除", "关闭报警", "停止报警")) or power == "off":
                return "hvs.alarm.disarm", {}
            if "在家" in text:
                return "hvs.alarm.arm_home", {}
            if any(word in text for word in ("布防", "开启报警", "启动报警")) or power == "on":
                return "hvs.alarm.arm_away", {}
            return None, {}

        if domain == "cover":
            if power == "off" or any(word in text for word in ("拉上", "合上", "关上")):
                return "hvs.cover.close", {}
            if number is not None and ("%" in text or "百分" in text or "位置" in text):
                return "hvs.cover.set_position", {"percentage": int(number)}
            return "hvs.cover.open", {}

        if domain == "vacuum":
            if any(word in text for word in ("回充", "回桩", "回家", "充电")):
                return "hvs.vacuum.return_home", {}
            if power == "off" or "停止" in text:
                return "hvs.vacuum.stop", {}
            return "hvs.vacuum.start", {}

        if domain == "media_player":
            # A concrete number always wins and keeps the existing absolute-set path.
            if number is not None:
                return "hvs.media_player.volume_set", {"volume_level": int(number)}
            # No number + a relative word ("声音大一点") -> real relative adjustment.
            if relative is not None:
                return "hvs.media_player.adjust_volume", {"delta_pct": relative}
            # Neither numeric nor relative: keep the historical on/off fallback
            # (e.g. "打开音响"), never guess a volume.
            return ("hvs.media_player.turn_on" if power == "on" else "hvs.media_player.turn_off"), {}

        if domain == "climate":
            for keyword, mode in _HVAC_MODES.items():
                if keyword in text:
                    return "hvs.climate.set_hvac_mode", {"mode": mode}
            if number is not None and any(word in text for word in ("度", "℃", "temperature")):
                return "hvs.climate.set_temperature", {"temperature_c": float(number)}
            if power == "off":
                return "hvs.climate.turn_off", {}
            if power == "on":
                return "hvs.climate.turn_on", {}
            return None, {}

        if domain == "fan":
            if number is not None:
                return "hvs.fan.set_percentage", {"percentage": int(min(100, max(0, number * 25)))}
            return ("hvs.fan.turn_on" if power == "on" else "hvs.fan.turn_off"), {}

        if domain == "switch":
            if power == "off":
                return "hvs.switch.turn_off", {}
            if power == "on":
                return "hvs.switch.turn_on", {}
            notes.append("没有识别到开/关动作，按不支持处理")
            return None, {}

        if domain == "light":
            if power == "off":
                return "hvs.light.turn_off", {}
            if number is not None:
                return "hvs.light.set_brightness", {"brightness_pct": int(number)}
            if relative is not None:
                return "hvs.light.adjust_brightness", {"delta_pct": relative}
            return "hvs.light.turn_on", {}

        return None, {}


class LlmSteps(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[dict[str, Any]] = Field(default_factory=list)


class LlmParser:
    """Local-LLM parser with schema-constrained output and rule fallback.

    F-006 (timeout + degradation) and F-010 (loopback-only unless the operator
    explicitly allows a remote endpoint) both land here.
    """

    name = "llm"

    def __init__(
        self,
        settings: Settings,
        *,
        registry: ToolRegistry | None = None,
        fallback: RuleParser | None = None,
        catalog: list[dict[str, str]] | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or build_default_registry()
        self.fallback = fallback or RuleParser(self.registry, catalog=catalog)
        self.catalog = catalog or []
        self._guard_endpoint(settings.ollama_base_url, settings.allow_cloud_llm)
        self.system_prompt = self._build_system_prompt()

    @staticmethod
    def _guard_endpoint(base_url: str, allow_cloud: bool) -> None:
        host = urllib.parse.urlparse(base_url).hostname or ""
        if host in {"127.0.0.1", "localhost", "::1", ""}:
            return
        if not allow_cloud:
            raise ValueError(
                f"NLU endpoint {base_url} is not loopback; set HVSL_ALLOW_CLOUD_LLM=true to opt in. "
                "Audio never leaves this machine; text only goes out when you opt in explicitly."
            )

    def _build_system_prompt(self) -> str:
        tools = [
            {"name": schema["name"], "input_schema": schema["input_schema"]}
            for schema in self.registry.json_schemas()
        ]
        entities = [{"entity_id": e["entity_id"], "display_name": e["display_name"]} for e in self.catalog]
        return (
            "你是智能家居指令解析器，只输出 JSON，不输出解释。\n"
            "输出格式：{\"steps\":[{\"tool\":\"<完整工具名>\",\"target\":\"<设备中文名>\","
            "\"all\":false,\"args\":{...}}]}\n"
            "约束：tool 必须来自下面的列表（必须带 hvs. 命名空间前缀）；target 用设备中文名或 entity_id；"
            "无法判断设备时 target 留空；不得编造工具或参数。\n"
            f"可用工具：{json.dumps(tools, ensure_ascii=False)}\n"
            f"受控设备：{json.dumps(entities, ensure_ascii=False)}"
        )

    def parse(self, utterance: str) -> Intent:
        try:
            payload = self._chat(utterance)
        except Exception as exc:  # any transport/model failure degrades, never crashes
            intent = self.fallback.parse(utterance)
            intent.degraded = True
            intent.notes.append(f"LLM 不可用，已降级到规则解析：{type(exc).__name__}: {exc}")
            intent.provider = f"{self.name}->{self.fallback.name}"
            return intent

        steps = self._normalise(payload)
        if not steps:
            intent = self.fallback.parse(utterance)
            intent.degraded = True
            intent.notes.append("LLM 输出无法通过 schema 校验，已降级到规则解析")
            intent.provider = f"{self.name}->{self.fallback.name}"
            return intent
        return Intent(
            utterance=utterance,
            name="device_command",
            steps=steps,
            provider=self.name,
            confidence=0.8,
        )

    def _chat(self, utterance: str) -> dict[str, Any]:
        url = self.settings.ollama_base_url.rstrip("/") + "/api/chat"
        body = {
            "model": self.settings.ollama_model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": utterance},
            ],
        }
        with httpx.Client(timeout=self.settings.nlu_timeout_seconds) as client:
            response = client.post(url, json=body)
            response.raise_for_status()
            content = response.json()["message"]["content"]
        return json.loads(content)

    def _normalise(self, payload: dict[str, Any]) -> list[PlannedCall]:
        try:
            wrapper = LlmSteps.model_validate({"steps": payload.get("steps", payload) if isinstance(payload, dict) else []})
        except ValidationError:
            return []
        out: list[PlannedCall] = []
        for raw in wrapper.steps:
            candidate = dict(raw)
            if "entity_ids" in candidate and "target" not in candidate:
                ids = candidate.pop("entity_ids")
                candidate["target"] = ids[0] if isinstance(ids, list) and ids else str(ids)
            candidate.setdefault("tool", "")
            candidate.setdefault("target", "")
            candidate.setdefault("all", False)
            candidate.setdefault("args", {})
            if not self.registry.has(candidate["tool"]):
                continue
            try:
                out.append(PlannedCall.model_validate(candidate))
            except ValidationError:
                continue
        return out
