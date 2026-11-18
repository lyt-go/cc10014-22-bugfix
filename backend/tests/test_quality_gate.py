"""测试：非法数值不得按 0 通过阈值校验，失败行与发布阻断一起落库。

覆盖：
- 非法数值（空白、非数字、NaN/Infinity）判失败，不做 0 值兜底
- 阈值开闭边界、表达式与配置口径可配
- 空表/采样为 0 明确为"无数据"
- 批次错序时列表与详情都取当前批次而不是最后写入的旧值
- 稽核中断后重试只续写未完成部分，规则版本保持完整
- 非阻断规则命中不阻断发布；阻断规则命中时失败行与阻断状态同一条落库
"""
from __future__ import annotations

import pytest

from app.services.data_publish import DataPublishService
from app.services.quality_check import QualityCheckService
from app.store import store


def reset_store():
    from app.seed import SEED_ROWS, SEED_TABLE_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data = {name: [dict(row) for row in rows] for name, rows in SEED_TABLE_ROWS.items()}
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._quality_runs.clear()
    store._publish_gates.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()


def _rule(**overrides):
    values = {
        "规则编号": "QC-T-001",
        "校验表名": "ods_amounts",
        "校验字段": "amount",
        "校验表达式": "",
        "阈值下界": "0",
        "阈值上界": "",
        "阻断级别": "阻断",
        "采样比例": "1.0",
    }
    values.update(overrides)
    return values


class TestIllegalNumberIsNotZero:
    def setup_method(self):
        reset_store()

    def test_non_numeric_value_fails_lower_bound(self):
        """非数字必须判失败：旧实现 float(val or 0) 会把它当成 0 通过。"""
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [
            {"amount": "-10", "order_id": "O1"},
            {"amount": "abc", "order_id": "O2"},
            {"amount": "", "order_id": "O3"},
            {"amount": "100", "order_id": "O4"},
        ])
        result = qc.execute_check(rule_id)["result"]
        assert result["failed"] == 3
        assert result["passed"] == 1
        assert result["qualified"] is False
        reasons = {row["row_index"]: row["reason"] for row in result["failed_rows"]}
        assert "非法数值" in reasons[2]
        # 失败行必须带表达式与阈值边界
        first = result["failed_rows"][0]
        assert first["expression"] == ""
        assert first["threshold_lower"] == "0"
        assert first["row"]["order_id"] == "O1"

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-", "1,000", "NULL", "  ", None])
    def test_special_values_never_pass(self, bad):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": bad}])
        result = qc.execute_check(rule_id)["result"]
        assert result["failed"] == 1

    def test_expression_lower_bound_also_strict(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule(校验表达式="amount >= 0 AND amount <= 999999"))
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "abc"}, {"amount": "5"}])
        result = qc.execute_check(rule_id)["result"]
        assert result["failed"] == 1
        assert "非法数值" in result["failed_rows"][0]["reason"]

    def test_blank_field_allowed_when_configured(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule(空值判定="允许"))
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": ""}, {"amount": "5"}])
        result = qc.execute_check(rule_id)["result"]
        assert result["failed"] == 0

    def test_exclusive_boundary_is_configurable(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule(阈值下界="10", 下界包含="不包含"))
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "10"}, {"amount": "10.01"}])
        result = qc.execute_check(rule_id)["result"]
        assert result["failed"] == 1
        assert "不包含边界" in result["failed_rows"][0]["reason"]

    def test_between_and_in_expression(self):
        qc = QualityCheckService()
        first_rule, _ = qc.create_entry(_rule(规则编号="QC-B-001", 校验表达式="amount BETWEEN 1 AND 10"))
        second_rule, _ = qc.create_entry(_rule(规则编号="QC-I-001", 校验字段="level", 阈值下界="", 阈值上界="",
                                               校验表达式="level IN ('普通','VIP')"))
        store.set_table_rows("ods_amounts", [
            {"amount": "5", "level": "VIP"},
            {"amount": "12", "level": "非法"},
        ])
        first = qc.execute_check(first_rule["id"])["result"]
        second = qc.execute_check(second_rule["id"])["result"]
        assert first["failed"] == 1
        assert second["failed"] == 1

    def test_config_error_is_not_silently_passed(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule(阈值下界="零"))
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "1"}])
        execution = qc.execute_check(rule_id)
        assert execution["ok"] is False
        assert execution["result"]["status"] == "配置错误"
        assert execution["result"]["qualified"] is False


