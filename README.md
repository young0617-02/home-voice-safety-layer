# home-voice-safety-layer · 本地优先智能家居语音助手的**决策与安全层**

> 一句话：不做唤醒词、不做硬件、不碰厂商云，只做
> **本地语音 → 意图 → 带命名空间的工具调用 → 危险动作确认门禁 → 幂等下发 → 全程留痕**
> 这条决策链，并用 68 条分级评测集与 16 条变异测试证明它可信。
>
> 定位：**开源作品集**，单人自主实现。面试/评审可以直接 clone 下来自己跑，不需要任何家庭设备。

---

## 1. 30 秒跑起来

```bash
git clone <你的仓库地址>
cd home-voice-safety-layer
uv sync --dev                  # 默认形态：CLI + 模拟后端 + 测试
# 可选：真实本地 ASR（faster-whisper + av）
uv sync --extra asr

python -m pytest -q            # 全部测试（含危险动作/幂等红线用例）
python evals/judge.py -v       # 68 条分级评测 + 拦截率/误下发/分段延迟
python scripts/mutate.py       # 注入 16 个缺陷，验证测试套件真的会响
python -m hvsl.cli doctor      # 环境自检（后端/工具/危险类别/ASR/TTS）
python -m hvsl.cli demo        # 无障碍场景演示脚本，13 步，退出码 0 = 全部符合预期
python -m hvsl.cli chat        # 自己说一句：「解锁入户门锁」→ 看它拒绝直接执行
```

> Home Assistant 真机冒烟：2026-11-03 已在 Docker Hub `homeassistant/home-assistant:stable` 上跑出 **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS**；`2025.1` 同级全 PASS，`2025.2.4` 同级全 PASS；2024.6 依赖不兼容（自带 pydantic 1.10.15），2025.3+ 未测。
> 证据见 [`docs/HA真机冒烟结果-v0.1.md`](docs/HA真机冒烟结果-v0.1.md)、[`docs/HA多版本矩阵-v0.1.md`](docs/HA多版本矩阵-v0.1.md)。ASR 合成语音 2/2 PASS；公开真人 4 条 small 可识别但有错字（`opencc` 繁转简 + `pypinyin` target 级同音纠错已接入、错字仍在），medium 已取得并试跑但错字仍在（观察/观光旅游已修正，邮局/邮箱/蜿蜒/白皑皑仍错，未做人工对齐 CER）；仍未覆盖真实设备、notify 真投递（HA 2026 legacy `file`/`rest` 均不可用，仅 `mobile_app` 未验证）。

**不需要 Home Assistant，不需要麦克风，不需要 GPU**：默认后端是内置模拟实体，
默认解析器是离线确定性规则。装好 HA / faster-whisper 之后，改 `.env` 即切到真实链路，
决策层代码一行不用改。

---

## 2. 为什么只做这一层

大厂助手的取向是"尽量把动作执行掉"；而"哪些动作不该被执行"这类门禁会让体验变差，
所以没人做重。这一层恰好是**单人可占、且开源可验证**的位置：

| 别的项目给的 | 这个项目给的 |
| --- | --- |
| 接了某某生态 | 只走本地 Home Assistant 社区集成，明确不承诺厂商云接入 |
| 识别率/延迟的单个漂亮数字 | 分段实测 + 三态口径（实测 / 设计值 / 未实测） |
| "支持很多设备" | 17 个受控实体逐台标注真实/模拟，危险实体一律保持模拟 |
| demo 视频 | `python -m hvsl.cli demo` 可盲跑，输出与预期逐条比对 |

---

## 3. 模块划分

