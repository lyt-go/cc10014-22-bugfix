"""发布版本业务规则：从transform_dag获取最新输出，创建可消费的发布版本。

质量闸门口径：
- 确认发布时重新执行该数据集生效的全部稽核规则，读取本次执行结果；
  不读取上一轮留存结果，也不只比对失败计数。
- 命中阻断级规则时，失败行与阻断状态作为同一条闸门记录落库，发布状态不前进。
- 非阻断（告警/非阻断）规则命中时仍放行，但保留异常明细供核对。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "data_publish"
LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
REQUIRED_FIELDS = ["版本编号", "数据集名称"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已撤回"]
ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "撤回版本": "已撤回", "标记下线": "已撤回"}
NEGATIVE_ACTIONS = []
GATE_BLOCKED = "阻断"
GATE_PASSED = "放行"


class PublishBlocked(Exception):
    """发布被质量闸门阻断；异常里带着已落库的闸门记录，避免调用方丢掉明细。"""

    def __init__(self, gate: dict[str, Any]) -> None:
        self.gate = gate
        super().__init__(gate.get("message", "发布被质量闸门阻断"))


class DataPublishService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("版本编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("来源转换节点"):
            entry["来源转换节点"] = values["来源转换节点"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"发布版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据发布可执行范围"
        # 质量稽核→数据发布：确认发布前必须通过质量闸门。
        record: dict[str, Any] | None = None
        if action == "确认发布":
            try:
                gate = self.evaluate_quality_gate(entry_id)
            except PublishBlocked as blocked:
                return None, str(blocked)
            record = gate["record"]
            if record["status"] == GATE_BLOCKED:
                return None, record["message"]
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if record is not None:
            entry["quality_gate"] = record
            self._attach_latest_output(entry)
        store.save_version(MODULE, entry_id, entry)
        return entry, f"发布版本已{action}"

    # ------------------------------------------------------------------
    # 质量闸门
    # ------------------------------------------------------------------

    def evaluate_quality_gate(self, entry_id: int) -> dict[str, Any]:
        """执行全部生效规则并落库一条闸门记录：失败行与阻断状态同一条写入。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"发布版本 {entry_id} 不存在或已归档")
        dataset = str(entry.get("数据集名称") or "")
        from app.services.quality_check import QualityCheckService
        qc = QualityCheckService()
        # 规则次序：阻断级优先、其余按规则次序，保证跨模块口径稳定。
        rules = qc.ordered_rules_for_tables({dataset})

        rule_results: list[dict[str, Any]] = []
        blocked_rules: list[dict[str, Any]] = []
        warning_rules: list[dict[str, Any]] = []
        for rule in rules:
            result = qc.run_rule(rule)  # 本次执行结果，不复用历史结果
            summary = {
                "规则编号": result.get("rule_id", ""),
                "规则次序": result.get("rule_order", ""),
                "阻断级别": result.get("blocking_level", ""),
                "校验字段": result.get("field", ""),
                "校验表达式": result.get("expression", ""),
                "阈值下界": result.get("threshold_lower", ""),
                "阈值上界": result.get("threshold_upper", ""),
                "结果状态": result.get("status", ""),
                "总行数": result.get("total", 0),
                "已检查行数": result.get("checked", 0),
                "失败行数": result.get("failed", 0),
                "结果说明": result.get("message", ""),
                "失败行": result.get("failed_rows", []),
                "批次": result.get("batch", ""),
            }
            rule_results.append(summary)
            if self._rule_blocks(result):
                blocked_rules.append(summary)
            elif self._rule_warns(result):
                warning_rules.append(summary)

        failed_rows: list[dict[str, Any]] = []
        for item in rule_results:
            for failed in item.get("失败行", []):
                row = dict(failed)
                row["rule_id"] = item.get("规则编号", "")
                row["rule_order"] = item.get("规则次序", "")
                failed_rows.append(row)
        if blocked_rules:
            status = GATE_BLOCKED
            message = (
                "数据发布被阻断："
                + "；".join(self._rule_message(item) for item in blocked_rules)
                + self._first_row_hint(blocked_rules)
            )
        elif warning_rules:
            status = GATE_PASSED
            message = (
                "发布放行，但存在非阻断异常："
                + "；".join(self._rule_message(item) for item in warning_rules)
            )
        else:
            status = GATE_PASSED
            message = (
                f"全部 {len(rules)} 条生效稽核规则通过" if rules
                else "无生效稽核规则，默认通过"
            )

        record = {
            "version_id": int(entry.get("id") or 0),
            "版本编号": entry.get("版本编号", ""),
            "数据集名称": dataset,
            "status": status,
            "阻断": status == GATE_BLOCKED,
            "message": message,
            "rule_count": len(rules),
            "blocked_rule_count": len(blocked_rules),
            "warning_rule_count": len(warning_rules),
            "rules": rule_results,
            "failed_rows": failed_rows,
            "evaluated_at": datetime.now().isoformat(),
        }
        record["gate_id"] = f"GATE-{record['version_id']}-{len(store.get_publish_gates(record['version_id'])) + 1}"
        store.save_publish_gate(record)
        # 版本上始终挂最近一次闸门结果，列表与详情可直接读取。
        entry["quality_gate"] = record
        if status == GATE_BLOCKED:
            entry["blocked"] = True
            entry["abnormal"] = True
            raise PublishBlocked(record)
        entry.pop("blocked", None)
        return {"passed": True, "record": record, "rules": rule_results}

    def _rule_blocks(self, result: dict[str, Any]) -> bool:
        """阻断级规则只要结果不合格（含中断/配置错误）就阻断，失败行数为 0 也不例外。"""
        return (
            str(result.get("blocking_level") or "阻断") == "阻断"
            and not bool(result.get("qualified"))
        )

    def _rule_warns(self, result: dict[str, Any]) -> bool:
        """非阻断规则的不合格结果保留明细但不阻断发布。"""
        return not bool(result.get("qualified"))

    def _rule_message(self, item: dict[str, Any]) -> str:
        if int(item.get("失败行数") or 0) > 0:
            return f"规则 {item['规则编号']} 失败 {item['失败行数']} 行"
        return f"规则 {item['规则编号']} {item.get('结果状态') or '未通过'}：{item.get('结果说明') or ''}"

    def _first_row_hint(self, blocked_rules: list[dict[str, Any]]) -> str:
        for item in blocked_rules:
            rows = item.get("失败行") or []
            if rows:
                first = rows[0]
                return (
                    f"；首个失败行：第{first.get('row_index')}行 "
                    f"值「{first.get('value')}」表达式「{first.get('expression')}」"
                    f"阈值[{first.get('threshold_lower')}, {first.get('threshold_upper')}]"
                )
        return ""

    def get_gate(self, entry_id: int) -> dict[str, Any] | None:
        return store.latest_publish_gate(entry_id)

    # ------------------------------------------------------------------
    # 转换节点输出
    # ------------------------------------------------------------------

    def _attach_latest_output(self, entry: dict[str, Any]) -> None:
        node_code = entry.get("来源转换节点", "")
        if not node_code:
            return
        # 查找转换节点
        for row in store.rows("transform_dag"):
            if row.get("节点编号") == node_code:
                output_table = row.get("输出表名", "")
                data = store.table_rows(output_table)
                entry["_published_data"] = {
                    "table": output_table,
                    "row_count": len(data),
                    "published_at": datetime.now().isoformat(),
                }
                break

    def get_published_data(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        node_code = entry.get("来源转换节点", "")
        if not node_code:
            return None
        for row in store.rows("transform_dag"):
            if row.get("节点编号") == node_code:
                return {
                    "table": row.get("输出表名", ""),
                    "rows": store.table_rows(row.get("输出表名", "")),
                }
        return None

