"""F-012 / A5: prove the suite is discriminating by injecting defects and checking that it fails.

Each mutation is a single, textual, reversible change to a source file. The suite
is re-run per mutation; a mutation that survives means the tests are decorative.
Sources are restored byte-for-byte and the sha256 is re-checked after every run.

    python scripts/mutate.py            # 全部变异
    python scripts/mutate.py --only M2  # 单条排查
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS: list[dict[str, object]] = [
    {
        "id": "M1",
        "name": "风险等级取最小而非最大（危险动作被降级为安全）",
        "file": "hvsl/risk.py",
        "find": "top = max(hits, key=lambda r: _SEVERITY[r.risk])",
        "replace": "top = min(hits, key=lambda r: _SEVERITY[r.risk])",
        "tests": ["tests/test_risk_policy.py", "tests/test_safety_gate.py", "tests/test_evals.py"],
    },
    {
        "id": "M2",
        "name": "确认门禁不拦路（危险计划直接进入下发）",
        "file": "hvsl/engine.py",
        "find": "        if plan_risk is RiskLevel.CONFIRM:\n            return self._park(",
        "replace": "        if False and plan_risk is RiskLevel.CONFIRM:\n            return self._park(",
        "tests": ["tests/test_safety_gate.py", "tests/test_ha_backend.py"],
    },
    {
        "id": "M3",
        "name": "去重窗口失效（重复指令重复下发）",
        "file": "hvsl/idempotency.py",
        "find": 'if now - float(row["first_ts"]) < self.window_seconds:',
        "replace": 'if now - float(row["first_ts"]) < 0:',
        "tests": ["tests/test_idempotency.py"],
    },
    {
        "id": "M4",
        "name": "不再先写留痕（下发前无 DISPATCHING 记录）",
        "file": "hvsl/engine.py",
        "find": 'dispatch_state="DISPATCHING",',
        "replace": 'dispatch_state="executed",',
        "tests": ["tests/test_audit.py"],
    },
    {
        "id": "M5",
        "name": "工具命名空间校验被关闭（HA 2026.9 前缀约定失效）",
        "file": "hvsl/tools.py",
        "find": "        if not tool.name.startswith(f\"{self.namespace}.\"):\n            raise NamespaceViolationError(",
        "replace": "        if False:\n            raise NamespaceViolationError(",
        "tests": ["tests/test_tools_registry.py"],
    },
    {
        "id": "M6",
        "name": "跳过 Pydantic 参数校验（越界参数直接下发）",
        "file": "hvsl/engine.py",
        "find": "            validated = self.registry.validate_args(tool.name, args)",
        "replace": "            validated = args",
        "tests": ["tests/test_tools_registry.py", "tests/test_nlu_llm.py"],
    },
    {
        "id": "M7",
        "name": "确认令牌不校验过期（超时后仍可下发）",
        "file": "hvsl/gate.py",
        "find": "        if action.expires_at <= now:\n            self._pending.pop(token, None)",
        "replace": "        if False:\n            self._pending.pop(token, None)",
        "tests": ["tests/test_safety_gate.py"],
    },
    {
        "id": "M8",
        "name": "真实/模拟标注失真（全部标成真实设备）",
        "file": "hvsl/catalog.py",
        "find": 'provenance=Provenance(spec.get("provenance", "simulated")),',
        "replace": "provenance=Provenance.REAL,",
        "tests": ["tests/test_risk_policy.py", "tests/test_inventory.py"],
    },
    {
        "id": "M9",
        "name": "拒绝规则失效（可用对话关掉安全门禁 / 承诺厂商云接入）",
        "file": "hvsl/risk.py",
        "find": "        for rule in self.refusals:\n            if any(pattern in utterance for pattern in rule.patterns):\n                return rule",
        "replace": "        for rule in []:\n            if any(pattern in utterance for pattern in rule.patterns):\n                return rule",
        "tests": ["tests/test_safety_gate.py", "tests/test_evals.py"],
    },
    {
        "id": "M10",
        "name": "危险实体自报标签可覆盖评审过的清单",
        "file": "hvsl/backends/home_assistant.py",
        "find": "    tags = list(spec.get(\"tags\", []))",
        "replace": "    tags = list(attributes.get(\"tags\") or spec.get(\"tags\", []))",
        "tests": ["tests/test_ha_backend.py"],
    },
    {
        "id": "M11",
        "name": "取消“模型不得自行宣布批量”的钳制（一句话点亮全屋）",
        "file": "hvsl/engine.py",
        "find": "        intent = guards.clamp_plan(intent)",
        "replace": "        intent = intent",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
    {
        "id": "M12",
        "name": "定时表达不再被拒绝（“明天七点”当场执行）",
        "file": "hvsl/engine.py",
        "find": "        late = guards.unsupported_reason(text)",
        "replace": "        late = None",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
    {
        "id": "M13",
        "name": "设备类指词表校验被关闭（“把窗户打开”会去拉窗帘）",
        "file": "hvsl/resolver.py",
        "find": "        elif probe and not self._hints_any_name(probe, pool, domain):",
        "replace": "        elif False:",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
    {
        "id": "M14",
        "name": "关掉“影子解析纠偏”（模型可以替用户把歧义消掉）",
        "file": "hvsl/engine.py",
        "find": "        if self.cross_check is None or not intent.steps:",
        "replace": "        if True:",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
    {
        "id": "M15",
        "name": "空目标时不再回到用户原话找类指词（“把窗户打开”可执行窗帘）",
        "file": "hvsl/resolver.py",
        "find": "        probe = phrase or (grounding_text or \"\").strip()",
        "replace": "        probe = phrase",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
    {
        "id": "M16",
        "name": "模型可用 entity_id 绕过原话校验（自己挑一盏灯）",
        "file": "hvsl/guards.py",
        "find": "        return any(name and name in utterance for name in names)",
        "replace": "        return True",
        "tests": ["tests/test_guards.py", "tests/test_evals.py"],
    },
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests(paths: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", *paths],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    killed_by = re.findall(r"FAILED (\S+)", output)
    return proc.returncode, ", ".join(killed_by[:3]) or output.strip().splitlines()[-1][:120]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="变异测试：注入缺陷证明测试套件有鉴别力")
    parser.add_argument("--only", action="append", default=[], help="只跑指定变异 id，可重复")
    parser.add_argument("--out", default=str(ROOT / "reports" / "变异测试.json"))
    args = parser.parse_args(argv)

    selected = [m for m in MUTATIONS if not args.only or m["id"] in args.only]
    results: list[dict[str, object]] = []
    survivors = 0

    for mutation in selected:
        target = ROOT / str(mutation["file"])
        original = target.read_bytes()
        digest_before = sha256(target)
        source = original.decode("utf-8")
        # Windows 下源文件是 CRLF，而变异定位片段以 LF 书写，不归一化会导致
        # 多行片段（如 M2/M5/M7）匹配 0 次。只在匹配阶段归一化，还原仍走
        # write_bytes(original) 的字节级路径，不影响哈希校验。
        source = source.replace("\r\n", "\n")
        find = str(mutation["find"])
        if source.count(find) != 1:
            raise SystemExit(f"{mutation['id']}: 定位片段出现 {source.count(find)} 次，变异脚本需要更新")

        target.write_text(source.replace(find, str(mutation["replace"])), encoding="utf-8")
        started = time.perf_counter()
        try:
            code, evidence = run_tests(list(mutation["tests"]))  # type: ignore[arg-type]
        finally:
            target.write_bytes(original)
        elapsed = round(time.perf_counter() - started, 1)
        digest_after = sha256(target)
        killed = code != 0
        restored = digest_after == digest_before
        survivors += 0 if killed else 1

        if not restored:
            raise SystemExit(f"{mutation['id']}: 还原失败，哈希不一致（{target}）")
        results.append(
            {
                "id": mutation["id"],
                "name": mutation["name"],
                "file": mutation["file"],
                "tests": mutation["tests"],
                "killed": killed,
                "killed_by": evidence,
                "seconds": elapsed,
                "restored_ok": restored,
                "sha256": digest_before,
            }
        )
        print(f"[{mutation['id']}] {'捕获' if killed else '逃逸'}  {mutation['name']}  ({elapsed}s)")
        print(f"      证据：{evidence}")

    summary = {
        "mutations": len(results),
        "killed": sum(1 for r in results if r["killed"]),
        "survived": survivors,
        "restore_hash_verified": all(bool(r["restored_ok"]) for r in results),
        "killed_all": survivors == 0,
        "results": results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n变异 {summary['mutations']} 条，捕获 {summary['killed']} 条，逃逸 {summary['survived']} 条；"
          f"源文件哈希全部还原：{summary['restore_hash_verified']}")
    print(f"明细写入 {out_path}")
    return 1 if (summary["survived"] or not summary["restore_hash_verified"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