```
home-voice-safety-layer/
├── hvsl/                       # 核心可安装包（不依赖 HA，不依赖网络）
│   ├── models.py               # 数据结构：DeviceEntity / PlannedCall / Intent /
│   │                           #   ResolvedStep / PendingAction / Decision / AuditRecord
│   ├── tools.py                # F-006 工具注册表：命名空间前缀 + Pydantic 参数模型 → JSON Schema
│   ├── risk.py                 # F-007 风险判定：声明式策略 → low / confirm / deny
│   ├── policy/risk_policy.json #   危险动作白名单 + 拒绝话题（改这里 = 改安全边界，需评审）
│   ├── resolver.py             # 中文设备词 → entity_id（歧义/无匹配是显式结果，不是猜）
│   ├── gate.py                 # F-007 确认门禁：待确认动作、TTL、单用途令牌、会话绑定
│   ├── idempotency.py          # F-008 幂等：SQLite 事务内抢锁，结果未知一律不重发
│   ├── audit.py + store.py     # F-009 留痕：先写后回写、按 trace 反查
│   ├── backends/               # F-003 设备抽象：simulated / home_assistant(REST) / HA 内本地
│   ├── engine.py               # 决策链编排（本项目的全部论点在这一个文件）
│   ├── app.py / cli.py / server.py / config.py
├── pipeline/                   # 语音载体：asr(faster-whisper 本地) / nlu(规则+LLM) / tts(本地)
├── custom_components/home_voice_safety/   # F-002 HA 集成（safety.py/audit.py 从核心包再导出）
├── devices/devices.json        # 已评审设备清单（danger:* 标签唯一来源）
├── devices/inventory.md        #   ↑ 由 scripts/gen_inventory.py 生成，禁止手改
├── evals/                      # F-011 分级评测集 + F-013 判分器
├── tests/                      # A1–A13 红线用例 + 契约测试
├── scripts/                    # gen_inventory / mutate / measure_latency / write_reports
├── demo/                       # F-014 无障碍场景脚本（可盲跑）
└── reports/                    # 实测产物（JSON 原始 + Markdown 渲染）
```

数据流（一句话）：

```
文本/音频 → Intent(有序步骤) → ToolRegistry 校验 → TargetResolver 定实体
        → RiskPolicy 定级 ─┬─ low    → 幂等抢锁 → 先写留痕 → 后端下发 → 回写结果
                           ├─ confirm→ 挂起为 PendingAction（此时一个字节都不下发）
                           │            └─ confirm(token) 通过后才走上面那条链
                           └─ deny     → 拒绝 + 留痕
```

### 与方案目录树的差异（有意为之，写明在这）

方案把 `safety.py` / `audit.py` 放在 `custom_components/<domain>/` 下。本项目把它们的**实现**
放进可 pip 安装的 `hvsl/` 包，集成侧只做再导出与 HA 适配。原因：HA 未安装时也要能跑测试、
跑评测、跑变异测试——红线证据不能依赖一个 1.5GB 的镜像。`custom_components/home_voice_safety/safety.py`
与 `audit.py` 仍然存在，且由 `tests/test_integration_backend.py` 断言它们与核心包是同一对象。

---

## 4. 输入输出与接口定义

### 4.1 决策链入口

| 入口 | 输入 | 输出 |
| --- | --- | --- |
| `engine.handle(text, session_id=)` | 一句话 | `Decision` |
| `engine.confirm(token=None, session_id=)` | 确认令牌（省略=该会话恰好一条待确认） | `Decision` |
| `engine.cancel(token=None, session_id=)` | 取消 | `Decision` |
| `engine.trace(trace_id)` | 一次请求的 trace | `list[AuditRecord]` |

`Decision` 关键字段：`kind`（`executed` / `awaiting_confirmation` / `clarification_required` /
`rejected` / `deduplicated` / `cancelled` / `partial`）、`steps[].risk_level`、
`steps[].risk_class`、`confirmation_token`、`dispatch_count`、`outcomes[]`、
`timings_ms{nlu,dispatch,total}`、`trace_id`。

### 4.2 工具层（28 个，全部带命名空间前缀）

