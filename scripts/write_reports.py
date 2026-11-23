"""Render the markdown reports from the JSON artifacts (no hand-typed numbers).

    python scripts/write_reports.py

Reads:  reports/评测结果.json、reports/评测结果-llm.json（可选）、
        reports/变异测试.json、reports/语音链路分段实测.json（可选）
Writes: reports/评测结果.md、reports/变异测试.md、reports/语音链路分段实测.md
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
# 输出目录。默认与输入同处；测试通过 monkeypatch 指向 tmp_path，避免写工作树。
OUT = REPORTS


def load(name: str) -> dict | None:
    path = REPORTS / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value) -> str:
    return "-" if value is None else f"{value}%"


def render_eval(name: str, out_name: str) -> str:
    data = load(name)
    if data is None:
        return ""
    summary = data["summary"]
    lines = [
        f"# 评测结果 · {summary['provider']} 解析器",
        "",
        f"> 口径：**实测**。延迟为「{summary['provider']} 解析 + 模拟后端」的分段实测值，不含 ASR/TTS；"
        "接真实设备或云端解析后需重新测量。字错率(CER)：**合成语音（已知文本）已实测**，见 "
        "`reports/ASR-CER.md`；**真人/家庭噪声无人工对齐参照，未实测**。",
        "",
        "## 一、总览",
        "",
        f"- 用例总数：**{summary['cases_total']}**，通过 **{summary['cases_passed']}**",
        f"- 危险动作用例：**{summary['danger_cases']}** 条（其中需二次确认 {summary['danger_cases_requiring_confirm']} 条）",
        f"- **危险动作正确拦截率：{pct(summary['danger_correct_interception_rate'])}**（红线目标 100%）→ "
        f"{'通过' if summary['danger_red_line_ok'] else '未通过'}",
        f"- 危险动作**越权下发**次数：**{summary.get('danger_unauthorized_dispatch', 0)}**"
        + (f"（用例 {summary['danger_unauthorized_ids']}）" if summary.get("danger_unauthorized_ids") else ""),
        f"- **误下发数（含歧义档越权）：{summary['misdispatch_count']}**（红线目标 0）"
        + (f"，涉及用例 {summary['misdispatch_ids']}" if summary["misdispatch_ids"] else ""),
        f"- 工具调用首次成功率：**{pct(summary['tool_first_attempt_rate'])}**（{summary['tool_call_cases']} 条应下发用例）；"
        f"其中被确定性纠偏过：**{pct(summary.get('parser_corrected_rate'))}**",
        f"- 留痕统计：{json.dumps(summary.get('audit', {}), ensure_ascii=False)}",
        # wall_seconds 每轮都不同；渲染进 md 会让 `judge → pytest` 必红，故只指向 JSON。
        f"- 整套跑完耗时：见 `reports/{name}` 的 `summary.wall_seconds`（每轮不同，不渲染进本文件）",
        "",
        "## 二、四档分档结果",
        "",
        "| 档位 | 通过 / 总数 |",
        "| --- | --- |",
    ]
    tier_labels = {"single": "单设备指令", "multi": "多步编排", "ambiguous": "歧义澄清", "danger": "危险动作"}
    for tier, bucket in sorted(summary["by_tier"].items()):
        lines.append(f"| {tier_labels.get(tier, tier)}（{tier}） | {bucket['passed']} / {bucket['total']} |")

    lat = summary["latency_ms"]
    lines += [
        "",
        "## 三、分段延迟（毫秒，不含 ASR/TTS）",
        "",
        "| 段 | 样本 | P50 | P95 | 最大 | 均值 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for stage, label in (("nlu", "意图解析"), ("dispatch", "门禁+下发"), ("total", "决策链合计")):
        row = lat[stage]
        lines.append(
            f"| {label} | {row['n']} | {row['p50']} | {row['p95']} | {row['max']} | {row['mean']} |"
        )
    lines += [
        "",
        "## 四、失败明细",
        "",
    ]
    failed = [row for row in data["results"] if not row["passed"]]
    if not failed:
        lines.append("无。")
    for row in failed:
        lines.append(f"- **{row['id']}**「{row['say']}」：{'；'.join(row['problems'])}")
    lines += [
        "",
        "## 五、这套数字不能说明什么",
        "",
        "- 设备是模拟实体：延迟里不含真实设备的网络往返与执行时间；接真实设备需重测。",
        "- 字错率（CER）：**合成语音（已知文本 n=10）已实测**，见 `reports/ASR-CER.md`；"
        "**真人/家庭噪声无人工对齐参照，未实测**，不对真人识别准确率作任何声称。",
        "- 「说完→下发 P95 ≤ 3 s」在方案里是**设计值**；本报告只给分段实测值，不合成单一对客指标。",
        "- 评测覆盖的是策略内的表达；口语多样性、方言、噪声场景不在本轮范围内。",
        "",
    ]

    OUT.joinpath(out_name).write_text("\n".join(lines), encoding="utf-8")
    return out_name


def render_mutation() -> str:
    data = load("变异测试.json")
    if data is None:
        return ""
    lines = [
        "# 变异测试（F-012 / A5）",
        "",
        "> 口径：**实测**。每条变异都是对源码的一次文本替换，跑相关测试，再把源文件按字节还原并校验 sha256。",
        "",
        f"- 注入变异：**{data['mutations']}** 条，被测试套件捕获 **{data['killed']}** 条，逃逸 **{data['survived']}** 条",
        f"- 源文件哈希全部还原一致：{'是' if data['restore_hash_verified'] else '否'}",
        f"- 结论：{'套件有鉴别力（≥5/5 捕获且还原一致）' if data['killed'] >= 5 and data['survived'] == 0 else '需要补测试'}",
        "",
        "| 变异 | 注入内容 | 被哪个测试捕获 | 耗时(s) |",
        "| --- | --- | --- | --- |",
    ]
    for row in data["results"]:
        evidence = row["killed_by"][:110].replace("|", "/")
        lines.append(f"| {row['id']} | {row['name']} | {evidence} | {row['seconds']} |")
    lines += [
        "",
        "## 复跑方式",
        "",
        "```bash",
        "python scripts/mutate.py            # 全部变异",
        "python scripts/mutate.py --only M2  # 单条排查",
        "```",
        "",
        "## 这套数字不能说明什么",
        "",
        "- 变异是人工挑选的、针对红线分支的定点破坏，不是完整的变异覆盖；",
        "  它证明“红线被改坏时测试会响”，不证明“任何缺陷都能被测出来”。",
        "- 每条变异只跑与该改动相关的测试文件（见上表），不是全量套件。",
        "",
    ]
    OUT.joinpath("变异测试.md").write_text("\n".join(lines), encoding="utf-8")
    return "变异测试.md"


STAGE_LABELS = {
    "asr.load_seconds": "ASR 模型加载(s)",
    "asr.decode_seconds": "ASR 解码+转写(s)",
    "asr.end_to_end_seconds": "ASR 进程端到端(s)",
    "asr.decode_rtf": "ASR 解码 RTF(倍)",
    "nlu.ms": "意图解析(ms)",
    "dispatch.ms": "门禁+下发(ms)",
    "decision_total.ms": "决策链合计(ms)",
    "tts.seconds": "本地 TTS 合成(s)",
}

LATENCY_SCENARIOS = [
    ("短音频 18s（真实会议片段，本机 GPU，每次新起进程）", "语音链路-短音频实测.json"),
    ("窗口切片 20s（从 81 分钟录音切段，含整条解码定位成本）", "语音链路分段实测.json"),
    ("整段 81 分钟（吞吐口径）", "asr-整段81分钟实测.json"),
]


def _ms(value) -> str:
    """Render a millisecond value without inventing precision; missing data is explicit."""
    return "未取到" if value is None else f"{value:g} ms"


def _latency_line(label: str, filename: str) -> str:
    """One conclusion line sourced from an eval JSON's ``summary.latency_ms``."""
    data = load(filename)
    if data is None:
        return f"- **{label}**：分段延迟**未取到**（缺少 `reports/{filename}`），不引用任何历史数字。"
    summary = data.get("summary") or {}
    lat = summary.get("latency_ms") or {}
    cells = []
    for stage, name in (("nlu", "意图解析"), ("dispatch", "门禁+下发"), ("total", "决策链合计")):
        row = lat.get(stage) or {}
        cells.append(
            f"{name} P50 {_ms(row.get('p50'))} / P95 {_ms(row.get('p95'))}（n={row.get('n', '未取到')}）"
        )
    return f"- **{label}（provider={summary.get('provider', '未取到')}）**：" + "；".join(cells) + "。"


