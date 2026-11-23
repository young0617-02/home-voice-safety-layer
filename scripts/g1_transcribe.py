"""G1 访谈录音转写：本机音频/目录 -> `<stem>.txt` + `<stem>.json`。

业务背景：G1 用户访谈会留下 45′ 左右的录音，访谈记录需要可检索、可复核的
文本，但录音本身属于敏感个人信息。因此本工具**只做本机转写**（复用
:class:`pipeline.asr.LocalAsr`：in-process 或子进程，`HF_HUB_OFFLINE=1`），
不联网上传、不自动删除源音频，并把隐私口径打印给操作者。

用法：
    python scripts/g1_transcribe.py --input var/g1/audio/P01.m4a
    python scripts/g1_transcribe.py --input var/g1/audio --out-dir var/g1/transcripts
    python scripts/g1_transcribe.py --input var/g1/audio --dry-run
    python scripts/g1_transcribe.py --input a.wav --language zh --model small --max-minutes 60

行为约定：
    * 发现 `.wav/.mp3/.m4a/.flac/.ogg`；目录**递归一层**（子目录的直接文件），
      按文件名排序，便于逐户复核。
    * 长度保护：WAV 用 ``wave`` 读时长；非 WAV 用 ``ffprobe``（不可用则告警并
      继续，不阻断转写）。超过 ``--max-minutes`` 的文件被拒绝并打印原因，
      其他文件照常处理。
    * 输出：纯文本 ``<stem>.txt`` + 元数据 ``<stem>.json``
      （``text/segments/duration_seconds/language/model/source``）；同名输出
      自动加 ``_2``、``_3`` 后缀，不覆盖既有转写。
    * 依赖缺失（未装 faster-whisper / worker 起不来）：打印
      ``uv sync --extra asr`` 与 ``uv pip install faster-whisper`` 提示，
      退出码 2；``--dry-run`` 完全不导入 ASR，只打印计划。
    * 退出码：全部成功 0；有文件失败 1；依赖缺失 2；参数错误 2。

隐私：转写文本属敏感个人信息，默认 30 天内删除；脚本不联网上传；不删除源音频。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT_DIR = ROOT / "var" / "g1" / "transcripts"
DEFAULT_LANGUAGE = "zh"
DEFAULT_MODEL = "small"
DEFAULT_MAX_MINUTES = 60.0

AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".ogg")

PRIVACY_NOTICE = (
    "转写文本属敏感个人信息，默认 30 天内删除；脚本不联网上传。",
    "脚本不改写、不删除源音频；访谈结束后请按录音同意书自行清理录音文件。",
)

DEPENDENCY_HINT = (
    "  uv sync --extra asr\n"
    "  或：uv pip install faster-whisper"
)

TranscriberFactory = Callable[..., Any]

STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class AsrUnavailable(RuntimeError):
    """本地 ASR 运行时不可用（未安装、worker 启动失败或模型缺失）。

    ``pipeline.asr`` 现有异常名为 ``FasterWhisperUnavailable``；这里同时按
    类型名识别，避免脚本与 pipeline 的异常命名耦合。
    """


def discover_audio_files(input_path: Path) -> list[Path]:
    """返回待转写音频：单文件原样返回；目录递归一层，按文件名排序。"""
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in AUDIO_SUFFIXES else []

    found: list[Path] = [
        child
        for child in input_path.iterdir()
        if child.is_file() and child.suffix.lower() in AUDIO_SUFFIXES
    ]
    subdirs = sorted(
        (child for child in input_path.iterdir() if child.is_dir()),
        key=lambda child: child.name.lower(),
    )
    # 递归一层：子目录自身的直接文件，不再往下钻，避免误把归档/备份目录全卷进来。
    for subdir in subdirs:
        found.extend(
            child
            for child in subdir.iterdir()
            if child.is_file() and child.suffix.lower() in AUDIO_SUFFIXES
        )
    return sorted(found, key=lambda path: (path.name.lower(), str(path).lower()))


def _probe_with_ffprobe(path: Path) -> tuple[float | None, str]:
    executable = shutil.which("ffprobe")
    if not executable:
        return None, "未找到 ffprobe，跳过非 WAV 时长校验（--max-minutes 仅对 WAV 生效）；建议安装 ffmpeg"
    try:
        proc = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffprobe 调用失败，跳过时长校验：{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        return None, f"ffprobe 无法读取时长（{detail}），跳过时长校验"
    try:
        return float((proc.stdout or "").strip()), ""
    except ValueError:
        return None, "ffprobe 输出无法解析为秒数，跳过时长校验"


def probe_duration(path: Path) -> tuple[float | None, str]:
    """读取音频时长；返回 ``(seconds, warning)``，读取失败时 seconds 为 None。"""
    if path.suffix.lower() != ".wav":
        return _probe_with_ffprobe(path)
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        return None, f"WAV 时长读取失败，跳过时长校验：{type(exc).__name__}: {exc}"
    if rate <= 0:
        return None, "WAV 采样率无效，跳过时长校验"
    return frames / float(rate), ""


def _allocate_output_paths(out_dir: Path, stem: str, reserved: set[str]) -> tuple[Path, Path]:
    """分配一对未被占用/未预留的 txt+json 路径；冲突时追加 ``_2``、``_3``。"""
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        txt_path = out_dir / f"{stem}{suffix}.txt"
        json_path = out_dir / f"{stem}{suffix}.json"
        names = {txt_path.name, json_path.name}
        if not names & reserved and not txt_path.exists() and not json_path.exists():
            reserved.update(names)
            return txt_path, json_path
        index += 1


def _segments_payload(result: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for segment in getattr(result, "segments", None) or []:
        if isinstance(segment, dict):
            start = segment.get("start", 0.0)
            end = segment.get("end", 0.0)
            text = segment.get("text", "")
        else:
            start = getattr(segment, "start", 0.0)
            end = getattr(segment, "end", 0.0)
            text = getattr(segment, "text", "")
        segments.append(
            {
                "start": round(float(start or 0.0), 3),
                "end": round(float(end or 0.0), 3),
                "text": str(text or ""),
            }
        )
    return segments


def write_transcript(
    source: Path,
    result: Any,
    txt_path: Path,
    json_path: Path,
    *,
    language: str,
    model: str,
    probed_duration: float | None,
) -> dict[str, Any]:
    """落盘 txt+json；json 字段固定为 text/segments/duration_seconds/language/model/source。"""
    text = str(getattr(result, "text", "") or "")
    audio_seconds = float(getattr(result, "audio_seconds", 0.0) or 0.0)
    duration_seconds = audio_seconds if audio_seconds > 0 else float(probed_duration or 0.0)
    payload: dict[str, Any] = {
        "text": text,
        "segments": _segments_payload(result),
        "duration_seconds": round(duration_seconds, 3),
        "language": str(getattr(result, "language", "") or language),
        "model": str(getattr(result, "model", "") or model),
        "source": str(source),
    }
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text(
        text + ("\n" if text and not text.endswith("\n") else ""),
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _default_transcriber_factory(**kwargs: Any) -> Any:
    """默认构造 ``pipeline.asr.LocalAsr``；只在真正要转写时才导入 ASR。"""
    from pipeline.asr import LocalAsr

    return LocalAsr(**kwargs)


def _load_settings() -> Any | None:
    """复用 hvsl 配置（含 .env）拿 ASR_PYTHON_BIN/ASR_MODEL_DIR/ASR_DEVICE。"""
    try:
        from hvsl.config import Settings
    except Exception:
        return None
    try:
        return Settings.load()
    except Exception:
        return None


def asr_options(args: argparse.Namespace) -> dict[str, Any]:
    settings = _load_settings()
    python_bin = (getattr(settings, "asr_python", "") or "") if settings else ""
    model_dir = (getattr(settings, "asr_model_dir", "") or "") if settings else ""
    device = (getattr(settings, "asr_device", "") or "") if settings else ""
    return {
        "python_bin": python_bin or None,
        "model_dir": model_dir or None,
        "model_size": args.model,
        "device": device or "cuda",
        "language": args.language,
    }


def _is_dependency_error(exc: BaseException) -> bool:
    if isinstance(exc, (ImportError, AsrUnavailable)):
        return True
    return type(exc).__name__ in {"FasterWhisperUnavailable", "AsrUnavailable"}


def _report_dependency_missing(exc: BaseException) -> None:
    print(
        f"[fatal] 无法使用本地 ASR：{type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    print(
        "请先安装 ASR 依赖（本脚本不联网下载模型）：\n" + DEPENDENCY_HINT,
        file=sys.stderr,
    )
    print(
        "模型权重需提前放到本机：在 .env 设置 ASR_MODEL_DIR，"
        "或设置 ASR_PYTHON_BIN 指向已装好 faster-whisper 的 venv。",
        file=sys.stderr,
    )


def _print_privacy_notice() -> None:
    for line in PRIVACY_NOTICE:
        print(f"[隐私] {line}")


def _print_capacity_notice(max_minutes: float) -> None:
    print(f"G1 转写：超过 {max_minutes:g} 分钟的音频将被拒绝，其余文件继续处理。")


def _dry_run(
    files: Sequence[Path],
    out_dir: Path,
    max_seconds: float,
    args: argparse.Namespace,
) -> int:
    """只列计划：不导入 ASR、不建目录、不调用 transcriber、不写任何文件。"""
    print("[dry-run] 不导入 ASR、不写文件；仅列出将处理的文件与输出路径。")
    reserved: set[str] = set()
    planned = skipped = 0
    total = len(files)
    for index, path in enumerate(files, 1):
        duration, warning = probe_duration(path)
        if warning:
            print(f"[警告] {path.name}：{warning}")
        if duration is not None and duration > max_seconds:
            print(
                f"[dry-run] {index}/{total} 拒绝 {path}："
                f"时长 {duration / 60:.1f} 分钟 > 上限 {args.max_minutes:g} 分钟"
            )
            skipped += 1
            continue
        txt_path, json_path = _allocate_output_paths(out_dir, path.stem, reserved)
        size_note = f"时长 {duration:.1f}s，" if duration is not None else ""
        print(
            f"[dry-run] {index}/{total} 将转写 {path}（{size_note}"
            f"语言 {args.language}，模型 {args.model}） -> {txt_path} + {json_path}"
        )
        planned += 1
    print(
        f"[dry-run] 计划转写 {planned} 个，按长度上限拒绝 {skipped} 个；"
        f"未创建输出目录 {out_dir}"
    )
    return 0


def _transcribe_files(
    files: Sequence[Path],
    out_dir: Path,
    max_seconds: float,
    args: argparse.Namespace,
    transcriber_factory: TranscriberFactory,
) -> int:
    transcriber: Any | None = None
    reserved: set[str] = set()
    success = skipped = failures = 0
    dependency_missing = False
    total = len(files)

    for index, path in enumerate(files, 1):
        duration, warning = probe_duration(path)
        if warning:
            print(f"[警告] {path.name}：{warning}")
        if duration is not None and duration > max_seconds:
            print(
                f"[跳过] {index}/{total} {path}："
                f"时长 {duration / 60:.1f} 分钟 > 上限 {args.max_minutes:g} 分钟"
            )
            skipped += 1
            continue

        if transcriber is None:
            try:
                transcriber = transcriber_factory(**asr_options(args))
            except Exception as exc:
                if _is_dependency_error(exc):
                    _report_dependency_missing(exc)
                    dependency_missing = True
                    break
                # 构造失败也算该文件失败，但不阻断其他文件（工厂可能按文件重试）。
                print(f"[失败] {index}/{total} {path}：{type(exc).__name__}: {exc}")
                failures += 1
                continue

        try:
            result = transcriber.transcribe(str(path))
            txt_path, json_path = _allocate_output_paths(out_dir, path.stem, reserved)
            payload = write_transcript(
                path,
                result,
                txt_path,
                json_path,
                language=args.language,
                model=args.model,
                probed_duration=duration,
            )
        except Exception as exc:
            if _is_dependency_error(exc):
                _report_dependency_missing(exc)
                dependency_missing = True
                break
            print(f"[失败] {index}/{total} {path}：{type(exc).__name__}: {exc}")
            failures += 1
            continue

        success += 1
        print(
            f"[ok] {index}/{total} {path} -> {txt_path.name} + {json_path.name}"
            f"（{payload['duration_seconds']:.1f}s，{len(payload['text'])} 字）"
        )

    if dependency_missing:
        print(
            f"转写中止：成功 {success}，跳过 {skipped}，失败 {failures}；"
            f"依赖缺失，退出码 2。",
            file=sys.stderr,
        )
        return 2
    if failures:
        print(f"完成：成功 {success}，跳过 {skipped}，失败 {failures}；退出码 1。")
        return 1
    print(f"完成：成功 {success}，跳过 {skipped}，失败 0。")
    return 0


def run(
    args: argparse.Namespace,
    *,
    transcriber_factory: TranscriberFactory | None = None,
) -> int:
    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    max_seconds = float(args.max_minutes) * 60.0
    files = discover_audio_files(input_path)

    _print_privacy_notice()
    print(f"G1 转写：输入 {input_path}，发现 {len(files)} 个音频文件。")
    _print_capacity_notice(float(args.max_minutes))
    if not files:
        print("未发现支持的音频文件（.wav/.mp3/.m4a/.flac/.ogg）。")
        return 0
    if args.dry_run:
        return _dry_run(files, out_dir, max_seconds, args)
    return _transcribe_files(
        files,
        out_dir,
        max_seconds,
        args,
        transcriber_factory or _default_transcriber_factory,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G1 访谈录音转写（本地 faster-whisper，不上传）"
    )
    parser.add_argument("--input", required=True, help="音频文件或目录（目录递归一层）")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help=f"输出目录（默认 {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="转写语言（默认 zh）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper 模型（默认 small）")
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=DEFAULT_MAX_MINUTES,
        help=f"单文件时长上限，超过则拒绝（默认 {DEFAULT_MAX_MINUTES:g} 分钟）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将处理的文件与输出路径，不导入 ASR、不写文件",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    transcriber_factory: TranscriberFactory | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_minutes) or args.max_minutes <= 0:
        parser.error("--max-minutes 必须是正数")
    input_path = Path(args.input)
    if not input_path.exists():
        parser.error(f"输入不存在：{input_path}")
    if input_path.is_file() and input_path.suffix.lower() not in AUDIO_SUFFIXES:
        parser.error(
            f"不支持的音频格式：{input_path.suffix or '（无后缀）'}；"
            f"支持 {', '.join(AUDIO_SUFFIXES)}"
        )
    return run(args, transcriber_factory=transcriber_factory)


if __name__ == "__main__":
    raise SystemExit(main())