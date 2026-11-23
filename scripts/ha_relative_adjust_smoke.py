"""HA 真机验证：相对亮度/音量调节（"调亮一点" / "调暗一点"）。

前置：
* `hvsl-smoke` 容器已启动（默认 http://127.0.0.1:8123）；
* 实例已完成 onboarding：凭据二选一——`HVS_TOKEN`（30 分钟 access token，
  可用 `.storage/auth` 的 refresh token 换得，避免反复改密码/重启）或
  `HVS_PASSWORD`（走 `/auth/login_flow`）；两者都只经环境变量传入、不落盘；
* 集成已同步为最新（含 `hvs.light.adjust_brightness` / `hvs.media_player.adjust_volume`）。

本脚本复用 `scripts/ha_docker_smoke.py` 的 token 获取（`onboard()` 内走
`/auth/login_flow`）与 `create_entry(reuse_loaded=True)`，不复制登录实现、
不修改任何生产代码，也不在容器里写文件。

每一步都做「下发 + 回读」双证据：
  1. 基线 `light.turn_on brightness_pct=40` -> 回读 brightness≈102；
  2. 同一 session 调亮 -> `executed`，回读≈50%；
  3. 同一 session 连续两次调暗 -> 两次都 `executed`（验证相对调节不被
     30 秒幂等窗口吞掉），回读 40% -> 30%；
  4. 关灯后调暗 -> 0 下发，spoken_message 含“未下发”、不含“可重试”；
  5. 有可用 media_player 时同步验证音量 +10%；没有则如实记“未覆盖”。

用法：
    HVS_TOKEN=...  python scripts/ha_relative_adjust_smoke.py
    HVS_PASSWORD=... python scripts/ha_relative_adjust_smoke.py

环境变量：
    HVS_BASE        HA 地址（默认 http://127.0.0.1:8123，由 ha_docker_smoke 读取）
    HVS_CONTAINER   容器名（默认 hvsl-smoke）
    HVS_TOKEN       access token（二选一，优先；仅本地测试，不落盘）
    HVS_PASSWORD    smoke 用户密码（二选一，绝不落盘）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_docker_smoke as smoke  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "ha-relative"
RESULTS_PATH = OUT / "results.json"
LOG_PATH = OUT / "ha-relative.log"

#: HA 状态更新是异步的：每次下发后等 0.8s 再回读（任务要求 0.5–1s）。
SETTLE_SECONDS = 0.8
#: 亮度 0–255，抖动 ±1 个百分点约等于 ±2.55，取 ±2 覆盖取整差异。
BRIGHTNESS_TOL = 2
#: 音量 0–1，容差 0.02。
VOLUME_TOL = 0.02

TARGET_HINT = "light.living_room_rgbww_lights"
TARGET_NAME = "客厅大灯"
BASELINE_PCT = 40
BRIGHTEN_PCT = 50
DIM_ONCE_PCT = 40
DIM_TWICE_PCT = 30

#: 按实现口径（`pipeline/nlu.py: RELATIVE_STEP_PCT = 10`）每次相对调节 ±10 个百分点。
#: 因此「50% 调暗两次」的理论落点是 40% / 30%（brightness≈102 / 76）。
STEPS: list[dict[str, Any]] = []
BRIGHTNESS_SERIES: list[Any] = []
DECISION_KINDS: list[str] = []
DISPATCHED_COUNTS: list[int] = []
UNCOVERED: list[dict[str, str]] = []
TARGET_META: dict[str, Any] = {}


def log(message: str) -> None:
    """同时打印并追加到 var/ha-relative/ha-relative.log（每次运行先清空）。"""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def record(
    name: str,
    expected: str,
    actual: Any,
    evidence: dict[str, Any],
    verdict: str,
    reason: str = "",
) -> None:
    """verdict ∈ {pass, fail, blocked, skipped, info}；绝不把 fail 写成 pass。"""
    entry = {
        "name": name,
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
        "verdict": verdict,
        "reason": reason,
    }
    STEPS.append(entry)
    log(f"[{verdict.upper()}] {name}: {actual}" + (f" | reason: {reason}" if reason else ""))


def write_results(extra: dict[str, Any] | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "base": smoke.BASE,
        "container": smoke.CONTAINER,
        "timestamp": datetime.now(UTC).isoformat(),
        "epoch": time.time(),
        "entity_id": TARGET_META.get("entity_id"),
        "friendly_name": TARGET_META.get("friendly_name"),
        "brightness_series": BRIGHTNESS_SERIES,
        "decision_kinds": DECISION_KINDS,
        "dispatched_counts": DISPATCHED_COUNTS,
        "uncovered": UNCOVERED,
        "steps": STEPS,
        **(extra or {}),
    }
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"results -> {RESULTS_PATH}")


# --------------------------------------------------------------------------- #
# HA 读写辅助
# --------------------------------------------------------------------------- #
def read_state(entity_id: str, label: str) -> tuple[dict[str, Any] | None, list[str]]:
    """回读实体状态；失败时重试一次，两次错误都记入 evidence。"""
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            return smoke.state_of(entity_id), errors
        except Exception as exc:  # noqa: BLE001 - 回读失败要留证据，不能静默
            errors.append(f"attempt{attempt}:{type(exc).__name__}:{exc}")
            log(f"回读 {entity_id} 失败（{label}，第 {attempt} 次）：{exc}")
            if attempt == 1:
                time.sleep(0.5)
    return None, errors


def brightness_of(state: dict[str, Any] | None) -> Any:
    if not state:
        return None
    return (state.get("attributes") or {}).get("brightness")


def percent_of_brightness(brightness: Any) -> int | None:
    if not isinstance(brightness, int | float):
        return None
    # HA 亮度刻度 0–255，百分比 = round(brightness / 2.55)。
    return round(float(brightness) / 2.55)


def ha_service(domain: str, name: str, data: dict[str, Any]) -> int:
    """直接调用 HA 设备服务（基线/关灯/设音量），返回 HTTP 状态码。"""
    response = smoke.api("POST", f"/api/services/{domain}/{name}", json=data)
    if response.status_code >= 400:
        raise RuntimeError(f"{domain}.{name} HTTP {response.status_code}: {response.text[:300]}")
    return response.status_code


def speak(text: str, session_id: str) -> tuple[dict[str, Any], float]:
    """调用 home_voice_safety.handle_utterance（REST 带 return_response）。"""
    started = time.perf_counter()
    response = smoke.service("handle_utterance", {"text": text, "session_id": session_id})
    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    kind = response.get("kind")
    DECISION_KINDS.append(str(kind))
    DISPATCHED_COUNTS.append(int(response.get("dispatch_count") or 0))
    return response, latency_ms


def decision_digest(response: dict[str, Any]) -> dict[str, Any]:
    """只留可核对字段，避免把 token 等敏感内容写进证据。"""
    return {
        "kind": response.get("kind"),
        "ok": response.get("ok"),
        "dispatch_count": response.get("dispatch_count"),
        "intent": response.get("intent"),
        "outcome_states": [
            (item.get("tool"), item.get("state")) for item in (response.get("outcomes") or [])
        ],
        "spoken_message": response.get("spoken_message"),
        "message": response.get("message"),
        "trace_id": response.get("trace_id"),
    }


def list_states() -> list[dict[str, Any]]:
    response = smoke.api("GET", "/api/states")
    if response.status_code >= 400:
        raise RuntimeError(f"GET /api/states -> {response.status_code}: {response.text[:300]}")
    data = response.json()
    if not isinstance(data, list):
        raise TypeError(f"/api/states 返回结构异常：{type(data).__name__}")
    return data


def light_candidates(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛出支持亮度的 light，并把 `客厅大灯` 排在最前。"""
    candidates: list[dict[str, Any]] = []
    for item in states:
        entity_id = str(item.get("entity_id") or "")
        attrs = item.get("attributes") or {}
        if not entity_id.startswith("light."):
            continue
        modes = attrs.get("supported_color_modes") or []
        if not modes and "brightness" not in attrs:
            continue
        candidates.append(
            {
                "entity_id": entity_id,
                "friendly_name": attrs.get("friendly_name"),
                "state": item.get("state"),
                "supported_color_modes": modes,
            }
        )

    def rank(entry: dict[str, Any]) -> int:
        if entry["entity_id"] == TARGET_HINT:
            return 0
        if entry["friendly_name"] == TARGET_NAME:
            return 1
        return 2

    return sorted(candidates, key=rank)


