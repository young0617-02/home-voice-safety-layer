"""Home Voice Safety Layer 真机冒烟脚本（Docker + Home Assistant）。

前置：容器已由外部启动（`docker run -d --name hvsl-smoke -p 8123:8123 ...`），
本脚本只负责等待 HA 就绪、onboarding、创建 config entry，并执行 9 步冒烟。

设计原则：
* 每一步都记录「预期 / 实际 / 证据 / 判定 / 失败原因」，写入 results.json；
* 某一步失败不静默继续冒充通过，后续步骤会因缺少前置条件而标记为 blocked；
* 产品代码只读不改；所有证据来自真实 HTTP 响应、容器内文件与 docker 日志。

用法：
    python scripts/ha_docker_smoke.py
环境变量：
    HVS_BASE=https://...       HA 地址（默认 http://127.0.0.1:8123）
    HVS_CONTAINER=hvsl-smoke   容器名
    HVS_PASSWORD=...           固定 smoke 用户密码（默认随机）
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any

import requests

BASE = os.environ.get("HVS_BASE", "http://127.0.0.1:8123").rstrip("/")
CONTAINER = os.environ.get("HVS_CONTAINER", "hvsl-smoke")
ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = ROOT / "var" / "ha-smoke"
CONFIG_DIR = SMOKE_DIR / "config"
RESULTS_PATH = SMOKE_DIR / "results.json"
CLIENT_ID = f"{BASE}/"
USERNAME = os.environ.get("HVS_USERNAME", "smoke")
PASSWORD = os.environ.get("HVS_PASSWORD") or secrets.token_urlsafe(18)
SESSION_ID = "smoke"
SESSION_ID_2 = "smoke-restart"
CONFIRM_TTL_SECONDS = 600.0  # 给「重启恢复」留足窗口（默认 120s 对 docker restart 太紧）
DEDUP_WINDOW_SECONDS = 30.0

STEPS: list[dict[str, Any]] = []
TOKEN: str | None = None


# --------------------------------------------------------------------------- #
# 证据记录
# --------------------------------------------------------------------------- #
def record(
    step: str,
    expected: str,
    actual: Any,
    evidence: Any,
    verdict: str,
    reason: str = "",
) -> None:
    """verdict ∈ {pass, fail, blocked, info}。绝不允许把 fail 写成 pass。"""
    entry = {
        "step": step,
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
        "verdict": verdict,
        "reason": reason,
    }
    STEPS.append(entry)
    icon = {"pass": "PASS", "fail": "FAIL", "blocked": "BLOCKED", "info": "INFO"}[verdict]
    print(f"[{icon}] {step}: {actual}")
    if reason:
        print(f"        reason: {reason}")


def save_results() -> None:
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "base": BASE,
                "container": CONTAINER,
                "ts": time.time(),
                "token_obtained": bool(TOKEN),
                "steps": STEPS,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nresults -> {RESULTS_PATH}")


def docker(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def docker_logs(tail: int = 400, since: str | None = None) -> str:
    args = ["logs", CONTAINER]
    if since:
        args += ["--since", since]
    args += ["--tail", str(tail)]
    return docker(*args).stdout + docker(*args).stderr


def docker_exec_python(code: str, env: dict[str, str] | None = None) -> str:
    """在容器内跑一段 Python（断网步骤里主机无法访问端口映射时用）。"""
    args = ["exec"]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args += [CONTAINER, "python3", "-c", code]
    result = docker(*args, timeout=60)
    return (result.stdout or "") + (result.stderr or "")


# --------------------------------------------------------------------------- #
# HTTP 基础
# --------------------------------------------------------------------------- #
def api(method: str, path: str, *, auth: bool = True, **kwargs) -> requests.Response:
    headers = dict(kwargs.pop("headers", {}))
    if auth:
        if not TOKEN:
            raise RuntimeError("还没有 access_token")
        headers["Authorization"] = f"Bearer {TOKEN}"
    url = path if path.startswith("http") else f"{BASE}{path}"
    return requests.request(method, url, headers=headers, timeout=60, **kwargs)


def service(name: str, data: dict[str, Any]) -> Any:
    """调用集成服务并取回 service_response（REST 需要 return_response=true）。"""
    response = api(
        "POST",
        f"/api/services/home_voice_safety/{name}?return_response=true",
        json=data,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"{name} HTTP {response.status_code}: {response.text[:500]}")
    payload = response.json()
    if isinstance(payload, dict) and "service_response" in payload:
        return payload["service_response"]
    return payload


def wait_ready(timeout: float = 600.0) -> dict[str, Any]:
    """轮询到 HA HTTP API 可访问；返回 onbording 阶段快照。"""
    started = time.time()
    last = ""
    while time.time() - started < timeout:
        try:
            response = requests.get(f"{BASE}/api/onboarding", timeout=10)
            if response.status_code == 200:
                return {
                    "wait_seconds": round(time.time() - started, 1),
                    "status": 200,
                    "onboarding": response.json(),
                }
            last = f"{response.status_code}: {response.text[:200]}"
        except requests.RequestException as exc:
            last = repr(exc)
        time.sleep(3)
    raise TimeoutError(f"HA 在 {timeout}s 内未就绪，最后错误：{last}")


# --------------------------------------------------------------------------- #
# Onboarding / 认证
# --------------------------------------------------------------------------- #
def _onboarding_completed(before_raw: Any) -> bool:
    """判断实例是否已完成 onboarding（HA 2024.6+ 返回 list[step]，旧版返回 dict）。"""
    if isinstance(before_raw, dict):
        return bool(before_raw.get("done"))
    if isinstance(before_raw, list):
        return any(step.get("step") == "user" and step.get("done") for step in before_raw)
    return False


def _login_with_password() -> str:
    """已 onboarding 的实例复用：走 login_flow 密码登录换 access token。

    HA 不再支持 POST /api/onboarding/users 重复创建用户（返回 403
    "User step already done"），因此复跑脚本时必须用已存在用户的密码登录。
    """
    if not os.environ.get("HVS_PASSWORD"):
        raise RuntimeError(
            "实例已完成 onboarding，复跑需要显式设置 HVS_PASSWORD（smoke 用户密码）"
        )
    flow_response = requests.post(
        f"{BASE}/auth/login_flow",
        json={
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
        timeout=30,
    )
    if flow_response.status_code >= 400:
        raise RuntimeError(
            f"POST /auth/login_flow -> {flow_response.status_code}: {flow_response.text[:500]}"
        )
    flow_id = flow_response.json().get("flow_id")
    if not flow_id:
        raise RuntimeError(f"login_flow 未返回 flow_id: {flow_response.text[:500]}")

    login_response = requests.post(
        f"{BASE}/auth/login_flow/{flow_id}",
        json={"client_id": CLIENT_ID, "username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    if login_response.status_code >= 400:
        raise RuntimeError(
            f"POST /auth/login_flow/{flow_id} -> {login_response.status_code}: {login_response.text[:500]}"
        )
    payload = login_response.json()
    auth_code = payload.get("result")
    if payload.get("type") != "create_entry" or not auth_code:
        # 密码错误时 HA 返回 type=form + errors.base=invalid_auth
        raise RuntimeError(f"登录失败（用户名/密码或 flow 状态不符）: {payload}")

    token_response = requests.post(
        f"{BASE}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if token_response.status_code >= 400:
        raise RuntimeError(
            f"POST /auth/token -> {token_response.status_code}: {token_response.text[:500]}"
        )
    return token_response.json()["access_token"]


def onboard() -> dict[str, Any]:
    global TOKEN

    before_raw = requests.get(f"{BASE}/api/onboarding", timeout=10).json()
    # 已 onboarding 的实例：不再重复创建用户，改用密码登录复用 token。
    if _onboarding_completed(before_raw):
        TOKEN = _login_with_password()
        record(
            "onboarding",
            "复用已有实例并取得 token",
            "HA 已完成 onboarding，通过 login_flow 密码登录取得 token",
            {"before": before_raw, "auth_code_received": True},
            "info",
            "复用已有实例（非首次启动）",
        )
        return {"reused": True}

    users = requests.post(
        f"{BASE}/api/onboarding/users",
        json={
            "client_id": CLIENT_ID,
            "name": "Smoke",
            "username": USERNAME,
            "password": PASSWORD,
            "language": "zh-Hans",
        },
        timeout=30,
    )
    if users.status_code >= 400:
        raise RuntimeError(f"POST /api/onboarding/users -> {users.status_code}: {users.text[:500]}")
    auth_code = users.json().get("auth_code")
    if not auth_code:
        raise RuntimeError(f"onboarding 未返回 auth_code: {users.text[:500]}")

    token_response = requests.post(
        f"{BASE}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if token_response.status_code >= 400:
        raise RuntimeError(f"POST /auth/token -> {token_response.status_code}: {token_response.text[:500]}")
    TOKEN = token_response.json()["access_token"]

    post_steps = {}
    for path in ("/api/onboarding/core_config", "/api/onboarding/analytics"):
        response = api("POST", path, json={})
        post_steps[path] = {"status": response.status_code, "body": response.text[:200]}
    record(
        "onboarding",
        "本地用户 + token + core_config/analytics 完成",
        "用户创建成功，token 已取得",
        {"auth_code_received": True, "post_steps": post_steps},
        "pass",
    )
    return post_steps


# --------------------------------------------------------------------------- #
# WebSocket 原生 API（config flow 只能用 WS，除非 REST 端点存在）
# --------------------------------------------------------------------------- #
def _recv_until(ws, predicate, limit: int = 50) -> dict[str, Any]:
    for _ in range(limit):
        message = json.loads(ws.recv())
        if predicate(message):
            return message
    raise RuntimeError("WebSocket 未收到期望消息")


def ws_flow(handler: str, submissions: list[dict[str, Any]]) -> dict[str, Any]:
    """用 HA WebSocket API 走完一个 config flow；返回最终 result。"""
    from websockets.sync.client import connect

    with connect(f"{BASE.replace('http', 'ws')}/api/websocket", open_timeout=30) as ws:
        _recv_until(ws, lambda m: m.get("type") == "auth_required")
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        auth_reply = _recv_until(ws, lambda m: m.get("type") in {"auth_ok", "auth_invalid"})
        if auth_reply.get("type") != "auth_ok":
            raise RuntimeError(f"WebSocket 认证失败：{auth_reply}")

        message_id = 1
        result = None
        last_error = None
        # HA 2024.6 的 WS 命令是 config_entries/flow；HA 2026 改为 .../flow/start。
        for command in ("config_entries/flow", "config_entries/flow/start"):
            ws.send(json.dumps({"id": message_id, "type": command, "handler": handler}))
            reply = _recv_until(
                ws,
                lambda m, mid=message_id: m.get("type") == "result" and m.get("id") == mid,
            )
            if reply.get("success", True):
                result = reply["result"]
                break
            last_error = reply
            message_id += 1
        if result is None:
            raise RuntimeError(f"config_entries/flow 失败：{last_error}")
        first_form = result
        for user_input in submissions:
            if result.get("type") != "form":
                break
            message_id += 1
            ws.send(
                json.dumps(
                    {
                        "id": message_id,
                        "type": "config_entries/flow/step",
                        "flow_id": result["flow_id"],
                        "user_input": user_input,
                    }
                )
            )
            reply = _recv_until(ws, lambda m, mid=message_id: m.get("type") == "result" and m.get("id") == mid)
            if not reply.get("success", True):
                raise RuntimeError(f"flow/step 失败：{reply}")
            result = reply["result"]
        return {"first": first_form, "last": result}


def ws_call(message: dict[str, Any]) -> Any:
    from websockets.sync.client import connect

    with connect(f"{BASE.replace('http', 'ws')}/api/websocket", open_timeout=30) as ws:
        _recv_until(ws, lambda m: m.get("type") == "auth_required")
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        _recv_until(ws, lambda m: m.get("type") in {"auth_ok", "auth_invalid"})
        ws.send(json.dumps({"id": 1, **message}))
        return _recv_until(ws, lambda m: m.get("type") == "result" and m.get("id") == 1)


def schema_variants(schema: Any) -> list[str]:
    """把 WS 返回的 schema（list 形式）转成可读字段列表。"""
    out: list[str] = []
    if isinstance(schema, list):
        for item in schema:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
    return out


def selector_options(schema: Any, field: str) -> list[Any]:
    if not isinstance(schema, list):
        return []
    for item in schema:
        if isinstance(item, dict) and item.get("name") == field:
            options = item.get("options")
            if isinstance(options, list):
                return options
            selector = item.get("selector") or {}
            select = (selector or {}).get("select") or {}
            return list(select.get("options") or [])
    return []


def config_entries() -> Any:
    """REST 优先；若端点不存在则走 WS。"""
    response = api("GET", "/api/config/config_entries/entry")
    if response.status_code == 200:
        return response.json()
    raise RuntimeError(f"GET config entries -> {response.status_code}: {response.text[:300]}")


def hvs_entries() -> list[dict[str, Any]]:
    raw = config_entries()
    if isinstance(raw, dict):
        raw = raw.get("entries", [])
    return [item for item in raw if item.get("domain") == "home_voice_safety"]


def create_entry(reuse_loaded: bool = True) -> dict[str, Any]:
    """第 2 步：走首次向导，返回 entry 与向导耗时。

    reuse_loaded=True 时，若实例上已有 loaded 的 home_voice_safety entry，
    直接复用而不重新走 flow —— HA 不允许同 domain 重复配置（再走 flow 会
    abort: already_configured），而 same-version 的多个附加脚本需要串行跑。
    """
    started = time.time()

    if reuse_loaded:
        existing = hvs_entries()
        loaded = [item for item in existing if item.get("state") == "loaded"]
        if loaded:
            entry = loaded[-1]
            record(
                "step1_install_and_entry",
                "集成 entry 已存在且 loaded（复用，不重走向导）",
                f"复用 entry {entry.get('entry_id')}（state=loaded）",
                {"entry": entry, "all_entries": existing},
                "info",
                "实例上已有 loaded entry，跳过向导以避免 already_configured",
            )
            return {
                "entry": entry,
                "detail": {"reused": True, "entry": entry, "all_entries": existing},
                "candidates": [],
                "chosen": [],
            }

    # 优先用 REST 走 config flow（HA 2026 的 WS 命令名已变化）。
    rest_probe = api("POST", "/api/config/config_entries/flow", json={"handler": "home_voice_safety"})
    rest_probe_info = {"status": rest_probe.status_code, "body": rest_probe.text[:300]}

    if rest_probe.status_code in (200, 201):
        flow_first = rest_probe.json()
    else:
        flow_first = ws_flow("home_voice_safety", [])["first"]

    flow_id = flow_first["flow_id"]
    step_id = flow_first.get("step_id")
    candidates = [
        option.get("value") if isinstance(option, dict) else option
        for option in selector_options(flow_first.get("data_schema"), "dangerous_entities")
    ]

    candidate_ids = [str(item) for item in candidates]
    record(
        "wizard_candidates",
        "向导第 1 步列出 lock/switch/cover/alarm/vacuum/climate 候选实体，可勾选",
        f"step_id={step_id}，候选 {len(candidate_ids)} 个",
        {"rest_probe": rest_probe_info, "candidates": candidate_ids[:20], "total": len(candidate_ids)},
        "pass" if candidate_ids else "fail",
        "" if candidate_ids else "向导没有返回任何候选实体（demo/template lock 未就绪或 states 为空）",
    )

    lock_ids = [item for item in candidate_ids if item.startswith("lock.")]
    switch_ids = [item for item in candidate_ids if item.startswith("switch.")]
    chosen = (lock_ids[:1] + switch_ids[:1]) or candidate_ids[:2]
    if not chosen:
        raise RuntimeError("没有可勾选的候选实体，无法创建配置条目")

    submissions = [
        {"dangerous_entities": chosen},
        {
            "confirm_ttl_seconds": CONFIRM_TTL_SECONDS,
            "dedup_window_seconds": DEDUP_WINDOW_SECONDS,
            "nlu_provider": "rule",
            "real_entities": "",
            "notify_service": os.environ.get("HVS_NOTIFY_SERVICE", ""),
        },
    ]
    result = flow_first
    for user_input in submissions:
        if result.get("type") != "form":
            break
        if rest_probe.status_code in (200, 201):
            response = api("POST", f"/api/config/config_entries/flow/{flow_id}", json=user_input)
            if response.status_code >= 400:
                raise RuntimeError(f"REST flow/step 失败：{response.status_code} {response.text[:500]}")
            result = response.json()
        else:
            result = ws_flow("home_voice_safety", [user_input])["last"]
    last = result
    if last.get("type") != "create_entry":
        raise RuntimeError(f"flow 未能创建配置条目：{last}")

    entry_id = last["result"]["entry_id"] if "entry_id" in last.get("result", {}) else None

    # 等待 entry 进入 loaded。
    deadline = time.time() + 120
    entry: dict[str, Any] | None = None
    while time.time() < deadline:
        entries = hvs_entries()
        if entry_id:
            entry = next((item for item in entries if item.get("entry_id") == entry_id), None)
        elif entries:
            entry = entries[-1]
        if entry and entry.get("state") in {"loaded", "setup_error", "setup_retry", "failed_unload"}:
            break
        time.sleep(2)
    elapsed = round(time.time() - started, 1)

    # 记录入口的 data/options（不含 token 等敏感信息）。
    detail = {
        "entry": entry,
        "flow_elapsed_seconds": elapsed,
        "chosen_dangerous_entities": chosen,
        "rest_flow_supported": rest_probe_info["status"] in (200, 201),
        "all_entries": hvs_entries(),
    }
    if entry and entry.get("state") == "loaded":
        record(
            "step1_install_and_entry",
            "集成被 HA 发现、config entry 处于 loaded，日志无 ModuleNotFoundError",
            f"entry loaded（{elapsed}s，候选 {len(candidate_ids)}，勾选 {len(chosen)}）",
            detail,
            "pass",
        )
    else:
        logs = docker_logs(tail=500)
        record(
            "step1_install_and_entry",
            "集成被 HA 发现、config entry 处于 loaded",
            f"entry state={entry.get('state') if entry else 'missing'}，reason={entry.get('reason') if entry else '-'}",
            {"entry": entry, "log_tail": logs[-4000:]},
            "fail",
            "配置条目未进入 loaded；日志见 evidence.log_tail",
        )
    return {"entry": entry, "detail": detail, "candidates": candidate_ids, "chosen": chosen}

# --------------------------------------------------------------------------- #
# 9 步冒烟
# --------------------------------------------------------------------------- #
def state_of(entity_id: str) -> dict[str, Any]:
    response = api("GET", "/api/states/" + urllib.parse.quote(entity_id, safe=""))
    if response.status_code >= 400:
        raise RuntimeError(f"GET /api/states/{entity_id} -> {response.status_code}")
    return response.json()


def pick_lock(candidates: list[str], chosen: list[str]) -> str:
    for entity_id in chosen:
        if entity_id.startswith("lock."):
            return entity_id
    for entity_id in candidates:
        if entity_id.startswith("lock."):
            return entity_id
    return ""


def wait_entry_loaded(entry_id: str, timeout: float = 180.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        for item in hvs_entries():
            if item.get("entry_id") == entry_id:
                last = item
                if item.get("state") == "loaded":
                    return item
        time.sleep(2)
    return last


def services_of(domain: str) -> list[str]:
    response = api("GET", "/api/services")
    if response.status_code >= 400:
        raise RuntimeError(f"GET /api/services -> {response.status_code}")
    for item in response.json():
        if item.get("domain") == domain:
            return sorted(item.get("services", {}).keys())
    return []


def run_smoke(entry_info: dict[str, Any]) -> None:
    entry = entry_info["entry"]
    candidates = entry_info["candidates"]
    chosen = entry_info["chosen"]
    lock_id = pick_lock(candidates, chosen)

    # ---- 第 2 步：向导（时间预算 <= 5 分钟 + 候选/勾选能力） ----
    flow_seconds = entry_info["detail"]["flow_elapsed_seconds"]
    record(
        "step2_wizard",
        "首次向导 <= 5 分钟，列出候选危险实体且可勾选后创建条目",
        f"{flow_seconds}s，候选 {len(candidates)} 个，勾选 {chosen}",
        {
            "flow_elapsed_seconds": flow_seconds,
            "candidate_count": len(candidates),
            "selected": chosen,
            "rest_flow_probe": entry_info["detail"]["rest_flow_supported"],
        },
        "pass" if flow_seconds <= 300 and candidates and chosen else "fail",
        "" if flow_seconds <= 300 and candidates and chosen else "耗时超 5 分钟或候选/勾选为空",
    )

    if not lock_id:
        record("step3_park", "危险动作只 park 不下发", "没有 lock 实体可测", {}, "blocked", "无 lock 候选")
        record("step4_confirm", "确认后恰好下发一次", "缺少 lock 实体", {}, "blocked", "无 lock 候选")
        record("step5_restart", "重启后 pending 恢复且旧 token 可确认", "缺少 lock 实体", {}, "blocked", "无 lock 候选")
        record("step6_dedup", "同一 token 重复确认不重发", "缺少 lock 实体", {}, "blocked", "无 lock 候选")
    else:
        # ---- 第 3 步：危险动作 park，dispatch_count=0，设备状态不变 ----
        before = state_of(lock_id)
        park = service("handle_utterance", {"text": "开大门", "session_id": SESSION_ID})
        after_park = state_of(lock_id)
        token = park.get("confirmation_token") or (park.get("confirmation") or {}).get("token")
        kind = park.get("kind")
        dispatch_count = park.get("dispatch_count")
        state_unchanged = before.get("state") == after_park.get("state")
        ok3 = kind == "awaiting_confirmation" and dispatch_count == 0 and state_unchanged
        record(
            "step3_park",
            "「开大门」返回 awaiting_confirmation、dispatch_count=0，且 lock 状态未变",
            f"kind={kind} dispatch_count={dispatch_count} lock={before.get('state')}->{after_park.get('state')}",
            {
                "lock": lock_id,
                "response": park,
                "state_before": before.get("state"),
                "state_after": after_park.get("state"),
            },
            "pass" if ok3 else "fail",
            "" if ok3 else "危险动作没有正确停在确认门禁，或已经改变设备状态",
        )

        if token:
            # ---- 第 4 步：确认后恰好下发一次 ----
            confirmed = service("confirm_action", {"token": token, "session_id": SESSION_ID})
            after_confirm = state_of(lock_id)
            changed = after_confirm.get("state") != after_park.get("state")
            ok4 = confirmed.get("kind") == "executed" and (changed or confirmed.get("dispatch_count") == 1)
            record(
                "step4_confirm",
                "带 token 确认后 kind=executed，lock 恰好被操作一次",
                f"kind={confirmed.get('kind')} dispatch_count={confirmed.get('dispatch_count')} "
                f"lock={after_park.get('state')}->{after_confirm.get('state')}",
                {
                    "lock": lock_id,
                    "response": confirmed,
                    "state_before": after_park.get("state"),
                    "state_after": after_confirm.get("state"),
                },
                "pass" if ok4 else "fail",
                "" if ok4 else "确认后未执行或设备状态没有按预期变化",
            )
        else:
            record("step4_confirm", "带 token 确认后 kind=executed", "第 3 步没有返回 token", {}, "blocked", "无 token")

        # ---- 第 5 步：重启恢复 ----
        park2 = service("handle_utterance", {"text": "锁大门", "session_id": SESSION_ID_2})
        token2 = park2.get("confirmation_token") or (park2.get("confirmation") or {}).get("token")
        restart_evidence: dict[str, Any] = {"park_response": park2}
        if token2:
            restarted = docker("restart", CONTAINER, timeout=180)
            restart_evidence["restart_returncode"] = restarted.returncode
            restart_evidence["restart_stderr"] = restarted.stderr[-500:]
            restart_started = time.time()
            ready = wait_ready(timeout=300)
            restart_evidence["ha_ready_seconds"] = round(time.time() - restart_started, 1)
            loaded = wait_entry_loaded(entry["entry_id"]) if entry else None
            restart_evidence["entry_after_restart"] = loaded
            pending = service("list_pending", {})
            tokens = [item.get("token") for item in pending.get("pending", [])]
            restart_evidence["list_pending"] = pending
            confirm2 = service("confirm_action", {"token": token2, "session_id": SESSION_ID_2})
            after_confirm2 = state_of(lock_id)
            restart_evidence["confirm_response"] = confirm2
            restart_evidence["lock_after_confirm"] = after_confirm2.get("state")
            ok5 = token2 in tokens and confirm2.get("kind") == "executed"
            record(
                "step5_restart_recovery",
                "重启后 list_pending 能看到旧 token，且旧 token 仍可确认成功",
                f"pending={len(tokens)}（含旧 token={token2 in tokens}），confirm kind={confirm2.get('kind')}",
                restart_evidence,
                "pass" if ok5 else "fail",
                "" if ok5 else "重启后 pending 丢失或旧 token 失效",
            )

            # ---- 第 6 步：同一 token 重复确认不重发 ----
            state_before6 = state_of(lock_id)
            repeat = service("confirm_action", {"token": token2, "session_id": SESSION_ID_2})
            state_after6 = state_of(lock_id)
            ok6 = (
                repeat.get("kind") != "executed"
                and repeat.get("dispatch_count", 0) == 0
                and state_before6.get("state") == state_after6.get("state")
            )
            record(
                "step6_repeat_confirm",
                "同一 token 再次确认被拒绝/去重，dispatch_count=0 且设备状态不变",
                f"kind={repeat.get('kind')} dispatch_count={repeat.get('dispatch_count')} "
                f"lock={state_before6.get('state')}->{state_after6.get('state')}",
                {
                    "response": repeat,
                    "state_before": state_before6.get("state"),
                    "state_after": state_after6.get("state"),
                },
                "pass" if ok6 else "fail",
                "" if ok6 else "重复确认触发了二次下发",
            )
        else:
            record("step5_restart_recovery", "重启后 pending 恢复且旧 token 可确认", "第 2 条危险动作没有返回 token", park2, "blocked", "无 token")
            record("step6_repeat_confirm", "同一 token 重复确认不重发", "缺少 token", {}, "blocked", "无 token")

    # ---- 第 7 步：审计导出 ----
    export = service("export_audit", {"format": "json", "redact_utterance": True})
    export_evidence: dict[str, Any] = {"response": export}
    ok7 = False
    reason7 = ""
    if export.get("ok") and export.get("path"):
        path = export["path"]
        listing = docker("exec", CONTAINER, "ls", "-l", path)
        export_evidence["ls"] = (listing.stdout + listing.stderr).strip()
        reader = (
            "import json;"
            f"p={path!r};"
            "data=json.load(open(p,encoding='utf-8'));"
            "print(json.dumps({'rows':len(data),'keys':sorted(data[0].keys()) if data else [],"
            "'raw_hits':sum(1 for r in data if str(r.get('utterance') or '').strip() not in ('','[redacted]')),"
            "'sample':data[0] if data else None},ensure_ascii=False))"
        )
        read_out = docker_exec_python(reader)
        export_evidence["content_check"] = read_out.strip()[:3000]
        try:
            parsed = json.loads(read_out.strip().splitlines()[-1])
            ok7 = bool(listing.returncode == 0 and parsed["rows"] > 0 and parsed["raw_hits"] == 0)
            if parsed["raw_hits"]:
                reason7 = "导出文件仍包含原句（脱敏失败）"
        except Exception as exc:  # noqa: BLE001 - 证据解析失败本身就是失败
            reason7 = f"导出文件无法解析：{exc}"
    else:
        reason7 = f"export_audit 返回失败：{export}"
    record(
        "step7_export_audit",
        "export_audit 生成文件、文件存在、JSON 可解析且不含原句",
        f"path={export.get('path')} rows={export.get('rows')} ok={export.get('ok')}",
        export_evidence,
        "pass" if ok7 else "fail",
        reason7,
    )

    # ---- 第 9 步（先于卸载执行：卸载会删除服务）：断网/本地决策 ----
    networks = docker("inspect", CONTAINER, "--format", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}")
    network_name = (networks.stdout or "bridge").split()[0] if (networks.stdout or "").strip() else "bridge"
    offline_evidence: dict[str, Any] = {"network": network_name}
    disconnected = docker("network", "disconnect", network_name, CONTAINER)
    offline_evidence["disconnect_returncode"] = disconnected.returncode
    offline_evidence["disconnect_stderr"] = disconnected.stderr.strip()[-300:]
    try:
        host_status: Any = requests.get(f"{BASE}/api/onboarding", timeout=5).status_code
    except requests.RequestException as exc:
        host_status = repr(exc)
    offline_evidence["host_reachable_after_disconnect"] = host_status

    container_code = (
        "import json,os,urllib.request;"
        "req=urllib.request.Request("
        "'http://127.0.0.1:8123/api/services/home_voice_safety/handle_utterance?return_response=true',"
        "data=json.dumps({'text':'开客厅灯','session_id':'smoke-offline'}).encode(),"
        "headers={'Authorization':'Bearer '+os.environ['HVS_TOKEN'],'Content-Type':'application/json'},"
        "method='POST');"
        "print(urllib.request.urlopen(req,timeout=30).read().decode())"
    )
    container_out = docker_exec_python(container_code, env={"HVS_TOKEN": TOKEN or ""})
    offline_evidence["container_exec_output"] = container_out.strip()[:3000]
    decision: dict[str, Any] = {}
    try:
        payload = json.loads(container_out.strip().splitlines()[-1])
        decision = payload.get("service_response") or {}
    except Exception:  # noqa: BLE001 - 解析失败即视为无决策结果
        decision = {}

    reconnected = docker("network", "connect", network_name, CONTAINER)
    offline_evidence["connect_returncode"] = reconnected.returncode
    recovered = wait_ready(timeout=120)
    offline_evidence["host_recovered"] = {"wait_seconds": recovered["wait_seconds"]}

    ok9 = bool(decision.get("kind")) and disconnected.returncode == 0
    record(
        "step9_offline_local",
        "断开容器网络后，本地指令仍能被决策层处理（不依赖外网）",
        f"断网后主机不可达（{host_status}），容器内 handle_utterance kind={decision.get('kind')}",
        offline_evidence,
        "pass" if ok9 else "fail",
        "" if ok9 else "断网后无法取得本地决策结果（见证据）",
    )

    # ---- 第 8 步：卸载 ----
    unload_evidence: dict[str, Any] = {}
    if entry:
        delete = api("DELETE", f"/api/config/config_entries/entry/{entry['entry_id']}")
        unload_evidence["delete_status"] = delete.status_code
        unload_evidence["delete_body"] = delete.text[:500]
        deadline = time.time() + 60
        remaining: list[dict[str, Any]] = hvs_entries()
        while remaining and time.time() < deadline:
            time.sleep(2)
            remaining = hvs_entries()
        unload_evidence["remaining_entries"] = remaining
        unload_evidence["hvs_services"] = services_of("home_voice_safety")
        states = api("GET", "/api/states").json()
        leftovers = [
            item["entity_id"]
            for item in states
            if str(item.get("entity_id", "")).startswith("sensor.home_voice_safety")
        ]
        unload_evidence["leftover_sensors"] = leftovers
        ok8 = (
            delete.status_code < 400
            and not remaining
            and not unload_evidence["hvs_services"]
            and not leftovers
        )
        record(
            "step8_uninstall",
            "删除条目后不再有 entry、home_voice_safety.* 服务与残留 sensor",
            f"delete={delete.status_code} remaining={len(remaining)} "
            f"services={len(unload_evidence['hvs_services'])} sensors={len(leftovers)}",
            unload_evidence,
            "pass" if ok8 else "fail",
            "" if ok8 else "卸载后仍有条目/服务/sensor 残留",
        )
    else:
        record("step8_uninstall", "删除条目后清理干净", "没有可删除的 entry", unload_evidence, "blocked", "entry 缺失")


def main() -> int:
    exit_code = 0
    try:
        ready = wait_ready()
        record("ha_ready", "HA HTTP API 就绪", f"{ready['wait_seconds']}s 后 /api/onboarding=200", ready, "info")
        onboard()
        # 主 9 步必须真实走向导创建 entry，不能复用已有条目。
        entry_info = create_entry(reuse_loaded=False)
        if entry_info["entry"] and entry_info["entry"].get("state") == "loaded":
            run_smoke(entry_info)
        else:
            record(
                "smoke_aborted",
                "配置条目 loaded 后执行 9 步冒烟",
                "配置条目未 loaded",
                entry_info["detail"],
                "blocked",
                "setup 失败，后续步骤无意义",
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
        save_results()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
