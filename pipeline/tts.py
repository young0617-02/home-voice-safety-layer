"""F-005 TTS: local synthesis only, writing to a WAV file by default.

Cloud TTS is deliberately not implemented - it would move household content off
the machine for no benefit at portfolio scope. Speaking out loud is opt-in
(``speak_out_loud=True``) so nothing emits sound on someone's machine by default.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Provider = Literal["auto", "windows-sapi", "macos-say", "espeak-ng", "null"]

_SAPI_SCRIPT = """
param([string]$Text, [string]$Out, [int]$Rate, [switch]$Loud)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = $Rate
if ($Loud) { $synth.SetOutputToDefaultAudioDevice() } else { $synth.SetOutputToWaveFile($Out) }
$synth.Speak($Text)
$synth.Dispose()
if (-not $Loud) { Write-Output $Out }
"""

MAX_CHARS = 500


class TtsResult(BaseModel):
    text: str
    provider: str
    wall_seconds: float
    audio_path: str = ""
    audio_bytes: int = 0
    spoken: bool = False
    note: str = ""


class TextTooLong(ValueError):
    pass


class Tts:
    def __init__(
        self,
        *,
        provider: Provider = "auto",
        out_path: str | Path = "var/tts_last.wav",
        rate: int = 0,
        speak_out_loud: bool = False,
    ) -> None:
        self.requested = provider
        self.out_path = Path(out_path)
        self.rate = rate
        self.speak_out_loud = speak_out_loud
        self.provider = self._resolve(provider)

    @staticmethod
    def _resolve(provider: Provider) -> str:
        if provider != "auto":
            return provider
        system = platform.system().lower()
        if system == "windows":
            return "windows-sapi"
        if system == "darwin":
            return "macos-say"
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            return "espeak-ng"
        return "null"

    def availability(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "requested": self.requested,
            "local_only": self.provider != "null",
            "binary": {
                "windows-sapi": shutil.which("powershell") or shutil.which("pwsh") or "",
                "macos-say": shutil.which("say") or "",
                "espeak-ng": shutil.which("espeak-ng") or shutil.which("espeak") or "",
            }.get(self.provider, ""),
        }

    def synthesize(self, text: str) -> TtsResult:
        text = (text or "").strip()
        if not text:
            return TtsResult(text="", provider=self.provider, wall_seconds=0.0, note="空文本，未合成")
        if len(text) > MAX_CHARS:
            raise TextTooLong(f"TTS text too long ({len(text)} > {MAX_CHARS}); keep replies short")

        started = time.perf_counter()
        handler = getattr(self, f"_{self.provider.replace('-', '_')}", self._null)
        path, bytes_out, note = handler(text)
        return TtsResult(
            text=text,
            provider=self.provider,
            wall_seconds=round(time.perf_counter() - started, 3),
            audio_path=path,
            audio_bytes=bytes_out,
            spoken=self.speak_out_loud and self.provider != "null",
            note=note,
        )

    # ---------------------------------------------------------------- providers

    def _windows_sapi(self, text: str) -> tuple[str, int, str]:
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return "", 0, "powershell 不可用，已退化为文本输出"
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        script = self.out_path.parent / "_tts.ps1"
        script.write_text(_SAPI_SCRIPT, encoding="utf-8")
        self.out_path.unlink(missing_ok=True)
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Text", text, "-Out", str(self.out_path), "-Rate", str(self.rate)]
            + (["-Loud"] if self.speak_out_loud else []),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if proc.returncode != 0:
            return "", 0, f"SAPI 合成失败：{proc.stderr.strip()[:200]}"
        size = self.out_path.stat().st_size if self.out_path.is_file() else 0
        return str(self.out_path), size, ""

    def _macos_say(self, text: str) -> tuple[str, int, str]:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        aiff = self.out_path.with_suffix(".aiff")
        cmd = ["say", "-o", str(aiff), text]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return "", 0, f"say 失败：{proc.stderr.strip()[:200]}"
        return str(aiff), aiff.stat().st_size, ""

    def _espeak_ng(self, text: str) -> tuple[str, int, str]:
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not exe:
            return "", 0, "espeak 未安装，已退化为文本输出"
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([exe, "-w", str(self.out_path), text], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return "", 0, f"espeak 失败：{proc.stderr.strip()[:200]}"
        return str(self.out_path), self.out_path.stat().st_size, ""

    def _null(self, text: str) -> tuple[str, int, str]:
        print(f"[TTS-文本回读] {text}", file=sys.stderr)
        return "", 0, "无本地 TTS，已退化为文本回读"
