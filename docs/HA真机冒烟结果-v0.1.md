# HA 真机冒烟结果 v0.1

> 执行日期：2026-11-03
> 环境：Windows 11 Pro / Docker 29.6.2 / 镜像 `homeassistant/home-assistant:stable`（Docker Hub，3.46GB）
> 容器：`hvsl-smoke`，端口 8123，挂载 `var/ha-config`
> 结论：**9/9 步通过**（不是 blocked，也不是静态检查；全部有 HTTP/WS 响应与设备状态证据）。

---

## 1. 结果总表

| # | 步骤 | 判定 | 实际证据 |
|---|---|---|---|
| 0 | HA 就绪 | PASS | `/api/onboarding` 200；2s 内就绪 |
| 0b | Onboarding | PASS | 用户创建成功、token 取得；core_config/analytics 200 |
| 2 | 向导候选 | PASS | `step_id=user`，候选 **20** 个（lock/switch/cover/alarm/vacuum/climate） |
| 3 | 安装/配置条目 | PASS | entry **loaded**，1.4s；日志无 `ModuleNotFoundError` |
| 4 | 向导完成 | PASS | 1.4s；勾选 `lock.front_door` + `switch.ac` |
| 5 | 危险动作挂起 | PASS | `kind=awaiting_confirmation`，`dispatch_count=0`，lock `locked→locked` |
| 6 | 确认后执行 | PASS | `kind=executed`，`dispatch_count=1`，lock `locked→unlocked` |
| 7 | 重启恢复 pending | PASS | 重启后 `pending=1` 且含旧 token；confirm `executed` |
| 8 | 重复确认不重发 | PASS | `kind=rejected`，`dispatch_count=0`，lock `locked→locked` |
| 9 | 审计导出 | PASS | JSON 20+ 行，`redacted=true`，`utterance` 列全部 `[redacted]` |
| 10 | 卸载 | PASS | `DELETE=200`，remaining=0，services=0，sensors=0 |
| 11 | 断网/本地执行 | PASS | 主机不可达时容器内仍返回 **`kind=executed`**（补中文名后本地解析成功） |

> 说明：第 11 步在最新一轮为 `executed`，证明断网后仍能完成本地决策并下发；早期未补中文名时曾返回 `rejected`。

## 2. 为什么之前是 0/9 blocked，现在能跑

| 阻塞 | 处理 |
|---|---|
| `ghcr.io/home-assistant/home-assistant:stable` 拉取后镜像未注册 | 改用 **Docker Hub** `homeassistant/home-assistant:stable`，一次拉取成功（3.46GB） |
| 原生 Windows 不支持 `hass.exe` | 放弃本地进程方案，回到 Docker（镜像已可用） |
| HA 新版 `/api/onboarding` 返回 list | 脚本兼容 list；首次启动继续创建用户 |
| WS `config_entries/flow` 命令不存在 | 改为 **REST** `/api/config/config_entries/flow` 启动 + `/flow/{id}` 提交 |
| `configuration.yaml` 被 HA 默认模板覆盖，`demo:` 丢失 | 重写配置并保留 `demo:` + `homeassistant.customize` |
| demo 实体是英文名，中文“大门”无法解析 | `customize`：`lock.front_door` → **大门**，`switch.ac` → **空调** |
| 审计导出 `detail` 仍含原句 | `audit.py` 递归脱敏 `detail`（`_redact_value`） |
| step7 判据把设备名“锁大门”当原句 | 判据改为只校验 `utterance` 列（`[redacted]`） |

## 3. 证据文件

| 文件 | 内容 |
|---|---|
| `var/ha-smoke/results.json` | 每步 expected/actual/evidence/verdict |
| `var/ha-smoke/ha.log` | 容器完整日志 |
| `var/ha-config/` | HA 配置（含 `customize` 中文名）与 `.storage` |
| `var/ha-config/home_voice_safety/exports/` | `export_audit` 生成的 JSON |

## 4. 本次真机验证覆盖了什么

