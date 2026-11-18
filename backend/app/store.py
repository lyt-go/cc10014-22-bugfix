"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS, SEED_TABLE_ROWS


def batch_sort_key(batch: Any) -> tuple[tuple[int, Any], ...]:
    """把批次号拆成可比较的自然序，避免字符串比较把 10 排在 9 前面。

    同源批次可能乱序到达，判断"当前结果"必须按数据批次而不是写入时间。
    """
    text = "" if batch is None else str(batch).strip()
    if not text or text == "全量":
        # 手动全量执行没有批次序号，语义上晚于任何具体批次。
        return ((2, ""),)
    parts: list[tuple[int, Any]] = []
    for token in re.split(r"(\d+)", text):
        if not token:
            continue
        parts.append((0, int(token)) if token.isdigit() else (1, token))
    return tuple(parts)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_TABLE_ROWS.items()
        }
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果（规则编号 -> 结果版本列表，同源批次可能乱序到达）
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 未完成的稽核运行（中断后可从中断点继续，只续写未完成部分）
        self._quality_runs: dict[str, dict[str, Any]] = {}
        # 发布质量闸门（失败行与阻断状态同一份记录落库）
        self._publish_gates: list[dict[str, Any]] = []
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        """追加一次稽核结果；同一规则的结果按批次而不是到达时间判定新旧。"""
        results = self._quality_results.setdefault(rule_id, [])
        saved = dict(result)
        saved.setdefault("seq", len(results) + 1)
        saved.setdefault("recorded_at", datetime.now().isoformat())
        results.append(saved)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    def latest_quality_result(
        self, rule_id: str, batch: str | None = None,
    ) -> dict[str, Any] | None:
        """取当前有效的稽核结果。

        批次错序时不能直接取最后写入的一条：按 (批次, 序号) 排序，
        这样旧批次晚到也不会把结果覆盖成旧值。
        """
        results = self._quality_results.get(rule_id, [])
        if batch is not None:
            results = [row for row in results if str(row.get("batch") or "") == str(batch)]
        if not results:
            return None
        ordered = sorted(
            results,
            key=lambda row: (batch_sort_key(row.get("batch")), int(row.get("seq") or 0)),
        )
        return ordered[-1]

    def list_quality_results(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        if rule_id is not None:
            return list(self._quality_results.get(rule_id, []))
        rows: list[dict[str, Any]] = []
        for result_list in self._quality_results.values():
            rows.extend(result_list)
        return rows

    def save_quality_run(self, rule_id: str, run: dict[str, Any]) -> None:
        self._quality_runs[rule_id] = dict(run)

    def get_quality_run(self, rule_id: str) -> dict[str, Any] | None:
        run = self._quality_runs.get(rule_id)
        return dict(run) if run else None

    def clear_quality_run(self, rule_id: str) -> None:
        self._quality_runs.pop(rule_id, None)

    # ---- 发布质量闸门 ----

    def save_publish_gate(self, gate: dict[str, Any]) -> None:
        """失败行与阻断状态必须作为同一条记录落库，避免只存计数。"""
        self._publish_gates.append(dict(gate))

    def get_publish_gates(self, version_id: int | None = None) -> list[dict[str, Any]]:
        if version_id is None:
            return [dict(gate) for gate in self._publish_gates]
        return [dict(gate) for gate in self._publish_gates
                if int(gate.get("version_id") or 0) == int(version_id)]

    def latest_publish_gate(self, version_id: int) -> dict[str, Any] | None:
        gates = self.get_publish_gates(version_id)
        return gates[-1] if gates else None

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区 ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))


store = Store()
