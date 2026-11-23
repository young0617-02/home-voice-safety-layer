"""ASR 输出繁转简（opencc 可选）测试。"""
from __future__ import annotations

import builtins

import pytest

from pipeline import asr


def test_to_simplified_empty() -> None:
    assert asr._to_simplified("") == ""


def test_to_simplified_when_opencc_available() -> None:
    pytest.importorskip("opencc")
    assert asr._to_simplified("校園閃電") == "校园闪电"


def test_to_simplified_degrades_without_opencc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr, "_OPENCC", None)
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "opencc":
            raise ImportError("opencc not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert asr._to_simplified("校園") == "校園"