- 集成被 HA 发现、config entry 创建并 **loaded**；
- 首次向导列出 20 个候选并完成两步配置；
- 危险动作**停在确认门禁**、0 下发；
- 确认后**恰好 1 次**下发；
- 重启 HA 后 pending **可恢复并用旧 token 确认**；
- 重复确认**不重发**；
- 审计导出可解析、默认脱敏；
- 卸载后**无残留** service/sensor；
- 断网后仍能完成**本地执行**（最新一轮 `kind=executed`）。

## 5. 仍未覆盖（不要夸大）

- **真实设备 0 台**：全部基于 HA `demo` 集成 + `customize` 中文名。
- **notify 真投递**未验证：HA 2026 legacy `notify` 平台不可用；事件 + blueprint 已 3/3，但手机推送需 `mobile_app`/`rest`。
- **ASR** 已在本机验证（合成 2/2、公开真人 4 条可识别但有错字）；**HA 内 ASR/TTS 接入** 未验证。
- **CSV 导出已验**：附加检查 635 行、表头与 `[redacted]` 均通过。
- **HA 多版本矩阵已完成**：stable、2025.1、2025.2.4 三者同级全 PASS（9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1）；2024.6 依赖不兼容（自带 pydantic 1.10.15），2025.3+ 未测，见 `docs/HA多版本矩阵-v0.1.md`。
- **HA 2026 blocking 警告已清零**：`__init__.py` 用 `async_add_executor_job(importlib.import_module, ...)` 导入 config_flow/coordinator/services，真机日志 `blocking call` = 0；9/9 仍 PASS。
- `home_voice_safety` 仍被 HA 标记为 “not tested by Home Assistant”（自定义集成正常警告，不是错误）。

## 6. 复现命令

```bash
docker pull homeassistant/home-assistant:stable
docker rm -f hvsl-smoke
docker run -d --name hvsl-smoke -p 8123:8123 \
  -v "$PWD/var/ha-config:/config" \
  homeassistant/home-assistant:stable
# 等待 /api/onboarding=200 后：
uv pip install --python .venv/Scripts/python.exe requests websockets
.venv/Scripts/python.exe scripts/ha_docker_smoke.py
docker logs hvsl-smoke > var/ha-smoke/ha.log
```

## 8. 附加真机检查（2026-11-03，3/3 PASS）

| 检查 | 结果 | 证据 |
|---|---|---|
| CSV 审计导出 | PASS | 635 行；表头含 `trace_id/stage/utterance/risk_class/dispatch_state`；`utterance` 全部 `[redacted]` |
| 50 次低风险 + 10 次危险挂起/取消 | PASS | 低风险 **50/50 executed**；危险 **10/10 挂起并取消**；pending=0；lock `locked→locked` |
| DB/审计增长 | PASS | DB 266KB；审计 825 行；无异常 |

脚本：`scripts/ha_extra_checks.py`；结果：`var/ha-extra/results.json`。

> 该检查暴露并修复了一个**真实产品缺口**：向导生成的 catalog 原先只含“勾选的危险设备”，灯/风扇不在受控清单，低风险指令被 `rejected`（首次 `low_dispatch=0/50`）。修复为 catalog 写入**所有可控实体**，仅选中设备加 `danger:user_selected`；修复后 `low_dispatch=50/50`。

## 9. 通知/蓝图确认闭环（2026-11-03，3/3 PASS）

| 检查 | 结果 | 证据 |
|---|---|---|
| Blueprint 自动化加载 | PASS | `automation.hvs_confirm_action` = 200 |
| `HVS_CONFIRM` 通知动作 | PASS | 触发 `mobile_app_notification_action`；lock `locked→unlocked` |
| `HVS_CANCEL` 通知动作 | PASS | 触发后 lock `locked→locked`，本轮 token 不在 pending，pending=0 |

脚本：`scripts/ha_confirm_loop.py`；结果：`var/ha-confirm/results.json`；blueprint：`var/ha-config/blueprints/automation/home_voice_safety/confirm_action.yaml`。

