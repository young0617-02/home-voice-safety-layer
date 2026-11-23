"""G2 订单台账测试。

所有订单均为 synthetic；全部输出写到 pytest tmp_path，不触碰仓库 var/。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g2_orders  # noqa: E402

SYNTHETIC = "synthetic"


def order_row(**overrides: str) -> dict[str, str]:
    row = {
        "order_id": "ORD-001",
        "household_id": "HH-01",
        "plan": "A",
        "amount": "99",
        "currency": "CNY",
        "created_at": "2026-11-01 10:00:00",
        "paid_at": "2026-11-01 10:05:00",
        "payment_method": SYNTHETIC,
        "payment_ref": "REF-001",
        "refund_status": "none",
        "refund_requested_at": "",
        "refunded_at": "",
        "refund_amount": "0",
        "consent_version": "v0.1",
        "note": f"{SYNTHETIC} test order",
        "synthetic": "true",
        "cohort": "g2",
    }
    row.update(overrides)
    return row


def make_ledger(paid_count: int, refunded: int = 0) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index in range(1, paid_count + 1):
        plan = ("A", "B", "C")[(index - 1) % 3]
        amount = str(int(g2_orders.PLAN_PRICES[plan]))
        row = order_row(
            order_id=f"ORD-{index:03d}",
            household_id=f"HH-{index:02d}",
            plan=plan,
            amount=amount,
            payment_ref=f"REF-{index:03d}",
        )
        if index <= refunded:
            row.update(
                {
                    "refund_status": "refunded",
                    "refund_requested_at": "2026-11-05 09:00:00",
                    "refunded_at": "2026-11-06 15:00:00",
                    "refund_amount": amount,
                }
            )
        rows.append(row)
    return rows


def write_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    g2_orders.write_orders(path, rows)


def test_new_creates_template_and_field_doc(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    assert g2_orders.main(["new", "--out", str(orders_path)]) == 0

    assert orders_path.is_file()
    assert orders_path.with_suffix(".fields.md").is_file()
    rows = g2_orders.read_orders(orders_path)
    assert len(rows) == 1
    assert rows[0]["synthetic"] == "true"
    assert "SYNTHETIC DEMO" in rows[0]["note"]
    assert g2_orders.validate_orders(rows) == []


def test_new_requires_force_to_overwrite(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    assert g2_orders.main(["new", "--out", str(orders_path)]) == 0
    assert g2_orders.main(["new", "--out", str(orders_path)]) == 2
    assert g2_orders.main(["new", "--out", str(orders_path), "--force"]) == 0


def test_validate_clean_ledger(tmp_path: Path) -> None:
    rows = make_ledger(2)
    assert g2_orders.validate_orders(rows) == []

    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, rows)
    assert g2_orders.main(["validate", "--orders", str(orders_path)]) == 0


def test_amount_plan_mismatch_rejected(tmp_path: Path) -> None:
    rows = [order_row(plan="A", amount="199")]
    issues = g2_orders.validate_orders(rows)
    assert any(issue.field == "amount" for issue in issues)

    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, rows)
    assert g2_orders.main(["validate", "--orders", str(orders_path)]) == 1


def test_duplicate_household_paid_requires_force(tmp_path: Path) -> None:
    rows = [
        order_row(order_id="ORD-001", household_id="HH-01", payment_ref="REF-001"),
        order_row(order_id="ORD-002", household_id="HH-01", payment_ref="REF-002"),
    ]
    issues = g2_orders.validate_orders(rows)
    assert any("重复有效付费单" in issue.message for issue in issues)

    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, rows)
    assert g2_orders.main(["validate", "--orders", str(orders_path)]) == 1
    assert (
        g2_orders.main(["validate", "--orders", str(orders_path), "--force"]) == 0
    )


def test_paid_at_before_created_at_rejected() -> None:
    rows = [
        order_row(
            created_at="2026-11-02 10:00:00",
            paid_at="2026-11-01 10:00:00",
        )
    ]
    issues = g2_orders.validate_orders(rows)
    assert any(issue.field == "paid_at" for issue in issues)


def test_refund_amount_exceeds_paid_rejected() -> None:
    rows = [
        order_row(
            plan="A",
            amount="99",
            refund_status="refunded",
            refund_requested_at="2026-11-05 09:00:00",
            refunded_at="2026-11-06 10:00:00",
            refund_amount="199",
        )
    ]
    issues = g2_orders.validate_orders(rows)
    assert any(issue.field == "refund_amount" for issue in issues)


def test_refund_state_machine_and_fields(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, [order_row(order_id="ORD-001")])

    # none → refunded 非法（必须先 requested）
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "ORD-001",
                "--status",
                "refunded",
                "--amount",
                "99",
                "--at",
                "2026-11-03 10:00:00",
            ]
        )
        == 1
    )

    # none → requested 合法
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "ORD-001",
                "--status",
                "requested",
                "--at",
                "2026-11-03 10:00:00",
            ]
        )
        == 0
    )
    row = g2_orders.read_orders(orders_path)[0]
    assert row["refund_status"] == "requested"
    assert row["refund_requested_at"] == "2026-11-03 10:00:00"

    # requested → refunded 缺 --amount 是参数错误
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "ORD-001",
                "--status",
                "refunded",
            ]
        )
        == 2
    )

    # requested → refunded 合法
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "ORD-001",
                "--status",
                "refunded",
                "--amount",
                "99",
                "--at",
                "2026-11-05 10:00:00",
            ]
        )
        == 0
    )
    row = g2_orders.read_orders(orders_path)[0]
    assert row["refund_status"] == "refunded"
    assert row["refund_amount"] == "99"
    assert row["refunded_at"] == "2026-11-05 10:00:00"

    # refunded 不可逆
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "ORD-001",
                "--status",
                "requested",
            ]
        )
        == 1
    )


def test_refund_unknown_order_returns_one(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, [order_row()])
    assert (
        g2_orders.main(
            [
                "refund",
                "--orders",
                str(orders_path),
                "--order-id",
                "NOT-EXIST",
                "--status",
                "requested",
            ]
        )
        == 1
    )


def test_refund_rate_20_percent_passes() -> None:
    summary = g2_orders.compute_summary(make_ledger(10, refunded=2))
    assert summary["paid_households"] == 10
    assert summary["refunded_count"] == 2
    assert summary["refund_rate"] == pytest.approx(0.2)
    assert summary["g2_pass"] is True
    assert summary["g2_action"] == "go"
    assert summary["gaps"] == []


def test_refund_rate_30_percent_fails() -> None:
    summary = g2_orders.compute_summary(make_ledger(10, refunded=3))

    assert summary["refunded_count"] == 3
    assert summary["refund_rate"] == pytest.approx(0.3)
    assert summary["g2_pass"] is False
    assert summary["g2_action"] == "降价或缩范围"
    assert any("退款率" in gap for gap in summary["gaps"])


def test_paid_nine_requires_price_cut() -> None:
    summary = g2_orders.compute_summary(make_ledger(9))
    assert summary["paid_households"] == 9
    assert summary["g2_pass"] is False
    assert summary["g2_action"] == "降价或缩范围"
    assert any("缺 1 户" in gap for gap in summary["gaps"])


def test_paid_four_requires_b2b() -> None:
    summary = g2_orders.compute_summary(make_ledger(4))
    assert summary["paid_households"] == 4
    assert summary["g2_pass"] is False
    assert summary["g2_action"] == "转 B2B"


def test_report_writes_summary_and_markdown(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, make_ledger(10, refunded=2))
    out_dir = tmp_path / "out"

    assert (
        g2_orders.main(
            ["report", "--orders", str(orders_path), "--out-dir", str(out_dir)]
        )
        == 0
    )
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["paid_households"] == 10
    assert summary["refunded_count"] == 2
    assert summary["g2_pass"] is True

    markdown = (out_dir / "g2_report.md").read_text(encoding="utf-8")
    for marker in ("G2 判据", "按户", "按单", "go", "退款率", "缺口"):
        assert marker in markdown


def test_report_rejects_invalid_ledger(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv"
    write_ledger(orders_path, [order_row(plan="A", amount="199")])
    out_dir = tmp_path / "out"
    assert (
        g2_orders.main(
            ["report", "--orders", str(orders_path), "--out-dir", str(out_dir)]
        )
        == 1
    )
    assert not (out_dir / "summary.json").exists()


def test_demo_artifacts_numbers_and_synthetic_markings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert g2_orders.main(["demo", "--out-dir", str(tmp_path)]) == 0
    out_dir = tmp_path / "demo" / "out"
    for name in ("orders.csv", "summary.json", "g2_report.md"):
        assert (out_dir / name).is_file(), f"缺少 demo/out/{name}"

    orders_text = (out_dir / "orders.csv").read_text(encoding="utf-8-sig")
    assert "SYNTHETIC DEMO" in orders_text

    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["synthetic_demo"] is True
    assert summary["paid_households"] == 10
    assert summary["paid_orders"] == 10
    assert summary["refunded_count"] == 1
    assert summary["refund_rate"] == pytest.approx(0.1)
    assert summary["gross_revenue"] == 1990
    assert summary["net_revenue"] == 1891
    assert summary["by_plan"]["A"]["households"] == 3
    assert summary["by_plan"]["B"]["households"] == 4
    assert summary["by_plan"]["C"]["households"] == 3
    assert summary["g2_pass"] is True
    assert summary["g2_action"] == "go"

    markdown = (out_dir / "g2_report.md").read_text(encoding="utf-8")
    assert markdown.startswith("> ⚠️ **SYNTHETIC DEMO")
    assert "1990" in markdown
    assert "G2 结论" in markdown

    output = capsys.readouterr().out
    assert "SYNTHETIC DEMO" in output
    assert "退款率 10.0%" in output


def test_reconcile_detects_missing_order_and_amount_mismatch(
    tmp_path: Path,
) -> None:
    orders = [
        order_row(order_id="ORD-001", payment_ref="REF-001", amount="99"),
        order_row(
            order_id="ORD-002",
            household_id="HH-02",
            plan="B",
            amount="199",
            payment_ref="REF-002",
        ),
    ]
    payments = [
        {
            "payment_ref": "REF-001",
            "amount": "99",
            "currency": "CNY",
            "paid_at": "2026-11-01 10:05:00",
            "household_id": "HH-01",
            "status": "paid",
        },
        {
            "payment_ref": "REF-002",
            "amount": "109",
            "currency": "CNY",
            "paid_at": "2026-11-01 10:05:00",
            "household_id": "HH-02",
            "status": "paid",
        },
        {
            "payment_ref": "REF-003",
            "amount": "299",
            "currency": "CNY",
            "paid_at": "2026-11-02 10:05:00",
            "household_id": "HH-03",
            "status": "paid",
        },
    ]
    result = g2_orders.reconcile_orders(orders, payments)
    assert result["ok"] is False
    assert [item["payment_ref"] for item in result["missing_in_orders"]] == [
        "REF-003"
    ]
    assert [item["payment_ref"] for item in result["amount_mismatch"]] == [
        "REF-002"
    ]

    orders_path = tmp_path / "orders.csv"
    payments_path = tmp_path / "payments.csv"
    out_path = tmp_path / "reconcile.json"
    write_ledger(orders_path, orders)
    with payments_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(payments[0]))
        writer.writeheader()
        writer.writerows(payments)

    assert (
        g2_orders.main(
            [
                "reconcile",
                "--orders",
                str(orders_path),
                "--payments",
                str(payments_path),
                "--out",
                str(out_path),
            ]
        )
        == 1
    )
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["ok"] is False
    assert saved["amount_mismatch"][0]["order_amount"] == 199
    assert saved["amount_mismatch"][0]["payment_amount"] == 109


def test_reconcile_clean_returns_zero(tmp_path: Path) -> None:
    orders = [
        order_row(order_id="ORD-001", payment_ref="REF-001", amount="99"),
        order_row(
            order_id="ORD-002",
            household_id="HH-02",
            plan="B",
            amount="199",
            payment_ref="REF-002",
        ),
    ]
    payments = [
        {
            "payment_ref": "REF-001",
            "amount": "99",
            "currency": "CNY",
            "paid_at": "2026-11-01 10:05:00",
            "household_id": "HH-01",
            "status": "paid",
        },
        {
            "payment_ref": "REF-002",
            "amount": "199",
            "currency": "CNY",
            "paid_at": "2026-11-01 10:05:00",
            "household_id": "HH-02",
            "status": "paid",
        },
    ]
    result = g2_orders.reconcile_orders(orders, payments)
    assert result["ok"] is True
    assert result["matched"] == 2

    orders_path = tmp_path / "orders.csv"
    payments_path = tmp_path / "payments.csv"
    write_ledger(orders_path, orders)
    with payments_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(payments[0]))
        writer.writeheader()
        writer.writerows(payments)
    assert (
        g2_orders.main(
            [
                "reconcile",
                "--orders",
                str(orders_path),
                "--payments",
                str(payments_path),
            ]
        )
        == 0
    )


def test_reconcile_skips_refund_payment_rows(tmp_path: Path) -> None:
    orders = [order_row(order_id="ORD-001", payment_ref="REF-001", amount="99")]
    payments = [
        {
            "payment_ref": "REF-001",
            "amount": "99",
            "currency": "CNY",
            "paid_at": "2026-11-01 10:05:00",
            "household_id": "HH-01",
            "status": "paid",
        },
        {
            "payment_ref": "REF-999",
            "amount": "-99",
            "currency": "CNY",
            "paid_at": "2026-11-05 10:05:00",
            "household_id": "HH-01",
            "status": "refunded",
        },
    ]
    payments_path = tmp_path / "payments.csv"
    with payments_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(payments[0]))
        writer.writeheader()
        writer.writerows(payments)

    loaded = g2_orders.read_payments(payments_path)
    assert len(loaded) == 1
    assert g2_orders.reconcile_orders(orders, loaded)["ok"] is True


def test_missing_orders_file_returns_two(tmp_path: Path) -> None:
    assert (
        g2_orders.main(
            ["validate", "--orders", str(tmp_path / "missing.csv")]
        )
        == 2
    )

def test_comment_lines_in_template_are_ignored(tmp_path: Path) -> None:
    """模板末尾的 # 注释段不能被当成订单行。"""
    orders_path = tmp_path / "orders.csv"
    with orders_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(g2_orders.ORDER_FIELDS))
        row = order_row()
        writer.writerow([row[field] for field in g2_orders.ORDER_FIELDS])
        writer.writerow([])
        writer.writerow(["# 以下为说明，导入前可删除"])
        writer.writerow(["# synthetic=1 的行不计入判据"])

    rows = g2_orders.read_orders(orders_path)
    assert len(rows) == 1
    assert g2_orders.validate_orders(rows) == []

