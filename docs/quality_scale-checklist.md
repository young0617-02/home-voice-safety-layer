# Quality Scale 自检（bronze）

> 结论口径：**`bronze` 是保守声明，尚未通过 Home Assistant 官方 hassfest / Quality Scale 全项核验。**
> 本清单只记录本地可核对的证据；标“部分/未核验”的项目不得对外写成“已通过官方 bronze”。

核对日期：2026-11-02。参考：HA Quality Scale 的 bronze 条目与当前仓库实现。

| # | Bronze 项 | 状态 | 本地证据 | 说明 |
|---|---|---|---|---|
| 1 | `action-setup` / config flow | 本地已满足 | `manifest.json` `config_flow: true`；`config_flow.py:245-335`；`tests/test_config_flow.py` | 用户/导入两条入口都走 `_validate_input` |
| 2 | `unique-config-entry` | 本地已满足 | `config_flow.py:312-313`；`tests/test_config_flow.py` | 以 `DOMAIN-config_dir` 作 unique id，重复配置会 abort |
| 3 | `config-entry-unloading` | 本地已满足 | `__init__.py:126-172`；`tests/test_lifecycle.py` | 最后一个 entry 卸载才移除 service；`hass.data.get` 防 KeyError |
| 4 | `config-entry-migration` | 本地已满足 | `__init__.py:174-245`；`tests/test_lifecycle.py` | v1→v2 additive；失败保持 v1，日志带 `reason=migration_*` |
| 5 | `integration-owner` | 本地已满足 | `manifest.json` `codeowners` | 已填真实 owner `@young0617-02` |
| 6 | `issue-tracker` | 本地已满足 | `manifest.json` `issue_tracker` | 已指向 `https://github.com/young0617-02/home-voice-safety-layer/issues` |
| 7 | `documentation` | **部分** | `manifest.json` `documentation`；`README.md` | URL 已指向真实仓库；无独立文档站 |
| 8 | `dependency-transparency` | **部分** | `manifest.json` `requirements`；`vendor/MANIFEST.sha256` | pydantic/httpx 显式声明；`hvsl/pipeline` 以 vendor 随集成分发，未发布到包索引 |
| 9 | `loggers` | 本地已满足 | `manifest.json` `loggers` | `custom_components.home_voice_safety` |
| 10 | `quality-scale` | 本地已满足（仅声明） | `manifest.json` `quality_scale: bronze` | 声明为 bronze；官方分级未核验 |
| 11 | `appropriate-polling` | 本地已满足 | `manifest.json` `iot_class: local_polling`；`coordinator.py:63-72` | 事件驱动 + 轮询兜底 |
| 12 | `entity-unique-id` | 本地已满足 | `sensor.py:45`；`tests/test_sensor_privacy.py` | `entry_id-key` 稳定唯一 |
| 13 | `has-entity-name` | 本地已满足 | `sensor.py:40` | 三个 sensor 均启用 |
| 14 | `device-info` | 本地已满足 | `sensor.py:48-54`；`tests/test_manifest.py` | identifiers/name/manufacturer/model/sw_version |
| 15 | `diagnostics` | 本地已满足 | `diagnostics.py:63-109`；`tests/test_lifecycle.py` | 白名单输出，不含 token/原话；官方分级待核验 |
| 16 | `translations` | **部分** | `strings.json`、`translations/en.json`、`translations/zh-Hans.json` | 有 config/options 文案；未跑 hassfest 翻译校验 |
| 17 | `tests` | **部分** | `tests/` 全套；`tests/test_lifecycle.py`、`test_manifest.py` | 本机全量绿；ConfigFlow 用轻量 stubs，未接官方 HA 测试框架 |
| 18 | `brands` | **部分** | `brand/README.md` | 只有放置说明，尚无 `icon.png`/`logo.png` |
| 19 | `docs-high-level-description` | **部分** | `README.md` §1、§2 | 无独立文档站 |
| 20 | `docs-installation-instructions` | **部分** | `README.md` §1；`hacs.json` | 有 HACS/手动安装说明，未在官方文档站发布 |
| 21 | `docs-removal-instructions` | 本地已满足 | `README.md` §10.1 | 卸载、`remove_data`、DB 保留已写明 |
| 22 | `runtime-data` | 未核验 | `__init__.py` 使用 `hass.data` | 未采用 HA `entry.runtime_data` 模式 |
| 23 | `reconfiguration-flow` | 未核验 | `config_flow.py` options flow | 无官方 `reconfigure` 步骤；属更高等级条目 |
| 24 | `discovery` | 不适用 | — | 本集成本轮不做自动发现 |

## 未核验 / 发布前必须补

1. 跑官方 `hassfest` 与 HACS validation，按实际结果修正本表。
2. ~~替换 `codeowners`、`documentation`、`issue_tracker` 三处 `【填：】` 占位~~ — 已完成（`@young0617-02`）。
3. 补 `brand/icon.png`（256×256）与 `brand/logo.png`（512×512）。
4. 安装并使用官方 HA 测试依赖，替代 ConfigFlow 的轻量 stubs 跑一次生命周期测试。
5. 在真实 HA 上完成 `scripts/ha_smoke.md` 的 9 步，并附证据；已完成（stable 2026-11-03 9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1；2025.1 同级；2025.2.4 同级），证据见 `docs/HA真机冒烟结果-v0.1.md` 与 `var/ha-*/results*.json`。

> 在以上 5 项完成前，`manifest.json` 的 `quality_scale: bronze` 只能理解为**保守目标声明**，不是官方认证结论。