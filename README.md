# home-voice-safety-layer · 本地优先智能家居语音助手的**决策与安全层**

> 一句话：不做唤醒词、不做硬件、不碰厂商云，只做
> **本地语音 → 意图 → 带命名空间的工具调用 → 危险动作确认门禁 → 幂等下发 → 全程留痕** 这条决策链，
> 并用 68 条分级评测集与 16 条变异测试证明它可信。
>
> 定位：单人自主实现的开源作品集。面试/评审可以直接 clone 下来自己跑，不需要任何家庭设备、麦克风或 GPU。

---

## 1. 项目简介

本项目只占一层位置：**在"听懂一句话"和"设备真的动起来"之间**，负责判断该不该动、动之前要不要问人、
重复说一遍会不会动两次、以及事后能不能查。

| 别的项目常见的做法 | 本项目给出的 |
| --- | --- |
| 接了某某生态 | 只走本地 Home Assistant 社区集成，明确不承诺厂商云接入 |
| 识别率/延迟的单个漂亮数字 | 分段实测 + 三态口径（实测 / 设计值 / 未实测） |
| "支持很多设备" | 17 个受控实体逐台标注真实/模拟，危险实体一律保持模拟 |
| demo 视频 | `python -m hvsl.cli demo` 可盲跑，13 步输出与预期逐条比对 |

数据流（一句话）：

```
文本/音频 → Intent(有序步骤) → ToolRegistry 校验 → TargetResolver 定实体
        → RiskPolicy 定级 ─┬─ low    → 幂等抢锁 → 先写留痕 → 后端下发 → 回写结果
                           ├─ confirm→ 挂起为 PendingAction（此时一个字节都不下发）
                           │            └─ confirm(token) 通过后才走上面那条链
                           └─ deny     → 拒绝 + 留痕
```

---

## 2. 核心功能

| 能力 | 实现要点 | 代码位置 |
| --- | --- | --- |
| 意图 → 工具调用 | 28 个工具，全部带 `hvs.` 命名空间前缀；参数由 Pydantic 模型约束并生成 JSON Schema（`additionalProperties=false`） | `hvsl/tools.py` |
| 设备寻址 | 中文设备词 → `entity_id`；歧义与无匹配都是**显式结果**，不猜 | `hvsl/resolver.py` |
| 风险定级 | 声明式策略 → `low` / `confirm` / `deny`；8 个危险类别 + 2 条拒绝话题 | `hvsl/risk.py`、`hvsl/policy/risk_policy.json` |
| 危险动作确认门禁 | 命中 `confirm` 时**整条计划**挂起，单用途令牌 + TTL + 会话绑定；确认后按计划顺序一次性下发 | `hvsl/gate.py`、`hvsl/engine.py` |
| 幂等下发 | 键 = `session+tool+args+targets`；SQLite 事务内抢锁；结果未知一律不重发 | `hvsl/idempotency.py` |
| 可审计 | 先写后回写，按 `trace_id` 反查；默认脱敏用户原话 | `hvsl/audit.py`、`hvsl/store.py` |
| 引擎层护栏 | 定时类请求直接拒绝；没说"都/全部"就钳掉批量猜测；类指词必须命中已评审词表 | `hvsl/guards.py` |
| 语音载体 | 本地 ASR（faster-whisper）、规则/LLM 解析、本地 TTS | `pipeline/asr.py`、`pipeline/nlu.py`、`pipeline/tts.py` |
| Home Assistant 集成 | 6 个服务 + 3 个只读 sensor + 确认事件 + 蓝图 | `custom_components/home_voice_safety/` |
| 评测与测试 | 68 条分级评测集、16 条变异测试、报告由 JSON 渲染且与 md 字节比对 | `evals/`、`tests/`、`reports/` |

---

## 3. 安装与运行

### 3.1 环境要求