def test_old_16_column_csv_defaults_to_cohort_g2(tmp_path: Path) -> None:
    rows = make_ledger(2)
    headers = [field for field in g2_orders.ORDER_FIELDS if field != "cohort"]
    old_path = tmp_path / "orders_16.csv"
    with old_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(
            [{key: row.get(key, "") for key in headers} for row in rows]
        )

    loaded = g2_orders.read_orders(old_path)
    assert len(loaded) == 2
    assert [row["cohort"] for row in loaded] == ["g2", "g2"]
    assert g2_orders.validate_orders(loaded) == []
    assert g2_orders.main(["validate", "--orders", str(old_path)]) == 0


def test_new_17_column_csv_round_trip(tmp_path: Path) -> None:
    rows = [
        order_row(order_id="ORD-001", cohort="g3"),
        order_row(
            order_id="ORD-002",
            household_id="HH-02",
            payment_ref="REF-002",
            cohort="g2",
        ),
    ]
    path = tmp_path / "orders.csv"
    g2_orders.write_orders(path, rows)

    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    assert len(header) == 17
    assert header[-1] == "cohort"

    loaded = g2_orders.read_orders(path)
    assert [row["cohort"] for row in loaded] == ["g3", "g2"]


def test_empty_cohort_normalizes_to_g2(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    g2_orders.write_orders(path, [order_row(cohort="")])
    loaded = g2_orders.read_orders(path)
    assert loaded[0]["cohort"] == "g2"
    assert g2_orders.validate_orders(loaded) == []


def test_summary_splits_paid_and_refund_by_cohort() -> None:
    rows = [
        order_row(order_id="ORD-001", household_id="HH-01", cohort="g3"),
        order_row(
            order_id="ORD-002",
            household_id="HH-02",
            payment_ref="REF-002",
            cohort="g3",
        ),
        order_row(
            order_id="ORD-003",
            household_id="HH-03",
            payment_ref="REF-003",
            cohort="g2",
        ),
        order_row(
            order_id="ORD-004",
            household_id="HH-04",
            payment_ref="REF-004",
            cohort="g2",
            refund_status="refunded",
            refund_requested_at="2026-11-05 09:00:00",
            refunded_at="2026-11-06 15:00:00",
            refund_amount="99",
        ),
    ]
    summary = g2_orders.compute_summary(rows)

    assert summary["paid_households"] == 4
    assert summary["paid_households_by_cohort"] == {"g2": 2, "g3": 2}
    assert summary["paid_orders_by_cohort"] == {"g2": 2, "g3": 2}
    assert summary["refunded_by_cohort"] == {"g2": 1, "g3": 0}
    assert summary["refund_rate_by_cohort"] == {"g2": 0.5, "g3": 0.0}
    assert summary["refund_rate"] == pytest.approx(0.25)


def test_invalid_cohort_rejected(tmp_path: Path) -> None:
    rows = [order_row(cohort="g9")]
    issues = g2_orders.validate_orders(rows)
    assert any(issue.field == "cohort" for issue in issues)

    path = tmp_path / "orders.csv"
    g2_orders.write_orders(path, rows)
    assert g2_orders.main(["validate", "--orders", str(path)]) == 1


def test_new_template_contains_cohort(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    assert g2_orders.main(["new", "--out", str(path)]) == 0

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert "cohort" in reader.fieldnames
    assert rows[0]["cohort"] == "g2"
    assert "cohort" in path.with_suffix(".fields.md").read_text(encoding="utf-8")


def test_shipped_import_template_is_valid() -> None:
    template = ROOT / "docs" / "订单导入模板-G2.csv"
    rows = g2_orders.read_orders(template)
    assert len(rows) == 3
    assert {row["cohort"] for row in rows} == {"g2", "g3"}
    assert g2_orders.validate_orders(rows) == []