> 真机同时暴露并修复了一个 **HA 2026 线程安全缺陷**：`coordinator._handle_decision_event` 在 executor 线程调用 `hass.async_create_task`，HA 2026 直接抛 `RuntimeError`；已改为 `loop.call_soon_threadsafe(...)`。修复后最近日志无线程错误。

> **notify 平台限制（2026-11-04 更新）**：
> - `platform: file` → HA 2026 报 “Invalid notify platform”，不可用。
> - `platform: rest` → 配置被接受、无 “Invalid platform”，但 `notify.hvs_rest` 实体 **missing or not currently available**；带 `data` 的 `send_message` 报 `not a valid option at 'data'`，降级纯文本仍无法投递（实体不存在）。
> - 结论：HA 2026 的 legacy notify 平台（file/rest）都不能完成 `notify.send_message` 真投递；真机推送只能走 `mobile_app`（真实手机，未验证）。
> - 副作用修复：`services._async_notify_confirmation` 改为 `async_create_task` 异步发送 + 失败降级纯文本；notify 失败不再阻塞确认事件，蓝图闭环恢复到 **3/3 PASS**。

## 10. 长期 soak（2026-11-03，1/1 PASS）

| 检查 | 结果 | 证据 |
|---|---|---|
| 500 次低风险 + 50 次危险挂起/取消 | PASS | 低风险 **500/500 executed**；危险 **50/50 挂起并取消**；pending=0；lock `locked→locked`；耗时 119.5s |
| DB/审计增长 | 可接受 | DB 290KB→843KB（550 次操作）；审计 2600 行；无异常 |

脚本：`scripts/ha_soak.py`；结果：`var/ha-soak/results.json`。

## 11. 结论（更新）

真机 **9/9 + 附加 3/3 + 蓝图 3/3 + 长期 soak 1/1** 通过；系统核心链路（安装→向导→挂起→确认→重启恢复→重复拒绝→审计→卸载→断网→通知/蓝图确认→500 次连续运行）在真实 HA 容器内可运行。
仍不等于生产可用：真实设备、手机推送（legacy notify 不可用，仅 `mobile_app` 未验证）、HA 内 ASR/TTS 接入、2025.3+ 版本、更长周期稳定性未验证。多版本：stable、2025.1、2025.2.4 三者同级 PASS（均含附加 3/3 + 蓝图 3/3 + soak 500+50）；2024.6 不兼容。
相对亮度调节已在 stable 容器补齐真机验证（亮度 4/4 PASS、音量 1 项未覆盖），见 §12。

## 12. 相对亮度/音量调节真机验证（2026-11-04）

> 执行脚本：`scripts/ha_relative_adjust_smoke.py`（可重复运行、每次先设基线；`HVS_BASE` / `HVS_CONTAINER` 可覆盖）
> 环境：stable 容器 `hvsl-smoke`（`http://127.0.0.1:8123`），复用已 loaded 的 config entry `01M6JPV6JHS4K7Z9G3ZE5C1Z6H`
> 目标实体：`light.living_room_rgbww_lights`，`friendly_name=客厅大灯`（`configuration.yaml` 的 `customize`），`supported_color_modes` 含 `rgbww`
> 结论：**7/7 PASS**（亮度 4/4 + 音量 1/1）。音量项首轮为 `rejected`/`partial`，根因是两个真实缺口（NLU 词表缺中文别名、媒体状态只认 `on`/`off`），修复过程见 12.3。

### 12.1 逐步结果

