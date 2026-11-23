"""catalog-aware 同音纠错测试。"""
from __future__ import annotations

import pytest

from pipeline.nlu import _correct_entities


def test_corrects_homophone_entity() -> None:
    pytest.importorskip("pypinyin")
    text, notes = _correct_entities("开可听打灯", ["客厅大灯"])
    assert text == "开客厅大灯"
    assert notes == ["同音纠错：可听打灯→客厅大灯"]


def test_exact_entity_is_not_touched() -> None:
    assert _correct_entities("打开客厅大灯", ["客厅大灯"]) == ("打开客厅大灯", [])


def test_unrelated_text_is_not_touched() -> None:
    assert _correct_entities("邮举门前有一个邮箱", ["客厅大灯"]) == ("邮举门前有一个邮箱", [])


def test_short_entity_is_skipped() -> None:
    assert _correct_entities("开门", ["门"]) == ("开门", [])


def test_low_similarity_is_not_corrected() -> None:
    assert _correct_entities("开空调", ["客厅大灯"]) == ("开空调", [])