工具名形如 `hvs.light.turn_on`、`hvs.lock.unlock`、`hvs.alarm.disarm`，
注册时**强制**前缀校验（HA 2026.9 起强制集成前缀，这里提前做到位）：

```bash
python -m hvsl.cli tools     # 打印全部工具 + JSON Schema（additionalProperties=false）
```

参数由 Pydantic 模型约束并生成 JSON Schema：`entity_ids` 必填非空、`brightness_pct∈[1,100]`、
`temperature_c∈[5,40]`、`volume_level∈[0,100]`、`delta_pct∈[-50,50]`，多余字段一律拒绝。

### 4.3 HTTP 接口（仅本机）

```bash
python -m hvsl.cli serve            # 127.0.0.1:8777，绑定非本机地址会被拒绝启动
POST /v1/utterance {"text": "...", "session_id": "..."}
POST /v1/confirm   {"token": "...", "session_id": "..."}
POST /v1/cancel    {"token": "...", "session_id": "..."}
GET  /v1/devices /v1/pending /v1/audit?trace_id=... /v1/health
```

契约核心是与框架无关的 `hvsl.server.handle_request`，测试直接打它（FastAPI 只是薄适配器）。

### 4.4 HA 集成服务

`home_voice_safety.handle_utterance` / `.confirm_action` / `.cancel_action` / `.list_pending`，
每次决策通过 `home_voice_safety_decision` 事件广播；三个只读 sensor 把
「待确认动作数 / 真实与模拟设备清点 / 决策统计」显示在 HA 界面里。

---

## 5. 关键约束（安全语义）

| 约束 | 具体语义 | 代码位置 | 证据 |
| --- | --- | --- | --- |
| **危险动作必须二次确认** | 8 个危险类别命中 `confirm` 时，**整条计划**挂起，一个实体都不下发；确认后一次性按计划顺序下发 | `hvsl/gate.py`、`hvsl/engine.py::_park` | A1：`tests/test_safety_gate.py` 逐类别遍历 + 评测 27 条危险用例 |
| 燃气开阀 = 直接拒绝 | 不是"确认后放行"，而是拒绝并提示人工现场操作；批量目标里混进燃气阀时**整批拒绝** | `hvsl/policy/risk_policy.json` | `tests/test_risk_policy.py::test_batch_target_including_the_gas_valve_is_refused_entirely` |
| 确认令牌单用途、TTL、会话绑定 | 超时作废（作废≠补发）；同一令牌第二次确认被拒；跨会话确认被拒；会话内多条待确认时裸确认→要求指认 | `hvsl/gate.py::take` | `tests/test_safety_gate.py` |
| **幂等：重复意图只下发一次** | 键 = `session+tool+args+targets`；窗口内重复→抑制；下发前先占锁，**结果未知一律不重发**；只有"证明没送达设备"的失败才释放键 | `hvsl/idempotency.py` | A2：`tests/test_idempotency.py`（含 3 线程并发） |
| 风险判定不采信设备自报 | `danger:*` 标签只读本地已评审清单；HA 设备在状态里自称安全无效 | `hvsl/backends/home_assistant.py::build_entity` | `tests/test_ha_backend.py::test_a_device_cannot_talk_its_way_out_of_the_danger_list` |
| 清单缺失即拒绝启动（fail closed） | HA 内后端读不到已评审设备清单时直接拒绝初始化，而不是"把所有设备当安全设备" | `custom_components/.../backend.py` | `tests/test_integration_backend.py` |
| **护栏与解析器无关**（三件事由引擎判定，模型说了不算） | ① 句子里出现"明天/定时/半小时后"等 → 直接拒绝，不会把"明天的事"当场执行掉；② 没说"都/全部/所有"就取消模型给的批量猜测（`all=true` 被钳回逐个指认）；③ 类指词必须能在设备词表里 hint 到本域设备，"把窗户打开"不会顺手去拉窗帘 | `hvsl/guards.py`、`hvsl/resolver.py::_hints_any_name`、`devices/devices.json` 的 `domain_words` | `tests/test_guards.py`（用一个"假装很确定"的假 LLM 计划验证钳制生效）；变异 M11–M16 专门打这几条 |
| 设备词表是评审对象 | `domain_words` 与 `danger:*` 同在清单里：加一个类指词等于扩大可控范围，要走评审 | `devices/devices.json` | `tests/test_guards.py::test_domain_words_come_from_the_reviewed_catalog` |
| 不承诺厂商云接入 | 只走本地 Home Assistant；策略里把"厂商云端接口/生态账号登录"类请求列为拒绝话题 | `hvsl/policy/risk_policy.json` | `tests/test_doc_claims.py`（文档 grep 命中 0） |
| 音频不出本机 | ASR 本地 faster-whisper（`local_files_only` + `HF_HUB_OFFLINE=1`）；TTS 本地合成落 WAV；外放需 `--loud` | `pipeline/asr.py`、`pipeline/tts.py` | A7：`tests/test_privacy.py`（把 socket 全堵死后决策链仍完整跑通） |
| 定时/场景类请求不假装执行 | 句子里带"明天/定时/预约"等 → 拒绝，而不是当场执行 | `pipeline/nlu.py` | 评测 A07 |