- Python **>= 3.11**（`pyproject.toml` 的 `requires-python`）。
- 包管理器推荐 [uv](https://docs.astral.sh/uv/)；`pip install -e .` 亦可。
- 默认路径不需要 Home Assistant、不需要麦克风、不需要 GPU：后端是内置模拟实体，解析器是离线确定性规则。
- Windows / macOS / Linux 均可运行；HA 真机验证只在 Docker（Linux 容器）里做过。

### 3.2 安装

```bash
git clone https://github.com/young0617-02/home-voice-safety-layer.git
cd home-voice-safety-layer

uv sync --dev                  # 默认形态：CLI + 模拟后端 + 测试 + ruff/mypy
uv sync --extra asr            # 可选：本地 ASR（faster-whisper + av + opencc + pypinyin）
uv sync --extra serve          # 可选：本机 HTTP 接口（fastapi + uvicorn）
```

pip 备选（`[dev]` 见 `pyproject.toml` 的 optional-dependencies）：

```bash
pip install -e ".[dev]"
```

安装后可用 `hvsl` 入口（`pyproject.toml` 的 `[project.scripts]`），等价于 `python -m hvsl.cli`：

```bash
uv run hvsl doctor
```

### 3.3 三条运行路径

| 路径 | 启动方式 | 适用 |
| --- | --- | --- |
| CLI | `uv run hvsl chat` / `listen` / `demo` / `audit` | 本机验证全部决策逻辑，零外部依赖 |
| HTTP | `uv sync --extra serve` 后 `uv run hvsl serve` | 仅监听 `127.0.0.1:8777`，绑定非本机地址会拒绝启动 |
| Home Assistant | 把 `custom_components/home_voice_safety/` 拷进 HA 的 `custom_components/`（或经 HACS） | 真机集成，需 HA >= 2025.1.0（见 `hacs.json`） |

> 集成自带 `vendor/` 源码快照，HACS 安装**不需要**再 `pip install hvsl`。
> 改了 `hvsl/` 或 `pipeline/` 必须重跑 `python scripts/sync_vendor.py`，否则 HA 里跑的还是旧代码。

### 3.4 环境自检

```bash
uv run hvsl doctor            # 后端 / 实体数 / 工具数 / 危险类别 / TTL / 去重窗口 / 解析器
uv run hvsl doctor --with-asr # 追加 faster-whisper 探测
uv run hvsl doctor --with-tts # 追加本地 TTS 探测
```

`doctor` 输出 JSON，可接进脚本做环境闸门（例：`entities_real`、`tools_registered`、`danger_classes`）。

---

## 4. 主要配置

配置来源优先级：**进程环境变量 > `.env` > `Settings` 默认值**（`hvsl/config.py::Settings.load`）。
仓库只提交 `.env.example`；`.env` 已在 `.gitignore` 里。

### 4.1 `.env` 中当前**生效**的键

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `HVSL_DB_PATH` | `var/decisions.db` | 审计与幂等 SQLite 位置（相对路径按仓库根解析） |
| `HVSL_POLICY_PATH` | `hvsl/policy/risk_policy.json` | 风险策略；改它等于改安全边界，需评审 |
| `HVSL_DEVICE_CATALOG` | `devices/devices.json` | 已评审设备清单，`danger:*` 标签的唯一来源 |
| `HVSL_CONFIRM_TTL_SECONDS` | `120` | 确认有效期；调小=更容易因来不及确认而作废 |
| `HVSL_DEDUP_WINDOW_SECONDS` | `30` | 幂等去重窗口；调小=重复指令更容易重复下发 |
| `HVSL_AUDIT_RETENTION_DAYS` | `30` | 审计保留天数，`0`=永久；负数会被显式拒绝 |
| `HVSL_STORE_UTTERANCE` | `1` | 是否把用户原句写入审计（`0` 则写 `[redacted]`） |
| `HVSL_DISPATCH_TIMEOUT_SECONDS` | `8` | 单次后端下发超时 |
| `HVSL_NLU_PROVIDER` | `rule` | `rule`=离线确定性；`llm`=本地 Ollama，失败自动降级回 `rule` |
| `HVSL_NLU_TIMEOUT_SECONDS` | `12` | LLM 解析超时 |
| `HVSL_LLM_CROSS_CHECK` | `true` | LLM 路径的影子纠偏；关掉会削弱歧义档护栏，不建议 |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | 本地 Ollama 地址；非 loopback 且未显式开闸时，构造解析器即抛错 |
| `OLLAMA_MODEL` | `qwen2.5:7b` | 本地模型名 |
| `HVSL_ALLOW_CLOUD_LLM` | `false` | 文本出机器的显式开关 |
| `HA_BASE_URL` | 空 | 用 HA REST 后端时的实例地址 |
| `HA_TOKEN` | 空 | HA 长期访问令牌（**不要提交**） |
| `HA_REAL_ENTITIES` | 空 | 可联调的真实 `entity_id`，逗号分隔；未列出的按模拟标注 |
| `ASR_PYTHON_BIN` | 空 | 装有 faster-whisper 的解释器；留空用当前解释器 |
| `ASR_MODEL_DIR` | 空 | 模型权重目录，加载强制 `local_files_only=True` |
| `ASR_DEVICE` | `cpu` | `cuda` / `cpu` |
| `TTS_WAV_OUT` | `var/tts_last.wav` | TTS 合成落盘位置 |

### 4.2 尚未接入的键（待补充）

以下键出现在 `.env.example`，但 `Settings.load()` 目前**没有读取**，改了对运行没有影响：

| 键 | 现状 |
| --- | --- |
| `HVSL_SERVER_HOST` / `HVSL_SERVER_PORT` | 只用于 `server.py` 的报错提示；实际监听地址来自 `Settings` 默认值 `127.0.0.1:8777` |
| `HVSL_IN_FLIGHT_STALE_SECONDS` | 未读取；陈旧 `in_flight` 的判定用固定语义而不是该值 |
| `ASR_MODEL_SIZE` | 未读取；模型由 `ASR_MODEL_DIR` 指向的权重决定 |

统一标记：无法从代码确认的事项在本文用 **待补充** 标注，其余待填位置用 `【填：…】`，两者都可全文搜索。

### 4.3 Home Assistant 向导配置

| 项 | 说明 |
| --- | --- |
| 危险设备勾选 | 向导从 HA 实体里枚举可控实体；勾选者写入 `danger:user_selected`，**只升不降** |
| `confirm_ttl` / `dedup_window` | 覆盖默认 120s / 30s |
| `nlu_provider` | `rule` 或 `llm` |
| `real_entities` | 真实设备 `entity_id`，未列出的按模拟标注 |
| `notify_service` | 可选，确认推送服务名；不填则只发事件 + 蓝图 |
| `remove_data` | 卸载时是否删 DB，默认 **false**（保留审计） |

---

## 5. 使用示例

### 5.1 CLI：跑一句话，并完成危险动作确认

```bash
uv run hvsl chat
# 你说> 打开客厅大灯                 → executed（低风险直接下发）
# 你说> 解锁入户门锁                 → awaiting_confirmation，给出 token 与 TTL
# 你说> /pending                     → 列出待确认动作
# 你说> /confirm <token>             → executed（此时才真正下发）
# 你说> /cancel <token>              → cancelled（不下发）
# 你说> /audit <trace_id>            → 按 trace 打印完整留痕
```

不使用交互式 shell 时：

```bash
uv run hvsl demo                         # 13 步盲跑，退出码 0 = 全部符合预期
uv run hvsl demo --out var/demo_run.json # 结果落盘
uv run hvsl audit --limit 20             # 看最近 20 条留痕
uv run hvsl audit --trace <trace_id>     # 反查一次请求
uv run hvsl tools                        # 打印 28 个工具与 JSON Schema
uv run hvsl say "门锁已确认但未下发" --loud  # 本地 TTS；不加 --loud 不外放
uv run hvsl listen var/clip18.wav --no-tts  # 音频 → 本地 ASR → 决策链
```

### 5.2 HTTP（需 `--extra serve`）

```bash
uv run hvsl serve
```

```bash
curl -s -X POST http://127.0.0.1:8777/v1/utterance \
  -H 'content-type: application/json' \
  -d '{"text":"解锁入户门锁","session_id":"living-room"}'

# 危险动作会返回 confirmation_token，且 dispatch_count=0
curl -s -X POST http://127.0.0.1:8777/v1/confirm \
  -H 'content-type: application/json' \
  -d '{"token":"<上一步的 token>","session_id":"living-room"}'

curl -s http://127.0.0.1:8777/v1/pending
curl -s "http://127.0.0.1:8777/v1/audit?trace_id=<trace>"
```

路由共 7 个：`GET /v1/health`、`GET /v1/devices`、`GET /v1/pending`、`GET /v1/audit`、
`POST /v1/utterance`、`POST /v1/confirm`、`POST /v1/cancel`。
契约核心是与框架无关的 `hvsl.server.handle_request`，FastAPI 只是薄适配层。

### 5.3 Home Assistant 服务

```yaml
service: home_voice_safety.handle_utterance
data:
  text: 解锁入户门锁
  session_id: living-room
```

随后用 `home_voice_safety.confirm_action`（可只给 `token`）或 `home_voice_safety.cancel_action` 收尾。
6 个服务：`handle_utterance`、`confirm_action`、`cancel_action`、`list_pending`、`export_audit`、`purge_audit`。
每次决策广播 `home_voice_safety_decision`；挂起危险动作额外触发 `home_voice_safety_confirmation_required`（含 token、目标、风险类别、过期时间、口语化文案）。

---

## 6. 目录结构

```
home-voice-safety-layer/
├── hvsl/                        # 核心可安装包（不依赖 HA、不依赖网络）
│   ├── models.py                # DeviceEntity / PlannedCall / Intent / ResolvedStep / PendingAction / Decision / AuditRecord
│   ├── tools.py                 # 工具注册表：命名空间前缀 + Pydantic 参数模型 → JSON Schema
│   ├── risk.py                  # 风险判定：声明式策略 → low / confirm / deny（含未评审实体兜底）
│   ├── policy/risk_policy.json  # 危险规则 9 条 + 拒绝话题 2 条（改这里 = 改安全边界）
│   ├── resolver.py              # 中文设备词 → entity_id（歧义/无匹配是显式结果）
│   ├── gate.py                  # 确认门禁：待确认动作、TTL、单用途令牌、会话绑定
│   ├── idempotency.py           # 幂等：SQLite 事务内抢锁，结果未知不重发
│   ├── audit.py / store.py      # 留痕：先写后回写、按 trace 反查、保留期清理
│   ├── guards.py                # 引擎层护栏：定时类拒绝、批量钳制、类指词词表校验
│   ├── backends/                # simulated / home_assistant(REST) / HA 内本地
│   ├── engine.py                # 决策链编排
│   └── app.py / cli.py / server.py / config.py / catalog.py
├── pipeline/                    # 语音载体：asr（faster-whisper）/ nlu（规则 + LLM）/ tts（本地）
├── custom_components/home_voice_safety/   # HA 集成（含 vendor/ 源码快照与 services.yaml）
├── devices/
│   ├── devices.json             # 已评审设备清单（danger:* 与 domain_words 的唯一来源）
│   └── inventory.md             # 由 scripts/gen_inventory.py 生成，禁止手改
├── evals/                       # golden_set_v1.jsonl（68 条）+ judge.py
├── tests/                       # 41 个 test_ 模块 + conftest.py：红线用例 + 契约测试 + 文档口径守卫
├── scripts/                     # mutate / measure_latency / write_reports / sync_vendor / gen_inventory / ha_* 冒烟
├── demo/                        # 13 步无障碍场景脚本
├── reports/                     # 实测产物（JSON 原始 + Markdown 渲染）
├── docs/                        # 方案、验证手册、HA 冒烟记录、脱敏与发布记录
├── var/                         # 运行产物（已 gitignore：DB / 日志 / 音频 / 本地 HA 配置）
└── pyproject.toml / uv.lock / requirements.txt / hacs.json / LICENSE
```

说明：方案原本把 `safety.py` / `audit.py` 放在集成目录下。本项目的**实现**在可安装的 `hvsl/` 里，
集成侧只做再导出与 HA 适配——目的是一台没装 HA 的机器也能跑测试、评测与变异测试。
`custom_components/home_voice_safety/safety.py` 与 `audit.py` 仍存在，且由 `tests/test_integration_backend.py` 断言与核心包是同一对象。

---

## 7. 测试方式

```bash
uv run pytest -o addopts="" -q     # 全量：本地实测 542 passed, 2 skipped（2026-11-23）
uv run pytest -k safety_gate -q    # 单组：危险动作门禁
uv run ruff check .                # lint（CI 同款）
uv run mypy hvsl pipeline custom_components  # 类型检查（CI 同款；本机基线 0 errors）
```

> `pyproject.toml` 的 `addopts = "-q --no-header"`；CI 里用 `-o addopts=""` 覆盖，
> 本地直接 `uv run pytest -q` 也可以。2 条 skip 是：需要真 Ollama 的实时用例、
> 以及 FastAPI TestClient 与本机依赖冲突的用例。

评测与产物（数字必须由产物生成，不允许手改）：

```bash
uv run python evals/judge.py -v                  # 68 条分级评测 → reports/评测结果.json + .md
uv run python evals/judge.py --provider llm -v   # 同一套用例换本地 Ollama → reports/评测结果-llm.json
uv run python scripts/mutate.py                  # 注入 16 个缺陷，验证测试套件会响 → reports/变异测试.json
uv run python scripts/write_reports.py           # 由 JSON 重渲 reports/*.md（禁止手改 md）
uv run python scripts/gen_inventory.py --check   # devices/inventory.md 与 devices.json 是否一致
uv run python scripts/sync_vendor.py --check     # 集成 vendor/ 快照是否漂移
uv run pytest -q tests/test_doc_claims.py        # README / 报告的措辞与数字守卫
```

---

## 8. 性能与评测口径（三态标注）

方案里 `RTF < 1`、`说完→下发 P95 ≤ 3 s` 都只是**设计值**。本项目把它们拆成可核的三类：

| 指标 | 状态 | 数字与口径 |
| --- | --- | --- |
| 危险动作正确拦截率 | **实测** | 规则解析 **100%**（27/27），越权下发 **0**；LLM 解析 96.3%，越权下发仍 **0** —— `reports/评测结果.md` |
| 工具调用首次成功率 | **实测** | 规则解析 **100%**；本地 Ollama(qwen2.5:7b) **62.07%**，另有 **37.93%** 被确定性纠偏后才可用 |
| 决策链分段延迟 | **实测** | 模拟后端 + 规则解析：意图解析 P95 **1.278 ms**、门禁+下发 P95 **0.015 ms** |
| ASR 分段延迟与 RTF | **实测**（本机 RTX 4060，large-v3/float16/VAD） | 18s 短音频：加载 3.13s、解码 0.81s、**decode RTF 0.045**；整段 81 分钟：**RTF ≈ 0.10~0.12** → `reports/语音链路分段实测.md` |
| ASR 字错率 CER | **实测**（仅本机 TTS 合成语音，n=10 已知文本） | 主口径（数字等价归一化后）：tiny **0.1345**、small **0.0536**、medium **0.0111**；原样口径 tiny **0.207**、small **0.1511**、medium **0.1086** → `reports/ASR-CER.md`。真人无人工对齐参照，**未实测** |
| TTS 合成延迟 | **实测**（本地 SAPI，落 WAV） | 同上报告；默认不外放 |
| 唤醒率 / 误唤醒 / 远场 | **未实测，且不在范围内** | 本项目不做唤醒词，因此不提供这些数字 |
| 端到端 P95 ≤ 3 s | **设计值**，冷启动口径下实测不成立 | 新起进程 4.14s（加载占 3.13s）；模型常驻约 0.81s；接真实设备网络往返后需重测 |

### 8.1 解析器对照（同一套 68 条分级评测集：single 19 / multi 12 / ambiguous 10 / danger 27 条，只换解析器）

| 解析器 | 通过 | 危险档 | 越权下发 | 首次可用率 | 意图解析 P95 |
| --- | --- | --- | --- | --- | --- |
| `rule`（默认，离线确定性） | **68 / 68** | 27 / 27 | **0** | 100% | 0.25 ms |
| `llm`（本机 Ollama qwen2.5:7b + 影子纠偏） | 67 / 68 | 26 / 27 | **0** | 62.07%（纠偏 37.93%） | 1901 ms |

读法：
- 危险档唯一的失败是模型给"把大门打开"选了开窗帘的工具，被清单词表拒绝——系统没有执行任何东西：
  这是能力缺口（少做），不是安全问题（多做）。
- LLM 路径下 37.93% 的方案是被确定性解析器纠偏后才可用的，这就是纠偏必须默认开的原因。
- LLM 意图解析 P95 约 1.9 s，比规则路径慢四个数量级；对"说一句就动"的场景这是真实体验成本。

> Measured on this repo (simulated backend, rule parser): 68 graded cases with 27 danger cases,
> **100% interception, 0 misdispatches**, and **16/16 injected defects caught** by the test suite.

---

## 9. 隐私与数据边界

家庭语音属于**高敏感**个人信息（行踪、居住、健康相关）。所以本项目把边界做成代码约束，而不是承诺：

1. **音频只在本地流转**：ASR 走本机 faster-whisper，模型加载强制 `local_files_only=True`，
   子进程环境变量置 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`；不实现任何云端转写。
2. **TTS 也是本地**：Windows SAPI / macOS `say` / `espeak-ng`，结果落 WAV；默认静音，播放需显式 `--loud`。
3. **文本出机器要显式开闸**：解析器默认 `rule`；用本地 Ollama 也只走 loopback。
   解析端点非 loopback 且未设 `HVSL_ALLOW_CLOUD_LLM=true` 时，**构造解析器阶段就抛错**。
4. **接口只听本机**：`serve` 绑定非 `127.0.0.1` / `localhost` / `::1` 会被拒绝启动；HTTP 接口没有鉴权，不要暴露到局域网。
5. **留痕在本地磁盘**：SQLite 单文件，不上传、不聚合、**不外发**；`export_audit` 默认脱敏用户原话。
6. **sensor 不带敏感字段**：状态属性只有计数与 `risk_class` / `session_alias` / `expires_in` / `trace_id_hash`，token 与原句绝不进 HA recorder。
7. **可自证**：`tests/test_privacy.py` 把 `socket.socket` / `create_connection` / `getaddrinfo` 换成抛错后，
   仍完整跑完"危险动作挂起 → 确认 → 下发 → 留痕"。

想复核出网行为：

```bash
uv run hvsl chat     # 决策链运行时不应出现任何出网连接（Windows 可用 Process Monitor 过滤 tcpout）
```

---

## 10. 未覆盖清单

| 缺口 | 现状 | 影响 |
| --- | --- | --- |
| Home Assistant 实机联调 | stable **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS**；2025.1 与 2025.2.4 同级全 PASS；2024.6 不兼容（自带 pydantic 1.10，集成要求 >=2.5）；2025.3+ 未测 | 9 步脚本覆盖的链路已验证，不等于 HA 全场景已验证 |
| 真实设备 | 本轮 **0 台**（17 个受控实体全为模拟） | 无法声称真实家庭环境下的可用性/延迟 |
| ASR 字错率 | 仅合成语音 n=10 已实测；真人/家庭噪声**未实测** | 真人、远距离、噪声场景不给数字 |
| 麦克风实时采集 | 未实现（输入是音频文件） | "说完→下发"中的麦克风段未计入 |
| notify 真投递 | HA 2026 的 legacy `file` 与 `rest` 平台均不可用，只有 `mobile_app` 未验证 | 确认推送只能靠事件 + 蓝图，不宣称手机通知已验证 |
| 方言、远场、噪声 | 不在范围 | 不声称 |
| 定时任务 / 场景 / 状态查询 | 显式拒绝 | 只覆盖即时设备指令 |
| 多语言 | 中文词表 | 英文指令不保证 |
| 认证 / 加密 / 多用户 | HTTP 无鉴权，仅限本机 | 不要暴露到局域网 |
| 相对调节覆盖度 | 已支持（±10%/次、按当前值 clamp、读不到状态即 0 下发），HA 真机 7/7 PASS，但**未纳入 68 例评测集** | 并发与真实物理设备未测 |

诊断、迁移与卸载边界：

- **诊断**：HA「下载诊断」只导出配置路径、调参值、`engine.stats()` 与 sensor 聚合；不含 token、原句或完整对话。
- **迁移**：配置条目 v1→v2 只补默认 options；资源缺失时迁移失败并保持 v1，日志带 `reason=migration_*`，可补路径后重试。
- **卸载**：默认保留 DB 以便审计；只有显式勾选 `remove_data` 才删 DB 与 `-wal`/`-shm`。多条目共存时，最后一个条目卸载才移除服务。
- **保留期**：`purge_audit` 按 `HVSL_AUDIT_RETENTION_DAYS` 清理；`failed_unknown` / `in_flight_unknown` 证据永不删除。

---

## 11. 贡献说明

本项目是单人作品，但欢迎以**可验证**的方式参与：

1. **提交前必须自测**（命令见 §7）：`pytest`（含 `-o addopts=""`）、`ruff check .`、`mypy hvsl pipeline custom_components`。
2. **不得放宽红线**：危险动作确认门禁、幂等不重发、未评审实体兜底、隐私边界（音频不出本机）。
   相关断言在 `tests/test_safety_gate.py`、`tests/test_idempotency.py`、`tests/test_privacy.py`、`tests/test_unreviewed_entity.py`。
3. **改 `hvsl/` 或 `pipeline/` 必须同步 vendor**：`python scripts/sync_vendor.py`，提交前跑 `--check`。
4. **改设备清单必须重新生成清点**：`python scripts/gen_inventory.py`，不要手改 `devices/inventory.md`。
5. **不要在报告里手改数字**：`reports/*.md` 由 `scripts/write_reports.py` 从 JSON 渲染，`tests/test_reports.py` 做字节比对。
6. **文档不得夸大**：`tests/test_doc_claims.py` 会 grep 未实测指标、厂商接入表述与数字漂移；
   新增未实测结论时按"实测 / 设计值 / 未实测"三态标注。
7. **提交信息**用中文概述"为什么要改"，一次提交只做一件事；不要提交 `.env`、`var/`、音频或数据库。

问题与建议请走仓库的 issue tracker；涉及安全红线的改动请先开 issue 说明边界影响。

---

## 12. 许可证

MIT（见 [`LICENSE`](LICENSE)）。与 Home Assistant 生态兼容。
本项目为单人自主实现的开源作品，全部演示设备为模拟实体；危险动作相关的实体全部保持模拟，不接任何真实负载。

生态参考：Home Assistant（本地集成宿主）、faster-whisper（本地 ASR）、Ollama（可选本地解析）。

---

## 13. 待补充清单

以下事项在当前仓库里**无法由代码或产物确认**，统一用 **待补充** 标注：

| 项 | 待补充的内容 |
| --- | --- |
| HA 版本上限 | 2025.3 及以后版本未测，`hacs.json` 只声明最低 2025.1.0 |
| notify 真投递 | 需要一台真实手机 + `mobile_app` 才能验证，当前只有事件与蓝图路径的结论 |
| 真实设备 | 待补充可联调设备清单；决定 `devices/devices.json` 里哪几台可以改成 `real` |
| 真人语音 CER | 需要逐条人工校对参照，当前只有 TTS 合成语音的 CER |
| 独立文档站 | 【填：文档站地址】——当前所有文档都在 `docs/` 目录内 |
| `hvsl.__version__` | 包内为 `0.1.0`，与 `pyproject.toml` / `manifest.json` 的 `0.2.0` 不一致；待补充以哪一处为准（当前以 `0.2.0` 为对外版本） |
| 4 个 `.env` 键 | 见 §4.2：`HVSL_SERVER_HOST`、`HVSL_SERVER_PORT`、`HVSL_IN_FLIGHT_STALE_SECONDS`、`ASR_MODEL_SIZE` 未接入 |
| Linux/macOS 本机验证 | 开发与真机验证都在 Windows + Docker 上完成；macOS/Linux 直跑为待补充 |