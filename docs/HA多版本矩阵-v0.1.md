# HA 多版本矩阵 v0.1

> 执行日期：2026-11-04（更新）
> 结论：**stable（2026 线）、2025.1、2025.2.4 均 9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS；2024.6 不兼容（pydantic 1.10）；2025.3+ 未测。**
> 注意：`2025.2` **滚动 tag** 的 image 元数据未注册（两次拉取层全在本地、`docker image inspect` 仍 No such image），但具体 patch tag **`2025.2.4` 可用并达到与 stable / 2025.1 同级**——不要因滚动 tag 未注册就判死整个 2025.2 线。

## 1. 结果

| HA 版本 | 镜像 | 结果 | 证据 |
|---|---|---|---|
| **stable（2026 线）** | `homeassistant/home-assistant:stable` | **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS** | `var/ha-*/results-stable.json`、`docs/HA真机冒烟结果-v0.1.md`（`var/ha-*/results.json` 已被 2025.2.4 本轮覆盖，查 stable 请用 `results-stable.json`） |
| **2025.1** | `homeassistant/home-assistant:2025.1` | **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS**（pydantic 2.10.4） | `var/ha-*/results-2025.1.json` |
| **2025.2.4** | `homeassistant/home-assistant:2025.2.4` | **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS**（pydantic 2.10.6） | `var/ha-smoke/results-2025.2.4.json`、`var/ha-extra/results-2025.2.4.json`、`var/ha-confirm/results-2025.2.4.json`、`var/ha-soak/results-2025.2.4.json` |
| **2025.2（滚动 tag）** | `homeassistant/home-assistant:2025.2` | **image 未注册**（层 `Already exists`，inspect 报 No such image） | `var/ha-2025.2-pull*.log` |
| **2024.6 / 2024.6.4** | `homeassistant/home-assistant:2024.6`、`:2024.6.4` | **不兼容（依赖硬冲突）** | 容器内 `pydantic 1.10.15`；`RequirementsNotFound: ['pydantic>=2.5']` |
| **2025.3+** | 未拉取 | **未测** | — |

## 2. 2025.2.4 真机细节

- 容器 `hvsl-smoke-2025-2`，端口 8126，独立配置目录 `var/ha-config-2025.2`（从 2025.1 配置复制并删 `.storage`）。
- pydantic **2.10.6**（满足 `>=2.5`），集成被发现。
- 结果：

```
[PASS] onboarding / 候选 20 / entry loaded 1.7s
[PASS] step3 挂起 0 下发 / step4 确认 1 次
[PASS] step5 重启恢复 / step6 重复拒绝
[PASS] step7 审计导出 / step9 断网 executed / step8 卸载无残留
[PASS] 附加：CSV 审计导出 1927 行（表头/脱敏校验通过）/ 50 次低风险 dispatch 50 + 10 次危险挂起取消（pending 0，lock locked→locked）/ DB 733184→1314816 字节
[PASS] 蓝图：`automation.hvs_confirm_action` 200 / 通知动作 confirm：locked→unlocked / cancel：lock 保持 locked、pending 0
[PASS] soak：低风险 500/500 + 危险 50/50，pending 0，lock locked→locked，102.2s
```

- 结论：**2025.2.4 与 stable / 2025.1 同级**——主 9 步、附加 3/3、蓝图 3/3、soak 500+50 全部 PASS，证据见 `var/ha-{smoke,extra,confirm,soak}/results-2025.2.4.json`。
- 复跑注意（`2025.2.4` 实测遇到，已在脚本侧修好）：实例已完成 onboarding 时 `POST /api/onboarding/users` 会 403 `User step already done`，`smoke.onboard()` 现改为走 `login_flow` 密码登录复用 token（需设 `HVS_PASSWORD`）；entry 已 loaded 时再走 config flow 会 `abort: already_configured`，`create_entry(reuse_loaded=True)` 复用现有条目，主 9 步脚本仍强制 `reuse_loaded=False` 真实走向导。

## 3. 2025.1 真机细节

- 容器 `hvsl-smoke-2025`，端口 8125，pydantic 2.10.4；主 9 步 9/9、附加 3/3、蓝图 3/3、soak 500+50 全 PASS，证据见 `var/ha-*/results-2025.1.json`。

## 4. 2024.6 的真实根因（不变）