> **为什么默认是规则解析：一次真实教训。** 第一版评测用本地 7B 模型跑同一套用例时，
> 歧义档被"猜穿"了：`把灯打开` 被模型直接补成"全部四盏灯"并下发；`把窗户打开` 被解析成窗帘；
> `调到26度` 被安到了客厅空调；`明天早上七点开灯` 变成了"现在就开灯"。
> 危险动作档当时仍然全部被门禁拦住（拦在解析之后，与解析器无关），但**歧义与拒答档不能靠模型自觉**。
> 所以上表最后三行的护栏被上移到引擎层，并且 `tests/test_guards.py` 用一个"假装很确定"的假解析器来验证它们；
> 评测集默认跑 `rule`，`--provider llm` 作为对照单独出报告。
>
> **加完护栏后重跑同一套（本机 qwen2.5:7b）**：67/68 通过，歧义档 10/10，**越权下发 0**；
> 唯一失败是模型给"把大门打开"选了开窗帘的工具、被清单词表拒绝（少做，不是多做）。
> 37.93% 的方案是被纠偏后才可用的 —— 这就是纠偏必须默认开的原因。

---

## 6. 异常处理与降级

| 情况 | 行为 | 留下的痕迹 |
| --- | --- | --- |
| 未知工具 / 无前缀工具名 | `rejected`，不下发 | `stage=reject` 行含原因 |
| 参数越界或多余字段 | Pydantic 校验失败 → `rejected` | 同上 |
| 目标设备找不到 | `rejected`（"找不到与…匹配的设备"） | 同上 |
| 目标有歧义（多候选） | `clarification_required` + 候选列表，整条计划不下发 | `stage=clarify` |
| 命中确认类危险动作 | `awaiting_confirmation` + 令牌 + TTL 文案 | `confirm(pending)` |
| 命中拒绝类（燃气开阀等） | `rejected`，不进待确认队列 | `confirm(deny)` |
| 确认超时 | `rejected`，明确"未下发" | `confirm(expired)` |
| LLM 解析超时/输出不合法 | **自动降级**到规则解析，`degraded=true` + 原因写进留痕 | `stage=nlu` |
| 后端超时 / 服务未注册 | 超时报 `failed` 且键保持占用（不重发）；未注册服务报"未送达"，允许重试 | `dispatch(failed)` |
| 进程在下发中途崩溃 | 键停在 `in_flight`，后续同一意图永不重发（宁可少做不可多做） | `dispatch(in_flight_unknown)` |

---

## 7. 性能与评测口径（三态标注）