| # | 步骤 | 预期 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | 基线 `light.turn_on` `brightness_pct=40` | 回读 `brightness≈102`（40%） | `HTTP=200`，`brightness=102`，`pct=40` | PASS |
| 2 | `handle_utterance("把客厅大灯调亮一点", session_id="rel-1")` | `executed`，回读≈50% | `kind=executed`，`dispatch=1`，`brightness=127`，`pct=50` | PASS |
| 3a | 同一 session 第一次 `"把客厅大灯调暗一点"` | `executed`，回读≈40% | `kind=executed`，`dispatch=1`，`brightness=102`，`pct=40` | PASS |
| 3b | **紧接着同一 session 第二次** `"把客厅大灯调暗一点"` | `executed`（不被 30 秒幂等窗口吞掉），回读≈30% | `kind=executed`，`dispatch=1`，`brightness=76`，`pct=30` | PASS |
| 4 | 先 `light.turn_off`，再 `"把客厅大灯调暗一点"`（`session_id="rel-off"`） | 0 下发、灯仍 `off`、brightness 不变、`spoken_message` 含“未下发”且不含“可重试” | `kind=partial`，`dispatch=0`，`off→off`，`brightness=None→None`，`spoken="下发失败 hvs.light.adjust_brightness -> 客厅大灯：未下发：设备已关闭，无法调低（「客厅大灯」）"` | PASS |
| 5 | 音量：设 `media_player.living_room` 到 `0.4`，再 `"电视声音大一点"` | `executed`，回读≈0.5 | `kind=executed`，`dispatch=1`，`volume=0.4→0.5` | PASS |

回读序列（`/api/states` 的 `attributes.brightness`，每次下发后 sleep 0.8s 再读）：

```
brightness_series = [102, 127, 102, 76, null, null]
volume_series     = [0.4, 0.5]
decision_kinds    = ["executed", "executed", "executed", "partial", "executed"]
dispatched_counts = [1, 1, 1, 0, 1]
```

- 第 3b 步是本次的核心证据：**同一 session 连续两次相对调暗都 `executed` 且各 `dispatch=1`**，说明 `idempotent=False` 的工具没有被 30 秒幂等窗口吞掉。
- 第 4 步 `kind=partial` 是既有约定：`dispatch_count=0`，失败原因写进 `outcomes[].error`（确定性拒绝，不带“可重试”）。此处 `spoken_message` 同时包含“未下发”与“设备已关闭”，不含“可重试”。

### 12.2 口径差异（相对步长）

- 任务书里同时出现“调亮→50%，连续两次调暗→30%/20%”与“回读 102 → 51（20%）”两种写法，**与实际实现不一致**。
- 实现口径（`pipeline/nlu.py`：`RELATIVE_STEP_PCT = 10`）是**每次 ±10 个百分点**：
  `40% → 调亮 50% → 调暗 40% → 调暗 30%`，对应 `brightness 102 → 127 → 102 → 76`。
- 本次以**代码与实际回读**为准（上表即实测序列）；“两次调暗到 20%”需要每次 -15~-20，当前实现不支持，也不应为了对齐文字而放宽断言。

### 12.3 音量项：从 `rejected` 到 `executed` 的两个真实缺口

首轮音量项是 `fail/未覆盖`，两次失败分别暴露了不同的真实问题（都如实记录、未改回读值凑 pass）：

1. **NLU 词表缺中文别名（`rejected`）**：`media_player.living_room` 的 `friendly_name` 是 `Living Room`，catalog 的 `display_name` 也是英文；NLU 把“电视声音”当设备名去受控词表匹配，直接 `rejected`、0 下发。
   修法（测试配置，非产品代码）：`var/ha-config/home_voice_safety/devices.json` 的该条目改为 `display_name="客厅电视"` + `aliases=["电视","客厅电视"]`，并在 `configuration.yaml` 的 `customize` 补 `media_player.living_room: friendly_name: 客厅电视`。**改动前快照**见 12.4。
   实测：`kind=partial`、`dispatch=0`，`spoken_message` 为“未下发：「客厅电视」当前状态为 'playing'，无法做相对调节”。