def _latency_conclusion() -> list[str]:
    """结论段只从评测 JSON 取数；指标缺失时写「未取到」，不写任何历史数字。"""
    lines = ["", "## 二、结论与口径边界", ""]
    lines.append(_latency_line("规则解析 + 模拟后端", "评测结果.json"))
    if (REPORTS / "评测结果-llm.json").is_file():
        lines.append(_latency_line("LLM 对照（本机 Ollama，同一套用例）", "评测结果-llm.json"))
    lines += [
        "- 以上为**实测值**；ASR/TTS 的分段值见「一、分段结果」（mean/min/max，样本 n=1~2，不给 P95）。",
        "- 「说完→动作下发 P95 ≤ 3s」仍是**设计值**：本文件只给分段实测，不合成单一对客指标；",
        "  模拟后端也不含真实设备的网络往返与执行时间。",
        "- 决策链本身是毫秒级，不是瓶颈；真实链路的瓶颈是模型加载与音频解码（见上表）。",
        "- 字错率 CER：合成语音（已知文本）已实测，见 `reports/ASR-CER.md`；"
        "真人/家庭噪声无人工对齐参照仍**未实测**，本文件不给真人识别准确率数字。",
        "- 未覆盖：麦克风实时采集与传输、真实设备的网络往返、远场与噪声场景。",
        "",
    ]
    return lines


