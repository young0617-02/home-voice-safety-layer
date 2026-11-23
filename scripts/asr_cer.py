"""ASR 字错率（CER）基线：TTS 已知文本（默认）与 OSR 真人样本（需显式 refs）。

用法：
    .venv/Scripts/python.exe scripts/asr_cer.py --model tiny --model small --model medium
    .venv/Scripts/python.exe scripts/asr_cer.py --osr --refs var/asr/osr_refs.json

口径（写进报告，避免数字被误读）：
- 默认模式是 **合成语音**：TTS（Windows SAPI）把已知文本读成 WAV，再本地 ASR 转写，
  因此参照文本是确定的、可复算的。它 **不等于** 真人/家庭噪声环境下的 CER。
- OSR 模式必须提供 ``--refs``：逐条参照文本必须人工核对；**禁止** 拿 ASR 输出反推
  参照文本，那会变成循环论证（自己证明自己）。
- ``normalize_for_cer`` 对 ref 与 hyp 使用同一套归一化，否则两边的标点/空格/繁体
  差异会被算成错字，数字不可比。

隐私：合成 WAV 只落在 ``var/asr_cer/tts/``，默认在 ``finally`` 里清理（沿用 R12
纪律）；``--keep-audio`` 才保留以便人工复核。脚本不打印音频路径内容，不上传，不外放。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 允许 `python scripts/asr_cer.py` 直接运行
    sys.path.insert(0, str(ROOT))

DEFAULT_MODELS = ("small",)
DEFAULT_OUT = ROOT / "reports" / "ASR-CER.json"
DEFAULT_MD = ROOT / "reports" / "ASR-CER.md"
DEFAULT_OSR_DIR = ROOT / "var" / "asr" / "osr"
TTS_AUDIO_DIR = ROOT / "var" / "asr_cer" / "tts"

#: 10 条家居指令短句：全部有确定参照文本，用于 TTS → ASR 的 CER 基线。
#: 选句覆盖开/关/调亮度/调温度/调音量/锁门/窗帘/排风扇，避免只测一种动词。
TTS_CASES: tuple[tuple[str, str], ...] = (
    ("tts_01", "打开客厅的灯"),
    ("tts_02", "把客厅大灯调到百分之五十"),
    ("tts_03", "把空调温度调到二十六度"),
    ("tts_04", "锁上大门"),
    ("tts_05", "打开卧室的窗帘"),
    ("tts_06", "把音量调到三十"),
    ("tts_07", "关闭厨房的开关"),
    ("tts_08", "把卧室的灯关掉"),
    ("tts_09", "打开卫生间的排风扇"),
    ("tts_10", "客厅太亮了把灯调暗"),
)

TOP_LINE = "> **合成语音 CER，不等于真人/家庭噪声环境 CER**"

# opencc 只初始化一次；缺失时记录一条 note，不抛错（asr extras 里是可选依赖）。
_OPENCC: Any = None
_OPENCC_TRIED = False
_NORMALIZATION_NOTES: list[str] = []


def _simplify_with_opencc(text: str) -> str:
    """繁体转简体；opencc 缺失时原样返回并记 note（不中断评测）。"""
    global _OPENCC, _OPENCC_TRIED
    if not text:
        return text
    if not _OPENCC_TRIED:
        _OPENCC_TRIED = True
        try:
            from opencc import OpenCC

            _OPENCC = OpenCC("t2s")
        except Exception as exc:  # ImportError 或词典加载失败
            _OPENCC = None
            _NORMALIZATION_NOTES.append(
                f"opencc 不可用，繁转简已跳过（{type(exc).__name__}）；繁体不会自动折算成简体"
            )
    if _OPENCC is None:
        return text
    return _OPENCC.convert(text)


#: 中文数字字符。`两` 按 2 处理，覆盖“两百/两万”等口语写法。
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
_CN_DIGIT_CHARS = "零〇一二两三四五六七八九"
_CN_NUM_CHARS = _CN_DIGIT_CHARS + "十百千万亿"
# 数字串：中文数字/阿拉伯数字 + 可选小数点（`点` 或 `.`）。
_NUMBER_TOKEN_RE = r"[" + _CN_NUM_CHARS + r"0-9]+(?:[点.][" + _CN_DIGIT_CHARS + r"0-9]+)?"
_NUMBER_RE = re.compile(_NUMBER_TOKEN_RE)
_PERCENT_RE = re.compile(r"百分之\s*(" + _NUMBER_TOKEN_RE + r")")


def _digit_of(ch: str) -> int:
    return int(ch) if ch.isdigit() else _CN_DIGITS[ch]


def _join_digits(token: str) -> str:
    """无单位的数字串逐位拼接：`二零二六` → `2026`，而不是按数位累加成 6。"""
    return "".join(str(_digit_of(ch)) for ch in token)


def _parse_cn_int(token: str) -> str:
    """中文整数串转阿拉伯数字；含十/百/千/万/亿时按数位累加。"""
    if not any(ch in _CN_UNITS for ch in token):
        return _join_digits(token)
    total = 0
    section = 0
    number = 0
    for ch in token:
        if ch in _CN_DIGITS or ch.isdigit():
            number = _digit_of(ch)
            continue
        unit = _CN_UNITS[ch]
        if unit >= 10000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            # `十五` 的“十”前面没有数字，按 1 处理。
            section += (number or 1) * unit
            number = 0
    return str(total + section + number)


def _parse_number_token(token: str) -> str:
    """单个数字串 → 阿拉伯数字文本；纯阿拉伯数字原样保留。"""
    if all(ch.isdigit() or ch == "." for ch in token):
        return token
    if "点" in token or "." in token:
        head, fraction = re.split(r"[点.]", token, maxsplit=1)
        integer = _parse_cn_int(head) if head else "0"
        decimals = _join_digits(fraction) if fraction else ""
        return f"{integer}.{decimals}" if decimals else integer
    return _parse_cn_int(token)


def _percent_repl(match: re.Match[str]) -> str:
    return f"{_parse_number_token(match.group(1))}%"


def normalize_numbers(text: str) -> str:
    """数字写法等价归一化：中文数字 → 阿拉伯数字、`百分之X` → `X%`。

    只做纯字符串替换，不改变语序：`二十三`→`23`、`五十`→`50`、`十五`→`15`、
    `一百`→`100`、`两百`→`200`、`二十六`→`26`、`三十`→`30`、
    `二十六点五`→`26.5`、`百分之五十`→`50%`。已是阿拉伯数字（含小数点）原样保留。

    **对称性**是前提：ref 与 hyp 走完全相同的替换，`一下`→`1下`、`万一`→`10001`
    这类误伤两侧同向发生，对两者的 CER 差异没有影响（口径写在报告 notes 里）。
    """
    if not text:
        return text
    replaced = _PERCENT_RE.sub(_percent_repl, text)
    return _NUMBER_RE.sub(lambda match: _parse_number_token(match.group(0)), replaced)


def normalize_for_cer(text: str, *, numbers: bool = False) -> str:
    """把参照与假设（转写）归一到同一字符空间。

    顺序：opencc 繁转简 → NFKC（全角/兼容字符折叠）→ [可选] 数字等价归一化
    → 丢弃空白 → 丢弃标点/符号 → ASCII 大写转小写 → 只保留 Unicode 字母（含 CJK）与数字。

    ``numbers=True`` 时启用 :func:`normalize_numbers`，得到“声学错字口径”；
    默认 ``False`` 保留“原样口径”。ref 与 hyp 必须用同一个函数、同一个参数：
    只归一一侧等于换了一套尺子量另一边。
    """
    simplified = _simplify_with_opencc(text or "")
    folded = unicodedata.normalize("NFKC", simplified)
    if numbers:
        folded = normalize_numbers(folded)
    chars: list[str] = []
    for ch in folded:
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] not in {"L", "N"}:
            continue
        chars.append(ch.lower() if "A" <= ch <= "Z" else ch)
    return "".join(chars)


def normalization_notes() -> list[str]:
    """返回归一化阶段产生的说明（当前只会有 opencc 缺失一条）。"""
    return list(_NORMALIZATION_NOTES)


def edit_distance(ref: str, hyp: str) -> tuple[int, int, int]:
    """字符级 Levenshtein，返回 (替换, 删除, 插入) 的计数。

    先算完整 DP，再从 (len(ref), len(hyp)) 回溯。回溯优先走对角（替换/匹配），
    其次删除，最后插入；这样同一对字符串的 S/D/I 分解是确定性的（总数不变，
    但分解可能有多种，固定一种才可复跑）。
    """
    rows, cols = len(ref), len(hyp)
    dp = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows + 1):
        dp[i][0] = i
    for j in range(cols + 1):
        dp[0][j] = j
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            substitution = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,  # 删除 ref[i-1]
                dp[i][j - 1] + 1,  # 插入 hyp[j-1]
                dp[i - 1][j - 1] + substitution,  # 替换/匹配
            )

    subs = dels = ins = 0
    i, j = rows, cols
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            substitution = 0 if ref[i - 1] == hyp[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + substitution:
                subs += substitution
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            dels += 1
            i -= 1
            continue
        ins += 1
        j -= 1
    return subs, dels, ins


def _measure(norm_ref: str, norm_hyp: str) -> dict[str, Any]:
    """单口径 (S+D+I)/len(ref)；空参照返回 None，不做除法。"""
    ref_chars = len(norm_ref)
    if ref_chars == 0:
        return {
            "cer": None,
            "reason": "empty_reference",
            "subs": 0,
            "dels": 0,
            "ins": 0,
            "ref_chars": 0,
        }
    subs, dels, ins = edit_distance(norm_ref, norm_hyp)
    return {
        "cer": round((subs + dels + ins) / ref_chars, 4),
        "subs": subs,
        "dels": dels,
        "ins": ins,
        "ref_chars": ref_chars,
    }


def cer(ref: str, hyp: str) -> dict[str, Any]:
    """双口径字符错误率：

    - ``cer_raw``：只做繁简/标点/空白/大小写归一化（旧口径）；
    - ``cer_norm``：再叠加数字等价归一化（中文数字→阿拉伯、百分之X→X%），**主口径**。

    同时返回两套 S/D/I 与参照字数。``cer``/``subs``/``dels``/``ins``/``ref_chars``
    是旧字段，等于 raw 口径，保持向后兼容；新代码应读 ``cer_norm``。
    空参照时两套 CER 都是 None（不除零）。
    """
    raw_ref = normalize_for_cer(ref)
    raw_hyp = normalize_for_cer(hyp)
    norm_ref = normalize_for_cer(ref, numbers=True)
    norm_hyp = normalize_for_cer(hyp, numbers=True)
    raw = _measure(raw_ref, raw_hyp)
    normalized = _measure(norm_ref, norm_hyp)
    result: dict[str, Any] = {
        "cer": raw["cer"],
        "subs": raw["subs"],
        "dels": raw["dels"],
        "ins": raw["ins"],
        "ref_chars": raw["ref_chars"],
        "cer_raw": raw["cer"],
        "subs_raw": raw["subs"],
        "dels_raw": raw["dels"],
        "ins_raw": raw["ins"],
        "ref_chars_raw": raw["ref_chars"],
        "cer_norm": normalized["cer"],
        "subs_norm": normalized["subs"],
        "dels_norm": normalized["dels"],
        "ins_norm": normalized["ins"],
        "ref_chars_norm": normalized["ref_chars"],
    }
    if raw["cer"] is None:
        result["reason"] = raw.get("reason", "empty_reference")
    return result


# --------------------------------------------------------------------- 模型定位


def _hub_roots(explicit: str | None) -> list[Path]:
    """收集可能的 HuggingFace hub 缓存根（只读、离线）。"""
    roots: list[Path] = []
    if explicit:
        roots.append(Path(explicit))
    for env_name in ("ASR_MODEL_DIR", "HUGGINGFACE_HUB_CACHE"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.append(Path(hf_home) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def resolve_model_snapshot(model: str, explicit: str | None = None) -> Path | None:
    """在本地缓存里找 ``models--Systran--faster-whisper-<model>`` 的 snapshot。

    找到就返回含 ``model.bin`` 的目录，交给 faster-whisper 以 local_files_only
    加载；找不到返回 None（仍会让 faster-whisper 走本地缓存解析，绝不触网）。
    """
    wanted = f"models--systran--faster-whisper-{model}".lower()
    for root in _hub_roots(explicit):
        if not root.is_dir():
            continue
        for repo in root.glob("models--*"):
            if repo.name.lower() != wanted:
                continue
            snapshots = sorted(
                (repo / "snapshots").glob("*/model.bin"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if snapshots:
                return snapshots[0].parent
    return None


# --------------------------------------------------------------------- OSR 契约


def load_osr_plan(
    refs_path: Path | None,
    osr_dir: Path,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """校验并加载 OSR 参照文本；任何契约问题都返回 (None, 说明)。

    参照文本必须由人逐条核对，脚本只负责把人和音频对上号；缺 refs 或 refs
    指向的文件不存在时直接失败，绝不用转写结果回填参照。
    """
    if refs_path is None:
        return (
            None,
            "--osr 模式必须提供 --refs <json>：逐条参照文本必须人工核对，"
            "禁止用 ASR 输出反推参照（会造成循环论证）。",
        )
    if not refs_path.is_file():
        return None, f"refs 文件不存在：{refs_path}"
    try:
        raw = json.loads(refs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"refs 文件无法解析为 JSON：{type(exc).__name__}: {exc}"
    if not isinstance(raw, dict) or not raw:
        return None, "refs 必须是非空 JSON 对象：{\"文件名.wav\": \"参照文本\"}"

    plan: list[dict[str, Any]] = []
    for name, ref in raw.items():
        wav_name = Path(str(name)).name
        if not isinstance(ref, str) or not normalize_for_cer(ref):
            return None, f"参照文本为空或类型不对：{wav_name}（必须人工填写真实文本）"
        wav_path = osr_dir / wav_name
        if not wav_path.is_file():
            return None, f"缺少音频文件：{wav_name}（refs 与音频必须一一对应）"
        plan.append({"id": wav_name, "ref": ref.strip(), "wav": wav_path, "error": None})
    return plan, None


# --------------------------------------------------------------------- 采集与汇总


def _reconfigure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - 控制台不支持时继续用默认编码
            pass


def _load_project_env() -> None:
    """读取仓库 `.env`（已有 dev 依赖 python-dotenv；缺失时静默跳过）。"""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def _resolve_asr_python(explicit: str | None, notes: list[str]) -> tuple[str | None, str | None]:
    """决定 ASR worker 用哪个解释器。

    `.env` 的 ``ASR_PYTHON_BIN`` 指向单独装好 CUDA 依赖的 ASR venv；项目 venv 里
    ctranslate2 可能缺 cuBLAS。显式 ``--asr-python`` 给错路径时直接失败，环境变量
    给错路径只记 note 并回落到当前解释器，保证脚本仍可跑（会由 worker 尝试 CPU）。
    """
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_file():
            return None, f"--asr-python 指向的解释器不存在：{candidate}"
        notes.append("ASR 子进程解释器：--asr-python 指定")
        return str(candidate), None
    env_value = os.environ.get("ASR_PYTHON_BIN")
    if env_value:
        candidate = Path(env_value)
        if candidate.is_file():
            notes.append("ASR 子进程解释器：ASR_PYTHON_BIN（独立 ASR venv）")
            return str(candidate), None
        notes.append("ASR_PYTHON_BIN 指向的解释器不存在，回落当前解释器")
    return None, None


def _require_asr() -> bool:
    """缺 faster-whisper 时友好退出，不打印 traceback。"""
    try:
        from pipeline.asr import in_process_available
    except Exception as exc:  # pragma: no cover - 项目内导入失败属于环境损坏
        print(f"[ASR-CER] 无法导入 pipeline.asr：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if not in_process_available():
        print(
            "[ASR-CER] 缺少 faster_whisper，无法转写。请先执行 `uv sync --extra asr`"
            "（或 `uv pip install \"faster-whisper>=1.0\" \"av>=11,<14\"`）后重试。",
            file=sys.stderr,
        )
        return False
    return True


def _synthesize_tts(notes: list[str]) -> tuple[dict[str, Path], str, str]:
    """合成 10 条已知短句，返回 {case_id: wav 路径}、provider、失败说明。"""
    from pipeline.tts import Tts

    TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    produced: dict[str, Path] = {}
    provider = ""
    errors: list[str] = []
    for case_id, text in TTS_CASES:
        out_path = TTS_AUDIO_DIR / f"{case_id}.wav"
        try:
            tts = Tts(out_path=out_path, speak_out_loud=False)
            provider = provider or tts.provider
            result = tts.synthesize(text)
            if result.audio_bytes <= 0 or not out_path.is_file():
                errors.append(f"{case_id}: {result.note or 'TTS 未产出音频'}")
                continue
            produced[case_id] = out_path
        except Exception as exc:  # TTS 是可选环境，失败要记录而不是炸整个评测
            errors.append(f"{case_id}: {type(exc).__name__}: {exc}")
    if errors:
        notes.append(f"TTS 合成失败 {len(errors)} 条：" + "；".join(errors))
    return produced, provider, ("；".join(errors) if errors else "")


def _blank_case(case_id: str, ref: str, error: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "ref": ref,
        "hyp": "",
        "cer": None,
        "subs": None,
        "dels": None,
        "ins": None,
        "ref_chars": len(normalize_for_cer(ref)),
        "cer_raw": None,
        "subs_raw": None,
        "dels_raw": None,
        "ins_raw": None,
        "ref_chars_raw": len(normalize_for_cer(ref)),
        "cer_norm": None,
        "subs_norm": None,
        "dels_norm": None,
        "ins_norm": None,
        "ref_chars_norm": len(normalize_for_cer(ref, numbers=True)),
        "error": error,
    }


def _run_model(model: str, items: list[dict[str, Any]], args: argparse.Namespace, notes: list[str]) -> dict[str, Any]:
    """用同一个模型逐条转写；模型/单条失败都记进结果，不跳过也不粉饰。"""
    from pipeline.asr import LocalAsr

    entry: dict[str, Any] = {
        "model": model,
        "cases": [],
        # 兼容旧字段（等于 raw 口径），新读者请用 *_norm / *_raw 双指标。
        "mean_cer": None,
        "total_ref_chars": 0,
        "total_errors": 0,
        "mean_cer_raw": None,
        "mean_cer_norm": None,
        "pooled_cer_raw": None,
        "pooled_cer_norm": None,
        "total_ref_chars_raw": 0,
        "total_ref_chars_norm": 0,
        "total_errors_raw": 0,
        "total_errors_norm": 0,
        "actual_device": "",
        "actual_compute_type": "",
        "valid_cases": 0,
        "failed_cases": 0,
    }
    snapshot = resolve_model_snapshot(model, args.model_dir)
    notes.append(
        f"{model}: {'已定位本地模型快照（离线加载）' if snapshot else '本地快照未定位，交回 faster-whisper 本地缓存解析'}"
    )

    try:
        # python_bin=sys.executable：每个模型在独立子进程里加载，跑完即释放显存，
        # 避免串行比较时前一个模型的显存碎片把后一个模型挤爆。
        asr = LocalAsr(
            python_bin=args.asr_python_resolved or sys.executable,
            model_dir=str(snapshot) if snapshot else None,
            model_size=model,
            device=args.device,
            compute_type=args.compute_type,
            beam_size=args.beam_size,
            vad_filter=True,
            language="zh",
        )
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"[:800]
        notes.append(f"{model}: 初始化失败：{entry['error']}")
        for item in items:
            entry["cases"].append(_blank_case(item["id"], item["ref"], entry["error"]))
            entry["failed_cases"] += 1
        return entry

    consecutive_failures = 0
    successes = 0
    for index, item in enumerate(items, 1):
        if item.get("error"):
            entry["cases"].append(_blank_case(item["id"], item["ref"], str(item["error"])))
            entry["failed_cases"] += 1
            continue
        if consecutive_failures >= 2 and successes == 0:
            skip = f"跳过：模型不可用（{entry.get('error') or '连续失败'}）"
            entry["cases"].append(_blank_case(item["id"], item["ref"], skip))
            entry["failed_cases"] += 1
            continue
        try:
            result = asr.transcribe(str(item["wav"]))
            hyp = (result.text or "").strip()
            measured = cer(item["ref"], hyp)
            case = {
                "id": item["id"],
                "ref": item["ref"],
                "hyp": hyp,
                **measured,
            }
            entry["cases"].append(case)
            successes += 1
            consecutive_failures = 0
            if result.device:
                entry["actual_device"] = result.device
            # AsrResult 只回传 device；compute_type 可能由 worker 回退（如 CUDA 不可用转 CPU/int8）。
            compute_used = getattr(result, "compute_type", "") or ""
            if compute_used:
                entry["actual_compute_type"] = compute_used
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:800]
            entry["error"] = entry.get("error") or message
            entry["cases"].append(_blank_case(item["id"], item["ref"], message))
            entry["failed_cases"] += 1
            consecutive_failures += 1
            notes.append(f"{model}/{item['id']}: 转写失败：{message}")

    measured_cases = [case for case in entry["cases"] if isinstance(case.get("cer_norm"), (int, float))]
    entry["valid_cases"] = len(measured_cases)
    if measured_cases:
        count = len(measured_cases)
        entry["mean_cer_norm"] = round(sum(case["cer_norm"] for case in measured_cases) / count, 4)
        entry["mean_cer_raw"] = round(sum(case["cer_raw"] for case in measured_cases) / count, 4)
        entry["total_ref_chars_raw"] = sum(int(case["ref_chars_raw"]) for case in measured_cases)
        entry["total_ref_chars_norm"] = sum(int(case["ref_chars_norm"]) for case in measured_cases)
        entry["total_errors_raw"] = sum(
            int(case["subs_raw"]) + int(case["dels_raw"]) + int(case["ins_raw"]) for case in measured_cases
        )
        entry["total_errors_norm"] = sum(
            int(case["subs_norm"]) + int(case["dels_norm"]) + int(case["ins_norm"]) for case in measured_cases
        )
        if entry["total_ref_chars_raw"]:
            entry["pooled_cer_raw"] = round(entry["total_errors_raw"] / entry["total_ref_chars_raw"], 4)
        if entry["total_ref_chars_norm"]:
            entry["pooled_cer_norm"] = round(entry["total_errors_norm"] / entry["total_ref_chars_norm"], 4)
        # 兼容旧字段：旧口径是 raw。
        entry["mean_cer"] = entry["mean_cer_raw"]
        entry["total_ref_chars"] = entry["total_ref_chars_raw"]
        entry["total_errors"] = entry["total_errors_raw"]
    if not entry["actual_device"]:
        entry["actual_device"] = args.device
    if not entry["actual_compute_type"]:
        entry["actual_compute_type"] = args.compute_type
    print(
        f"[{model}] {entry['valid_cases']}/{len(items)} 条有效，"
        f"mean CER(norm)={entry['mean_cer_norm']}，mean CER(raw)={entry['mean_cer_raw']}，"
        f"失败 {entry['failed_cases']} 条",
        file=sys.stderr,
    )
    return entry


# --------------------------------------------------------------------- 输出


def _fmt(value: Any) -> str:
    # 直接使用 JSON 中的数值文本，避免报告补尾零后与 JSON 看起来不一致（0.2070 vs 0.207）。
    return "-" if value is None else str(value)


def _fmt_sdi(case: dict[str, Any], suffix: str = "") -> str:
    subs = case.get(f"subs{suffix}")
    if subs is None:
        return "-"
    return f"{subs}/{case[f'dels{suffix}']}/{case[f'ins{suffix}']}"


def _sum_sdi(entry: dict[str, Any], suffix: str) -> tuple[int, int, int]:
    subs = sum(int(case.get(f"subs{suffix}") or 0) for case in entry["cases"])
    dels = sum(int(case.get(f"dels{suffix}") or 0) for case in entry["cases"])
    ins = sum(int(case.get(f"ins{suffix}") or 0) for case in entry["cases"])
    return subs, dels, ins


def print_table(payload: dict[str, Any]) -> None:
    """stdout 汇总表（报告与简报都从这张表核对）。主口径=归一化后 CER。"""
    print("模型       条数  CER归一   CER原样   总字数    S   D   I")
    print("-" * 60)
    for entry in payload["models"]:
        if entry.get("valid_cases", 0) == 0 and entry.get("error"):
            print(f"{entry['model']:<8}  {0:>4}  {'FAILED':>8}  {'-':>8}  {'-':>7}  {'-':>3} {'-':>3} {'-':>3}")
            continue
        subs, dels, ins = _sum_sdi(entry, "_norm")
        print(
            f"{entry['model']:<8}  {entry['valid_cases']:>4}  {_fmt(entry['mean_cer_norm']):>8}  "
            f"{_fmt(entry['mean_cer_raw']):>8}  {entry['total_ref_chars_norm']:>7}  "
            f"{subs:>3} {dels:>3} {ins:>3}"
        )
    print("-" * 60)
    print("说明：主口径=数字归一化后的 CER；S/D/I 同为归一化后的替换/删除/插入；平均 CER 为逐条算术平均。")
    print("口径：合成语音 CER，不等于真人/家庭噪声环境 CER；数字写法已做等价归一化，原样 CER 一并保留。")


def render_markdown(payload: dict[str, Any]) -> str:
    """从同一份 JSON 渲染 Markdown，避免报告与 JSON 两套数字。"""
    is_osr = payload.get("mode") == "osr"
    lines: list[str] = [
        TOP_LINE if not is_osr else "> **真人样本 CER 必须有人工核对过的参照文本；不等于家庭噪声环境 CER**",
        "> 数字写法已做等价归一化，原样 CER 一并保留；主口径为归一化后 CER。",
        "",
        "# ASR 字错率（CER）实测 · " + ("真人样本" if is_osr else "合成语音"),
        "",
        f"- 生成时间：`{payload.get('generated_at', '')}`",
        f"- 模式：`{payload.get('mode', '')}`（synthetic={str(payload.get('synthetic')).lower()}）",
        f"- 采样：{payload.get('case_count', 0)} 条" + ("（内置家居指令短句，参照文本已知）" if not is_osr else "（参照文本由人工逐条核对）"),
        f"- 环境：device=`{payload.get('device', '')}` / compute_type=`{payload.get('compute_type', '')}` / "
        f"beam_size=`{payload.get('beam_size', 1)}`",
        "- 归一化（原样口径）：opencc 繁转简 → NFKC（全角折叠）→ 去空白 → 去中英文标点 → ASCII 小写 → "
        "只保留 Unicode 字母（含 CJK）与数字；ref 与 hyp 共用同一函数",
        "- 归一化（主口径）：在原样口径基础上，ref 与 hyp 再走同一套数字等价归一化"
        "（中文数字→阿拉伯数字、`百分之X`→`X%`）；两侧同规则，`一下`→`1下` 这类误伤同向发生",
        "- 平均 CER = 每条 CER 的算术平均；pooled CER = 总错误 / 总参照字数；"
        "总字数 = 数字归一化后的参照字符数；S/D/I = 替换/删除/插入",
        "",
        "## 一、结果",
        "",
        "| 模型 | 条数 | 平均 CER（归一化后，主口径） | 平均 CER（原样） | 总字数 | S/D/I（归一化后） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in payload["models"]:
        if entry.get("valid_cases", 0) == 0 and entry.get("error"):
            lines.append(f"| {entry['model']} | 0 | 失败 | - | - | - |")
            continue
        subs, dels, ins = _sum_sdi(entry, "_norm")
        lines.append(
            f"| {entry['model']} | {entry['valid_cases']} | {_fmt(entry['mean_cer_norm'])} | "
            f"{_fmt(entry['mean_cer_raw'])} | {entry['total_ref_chars_norm']} | {subs}/{dels}/{ins} |"
        )
    pooled_parts: list[str] = []
    raw_parts: list[str] = []
    for entry in payload["models"]:
        if entry.get("valid_cases", 0) == 0 and entry.get("error"):
            continue
        pooled_parts.append(
            f"{entry['model']} 归一化 {_fmt(entry['pooled_cer_norm'])} / 原样 {_fmt(entry['pooled_cer_raw'])}"
        )
        raw_parts.append(
            f"{entry['model']} {entry['total_ref_chars_raw']} 字 / {entry['total_errors_raw']} 错"
        )
    lines.append("")
    if pooled_parts:
        lines += [
            f"- pooled CER（总错误/总字数）：{'；'.join(pooled_parts)}",
            f"- 原样口径总字数/总错误：{'；'.join(raw_parts)}（归一化后总字数见上表；"
            "两套 S/D/I 与全部逐条数据见 `reports/ASR-CER.json`）",
            "",
        ]

    if any(entry.get("error") for entry in payload["models"]):
        lines += ["### 失败记录", ""]
        for entry in payload["models"]:
            if not entry.get("error"):
                continue
            failed = [case for case in entry["cases"] if case.get("error")]
            lines.append(f"- `{entry['model']}`：{entry['error']}（失败 {len(failed)} 条）")
        lines.append("")

    lines += [
        "## 二、逐条对照",
        "",
        "> ref/hyp 保存原始文本；CER 归一化（主口径）为数字等价归一化后计算，CER 原样为旧口径；两者可复算（见 §四）。",
        "",
    ]
    for entry in payload["models"]:
        lines += [
            f"### {entry['model']}",
            "",
            "| # | 参照（ref） | 转写（hyp） | CER 归一化（主） | CER 原样 | S/D/I 归一化 | S/D/I 原样 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for case in entry["cases"]:
            hyp = case.get("hyp") or "—"
            if case.get("error"):
                hyp = f"（失败）{case['error'][:60]}"
            lines.append(
                f"| {case['id']} | {case['ref']} | {hyp} | {_fmt(case.get('cer_norm'))} | "
                f"{_fmt(case.get('cer_raw'))} | {_fmt_sdi(case, '_norm')} | {_fmt_sdi(case)} |"
            )
        lines.append("")

    lines += [
        "## 三、边界",
        "",
        "- 本文件数字来自本机真实运行：TTS 为 Windows SAPI 本地合成，ASR 为 faster-whisper 本地转写，"
        "音频只在本地读写。",
        "- **合成语音不等于真人**：不覆盖口音、语速变化、远距离拾音、家庭噪声、多人同时说话。",
        "- 10 条短句是小样本，只能说明“在清晰近场的已知文本上错字有多少”，不能外推成家庭场景可用。",
        "- 数字写法已做等价归一化（中文数字→阿拉伯数字、`百分之X`→`X%`），原样 CER 同时保留；"
        "两者差异主要来自数字写法，**归一化后才是声学错字口径**。归一化是纯字符串替换，"
        "`一下`→`1下`、`万一`→`10001` 这类误伤会出现，但 ref/hyp 同规则、同向变化，不改变 CER 差异。",
        "- 真人 8kHz 样本（OSR）的参照文本仍需人工逐条核对，因此**真人 CER 仍未实测**；"
        "本轮没有伪造 OSR refs，也没有拿转写结果反推参照。",
        "- 单独一个模型的 CER 更低，不代表整条决策链路通过率更高；决策还受词表与歧义处理影响。",
        "",
        "## 四、复跑",
        "",
        "```bash",
        "# 合成语音（默认，10 条已知短句）",
        ".venv/Scripts/python.exe scripts/asr_cer.py --model tiny --model small --model medium "
        "--device cuda --compute-type float16",
        "",
        "# 真人样本（参照文本必须人工核对后写入 JSON；没有 refs 会以退出码 2 拒绝运行）",
        ".venv/Scripts/python.exe scripts/asr_cer.py --osr --refs var/asr/osr_refs.json",
        "```",
        "",
        "## 五、证据与备注",
        "",
        "- 机器可读结果：`reports/ASR-CER.json`（本文件由它渲染，单一真源）",
        "- 采集脚本：`scripts/asr_cer.py`；单元测试：`tests/test_asr_cer.py`",
    ]
    norm_notes = [note for note in payload.get("notes", []) if "opencc" in note]
    if norm_notes:
        lines.append(f"- 归一化备注：{norm_notes[0]}")
    others = [
        note
        for note in payload.get("notes", [])
        if note
        and "opencc" not in note
        and "合成语音 CER" not in note
    ]
    for note in others:
        lines.append(f"- 备注：{note}")
    lines.append("")
    return "\n".join(lines)


def _write_outputs(payload: dict[str, Any], out_path: Path, md_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"\n已写入 {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}")
    print(f"已写入 {md_path.relative_to(ROOT) if md_path.is_relative_to(ROOT) else md_path}")


# --------------------------------------------------------------------- 主流程


def _build_payload(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "device": args.device,
        "compute_type": args.compute_type,
        "beam_size": args.beam_size,
        "models": [],
        "notes": [
            "数字等价归一化已启用：中文数字→阿拉伯数字、百分之X→X%；"
            "主口径为 mean_cer_norm / pooled_cer_norm，原样口径 mean_cer_raw / pooled_cer_raw 一并保留。",
            "数字归一化是纯字符串替换，“一下”→“1下”、“万一”→“10001”这类误伤不可避免；"
            "ref 与 hyp 同一规则、同向变化，对 CER 差异无影响。",
        ],
        "synthetic": mode == "tts",
    }


def _run_tts(args: argparse.Namespace, models: list[str]) -> tuple[dict[str, Any] | None, int]:
    payload = _build_payload(args, "tts")
    notes: list[str] = payload["notes"]
    notes.append("TTS 为 Windows SAPI 本地合成、不外放；合成语音 CER 不等于真人/家庭噪声环境 CER。")


    try:
        produced, provider, _ = _synthesize_tts(notes)
        if not produced:
            print(
                "[ASR-CER] 本地 TTS 未产出任何音频（无可用 SAPI/espeak），无法建立合成语音 CER 基线。",
                file=sys.stderr,
            )
            return None, 2
        payload["tts_provider"] = provider
        payload["case_count"] = len(TTS_CASES)
        items = []
        for case_id, text in TTS_CASES:
            if case_id in produced:
                items.append({"id": case_id, "ref": text, "wav": produced[case_id], "error": None})
            else:
                items.append({"id": case_id, "ref": text, "wav": None, "error": "TTS 未合成成功"})

        exit_code = 0
        for model in models:
            entry = _run_model(model, items, args, notes)
            if entry.get("valid_cases", 0) == 0:
                exit_code = 1
            payload["models"].append(entry)
    finally:
        # R12：临时音频只在 finally 清理；--keep-audio 才留给人工复核。
        if not args.keep_audio:
            shutil.rmtree(TTS_AUDIO_DIR, ignore_errors=True)
        else:
            print(f"[ASR-CER] 已保留合成音频：{TTS_AUDIO_DIR}（--keep-audio）", file=sys.stderr)
    return payload, exit_code


def _run_osr(args: argparse.Namespace, models: list[str], plan: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    payload = _build_payload(args, "osr")
    notes: list[str] = payload["notes"]
    notes.append(
        "OSR 参照文本由人工逐条核对后写入 --refs；禁止用 ASR 输出反推参照（循环论证）。"
        "真人 8kHz 电话带宽样本不等于家庭场景。"
    )
    payload["case_count"] = len(plan)
    payload["osr_dir"] = args.osr_dir.name
    exit_code = 0
    for model in models:
        entry = _run_model(model, plan, args, notes)
        if entry.get("valid_cases", 0) == 0:
            exit_code = 1
        payload["models"].append(entry)
    return payload, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asr_cer.py",
        description="ASR 字错率（CER）基线：TTS 已知文本（默认）或 OSR 真人样本（需 --refs）。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tts", action="store_true", help="TTS 合成已知文本模式（默认）")
    mode.add_argument("--osr", action="store_true", help="公开真人样本模式；必须同时提供 --refs")
    parser.add_argument(
        "--refs",
        type=Path,
        default=None,
        help='OSR 参照文本 JSON：{"文件名.wav": "人工核对后的参照文本"}',
    )
    parser.add_argument("--osr-dir", type=Path, default=DEFAULT_OSR_DIR, help="OSR WAV 目录（默认 var/asr/osr）")
    parser.add_argument(
        "--model",
        action="append",
        default=None,
        metavar="SIZE",
        help="模型，可重复（默认 small）；示例：--model tiny --model small --model medium",
    )
    parser.add_argument("--device", default="cuda", help="cuda 或 cpu（默认 cuda）")
    parser.add_argument("--compute-type", default="float16", help="默认 float16")
    parser.add_argument("--beam-size", type=int, default=1, help="默认 1")
    parser.add_argument(
        "--asr-python",
        default=None,
        help="ASR worker 解释器（可选；默认读 .env 的 ASR_PYTHON_BIN，再回落当前解释器）",
    )
    parser.add_argument(
        "--model-dir",
        default=None,
        help="HuggingFace 缓存根目录（可选；默认自动探测 ASR_MODEL_DIR / HF_HOME / 用户缓存）",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="JSON 输出路径（默认 reports/ASR-CER.json）")
    parser.add_argument("--md", default=str(DEFAULT_MD), help="Markdown 输出路径（默认 reports/ASR-CER.md）")
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="保留 var/asr_cer/tts/ 下的合成 WAV 以便人工复核（默认 try/finally 清理）",
    )
    return parser


def _resolve_under_root(path: Path) -> Path:
    return path if path.is_absolute() else (ROOT / path)


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdout()
    _load_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    models: list[str] = []
    for model in args.model or list(DEFAULT_MODELS):
        if model not in models:
            models.append(model)

    bootstrap_notes: list[str] = []
    asr_python, python_error = _resolve_asr_python(args.asr_python, bootstrap_notes)
    if python_error:
        print(f"[ASR-CER] {python_error}", file=sys.stderr)
        return 2
    args.asr_python_resolved = asr_python

    # 契约校验放在模型加载之前：缺 refs / refs 与音频对不上时立即退出，绝不触网。
    plan: list[dict[str, Any]] | None = None
    if args.osr:
        plan, error = load_osr_plan(args.refs, _resolve_under_root(args.osr_dir))
        if error:
            print(f"[ASR-CER] {error}", file=sys.stderr)
            return 2

    if not _require_asr():
        return 2

    # 即使调用方没有设环境变量，也强制离线：模型只从本地缓存读，绝不外发。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    if plan is not None:
        payload, exit_code = _run_osr(args, models, plan)
    else:
        payload, exit_code = _run_tts(args, models)
        if payload is None:
            return exit_code

    payload["notes"].extend(bootstrap_notes)
    payload["notes"].extend(normalization_notes())
    if any(entry.get("error") for entry in payload["models"]):
        payload["notes"].append("存在失败模型/用例：失败原因已写入 models[].error 与 cases[].error，未跳过。")
    if exit_code != 0:
        payload["notes"].append("本次运行有模型未能产出有效 CER，报告保留失败记录。")

    out_path = _resolve_under_root(Path(args.out))
    md_path = _resolve_under_root(Path(args.md))
    _write_outputs(payload, out_path, md_path)
    print_table(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())