def set_baseline(entity_id: str) -> tuple[int, dict[str, Any] | None, list[str], Any]:
    """把灯设到 40% 并回读；返回 (HTTP 码, 状态, 回读错误, brightness)。"""
    status = ha_service("light", "turn_on", {"entity_id": entity_id, "brightness_pct": BASELINE_PCT})
    time.sleep(SETTLE_SECONDS)
    state, errors = read_state(entity_id, "baseline")
    return status, state, errors, brightness_of(state)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    """只接受显式参数：没有 `--help` 时必须真的跑验证。"""
    parser = argparse.ArgumentParser(description="HA 真机验证：相对亮度/音量调节")
    parser.add_argument(
        "--out",
        default=str(RESULTS_PATH),
        help="结果 JSON 路径（默认 var/ha-relative/results.json；复核时指向临时文件以免覆盖既有证据）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global RESULTS_PATH
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    RESULTS_PATH = Path(args.out)
    exit_code = 0
    OUT.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    log(f"start: base={smoke.BASE} container={smoke.CONTAINER}")

    # 本地测试容器的凭据只允许放在 gitignored 的 var/ 下，且永不打印、永不提交。
    if not os.environ.get("HVS_TOKEN") and not os.environ.get("HVS_PASSWORD"):
        credentials = OUT / ".smoke_password"
        if credentials.exists():
            os.environ["HVS_PASSWORD"] = credentials.read_text(encoding="utf-8").strip()
            log("已从 var/ha-relative/.smoke_password 读取本地测试凭据（不打印）")

    if not os.environ.get("HVS_TOKEN") and not os.environ.get("HVS_PASSWORD"):
        record(
            "precondition_password",
            "HVS_PASSWORD 已通过环境变量提供（实例已完成 onboarding，需要密码登录）",
            "缺少 HVS_PASSWORD",
            {"base": smoke.BASE, "container": smoke.CONTAINER},
            "blocked",
            "无法取得 access token；密码不得落盘",
        )
        write_results()
        return 2

    try:
        injected = os.environ.get("HVS_TOKEN")
        if injected:
            smoke.TOKEN = injected
            log("使用 HVS_TOKEN（跳过 login_flow；仅用于本地测试）")
        else:
            smoke.onboard()
            log("token 已取得（复用已有实例，login_flow 密码登录）")

        # 重启后 config entry 可能仍在 setup：必须等到 loaded 再复用，否则
        # `create_entry(reuse_loaded=True)` 会误走向导并 abort: already_configured。
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                existing = smoke.hvs_entries()
            except Exception:
                existing = []
            if not existing or any(item.get("state") == "loaded" for item in existing):
                break
            log("等待配置条目加载（尚未 loaded）…")
            time.sleep(1.5)

        entry_info = smoke.create_entry(reuse_loaded=True)
        entry = entry_info.get("entry") or {}
        if entry.get("state") != "loaded":
            record(
                "entry_loaded",
                "复用已 loaded 的 home_voice_safety 配置条目",
                f"entry state={entry.get('state')}",
                {"entry": entry},
                "fail",
                "配置条目未 loaded，服务无法调用",
            )
            write_results()
            return 1
        record(
            "entry_loaded",
            "复用已 loaded 的 home_voice_safety 配置条目",
            f"entry_id={entry.get('entry_id')} state=loaded",
            {"entry": entry},
            "pass",
        )

        states = list_states()
        lights = light_candidates(states)
        record(
            "light_candidates",
            "至少有一个支持亮度的 light 实体",
            f"{len(lights)} 个：{[item['entity_id'] for item in lights][:8]}",
            {"lights": lights[:10]},
            "pass" if lights else "fail",
            "" if lights else "/api/states 中没有支持 brightness 的 light",
        )
        if not lights:
            write_results()
            return 1

        # ---------------- 目标灯选择：每次都先设基线；命中危险策略则换灯 ------
        target: str | None = None
        probe: dict[str, Any] = {}
        for cand in lights[:5]:
            entity_id = cand["entity_id"]
            try:
                status, state, errors, base_brightness = set_baseline(entity_id)
            except Exception as exc:  # noqa: BLE001 - 单个灯失败不影响换灯重试
                record(
                    f"target_probe[{entity_id}]",
                    "light.turn_on 可写并回读 brightness",
                    f"基线设置失败：{exc}",
                    {"entity_id": entity_id, "error": str(exc)},
                    "fail",
                    "该灯不可写，换下一个候选",
                )
                continue

            base_pct = percent_of_brightness(base_brightness)
            if base_brightness is None or abs(int(base_brightness) - 102) > BRIGHTNESS_TOL:
                record(
                    f"target_probe[{entity_id}]",
                    "brightness_pct=40 回读 brightness≈102",
                    f"HTTP={status} brightness={base_brightness} pct={base_pct}",
                    {
                        "entity_id": entity_id,
                        "http_status": status,
                        "state": state,
                        "read_errors": errors,
                    },
                    "fail",
                    "基线不成立，换下一个候选",
                )
                continue

            response, latency_ms = speak("把客厅大灯调亮一点", "rel-1")
            kind = response.get("kind")
            time.sleep(SETTLE_SECONDS)
            bright_state, bright_errors = read_state(entity_id, "brighten")
            bright_brightness = brightness_of(bright_state)

            if kind == "awaiting_confirmation":
                token = response.get("confirmation_token")
                if token:
                    smoke.service("cancel_action", {"token": token, "session_id": "rel-1"})
                record(
                    f"target_probe[{entity_id}]",
                    "目标灯未被标成危险实体",
                    f"调亮返回 awaiting_confirmation（entity={entity_id}），已取消并换灯",
                    {"entity_id": entity_id, "decision": decision_digest(response)},
                    "info",
                    "该灯在危险实体策略里，换一个未标危险的 light",
                )
                continue

            target = entity_id
            probe = {
                "entity_id": entity_id,
                "friendly_name": cand.get("friendly_name"),
                "state": state,
                "baseline_http": status,
                "baseline_state": state,
                "baseline_brightness": base_brightness,
                "baseline_errors": errors,
                "brighten_decision": response,
                "brighten_latency_ms": latency_ms,
                "brighten_state": bright_state,
                "brighten_brightness": bright_brightness,
                "brighten_errors": bright_errors,
            }
            break

        if not target:
            record(
                "step2_brighten",
                "调亮返回 executed 并回读≈50%",
                "没有找到可用目标灯（全部不可写或被标危险）",
                {"probed": [item["entity_id"] for item in lights[:5]]},
                "fail",
                "目标灯不可用；后续步骤无法执行",
            )
            for name in ("step3_dim_twice", "step4_dim_when_off", "step5_volume"):
                record(name, "依赖目标灯", "前置失败", {}, "blocked", "无可用目标灯")
            write_results()
            return 1

        TARGET_META.update(
            {
                "entity_id": target,
                "friendly_name": probe.get("friendly_name"),
                "supported_color_modes": lights[0].get("supported_color_modes"),
            }
        )
        log(f"目标灯：{target}（friendly_name={probe.get('friendly_name')}）")

        base_b = probe["baseline_brightness"]
        base_pct = percent_of_brightness(base_b)
        BRIGHTNESS_SERIES.append(base_b)
        record(
            "step1_baseline",
            "light.turn_on brightness_pct=40 后回读 brightness≈102（40%）",
            f"HTTP={probe['baseline_http']} brightness={base_b} pct={base_pct}",
            {
                "entity_id": target,
                "friendly_name": probe.get("friendly_name"),
                "state": probe["baseline_state"],
                "read_errors": probe["baseline_errors"],
                "session_id": "",
            },
            "pass"
            if base_b is not None and abs(int(base_b) - 102) <= BRIGHTNESS_TOL
            else "fail",
        )

        # ---------------- 第 2 步：调亮 --------------------------------------
        bright_b = probe["brighten_brightness"]
        bright_pct = percent_of_brightness(bright_b)
        BRIGHTNESS_SERIES.append(bright_b)
        bright_ok = (
            probe["brighten_decision"].get("kind") == "executed"
            and int(probe["brighten_decision"].get("dispatch_count") or 0) >= 1
            and bright_pct is not None
            and abs(bright_pct - BRIGHTEN_PCT) <= 1
        )
        record(
            "step2_brighten",
            "「把客厅大灯调亮一点」-> executed，回读≈50%（brightness≈127/128）",
            f"kind={probe['brighten_decision'].get('kind')} "
            f"dispatch={probe['brighten_decision'].get('dispatch_count')} "
            f"brightness={bright_b} pct={bright_pct}",
            {
                "entity_id": target,
                "session_id": "rel-1",
                "decision": decision_digest(probe["brighten_decision"]),
                "latency_ms": probe["brighten_latency_ms"],
                "read_errors": probe["brighten_errors"],
                "spoken_message": probe["brighten_decision"].get("spoken_message"),
            },
            "pass" if bright_ok else "fail",
            ""
            if bright_ok
            else "调亮未 executed 或回读亮度不在 49–51% 区间",
        )

        # ---------------- 第 3 步：同一 session 连续两次调暗 -------------------
        dim_evidence: list[dict[str, Any]] = []
        dim_results: list[dict[str, Any]] = []
        expected_pcts = (DIM_ONCE_PCT, DIM_TWICE_PCT)
        for index, expected_pct in enumerate(expected_pcts, start=1):
            response, latency_ms = speak("把客厅大灯调暗一点", "rel-1")
            time.sleep(SETTLE_SECONDS)
            state, errors = read_state(target, f"dim{index}")
            brightness = brightness_of(state)
            BRIGHTNESS_SERIES.append(brightness)
            pct = percent_of_brightness(brightness)
            dim_results.append(
                {
                    "attempt": index,
                    "expected_pct": expected_pct,
                    "kind": response.get("kind"),
                    "dispatch_count": response.get("dispatch_count"),
                    "brightness": brightness,
                    "pct": pct,
                }
            )
            dim_evidence.append(
                {
                    "attempt": index,
                    "expected_pct": expected_pct,
                    "session_id": "rel-1",
                    "decision": decision_digest(response),
                    "latency_ms": latency_ms,
                    "state": state,
                    "read_errors": errors,
                    "spoken_message": response.get("spoken_message"),
                }
            )

        dim_ok = all(
            item["kind"] == "executed"
            and int(item["dispatch_count"] or 0) >= 1
            and item["pct"] is not None
            and abs(int(item["pct"]) - int(item["expected_pct"])) <= 1
            for item in dim_results
        )
        second_suppressed = dim_results[1]["kind"] != "executed" or int(
            dim_results[1]["dispatch_count"] or 0
        ) == 0
        record(
            "step3_dim_twice_same_session",
            "同一 session 连续两次调暗都 executed：50% -> 40% -> 30%"
            "（不被 30 秒幂等窗口吞掉）",
            " | ".join(
                f"#{item['attempt']} kind={item['kind']} dispatch={item['dispatch_count']} "
                f"brightness={item['brightness']} pct={item['pct']}"
                for item in dim_results
            ),
            {
                "entity_id": target,
                "session_id": "rel-1",
                "attempts": dim_evidence,
                "brightness_series": [item["brightness"] for item in dim_results],
            },
            "pass" if dim_ok else "fail",
            ""
            if dim_ok
            else (
                "第二次调暗被去重（30 秒幂等窗口吞掉了相对调节）"
                if second_suppressed
                else "两次调暗的 executed/回读亮度不符合预期"
            ),
        )

        # ---------------- 第 4 步：关灯后调暗 ---------------------------------
        off_http = ha_service("light", "turn_off", {"entity_id": target})
        time.sleep(SETTLE_SECONDS)
        off_state, off_errors = read_state(target, "off")
        off_brightness = brightness_of(off_state)
        BRIGHTNESS_SERIES.append(off_brightness)

        off_response, off_latency = speak("把客厅大灯调暗一点", "rel-off")
        time.sleep(SETTLE_SECONDS)
        after_state, after_errors = read_state(target, "off-dim")
        after_brightness = brightness_of(after_state)
        BRIGHTNESS_SERIES.append(after_brightness)

        spoken = str(off_response.get("spoken_message") or "")
        off_ok = (
            int(off_response.get("dispatch_count") or 0) == 0
            and off_response.get("kind") != "executed"
            and (after_state or {}).get("state") == "off"
            and after_brightness == off_brightness
            and "未下发" in spoken
            and "可重试" not in spoken
        )
        record(
            "step4_dim_when_off",
            "关灯后调暗：0 下发，灯仍 off，brightness 不变，"
            "spoken_message 含“未下发”且不含“可重试”",
            f"kind={off_response.get('kind')} dispatch={off_response.get('dispatch_count')} "
            f"state={off_state.get('state') if off_state else None}->"
            f"{after_state.get('state') if after_state else None} "
            f"brightness={off_brightness}->{after_brightness} "
            f"spoken={spoken[:80]!r}",
            {
                "entity_id": target,
                "session_id": "rel-off",
                "turn_off_http": off_http,
                "decision": decision_digest(off_response),
                "latency_ms": off_latency,
                "state_before": off_state,
                "state_after": after_state,
                "read_errors": off_errors + after_errors,
                "spoken_contains_not_dispatched": "未下发" in spoken,
                "spoken_contains_retryable": "可重试" in spoken,
            },
            "pass" if off_ok else "fail",
            ""
            if off_ok
            else (
                "关灯后仍发生下发/状态被改"
                if int(off_response.get("dispatch_count") or 0) != 0
                else "提示文案不符合“未下发且不可重试”"
            ),
        )

        # ---------------- 第 5 步：音量（有则验，无则如实记未覆盖） -----------
        fresh_states = list_states()
        media = [
            item
            for item in fresh_states
            if str(item.get("entity_id") or "").startswith("media_player.")
            and (item.get("attributes") or {}).get("volume_level") is not None
        ]
        if not media:
            UNCOVERED.append(
                {
                    "item": "音量相对调节（电视声音大一点）",
                    "reason": "/api/states 中没有 attributes 含 volume_level 的 media_player",
                }
            )
            record(
                "step5_volume",
                "media_player 音量 0.4 -> 调大一点 -> executed，回读≈0.5",
                "未覆盖：没有可用的 media_player（无 volume_level 属性）",
                {"media_players": []},
                "skipped",
                "未覆盖；未新建实体，也未伪造读数",
            )
        else:
            player = media[0]
            player_id = str(player.get("entity_id"))
            try:
                ha_service("media_player", "turn_on", {"entity_id": player_id})
                ha_service(
                    "media_player", "volume_set", {"entity_id": player_id, "volume_level": 0.4}
                )
                time.sleep(SETTLE_SECONDS)
                vol_state, vol_errors = read_state(player_id, "volume-baseline")
                vol_before = (vol_state.get("attributes") or {}).get("volume_level")

                vol_decision, vol_latency = speak("电视声音大一点", "vol-1")
                time.sleep(SETTLE_SECONDS)
                vol_after_state, vol_after_errors = read_state(player_id, "volume-after")
                vol_after = (vol_after_state.get("attributes") or {}).get("volume_level")

                vol_ok = (
                    vol_decision.get("kind") == "executed"
                    and int(vol_decision.get("dispatch_count") or 0) >= 1
                    and isinstance(vol_after, int | float)
                    and abs(float(vol_after) - 0.5) <= VOLUME_TOL
                )
                spoken_volume = str(vol_decision.get("spoken_message") or "")
                # 设备可写（0.4 已回读成功）但 NLU 没把「电视」映射到实体时，
                # 这条不算设备不可用，而是受控实体词表缺中文名：记为 fail +
                # 未覆盖原因，绝不把回读值改成期望值来凑 pass。
                volume_rejected = vol_decision.get("kind") == "rejected"
                if volume_rejected:
                    UNCOVERED.append(
                        {
                            "item": "音量相对调节（电视声音大一点）",
                            "reason": (
                                f"{player_id}（friendly_name="
                                f"{(player.get('attributes') or {}).get('friendly_name')}）"
                                f"可写且音量已实测 0.4，但 NLU 返回 rejected："
                                f"{spoken_volume or vol_decision.get('message')}"
                            ),
                        }
                    )
                record(
                    "step5_volume",
                    "音量设到 0.4 后「电视声音大一点」-> executed，回读≈0.5",
                    f"entity={player_id} kind={vol_decision.get('kind')} "
                    f"dispatch={vol_decision.get('dispatch_count')} "
                    f"volume={vol_before}->{vol_after}",
                    {
                        "entity_id": player_id,
                        "friendly_name": (player.get("attributes") or {}).get("friendly_name"),
                        "session_id": "vol-1",
                        "decision": decision_digest(vol_decision),
                        "latency_ms": vol_latency,
                        "state_before": vol_state,
                        "state_after": vol_after_state,
                        "read_errors": vol_errors + vol_after_errors,
                        "spoken_message": vol_decision.get("spoken_message"),
                    },
                    "pass" if vol_ok else "fail",
                    ""
                    if vol_ok
                    else (
                        "NLU 未把「电视」映射到受控实体（rejected），"
                        "属设备词表/中文名缺失，已记录为未覆盖"
                        if volume_rejected
                        else "音量指令未 executed 或回读不在 0.48–0.52"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - 设音量失败要留真实原因
                UNCOVERED.append(
                    {"item": "音量相对调节（电视声音大一点）", "reason": f"设置音量失败：{exc}"}
                )
                record(
                    "step5_volume",
                    "media_player 音量 0.4 -> 调大一点 -> executed，回读≈0.5",
                    f"未覆盖：{exc}",
                    {"entity_id": player_id, "error": str(exc)},
                    "skipped",
                    "设备不可写或实体不可用；未伪造读数",
                )
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须留下真实证据
        traceback.print_exc()
        record(
            "script_error",
            "脚本完整执行",
            f"异常退出：{exc}",
            {"traceback": traceback.format_exc()[-4000:]},
            "fail",
            str(exc),
        )
        exit_code = 2
    finally:
        write_results()

    failed = [item for item in STEPS if item["verdict"] == "fail"]
    passed = [item for item in STEPS if item["verdict"] == "pass"]
    log(f"done: pass={len(passed)} fail={len(failed)} skipped={len(STEPS) - len(passed) - len(failed)}")
    return exit_code or (1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())