1. HA 2024.6 镜像自带 **pydantic 1.10.15**；本集成使用 pydantic 2 API，`manifest.json` 要求 `pydantic>=2.5`。
2. 2024.6 的 pip constraints 无法安装 pydantic 2 → `ResolutionImpossible`，集成 setup 失败。
3. 集成未 loaded → config flow 未注册；2024.6 只有 REST `/api/config/config_entries/flow`，没有 WS flow 命令。
4. 这不是端口/配置问题，是依赖不兼容；补 `scripts.yaml/scenes.yaml/themes` 后仍因 pydantic 失败。

## 5. 对外口径

- **已验证**：stable、**2025.1**、**2025.2.4** 的安装/向导/挂起/确认/重启恢复/重复拒绝/审计/卸载/断网；三者均含附加 3/3 + 蓝图 3/3 + soak 500+50。
- **已验证不兼容**：2024.6 / 2024.6.4（pydantic 1.10）。
- `hacs.json` 的 `homeassistant: 2025.1.0` 已由 2025.1 实机支撑；2025.2.4 进一步说明“2025.2 线可用，且与 2025.1 同级”。
- 仍不得写“所有 2025.x 已支持”：2025.3+ 未测；2025.2 滚动 tag 仍未注册，只有 `2025.2.4` 这一具体 patch 完成验证。

## 6. 复现命令

```bash
# 2025.2.4（滚动 tag 2025.2 可能未注册，用具体 patch）
docker run -d --name hvsl-smoke-2025-2 -p 8126:8123 \
  -v "$PWD/var/ha-config-2025.2:/config" \
  homeassistant/home-assistant:2025.2.4
HVS_BASE=http://127.0.0.1:8126 HVS_CONTAINER=hvsl-smoke-2025-2 \
  .venv/Scripts/python.exe scripts/ha_docker_smoke.py

# 已在 onboarding 完成 / entry 已存在的实例复用跑（需提供 smoke 用户密码）
export HVS_BASE=http://127.0.0.1:8126 HVS_CONTAINER=hvsl-smoke-2025-2 HVS_PASSWORD=<smoke 用户密码>
.venv/Scripts/python.exe scripts/ha_extra_checks.py    # 附加 3/3  -> var/ha-extra/results.json
.venv/Scripts/python.exe scripts/ha_confirm_loop.py    # 蓝图 3/3  -> var/ha-confirm/results.json
.venv/Scripts/python.exe scripts/ha_soak.py --low-loops 500 --danger-loops 50   # -> var/ha-soak/results.json
cp var/ha-extra/results.json var/ha-extra/results-2025.2.4.json
cp var/ha-confirm/results.json var/ha-confirm/results-2025.2.4.json
cp var/ha-soak/results.json var/ha-soak/results-2025.2.4.json

# 2025.1
docker run -d --name hvsl-smoke-2025 -p 8125:8123 \
  -v "$PWD/var/ha-config-2025:/config" \
  homeassistant/home-assistant:2025.1
HVS_BASE=http://127.0.0.1:8125 HVS_CONTAINER=hvsl-smoke-2025 \
  .venv/Scripts/python.exe scripts/ha_docker_smoke.py

# stable
docker run -d --name hvsl-smoke -p 8123:8123 \
  -v "$PWD/var/ha-config:/config" \
  homeassistant/home-assistant:stable
HVS_BASE=http://127.0.0.1:8123 HVS_CONTAINER=hvsl-smoke \
  .venv/Scripts/python.exe scripts/ha_docker_smoke.py
```

## 7. 边界

- 2025.2.4 已补齐至与 stable / 2025.1 同级（9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1），证据见 `var/ha-{smoke,extra,confirm,soak}/results-2025.2.4.json`；但附加与 soak 均为单轮（50+10、500+50 各一次），未做多轮长时间内存/句柄曲线观测，因此“同级”指功能与闭环，不等同于长期稳定性认证。
- 真实设备、手机 `mobile_app` 推送、ASR/TTS 真机接入仍未验证。
- 2024.6 的失败是依赖不兼容，不计为“集成功能失败”，也不计为 pass。
- 滚动 tag 未注册是可随时间自愈的暂时态；2025.1 曾同样先未注册、后来成功，2025.2 的滚动 tag 可在后续重试，但不要无限重试。