方案 §5 里 `RTF < 1`、`说完→下发 P95 ≤ 3 s` 都标注为**设计值**。本项目把它们拆成可核的三类：

| 指标 | 状态 | 数字与口径 |
| --- | --- | --- |
| 危险动作正确拦截率 | **实测** | 规则解析 **100%**（27/27），误下发 **0**；LLM 解析 96.3%（唯一失败是模型选错工具→系统拒绝，**仍然 0 越权下发**）—— `reports/评测结果.md` |
| 工具调用首次成功率 | **实测** | 规则解析 **100%**；本地 Ollama(qwen2.5:7b) **62.07%**，另有 **37.93%** 被确定性纠偏后才可用（见 §7.1） |
| 决策链分段延迟 | **实测** | 模拟后端 + 规则解析下的 nlu/dispatch/total 分段 P50/P95 —— `reports/评测结果.md` |
| ASR 分段延迟与 RTF | **实测**（本机 RTX 4060，large-v3/float16/VAD） | 18s 短音频：模型加载 3.13s、解码 0.81s、**decode RTF 0.045**；整段 81 分钟：**RTF ≈ 0.10~0.12**。详见 `reports/语音链路分段实测.md` |
| ASR 字错率 CER | **实测**（仅本机 TTS 合成语音，n=10 已知文本） | 主口径（数字等价归一化后）：tiny **0.1345（13.45%）**、small **0.0536（5.36%）**、medium **0.0111（1.11%）**；括号保留原样口径 tiny **0.207（20.70%）**、small **0.1511（15.11%）**、medium **0.1086（10.86%）**。归一化后总参照字数 **75**，S/D/I = **10/0/0**、**3/0/0**、**1/0/0**（pooled CER 0.1333 / 0.04 / 0.0133）；device=cuda / float16 / beam_size=1。逐条双口径见 `reports/ASR-CER.md`。真人 8kHz 无人工对齐参照，**未实测** |
| TTS 合成延迟 | **实测**（本地 SAPI，落 WAV） | 同上文件；不外放 |
| 唤醒率 / 误唤醒 / 远场指标 | **未实测，且不在范围内** | 本项目不做唤醒词（F-004），因此不提供这些数字 |
| 端到端 P95 ≤ 3 s | **设计值**，且**冷启动口径下实测不成立** | 实测：新起进程 = 4.14s（加载占 3.13s）；模型常驻 = 约 0.81s。接真实设备网络往返后仍需重测 |


### 7.1 解析器对照（同一套 68 条用例，只换解析器）

| 解析器 | 通过 | 危险档 | 越权下发 | 首次可用率 | 意图解析 P95 |
| --- | --- | --- | --- | --- | --- |
| `rule`（默认，离线确定性） | **68 / 68** | 27 / 27 | **0** | 100% | 0.25 ms |
| `llm`（本机 Ollama qwen2.5:7b + 影子纠偏） | 67 / 68 | 26 / 27 | **0** | 62.07%（纠偏 37.93%） | 1901 ms |

读法：
- 危险档那一条唯一的失败，是模型给"把大门打开"选了**开窗帘**的工具，被清单词表拒绝——
  系统没有执行任何东西。这是能力缺口（少做），不是安全问题（多做）。
- LLM 路径下 37.93% 的方案是被确定性解析器纠偏后才可用的：这正是要把纠偏做成引擎层默认开的原因。
- LLM 意图解析 P95 约 1.9 s，比规则路径慢四个数量级；对讲一句就动的场景，这是真实的体验成本。

---

## 8. 验收标准对照（A1–A13）

