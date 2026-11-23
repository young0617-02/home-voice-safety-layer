# ASR 语音链路结果 v0.1

> 执行日期：2026-11-03
> 环境：Windows 11 / Python 3.12.8 / faster-whisper（`uv pip install`）/ av 13.1.0
> 模型：`Systran/faster-whisper-small`（464MB，经 `hf-mirror.com` 下载）
> TTS：Windows SAPI `Microsoft Huihui Desktop`（zh-CN）
> 结论：**2/2 PASS**（真实 ASR 文本 → 决策引擎）

## 1. 结果

| 语音 | ASR 文本 | 决策 | 下发 | 判定 |
|---|---|---|---|---|
| 开大门 | `开大门` | `awaiting_confirmation` | 0 | PASS |
| 打开客厅大灯 | `打开客厅大灯` | `executed` | 1 | PASS |

脚本：`scripts/asr_decision_e2e.py`；结果：`var/asr/e2e_results.json`；音频：`var/asr/*.wav`。

## 2. 过程中暴露的真实问题

1. **tiny 模型中文精度不足**：`开大门` → `開大門`（繁体）、`打开客厅大灯` → `打開可聽打燈`（错字），NLU 词表匹配不上。
2. **small 模型中文正确**：同一音频输出 `开大门` / `打开客厅大灯`，决策链路通过。
3. **HuggingFace 直连小文件 0 字节**：`config.json`/`tokenizer.json`/`vocabulary.txt` 下载为 0；改用 `HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1` 后小文件成功；`model.bin` 的 xet 401 用普通 HTTP 补下。
4. **av 版本不兼容**：旧 `av` 不支持 `metadata_errors` 参数；升级到 `av 13.1.0` 后正常。

## 3. 边界（不夸大）

- 语音是 **TTS 合成**，不是真人；近场、无噪声、无方言/口音。
- 只验证 2 条短句；未测 CER/RTF、远场、多说话人、儿童/老人语音。
- 不是 HA 真机语音链路：HA 容器未接 ASR/TTS，本验证在项目进程内完成。
- `faster-whisper`/`av` 通过 `uv pip install` 装入项目 venv，**未改 pyproject/uv.lock**；生产安装需显式声明。

## 4. 复现命令

```bash
# 1) TTS 生成音频（Windows SAPI）
powershell -Command 'Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.SelectVoice("Microsoft Huihui Desktop"); $s.SetOutputToWaveFile("var/asr/kai_da_men.wav"); $s.Speak("开大门"); $s.Dispose()'

# 2) 安装依赖（正式方式；asr extras 已声明 faster-whisper + av）
uv sync --extra asr
# 临时方式（不改 pyproject/uv.lock）：
# uv pip install faster-whisper "av>=11,<14"

# 3) 下载 small 模型（国内镜像 + 禁用 xet）
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 \
  .venv/Scripts/python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small')"

# 4) 跑 ASR→决策
.venv/Scripts/python.exe scripts/asr_decision_e2e.py
```
## 5. 公开真人语音（OSR，2026-11-03）

