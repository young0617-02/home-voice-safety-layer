# Home Assistant 真机 9 步冒烟清单（已执行）

> 状态：**已执行**。stable（2026-11-03）、2025.1、2025.2.4 三个版本均复用本脚本，且均已补齐附加 3/3 + 蓝图 3/3 + soak 500+50；证据见 `var/ha-smoke/results*.json`、`var/ha-extra|ha-confirm|ha-soak/results-*.json` 及 `docs/HA真机冒烟结果-v0.1.md`、`docs/HA多版本矩阵-v0.1.md`。
> 目标：在真实 Home Assistant 上验证安装、向导、确认门禁、重启恢复、幂等、审计、卸载与本地离线边界。
> 执行前请备份 `/config`；首次联调建议用一把**不控制真实门锁**的模拟/测试锁，或断开执行器电源，避免误开真实门锁。

## 0. 前置记录

| 项 | 填写 |
|---|---|
| 执行日期 | 【填：YYYY-MM-DD】 |
| HA 版本 / 安装方式 | 【填：例如 2024.6 Docker / HAOS】 |
| 集成安装方式 | 【填：HACS / 手动复制 custom_components】 |
| 被测设备 | 【填：entity_id，例如 lock.test_door、switch.test_gas】 |
| 执行人 / 环境 | 【填：姓名、网络是否隔离】 |

---

## 1. 安装并重启

**操作**
1. 将 `custom_components/home_voice_safety/` 复制到 HA 的 `/config/custom_components/`（或通过 HACS 安装）。
2. 重启 Home Assistant。
3. 查看日志与“设置 → 设备与服务 → 添加集成”。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 日志无 `ModuleNotFoundError: hvsl` / `pipeline`；添加集成列表出现“Home Voice Safety Layer” | 【填：实际】 | 【填：日志片段/截图路径】 | 集成可被添加；无导入错误 |

---

## 2. 首次向导 ≤5 分钟

**操作**
1. 打开配置向导，确认自动列出候选实体（lock/switch/cover/alarm_control_panel/vacuum/climate）。
2. 显式勾选门锁与燃气开关（至少一个危险设备）。
3. 完成安全参数页并提交。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 候选列表自动出现；无需手填路径；未勾选时不能提交；全程 ≤5 分钟 | 【填：实际】 | 【填：开始/结束时间、截图】 | 2 步完成；`/config/home_voice_safety/devices.json` 与 `risk_policy.json` 生成 |

---

## 3. 危险指令只挂起、不下发

**操作**
1. Developer Tools → Actions 调用 `home_voice_safety.handle_utterance`，`text` 填“开大门”。
2. 观察 response、`home_voice_safety_decision` 事件与审计；不要确认。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| response `kind=awaiting_confirmation`，带 `confirmation_token`；目标设备服务调用次数为 0 | 【填：实际】 | 【填：response 截图、事件 JSON、审计 trace_id】 | 下发次数 = 0；审计有 `stage=confirm, confirmation_state=pending` |

---

## 4. 手机推送 / 语音确认后恰好一次

**操作**
1. 通过手机通知 action 或语音说“确认”（同一 `session_id`）。
2. 观察 `lock.unlock` / `switch.turn_on` 等服务调用与审计。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 恰好 1 次服务调用；审计 `dispatch_state=executed` 且 `dispatch_count=1` | 【填：实际】 | 【填：服务调用日志、审计 trace】 | 调用次数 = 1；无第二次下发 |

---

## 5. 重启 HA 后未过期待确认仍可确认

**操作**
1. 重新发起一条危险指令并挂起（不确认）。
2. 重启 Home Assistant。
3. 在 TTL 内调用 `home_voice_safety.list_pending`，再确认。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 重启后 pending 仍在；TTL 内确认成功且只下发 1 次；过期后确认被拒且审计留痕 | 【填：实际】 | 【填：重启前后 list_pending 输出、审计】 | 未过期可恢复；过期不下发 |

---

## 6. 重复确认不重发

**操作**
1. 对上一步已确认的 token 再调用一次 `confirm_action`（或重复同一句危险指令）。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 返回 `deduplicated` / 无效 token；服务调用次数不增加；审计 `suppressed` | 【填：实际】 | 【填：response、审计 trace】 | 下发次数保持 1 |

---

## 7. 审计导出 CSV / JSON

**操作**
1. 调用 `home_voice_safety.export_audit`（`format=csv`，带第 3/4 步的 `trace_id`）。
2. 再导出一次 `format=json`。
3. 打开 `/config/home_voice_safety/exports/` 下文件。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 文件生成；CSV 列含 trace/stage/risk/confirm/dispatch/tool/targets；JSON 可按 trace 还原完整链路 | 【填：实际】 | 【填：文件路径、表头、行数】 | 两种格式均可读；不含明文 token/原始音频 |

---

## 8. 卸载后无残留

**操作**
1. 在“设备与服务”中删除该集成；必要时重启 HA。
2. 检查 service、entity、事件与日志。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| `home_voice_safety.*` service 与 3 个 sensor 消失；无异常日志；DB/导出按选项保留或删除 | 【填：实际】 | 【填：service 列表、实体列表、日志】 | 无残留 service/entity；无报错 |

---

## 9. 断网仍可本地决策

**操作**
1. 断开 WAN（保留 HA 与设备所在 LAN）；解析器保持 `rule`。
2. 重复第 3、4 步的挂起与确认。
3. 如条件允许，用抓包工具确认无外网连接。

| 预期 | 实际 | 证据 | 通过标准 |
|---|---|---|---|
| 断网后仍能挂起并确认；设备只走本地 Home Assistant；无云端调用 | 【填：实际】 | 【填：操作截图、抓包/日志】 | 决策链可用；无出网流量 |

---

## 结果汇总

| 步骤 | 通过（是/否） | 失败现象 | 备注 |
|---|---|---|---|
| 1 安装 | 【填】 | 【填】 | |
| 2 向导 | 【填】 | 【填】 | |
| 3 挂起 | 【填】 | 【填】 | |
| 4 确认一次 | 【填】 | 【填】 | |
| 5 重启恢复 | 【填】 | 【填】 | |
| 6 重复确认 | 【填】 | 【填】 | |
| 7 审计导出 | 【填】 | 【填】 | |
| 8 卸载 | 【填】 | 【填】 | |
| 9 断网 | 【填】 | 【填】 | |

> 9 步已全部执行并附证据（stable 2026-11-03、2025.1、2025.2.4 均复用同脚本，且三者均已补齐附加 3/3 + 蓝图 3/3 + soak 500+50），README 的“HA 真机联调”已按证据更新；下方表格保留为执行模板。