| 编号 | 项 | 状态 | 复跑命令 |
| --- | --- | --- | --- |
| **A1 ★** | 危险动作必须确认后下发 | **已验证**（100% 拦截，0 误下发，有留痕） | `pytest tests/test_safety_gate.py` + `python evals/judge.py` |
| **A2 ★** | 幂等：重复意图不重复执行 | **已验证**（重放 3 次只下发 1 次，含并发） | `pytest tests/test_idempotency.py` |
| **A3 ★** | 不宣称厂商官方接入 | **已验证**（文档 grep 命中 0） | `pytest tests/test_doc_claims.py` |
| **A4 ★** | 无未实测的性能宣称 | **已验证**（§7 三态表 + 报告口径声明） | `pytest tests/test_doc_claims.py` |
| **A5 ★** | 测试有鉴别力 | **已验证**（16/16 变异被捕获，还原哈希一致） | `python scripts/mutate.py` |
| A6 | 真实/模拟清单 | **已验证**（17 台逐台标注，文件由清单生成且校验一致） | `pytest tests/test_inventory.py` |
| A7 | 音频不出本机 | **部分验证**：决策链在 socket 全堵死下跑通；**抓包验证待你在真机执行** | `pytest tests/test_privacy.py`，抓包见 §10 |
| A8 | 可安装可复现 | **已验证**：本机 `pytest -q` 全绿（数量以 CI 输出为准；跳过的是需要真 Ollama 的实时用例与 FastAPI TestClient 本机依赖冲突用例） | `pip install -e . && pytest -q` |
| A9 | 评测含危险动作档 | **已验证**（危险动作 27 条 ≥ 20） | `python evals/judge.py` |
| A10 | 有 LICENSE | **已验证**（MIT，与 HA 生态兼容） | `head -3 LICENSE` |
| A11 | 演示可盲跑 | **已验证**（13 步全通过，无手工步骤） | `python -m hvsl.cli demo` |
| A12 | 无对外交付类表述 | **已验证**（grep 命中 0） | `pytest tests/test_doc_claims.py` |
| A13 | 隐私与个保法边界写明 | **已验证**（§9 单列一节） | 见下 |

> **HA 真机联调（F-001 闸门）已执行**：2026-11-03 在 Docker Hub HA stable 上 **9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1 PASS**；`2025.1` 同级全 PASS，`2025.2.4` 同级全 PASS；2024.6 依赖不兼容（自带 pydantic 1.10.15），2025.3+ 未测。
> 证据见 [`docs/HA真机冒烟结果-v0.1.md`](docs/HA真机冒烟结果-v0.1.md)、[`docs/HA多版本矩阵-v0.1.md`](docs/HA多版本矩阵-v0.1.md)。ASR 合成语音 2/2 PASS；公开真人 4 条 small 可识别但有错字（`opencc` 繁转简 + `pypinyin` target 级同音纠错已接入、错字仍在），medium 已取得并试跑但错字仍在（观察/观光旅游已修正，邮局/邮箱/蜿蜒/白皑皑仍错，未做人工对齐 CER）；真实设备、notify 真投递仍未验证（HA 2026 legacy `file`/`rest` 均不可用，仅 `mobile_app` 未验证）。

---

## 9. 隐私与数据边界（单列，A13）

家庭语音数据在高敏感个人信息清单里属于"行踪、居住、健康"相关类别，一旦上云就不是
"传输成本"问题，而是"责任归属"问题。所以本项目把边界做成代码约束，不是承诺：

1. **音频只在本地流转**：ASR 走本机 faster-whisper，模型加载强制 `local_files_only=True`
   且子进程环境变量 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；本项目不实现任何云端转写。
2. **TTS 也是本地**：Windows SAPI / macOS `say` / `espeak-ng`，合成结果落 WAV；
   默认**不外放声音**，需要扬声器播放必须显式加 `--loud`。
3. **文本出机器需要显式开闸**：解析器默认 `rule`（离线）。要用本地 Ollama 也只在
   loopback；解析端点非 loopback 且未设置 `HVSL_ALLOW_CLOUD_LLM=true` 时，
   **构造解析器阶段就抛错**（不是发请求前才拦）。