来源：[Open Speech Repository – Mandarin Chinese](https://www.voiptroubleshooter.com/open_speech/chinese.html)（8kHz 16-bit PCM；许可证允许测试/研究，需注明来源）。

| 文件 | small 转写（节选） | 观察 |
|---|---|---|
| OSR_cn_000_0072_8k.wav | 院子门口不远处就是一个地铁站……邮举门前的人行道上有一个蓝色的邮箱 | “邮局”识别为“邮举” |
| OSR_cn_000_0073_8k.wav | ……一幅美丽的人生狼图……出色楼旁边的十字路口…… | 多处错字 |
| OSR_cn_000_0074_8k.wav | 夏天有很多小朋友在沙滩上玩耍校園一山黃湖風景如畫…… | 繁体与错字（校園/黃湖/閃電） |
| OSR_cn_000_0075_8k.wav | 天文望远镜可以用来光查天空……光光旅游……汪延曲折……白矮矮 | “观察/观光/蜿蜒/白皑皑”错字 |

**结论**：

- 真人 8kHz 电话带宽音频下，small 能识别出句子，但**错字与繁体明显**，不能直接用于家居指令。
- TTS 合成（近场、清晰）2/2 正确，**不能代表真人/远场可用**。
- 若要做真人可用，需要：medium/large 模型、繁转简后处理、领域词表纠错、AEC/降噪；本轮未做 CER（无人工对齐参照）。

**边界**：OSR 是公开测试语音，非家庭场景；不代表真实家庭环境。
## 6. 繁转简后处理（opencc，2026-11-03）

- 已接入 `pipeline/asr.py`：`LocalAsr.transcribe` 在返回前自动 `t2s`；`opencc-python-reimplemented` 加入 `asr` extras，未安装时原样返回。
- 0074 的结果：`校園/黃湖/風景/北風/閃電/古城門/休閒` → `校园/黄湖/风景/北风/闪电/古城门/休闲`。
- **但错字仍在**：`邮举`（邮局）、`光查`（观察）、`光光旅游`（观光旅游）、`汪延`（蜿蜒）、`白矮矮`（白皑皑）。
- 结论：繁转简解决“繁体导致 NLU 词表不匹配”，**不能解决声学错字**；真人可用仍需 medium/large + 领域纠错。

## 7. medium 模型：曾误判未取得（后已取得，见 §9）

- `Systran/faster-whisper-medium` 多次下载：首次 900s 超时；续传三轮后缓存到 1.7GB、存在两个 `.incomplete`，当时检查未见 `model.bin` 生成（后复查实际已完整，见 §9）。
- 按“不无限重试”原则当时记为**未取得**，不作为 ASR 结果；该判断已于 2026-11-04 纠正。
- medium 已取得（1.53GB `model.bin`，HF 镜像）并试跑：观察/观光旅游已修正，邮局/邮箱/蜿蜒/白皑皑仍错；仍不得写“真人语音已达可用”（合成语音 CER 见 §10；真人 8kHz 未做人工对齐 CER）。
## 8. 领域同音纠错（pypinyin，2026-11-03）

- 已接入 `pipeline/nlu.py::_correct_entities`：只在 RuleParser 解析出的 **target 短语** 上做同音纠错（音节完全一致才替换），不整句替换、不跨动作/批量词。
- 依赖 `pypinyin` 加入 `asr` extras；未安装则跳过。
- 真实验证：

| 输入 | 结果 |
|---|---|
| `开可听打灯` | 纠成 `开客厅大灯` → **executed**，target `light.living_room_main` |
| `打开客厅的灯` | **不纠**（“的”≠“大”），仍 `clarification_required`，保留歧义 |
| `把客厅灯都关掉，再关掉卧室主灯` | 批量 targets 完整，`dispatch=2` |
| `关掉电暖器插座` | 仍 `awaiting_confirmation`，target `switch.heater_socket` |

- 原因：首版整句纠错会把“客厅的灯”消歧成“客厅大灯”，并破坏 M05/A02/D14 三个评测用例；已改为 **target 级 + 同音（distance=0）**，全量评测恢复。
- 仍不能解决 OSR 新闻句子的声学错字（邮举/光查/汪延）；真人可用仍需 medium/large + 领域词表纠错。
## 9. medium 模型结果（2026-11-04，已取得）

- `Systran/faster-whisper-medium` 的 `model.bin` 已完整生成（**1,527,906,378 bytes ≈ 1.53GB**；此前只看到两个 `.incomplete` 残留，属误判“未取得”，实际快照已完成）。
- OSR 4 条 medium 转写对比：

| 样本 | medium 转写（节选） | 观察 |
|---|---|---|
| 0072 | ……游举门前的人形道上有一个蓝色的油香 | 邮局→游举、人行道→人形道、邮箱→油香 |
| 0073 | ……人生狼图……书社楼旁边的十字路口…… | 狼图/书社楼仍错 |
| 0074 | ……校园衣衫黄糊……忽然一到闪电……古城门…… | 校园/黄湖仍错，闪电/古城门正确 |
| 0075 | 天文望远镜可以用来观察天空……观光旅游……汪严曲折……白矮矮 | **观察/观光旅游已修正**；蜿蜒→汪严、白皑皑→白矮矮仍错 |

- **结论**：medium 比 small 有改善（`光查→观察`、`光光旅游→观光旅游` 已修正），但真人 8kHz 电话带宽 + 口音下仍有错字，**不能直接作为家居指令**；TTS 合成 2/2 仍只代表清晰近场。
- 未做人工对齐 CER，**不能量化“精度提升 X%”**；只能定性说“部分错字修正、整体仍不可用”。
- 下一步更实际的方向不是继续堆大模型，而是：真人家居用**短指令 + 领域词表纠错**（`pypinyin` target 级纠错已落地）、ASR 仅在清晰近场/可选云场景使用。

## 10. CER 实测（合成语音）

> 口径：**合成语音 CER，不等于真人/家庭噪声环境 CER**。

- 时间：2026-11-04；脚本 `scripts/asr_cer.py`；机器可读结果 `reports/ASR-CER.json`，渲染报告 `reports/ASR-CER.md`。
- 样本：内置 10 条家居指令短句（参照文本已知），Windows SAPI 本地合成 WAV（不外放），本地 faster-whisper 转写。
- 环境：本机 RTX 4060 8GB；device=`cuda` / compute_type=`float16` / beam_size=`1`；三个模型串行、各自独立子进程加载（跑完释放显存）；tiny/small/medium 均命中本地快照、离线加载。
- 归一化（原样口径）：opencc 繁转简 → NFKC（全角折叠）→ 去空白与中英文标点 → ASCII 小写 → 只保留 CJK/字母/数字；ref 与 hyp 共用同一函数。
- 归一化（主口径）：在原样口径基础上，ref 与 hyp 再走同一套数字等价归一化（中文数字→阿拉伯数字、`百分之X`→`X%`）；两侧同规则，`一下`→`1下` 这类误伤同向发生。
- 指标：`mean_cer_norm`（主口径）/ `mean_cer_raw`（原样）为逐条算术平均；`pooled_cer` = 总错误 / 总参照字数；两套 S/D/I 都保留。归一化后总字数 **75**，原样总字数 **79**（数字串变短）。

| 模型 | 条数 | 平均 CER（归一化后，主口径） | 平均 CER（原样） | 总字数 | S/D/I（归一化后） |
| --- | --- | --- | --- | --- | --- |
| tiny | 10 | 0.1345 | 0.207 | 75 | 10/0/0 |
| small | 10 | 0.0536 | 0.1511 | 75 | 3/0/0 |
| medium | 10 | 0.0111 | 0.1086 | 75 | 1/0/0 |

pooled CER（总错误/总字数）：tiny 归一化 **0.1333**（10/75）/ 原样 0.2278（18/79）；small 归一化 **0.04**（3/75）/ 原样 0.1646（13/79）；medium 归一化 **0.0133**（1/75）/ 原样 0.1392（11/79）。

换算成百分比：tiny **13.45%**（原样 20.70%）、small **5.36%**（原样 15.11%）、medium **1.11%**（原样 10.86%）。

**典型错例**（ref → hyp，以归一化后 CER 为准）：

- tiny `把客厅大灯调到百分之五十` → `把客厅大灯掉到15%`：归一化后 0.3333（3 替换），"调到→掉到"、"50→15" 都是**声学错字**；原样 0.5。
- tiny `打开卫生间的排风扇` → `打开卫生间的排封闪`（0.2222，2 替换）：近音错字。
- tiny `把卧室的灯关掉` → `把握式的灯关掉`（0.2857，2 替换）；small `锁上大门` → `索上大门`（0.25，1 替换）：短指令仍有声学错字。
- medium 只剩 1 处声学错字：`客厅太亮了把灯调暗` → `客厅太亮了把灯掉暗`（0.1111，1 替换）。
- **纯数字写法差异（归一化后已消除）**：small/medium `把客厅大灯调到百分之五十` → `…50%`（原样 0.4167 → 归一化 0）；三个模型 `把空调温度调到二十六度` → `…26度`（0.2727 → 0）；三个模型 `把音量调到三十` → `…30`（0.2857 → 0）。这类差异是排版/写法，不是声学错误；归一化后才是声学错字口径。

**边界**：

- 合成语音是清晰近场、无噪声、无口音/语速变化，**不代表**真人、远距离拾音、家庭噪声、多人同时说话。
- n=10 是小样本，只能说明“已知文本上的错字比例”，不能外推成家庭场景可用；medium 的归一化 CER 更低 **不等于** 决策链路在真人场景可用。
- 原样 CER 与归一化 CER 的差异**主要来自数字写法**；原样口径仍保留在同一份 JSON/MD 中，便于核对口径变化。
- 真人 8kHz（OSR）参照文本必须人工逐条核对，**真人 CER 仍未实测**；本轮没有伪造 OSR refs，也没有用转写输出反推参照（循环论证）。

**复跑**：

```bash
.venv/Scripts/python.exe scripts/asr_cer.py --model tiny --model small --model medium --device cuda --compute-type float16
```

**证据**：`reports/ASR-CER.json`、`reports/ASR-CER.md`、`scripts/asr_cer.py`、`tests/test_asr_cer.py`。

## 11. 模型清洗说明

- 旧 `.incomplete`（约 901MB）是早期中断残留，不影响 `model.bin` 使用；可安全删除以释放空间。
- `medium` 未写入 `asr` extras 的默认模型清单（项目默认仍是 `small`/`tiny` 由 `ASR_MODEL_SIZE` 控制）；medium 只作本机质量对比，不改变默认部署。