class TestEmptyAndSampling:
    def setup_method(self):
        reset_store()

    def test_empty_table_reports_no_data(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        result = qc.execute_check(rule_id)["result"]
        assert result["status"] == "无数据"
        assert result["message"].startswith("目标表")
        assert "没有数据" in result["message"]

    def test_zero_sample_reports_no_data(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule(采样比例="0"))
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "1"}])
        result = qc.execute_check(rule_id)["result"]
        assert result["status"] == "无数据"


class TestBatchOrdering:
    def setup_method(self):
        reset_store()

    def test_out_of_order_batch_does_not_show_stale_value(self):
        """旧批次晚到不能覆盖当前结果：列表与详情都必须显示新批次。"""
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "5"}])
        qc.execute_check(rule_id, batch="2026-11-10")
        store.set_table_rows("ods_amounts", [{"amount": "5"}, {"amount": "bad"}])
        qc.execute_check(rule_id, batch="2026-11-11")
        # 旧批次重跑（同源错误批次晚到）
        store.set_table_rows("ods_amounts", [{"amount": "5"}])
        qc.execute_check(rule_id, batch="2026-11-10")

        items, _ = qc.list_results(keyword="QC-T-001")
        assert len(items) == 1
        assert items[0]["批次"] == "2026-11-11"
        assert items[0]["失败行数"] == 1

        detail = qc.result_detail(rule_id)
        assert detail["latest"]["batch"] == "2026-11-11"
        current = [item for item in detail["items"] if item["是否当前"]]
        assert len(current) == 1 and current[0]["batch"] == "2026-11-11"

    def test_numeric_batch_ordering(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": "1"}])
        qc.execute_check(rule_id, batch="batch-9")
        qc.execute_check(rule_id, batch="batch-10")
        assert qc.result_detail(rule_id)["latest"]["batch"] == "batch-10"


class TestInterruptedResume:
    def setup_method(self):
        reset_store()

    def test_interrupted_run_resumes_only_remaining_rows(self):
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": str(i)} for i in range(10)])

        calls = {"n": 0}
        original = qc._check_value

        def flaky(value, config):
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("source read timeout")
            return original(value, config)

        qc._check_value = flaky
        first = qc.execute_check(rule_id)
        assert first["interrupted"] is True
        assert first["result"]["status"] == "中断"
        assert first["result"]["checked"] == 3
        # 规则本身保持完整版本，没有被截断
        assert qc.get_entry(rule_id)["阈值下界"] == "0"

        qc._check_value = original
        second = qc.execute_check(rule_id)
        assert second["interrupted"] is False
        result = second["result"]
        assert result["status"] == "完成"
        assert result["resumed_from"] == 3
        assert result["checked"] == 10
        assert result["failed"] == 0

    def test_resume_matches_only_same_batch_and_data(self):
        """数据变了就不能续写，避免把两批数据拼接成一份结果。"""
        qc = QualityCheckService()
        entry, _ = qc.create_entry(_rule())
        rule_id = entry["id"]
        store.set_table_rows("ods_amounts", [{"amount": str(i)} for i in range(6)])
        calls = {"n": 0}
        original = qc._check_value

        def flaky(value, config):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("timeout")
            return original(value, config)

        qc._check_value = flaky
        qc.execute_check(rule_id)
        qc._check_value = original
        store.set_table_rows("ods_amounts", [{"amount": "100"}])
        result = qc.execute_check(rule_id)["result"]
        assert result["resumed_from"] == 0
        assert result["checked"] == 1


class TestPublishGate:
    def setup_method(self):
        reset_store()

    def _publish_case(self, blocking_level: str):
        qc = QualityCheckService()
        rule, _ = qc.create_entry(_rule(校验表名="dws_x", 阻断级别=blocking_level))
        qc.run_action(rule["id"], "标记生效")
        store.set_table_rows("dws_x", [{"amount": "5"}, {"amount": "坏值"}])
        dp = DataPublishService()
        version, _ = dp.create_entry({"版本编号": "PUB-X", "数据集名称": "dws_x"})
        dp.version_id = version["id"]
        return dp

    def test_blocking_rule_records_rows_and_status_together(self):
        dp = self._publish_case("阻断")
        entry, message = dp.run_action(dp.version_id, "确认发布")
        assert entry is None
        assert "阻断" in message
        assert "第2行" in message

        gate = dp.get_gate(dp.version_id)
        assert gate["status"] == "阻断"
        assert gate["failed_rows"][0]["row_index"] == 2
        assert gate["failed_rows"][0]["threshold_lower"] == "0"
        # 发布状态不前进
        assert dp.get_entry(dp.version_id)["status"] == "草稿"
        # 缓存结果里同样能看到阻断状态
        assert dp.get_entry(dp.version_id)["quality_gate"]["status"] == "阻断"

    def test_warning_rule_does_not_block_but_keeps_rows(self):
        dp = self._publish_case("告警")
        entry, message = dp.run_action(dp.version_id, "确认发布")
        assert entry is not None
        assert entry["status"] == "已发布"
        assert "非阻断" in entry["quality_gate"]["message"]
        gate = dp.get_gate(dp.version_id)
        assert gate["status"] == "放行"
        assert gate["failed_rows"]

    def test_gate_uses_fresh_result_not_previous_one(self):
        """上一次结果通过、本次数据变脏时必须重新执行并阻断。"""
        qc = QualityCheckService()
        rule, _ = qc.create_entry(_rule(校验表名="dws_y"))
        qc.run_action(rule["id"], "标记生效")
        store.set_table_rows("dws_y", [{"amount": "5"}])
        qc.execute_check(rule["id"])  # 留存一条"通过"的历史结果
        dp = DataPublishService()
        version, _ = dp.create_entry({"版本编号": "PUB-Y", "数据集名称": "dws_y"})

        store.set_table_rows("dws_y", [{"amount": "5"}, {"amount": "bad"}])
        entry, message = dp.run_action(version["id"], "确认发布")
        assert entry is None, message
        gate = dp.get_gate(version["id"])
        assert gate["failed_rows"][0]["row_index"] == 2

    def test_no_rules_defaults_to_pass(self):
        dp = DataPublishService()
        version, _ = dp.create_entry({"版本编号": "PUB-N", "数据集名称": "dws_none"})
        entry, message = dp.run_action(version["id"], "确认发布")
        assert entry is not None
        assert "无生效稽核规则" in entry["quality_gate"]["message"]


class TestUnqualifiedBlocks:
    """不合格但不一定产生失败行的结果（无数据、配置错误、中断）也必须按阻断级别处理。"""

    def setup_method(self):
        reset_store()

    def _version(self, dataset: str) -> int:
        dp = DataPublishService()
        version, _ = dp.create_entry({"版本编号": f"PUB-{dataset}", "数据集名称": dataset})
        return version["id"]

    def test_blocking_rule_with_no_data_blocks_publish(self):
        qc = QualityCheckService()
        rule, _ = qc.create_entry(_rule(校验表名="dws_empty"))
        qc.run_action(rule["id"], "标记生效")
        dp = DataPublishService()
        version_id = self._version("dws_empty")
        entry, message = dp.run_action(version_id, "确认发布")
        assert entry is None
        assert "无数据" in message
        assert dp.get_gate(version_id)["rules"][0]["结果状态"] == "无数据"

    def test_non_blocking_rule_with_no_data_does_not_block(self):
        qc = QualityCheckService()
        rule, _ = qc.create_entry(_rule(校验表名="dws_empty2", 阻断级别="告警"))
        qc.run_action(rule["id"], "标记生效")
        dp = DataPublishService()
        version_id = self._version("dws_empty2")
        entry, _ = dp.run_action(version_id, "确认发布")
        assert entry is not None
        assert dp.get_gate(version_id)["status"] == "放行"

    def test_config_error_blocks_and_keeps_message(self):
        qc = QualityCheckService()
        rule, _ = qc.create_entry(_rule(校验表名="dws_cfg", 阈值下界="零"))
        qc.run_action(rule["id"], "标记生效")
        store.set_table_rows("dws_cfg", [{"amount": "1"}])
        dp = DataPublishService()
        version_id = self._version("dws_cfg")
        entry, message = dp.run_action(version_id, "确认发布")
        assert entry is None
        assert "配置错误" in message or "不是合法数值" in message

    def test_blocking_rules_sorted_before_warnings(self):
        qc = QualityCheckService()
        warn, _ = qc.create_entry(_rule(规则编号="QC-W", 校验表名="dws_mix", 阻断级别="告警", 规则次序="1"))
        block, _ = qc.create_entry(_rule(规则编号="QC-B", 校验表名="dws_mix", 阻断级别="阻断", 规则次序="2"))
        qc.run_action(warn["id"], "标记生效")
        qc.run_action(block["id"], "标记生效")
        store.set_table_rows("dws_mix", [{"amount": "bad"}])
        dp = DataPublishService()
        version_id = self._version("dws_mix")
        dp.run_action(version_id, "确认发布")
        gate = dp.get_gate(version_id)
        assert [item["规则编号"] for item in gate["rules"]][0] == "QC-B"
        assert gate["blocked_rule_count"] == 1
        assert gate["warning_rule_count"] == 1