4. **接口只听本机**：`serve` 绑定非 127.0.0.1/localhost 会被拒绝启动。
5. **留痕在本地磁盘**：SQLite 单文件（`var/decisions.db`），不上传、不聚合、不外发。
6. **可自证**：`tests/test_privacy.py` 把 `socket.socket` / `create_connection` / `getaddrinfo`
   全部替换为抛错后，仍完整跑完"危险动作挂起 → 确认 → 下发 → 留痕"这条链。

想抓包复核：

```bash
# 跑决策链时不应出现任何出网连接（示例：Windows 用 Process Monitor 过滤 tcpout）
python -m hvsl.cli chat
```

---

## 10. 未覆盖清单（不粉饰）

| 缺口 | 现状 | 影响 |
| --- | --- | --- |
| Home Assistant 实机联调 | **stable 9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1；2025.1 同级全 PASS；2025.2.4 同级全 PASS**（见 `docs/HA真机冒烟结果-v0.1.md` / `docs/HA多版本矩阵-v0.1.md`） | F-001 闸门已闭环；ASR 合成 2/2、真人 4 条有错字（`opencc` 繁转简 + `pypinyin` 同音纠错已接入、medium 已取得但错字仍在）；2024.6 不兼容 pydantic 1.10；notify legacy `file`/`rest` 均不可用（仅 `mobile_app` 未验证）；真实设备未验证 |
| 真实设备 | **本轮 0 台**（17 个受控实体全为模拟） | 无法声称任何真实家庭环境下的可用性/延迟 |
| ASR 字错率 | 合成语音已实测（主口径=数字等价归一化后）：tiny **0.1345（原样 0.207）**、small **0.0536（原样 0.1511）**、medium **0.0111（原样 0.1086）**，n=10 已知文本，见 §7 与 `reports/ASR-CER.md`；真人/家庭语音**未实测**（无逐条人工校对参照） | 真人、远距离、噪声场景不给数字 |
| 麦克风实时采集 | 未实现（输入是音频文件） | "说完→下发"的麦克风段未计入 |
| 方言、远场、噪声 | 不在范围 | 不声称 |
| 定时任务/场景/状态查询 | 显式拒绝 | 只覆盖即时设备指令 |
| 音量/亮度相对调节（"调大一点"） | 已支持（±10%/次，按当前值 clamp；当前状态缺失时安全失败、不下发）；**HA 真机 7/7 PASS（证据 `var/ha-relative/results.json`）**：同一 session 连续两次调暗 50%→40%→30% 均 `executed`、各 1 次下发；关灯后调暗 0 下发；媒体实体 `playing` 状态下音量 40%→50%（音量与播放状态解耦，读不到音量仍 0 下发）；仍未纳入 68 例评测集；连续相对调节每次生效（该工具 `idempotent=False`）；确定性拒绝提示“未下发”、不计为可重试 | 仍受设备状态可读性约束；真实物理设备 0 台、并发未测 |
| 多语言 | 中文词表 | 英文指令不保证 |
| HA 版本漂移 | stable 9/9 + 附加 3/3 + 蓝图 3/3 + soak 1/1；**2025.1 同级全 PASS**（pydantic 2.10.4）；**2025.2.4 同级全 PASS**（pydantic 2.10.6）；2025.2 滚动 tag 镜像未注册；2025.3+ 未测；HA 2024.6 **不兼容**（自带 pydantic 1.10，集成需 >=2.5），`hacs.json` 最低版本 2025.1.0 已有实机支撑 | 上游改动可能打断（方案 R3） |
| 认证/加密/多用户 | HTTP 接口无鉴权，仅限本机 | 不要暴露到局域网 |

### 10.1 诊断、迁移、卸载与数据保留

