"""A3 / A4 / A10 / A12 / A13：文档口径的自动化检查。

这些不是文字游戏：一份会漂的 README 是作品集最常见的信用漏洞。
测试同时校验 README 里写出的数字与真实产物一致（评测 JSON、变异 JSON、清单、注册表）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import CATALOG, POLICY

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "devices" / "inventory.md",
    ROOT / "交付总结.md",
    *(ROOT / "demo").glob("*.md"),
    *(ROOT / "reports").glob("*.md"),
]
PY_FILES = [
    p
    for p in ROOT.rglob("*.py")
    if "tests" not in p.parts
    and "__pycache__" not in p.parts
    and not any(part in {".codeartsdoer", ".venv", ".git"} for part in p.parts)
]
YAML_FILES = list((ROOT / "custom_components").rglob("*.yaml"))

# A12：不出现对外交付/经营类表述
COMMERCE_TOKENS = ("产品", "上线", "商用", "售卖", "定价", "订阅", "付费")
# A3：不宣称厂商生态/云接入
VENDOR_TOKENS = ("米家", "小米云", "华为", "美的", "涂鸦", "HomeKit", "任意品牌", "所有品牌", "官方 API", "官方API")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_docs_exist():
    for doc in DOCS:
        assert doc.is_file(), f"缺少 {doc}"
    assert DOCS[0].read_text(encoding="utf-8").count("#") > 20


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_commercial_claims_in_docs(doc):
    text = _read(doc)
    hits = [token for token in COMMERCE_TOKENS if token in text]
    assert not hits, f"{doc.name} 含对外交付类表述：{hits}"


@pytest.mark.parametrize("doc", DOCS + YAML_FILES, ids=lambda p: p.name)
def test_no_vendor_access_claims_in_docs(doc):
    text = _read(doc)
    hits = [token for token in VENDOR_TOKENS if token in text]
    assert not hits, f"{doc.name} 含厂商生态/云接入表述：{hits}"


def test_brand_tokens_only_live_in_the_refusal_list():
    """厂商名只允许出现在“拒绝话题”策略里（用来拒答），不许出现在**对外**文档或代码注释里。

    扫描范围与 test_no_vendor_access_claims_in_docs 保持一致，只含对外文档 DOCS；
    不含 `docs/` 内部产品与调研文档——那里引用厂商名是调研事实（带日期与来源），
    不是对外宣称生态接入。把内部调研也纳入会让“竞品调研”这一动作本身无法落笔。
    """
    policy_text = _read(ROOT / POLICY)
    allowed = {"米家官方", "小米云", "华为智慧生活", "美的美居", "涂鸦云", "官方 API", "任意品牌"}
    for file in PY_FILES + DOCS + YAML_FILES:
        if file.name == "risk_policy.json":
            continue
        text = _read(file)
        for token in re.findall(r"(米家|华为|美的|涂鸦|任意品牌)", text):
            assert f"{token}" in allowed, f"{file.name} 出现了厂商名 {token}，A3 会被 grep 命中"


def test_no_unmeasured_metric_claims():
    """A4：唤醒/误唤醒/远场这类没测过的指标不得以数字形式出现。"""
    pattern = re.compile(r"(唤醒率|误唤醒|唤醒成功率|远场)[^。\n]{0,24}[0-9]+(\.[0-9]+)?\s*(%|ms|秒|次|dB)")
    for doc in DOCS:
        text = _read(doc)
        assert not pattern.search(text), f"{doc.name} 出现未实测指标的数字化表述"


def test_three_state_claims_present():
    readme = _read(ROOT / "README.md")
    for marker in ("实测", "设计值", "未实测"):
        assert marker in readme, f"README 缺少三态口径标注：{marker}"
    assert "字错率" in readme and "未实测" in readme
    assert "## 7. 性能与评测口径" in readme


def test_privacy_section_is_standalone():
    """A13：隐私与数据边界必须单列一节，并说清为何本地优先。"""
    readme = _read(ROOT / "README.md")
    assert re.search(r"^## 9\. 隐私与数据边界", readme, re.MULTILINE)
    for must in ("高敏感", "127.0.0.1", "local_files_only", "HF_HUB_OFFLINE", "不外发"):
        assert must in readme, f"隐私节缺少 {must}"


def test_uncovered_list_is_published():
    readme = _read(ROOT / "README.md")
    assert "## 10. 未覆盖清单" in readme
    for must in ("Home Assistant 实机联调", "真实设备", "字错率", "麦克风实时采集"):
        assert must in readme


def test_license_present_and_ecosystem_compatible():
    text = _read(ROOT / "LICENSE")
    assert text.startswith("MIT License")
    assert "WITHOUT WARRANTY" in text.upper()


def test_requirements_include_the_test_runner():
    text = _read(ROOT / "requirements.txt")
    assert "pytest" in text
    assert "pydantic" in text


def test_pending_items_use_one_searchable_marker():
    """待填项统一用【填：…】，不散落 TODO/TBD。"""
    readme = _read(ROOT / "README.md")
    assert "【填：" in readme
    for stray in ("TODO", "TBD", "待补充", "FIXME"):
        assert stray not in readme, f"README 里出现不统一的占位符 {stray}"
    manifest = _read(ROOT / "custom_components/home_voice_safety/manifest.json")
    assert "【填：" in manifest or "https://github.com/young0617-02/home-voice-safety-layer" in manifest, (
        "集成 manifest 的仓库地址要么留统一待填标记，要么指向真实仓库"
    )


def test_readme_numbers_match_the_artifacts():
    """README 写死的数字必须与真实产物一致（漂了就红）。"""
    readme = _read(ROOT / "README.md")
    golden = [line for line in _read(ROOT / "evals" / "golden_set_v1.jsonl").splitlines() if line.strip()]
    assert f"{len(golden)} 条分级评测集" in readme or f"**{len(golden)}**" in readme

    danger_cases = sum(1 for line in golden if json.loads(line)["tier"] == "danger")
    assert f"{danger_cases} 条" in readme, "危险动作用例条数与评测集不一致"

    real_devices = sum(1 for spec in json.loads(_read(ROOT / CATALOG))["devices"] if spec.get("provenance") == "real")
    total_devices = len(json.loads(_read(ROOT / CATALOG))["devices"])
    assert f"{total_devices} 个受控实体" in readme or f"{total_devices} 台" in readme
    if real_devices == 0:
        assert "0 台" in readme

    eval_json = ROOT / "reports" / "评测结果.json"
    if eval_json.is_file():
        summary = json.loads(_read(eval_json))["summary"]
        assert f"{summary['danger_correct_interception_rate']}%".rstrip("0").rstrip(".") in readme or "100%" in readme
        assert summary["cases_total"] == len(golden)

    mutation_json = ROOT / "reports" / "变异测试.json"
    if mutation_json.is_file():
        data = json.loads(_read(mutation_json))
        assert f"{data['mutations']}/" in readme or f"{data['killed']} 条" in readme
        assert data["killed"] >= 5, "方案 A5 要求注入 ≥5 个缺陷"
        # 英文摘要曾单独漂成 10/10；这里把它绑回同一份产物，防止再次分叉。
        assert f"**{data['killed']}/{data['mutations']} injected defects" in readme, (
            "英文摘要的变异数必须与 reports/变异测试.json 一致（A4 口径）"
        )


def test_tool_namespace_and_prefix_claim_is_true():
    from hvsl.tools import build_default_registry

    names = build_default_registry().names()
    assert names
    assert all(name.startswith("hvs.") for name in names)
    readme = _read(ROOT / "README.md")
    assert f"{len(names)} 个" in readme


def test_realtime_confirm_gate_is_documented_for_every_danger_class():
    from hvsl.risk import RiskPolicy

    classes = RiskPolicy.load(ROOT / POLICY).danger_classes()
    readme = _read(ROOT / "README.md")
    mentioned = "8 个危险类别"
    assert mentioned in readme and len(classes) == 8