2. **媒体状态只认 `on`/`off`（真实产品缺陷）**：HA 的媒体实体返回的是 `playing`（证据 `state_before.state="playing"`，`volume_level=0.4` 可读），而 `resolve_adjust_args` 只接受 `on`/`off`。真实音响/电视几乎从不返回 `on`，**这等于相对音量在真实设备上不可用**。
   修法（产品代码）：`hvsl/backends/base.py` 对 `media_player` 把“音量”与“播放状态”解耦——只要 `volume_level` 可读且状态不是 `unavailable`/`unknown` 就允许调节（`playing`/`paused`/`idle`/`off` 均可）；读不到音量仍 `PreDispatchError`、0 下发。新增 5 个单测（`playing`/`paused`/`idle` 各一、`off` 且音量可读、无音量拒绝），`tests/test_relative_adjust.py` 27 passed。
   复跑：`kind=executed`、`dispatch=1`、`volume 0.4→0.5`。

附带发现（产品层面待改进，本轮未改代码）：**向导重建 catalog 时只保留 `display_name`、会丢掉 `aliases`**——本轮一次误走向导（`already_configured`）后，`devices.json` 的 mtime 变为该时刻且 `aliases` 消失。若后续要让“电视/客厅电视”等多个叫法稳定生效，向导生成 catalog 时不应丢别名。

### 12.4 证据与复现

| 文件 | 内容 |
|---|---|
| `var/ha-relative/results.json` | 每步 `name/expected/actual/evidence/verdict`，附 `entity_id`、`brightness_series`、`volume_series`、`decision_kinds`、`dispatched_counts`、`uncovered`、`timestamp` |
| `var/ha-relative/ha-relative.log` | 本次运行逐行日志（含回读失败重试记录） |
| `var/ha-relative/devices.before-relative.json` | 测试 catalog 改动前快照（补中文名/别名前的原样），用于核对只是 2 行 diff |
| `var/ha-relative/configuration.before-relative.yaml` | `customize` 补 `media_player.living_room` 前的快照 |

> 凭据卫生：本轮不再反复改密码——改为用 HA `.storage/auth` 里已有的 refresh token 调 `/auth/token`（`grant_type=refresh_token`）换取 **30 分钟** access token，只经 `HVS_TOKEN` 环境变量注入脚本，**不落盘、不打印**；脚本也支持 `HVS_PASSWORD`（login_flow）作为备选。此前重置密码时备份的 `auth_provider` 文件（只含 bcrypt 哈希）已删除。
>
> 复跑方式：脚本缺 `HVS_TOKEN`/`HVS_PASSWORD` 时以退出码 2 记 `blocked`（不伪造）；`--out` 可指向临时文件避免覆盖既有证据；重启后若 config entry 尚未 `loaded`，脚本会轮询最多 90s 再复用，避免误走向导触发 `already_configured`。2026-11-04 leader 独立复跑三次：两次 6 pass / 1 未覆盖（对应 12.3 的两次真实缺口），修复后**7/7 PASS**。

```bash
# 两种凭据来源二选一，均只在环境变量中
HVS_TOKEN=***   HVS_BASE=http://127.0.0.1:8123 HVS_CONTAINER=hvsl-smoke \
  .venv/Scripts/python.exe scripts/ha_relative_adjust_smoke.py
# 或：HVS_PASSWORD=***
```

### 12.5 边界（不要外推）

- 只覆盖 HA `demo` 集成：**1 个模拟灯**（`light.living_room_rgbww_lights`）+ **1 个模拟媒体实体**（`media_player.living_room`）；**真实物理设备 0 台**。
- **并发未测**：只验证单 session 顺序调节；多 session/多用户同时调节、与危险动作确认并发未覆盖。
- 音量只在 `state=playing` + `volume_level` 可读的 demo 实体上验证；真实音响/电视若返回 `unavailable`/`standby` 或没有 `volume_level`，仍按“读不到即安全失败”处理，属未验证路径。
- 中文别名依赖手工补 catalog（向导重建会丢 `aliases`，见 12.3 附带发现）；产品代码未修，属待改进项。
- 回读依赖 HA 状态机异步更新，脚本固定 sleep 0.8s；未做“状态回调”级别的一致性验证。