- **诊断**：在“设置 → 设备与服务 → Home Voice Safety Layer → 下载诊断”导出 JSON。内容只含配置路径、调参值、`engine.stats()` 与 sensor 聚合状态；**不含 token、用户原话或完整对话**。
- **迁移**：配置条目从 v1 升到 v2 时只补默认 options（`remove_data=false`、空通知服务），保留已有 catalog/policy 路径与用户参数；资源缺失时迁移失败并保持 v1，日志带 `reason=migration_*`，可补路径后重试。
- **卸载**：默认**保留** `/config/home_voice_safety_decisions.db` 以便审计；只有显式勾选 `remove_data` 才删除 DB、`-wal`、`-shm`。多个配置条目共存时，只有最后一个条目卸载才移除 `home_voice_safety.*` service。
- **数据保留**：审计导出在 `/config/home_voice_safety/exports/`；删除 DB 前先备份，导出默认脱敏用户原话。
- **HA 真机冒烟**：9 步清单见 [`scripts/ha_smoke.md`](scripts/ha_smoke.md)，2026-11-03 结果 **9/9 PASS**，证据见 [`docs/HA真机冒烟结果-v0.1.md`](docs/HA真机冒烟结果-v0.1.md)。

---

## 11. English summary

**home-voice-safety-layer** is the *decision and safety layer* of a local-first smart-home
voice assistant: local ASR → intent → namespaced tool calls constrained by JSON Schema and
Pydantic → a **confirmation gate for dangerous actions** (door unlock, gas, main breaker,
high-power sockets, alarm disarm) → **idempotent dispatch** with pre-write/commit-write-back
audit rows in SQLite.

It deliberately does **not** do wake words, hardware, vendor cloud APIs, or multi-brand
ecosystem access. Everything runs offline: the default backend is a set of labelled
simulated entities and the default parser is deterministic, so `pip install -r
requirements.txt && pytest -q && python evals/judge.py && python scripts/mutate.py`
reproduces the claims on any machine.

Verified numbers (measured, this repo, simulated backend): 68 graded cases across four
tiers (27 danger cases), **100% correct interception of dangerous actions, 0 misdispatches**,
100% first-attempt tool-call success with the rule parser, and **16/16 injected defects
caught** by the test suite with byte-identical source restoration. Audio never leaves the
machine: ASR is local faster-whisper with `local_files_only` + `HF_HUB_OFFLINE`, TTS is local,
the HTTP server binds to loopback only, and the test suite runs the whole chain with all
network sockets monkeypatched to raise. Not measured and not claimed: real-human character error rate,
wake-word performance, far-field behaviour, end-to-end latency with real devices.

Licensed under MIT. Not a commercial offering; it is a portfolio piece, and the boundary is
enforced by code and tests rather than by wording.

---

## 12. 待确认清单

统一标记 `【填：…】`，全文可搜索：

- 【填：B4 可联调真实设备清单与是否有常开独显机】——决定 `devices/devices.json` 里哪几台改成 `real`
- 【填：B5 每周 1.5 人日是否含周末整块时间】——方案判定不成立则本方向不成立
- 【填：B1 是否主投智能硬件厂】——决定本盘子是备选还是主选
- 【填：选定 HA 版本与安装方式（Docker ghcr.io / venv）】——本机探测：Docker Hub 不可达，ghcr.io 可达
- 【填：许可证最终选择 MIT / Apache-2.0】——当前按 MIT 落地，Apache-2.0 需换 `LICENSE`
- 【已完成】GitHub 用户名确定为 `young0617-02`；`manifest.json` 的 `codeowners` / `documentation` / `issue_tracker` 已指向真实仓库
- 【已完成】HA 真机 9 步冒烟：9/9 PASS，证据见 `docs/HA真机冒烟结果-v0.1.md`

---

## 13. 许可与声明

- 许可：MIT（见 `LICENSE`）。
- 本项目为单人自主实现的开源作品集，全部演示设备为模拟实体；
  危险动作相关的实体全部保持模拟，不接任何真实负载。
- 生态参考：Home Assistant（本地集成宿主）、faster-whisper（本地 ASR）、Ollama（可选本地解析）。