def render_latency() -> str:
    blocks: list[str] = []
    for label, filename in LATENCY_SCENARIOS:
        data = load(filename)
        if data is None:
            continue
        agg = data["aggregates"]
        lines = [
            f"### {label}",
            "",
            f"- 口径：**实测**。{data.get('口径声明') or ''}",
            f"- 环境：{json.dumps(data.get('settings', {}), ensure_ascii=False)}",
            "",
            "| 段 | 样本 | 均值 | 最小 | 最大 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for key, row in agg.items():
            lines.append(
                f"| {STAGE_LABELS.get(key, key)} | {row['n']} | {row['mean']} | {row['min']} | {row['max']} |"
            )
        first_asr = (data["rows"][0] or {}).get("asr") or {}
        if first_asr:
            lines += [
                "",
                f"- 音频 {first_asr.get('audio_seconds')}s / 设备 {first_asr.get('device')} / "
                f"执行 {first_asr.get('execution')} / 分段 {first_asr.get('segments')} / "
                f"窗口切片 {first_asr.get('windowed')}",
                f"- 转写首段预览：{str(first_asr.get('text'))[:60]}",
            ]
        if first_asr.get("warnings"):
            lines += ["", f"- 警告：{first_asr['warnings']}"]
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    header = [
        "# 语音链路分段实测（F-004 / F-005 / F-013）",
        "",
        "> 全部为**实测值**：本机 RTX 4060 8GB，faster-whisper large-v3 / float16 / VAD / beam_size=1；",
        "> 决策层为模拟后端 + 规则解析；TTS 为 Windows SAPI 本地合成到 WAV（默认不外放）。",
        "> 模型加载与稳态解码**分开报**：把一次性加载摊进单个延迟数字里会误导读者。",
        "",
        "## 一、分段结果",
        "",
    ]
    conclusion = _latency_conclusion() + [
        "## 三、怎么复现",
        "",
        "```bash",
        "# 1) 先切一段短音频（需要装有 faster-whisper 的 venv）",
        "$ASR_PYTHON scripts/make_clip.py --audio <长录音> --offset 120 --duration 18 --out var/clip18.wav",
        "# 2) 短音频端到端实测",
        "python scripts/measure_latency.py --audio var/clip18.wav --runs 2 --with-tts --say 打开客厅大灯 \\",
        "  --out reports/语音链路-短音频实测.json",
        "# 3) 整段吞吐（81 分钟，耗时约 8~10 分钟）",
        "python scripts/measure_latency.py --audio <长录音> --runs 1 --out reports/asr-整段81分钟实测.json",
        "# 4) 渲染本文件",
        "python scripts/write_reports.py",
        "```",
        "",
    ]
    OUT.joinpath("语音链路分段实测.md").write_text(
        "\n".join(header + blocks + conclusion), encoding="utf-8"
    )
    return "语音链路分段实测.md"


def main(_argv=None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    name = render_eval("评测结果.json", "评测结果.md")
    if name:
        written.append(name)
    if (REPORTS / "评测结果-llm.json").is_file():
        name = render_eval("评测结果-llm.json", "评测结果-llm.md")
        if name:
            written.append(name)
    name = render_mutation()
    if name:
        written.append(name)
    name = render_latency()
    if name:
        written.append(name)
    print("已生成：" + "、".join(written) if written else "没有可渲染的 JSON 产物")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
