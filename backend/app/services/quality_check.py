"""质量稽核业务规则：按规则与阈值口径执行校验，生成可追溯的稽核结果。

关键口径：
- 数值比较前先做严格转换，空白、非数字、NaN/Infinity 都视为非法数值；
  非法数值不能当作 0 参与下界/上界比较，必须判定为失败。
- 阈值下界、上界、开闭区间、空值判定、采样比例、阻断级别与判定条件均可配置。
- 失败行、命中表达式与阈值边界随结果一起落库，供发布闸门与详情页核对。
- 同一规则的结果按数据批次而不是写入顺序判定新旧，批次错序不会把旧值当成当前值。
- 稽核中断时保留未完成运行，重试只续写尚未检查的行，规则本身保持完整版本。
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.store import batch_sort_key, store

MODULE = "quality_check"
LIST_FIELDS = ["规则编号", "校验表名", "校验字段", "校验表达式", "阈值下界", "阈值上界", "阻断级别", "采样比例"]
REQUIRED_FIELDS = ["规则编号", "校验表名", "校验字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]

# 阻断级别优先级：阻断级规则先判定，告警/非阻断只记录不阻断发布。
BLOCKING_LEVELS = ("阻断", "告警", "非阻断")
BLOCKING_LEVEL_RANK = {"阻断": 0, "告警": 1, "非阻断": 2}
DEFAULT_BLOCKING_LEVEL = "阻断"
RESULT_COMPLETE = "完成"
RESULT_INTERRUPTED = "中断"
RESULT_NO_DATA = "无数据"
RESULT_CONFIG_ERROR = "配置错误"

# 结果明细里每个失败行最多展开的条数（默认全量保留，可用配置收窄）。
DEFAULT_MAX_DETAIL_ROWS = 1000

_COMPARE_RE = re.compile(
    r"^\s*(?:(?P<field>[A-Za-z_][\w]*)\s*)?"
    r"(?P<op>>=|<=|<>|!=|==|=|>|<)\s*(?P<rhs>.+?)\s*$"
)
_IN_RE = re.compile(r"^\s*(?:(?P<field>[A-Za-z_][\w]*)\s*)?(?P<neg>NOT\s+)?IN\s*\((?P<items>[^)]*)\)\s*$", re.I)
_BETWEEN_RE = re.compile(
    r"^\s*(?:(?P<field>[A-Za-z_][\w]*)\s*)?BETWEEN\s+(?P<low>\S+)\s+AND\s+(?P<high>\S+)\s*$", re.I)
_NULL_RE = re.compile(
    r"^\s*(?:(?P<field>[A-Za-z_][\w]*)\s*)?IS\s+(?P<neg>NOT\s+)?NULL\s*$", re.I)
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _to_decimal(value: Any) -> Decimal | None:
    """严格数值转换：任何无法确定的文本都返回 None，绝不默认成 0。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return None
    else:
        text = str(value).strip()
        if not text or not _NUMBER_RE.match(text):
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    if not number.is_finite():
        return None
    return number


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _truthy_option(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"是", "包含", "include", "inclusive", "true", "1", "y", "yes"}:
        return True
    if text in {"否", "不包含", "exclude", "exclusive", "false", "0", "n", "no"}:
        return False
    return default


def _split_top_level(text: str, word: str) -> list[str]:
    """按顶层 AND/OR 拆分，忽略括号与引号里的分隔符，也不拆 BETWEEN ... AND ...。"""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = ""
    index = 0
    upper = text.upper()
    target = word.upper()
    while index < len(text):
        char = text[index]
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "(":
            depth += 1
            buffer.append(char)
            index += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            buffer.append(char)
            index += 1
            continue
        if depth == 0 and upper.startswith(target, index):
            before_ok = index == 0 or not (upper[index - 1].isalnum() or upper[index - 1] == "_")
            after = index + len(target)
            after_ok = after >= len(text) or not (upper[after].isalnum() or upper[after] == "_")
            if before_ok and after_ok:
                current = "".join(buffer).strip()
                if target == "AND" and re.search(r"\bBETWEEN\s+\S+$", current, re.I):
                    buffer.append(text[index:after])
                    index = after
                    continue
                if current:
                    parts.append(current)
                buffer = []
                index = after
                continue
        buffer.append(char)
        index += 1
    current = "".join(buffer).strip()
    if current:
        parts.append(current)
    return parts


class QualityCheckService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_rule_no(self, rule_no: str) -> dict[str, Any] | None:
        return store.find_by_field(MODULE, "规则编号", rule_no)

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
        # 可配置口径：规则次序、开闭边界、空值判定、明细保留上限与校验批次。
        entry["规则次序"] = values.get("规则次序") or str(entry["id"])
        entry["下界包含"] = values.get("下界包含", "包含")
        entry["上界包含"] = values.get("上界包含", "包含")
        entry["空值判定"] = values.get("空值判定", "拒绝")
        entry["最大明细行数"] = values.get("最大明细行数", DEFAULT_MAX_DETAIL_ROWS)
        if values.get("校验批次"):
            entry["校验批次"] = values["校验批次"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"稽核规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于质量稽核可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"稽核规则已{action}"

    # ------------------------------------------------------------------
    # 结果查询（列表入口与详情入口使用同一套批次排序，避免显示旧值）
    # ------------------------------------------------------------------

    def list_results(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        blocking_level: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        summaries: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            rule_no = str(entry.get("规则编号") or "")
            if keyword and keyword.casefold() not in rule_no.casefold():
                continue
            latest = store.latest_quality_result(rule_no)
            summary = self._result_summary(entry, latest)
            # 中断的运行要让列表入口直接可见，而不是显示成上一次的旧值。
            run = store.get_quality_run(rule_no)
            if run and run.get("status") == RESULT_INTERRUPTED and (
                latest is None or run.get("batch") == latest.get("batch")
            ):
                summary["结果状态"] = RESULT_INTERRUPTED
                summary["批次"] = run.get("batch", "")
                summary["已检查行数"] = int(run.get("processed") or 0)
                summary["结果说明"] = "上次稽核中断，重试只续写未完成部分"
            if status and summary.get("结果状态") != status:
                continue
            if blocking_level and summary.get("阻断级别") != blocking_level:
                continue
            summaries.append(summary)
        summaries.sort(key=lambda item: str(item.get("规则编号") or ""))
        total = len(summaries)
        start = (page - 1) * size
        return summaries[start:start + size], total

    def result_detail(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        rule_no = str(entry.get("规则编号") or "")
        results = store.get_quality_results(rule_no)
        # 详情与列表共用 latest_quality_result，避免两处对"当前结果"判断不一致。
        latest = store.latest_quality_result(rule_no)
        ordered = sorted(
            results,
            key=lambda row: (batch_sort_key(row.get("batch")), int(row.get("seq") or 0)),
        )
        items = []
        for result in ordered:
            item = dict(result)
            item["是否当前"] = bool(
                latest
                and item.get("batch") == latest.get("batch")
                and item.get("seq") == latest.get("seq")
            )
            items.append(item)
        run = store.get_quality_run(rule_no)
        return {
            "entry": entry,
            "latest": latest,
            "items": items,
            "unfinished_run": run,
            "empty_message": "" if items else "暂无质量稽核结果数据，请先从列表发起执行",
        }

    def _result_summary(self, entry: dict[str, Any], latest: dict[str, Any] | None) -> dict[str, Any]:
        rule_no = str(entry.get("规则编号") or "")
        if latest is None:
            return {
                "规则编号": rule_no,
                "校验表名": entry.get("校验表名", ""),
                "校验字段": entry.get("校验字段", ""),
                "校验表达式": entry.get("校验表达式", ""),
                "阈值下界": entry.get("阈值下界", ""),
                "阈值上界": entry.get("阈值上界", ""),
                "阻断级别": entry.get("阻断级别") or DEFAULT_BLOCKING_LEVEL,
                "结果状态": "暂无结果",
                "批次": "",
                "总行数": 0,
                "已检查行数": 0,
                "失败行数": 0,
                "通过行数": 0,
                "通过": False,
                "结果说明": "暂无质量稽核结果数据，请先从列表发起执行",
                "失败行": [],
                "检查时间": "",
            }
        return {
            "规则编号": rule_no,
            "校验表名": latest.get("table", entry.get("校验表名", "")),
            "校验字段": latest.get("field", entry.get("校验字段", "")),
            "校验表达式": latest.get("expression", entry.get("校验表达式", "")),
            "阈值下界": latest.get("threshold_lower", entry.get("阈值下界", "")),
            "阈值上界": latest.get("threshold_upper", entry.get("阈值上界", "")),
            "阻断级别": latest.get("blocking_level") or entry.get("阻断级别") or DEFAULT_BLOCKING_LEVEL,
            "结果状态": latest.get("status", ""),
            "批次": latest.get("batch", ""),
            "总行数": int(latest.get("total") or 0),
            "已检查行数": int(latest.get("checked") or 0),
            "失败行数": int(latest.get("failed") or 0),
            "通过行数": latest.get("passed", 0),
            "通过": bool(latest.get("qualified")),
            "结果说明": latest.get("message", ""),
            "失败行": latest.get("failed_rows", []),
            "检查时间": latest.get("checked_at", ""),
        }

    # ------------------------------------------------------------------
    # 执行稽核
    # ------------------------------------------------------------------

    def execute_check(
        self,
        entry_id: int,
        batch: str | None = None,
        *,
        restart: bool = False,
    ) -> dict[str, Any]:
        """读取目标表数据执行校验；中断后再次执行会从未完成的位置续写。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "稽核规则不存在", "interrupted": False, "result": None}
        rule_no = str(entry.get("规则编号") or "")

        config_error = self._config_error(entry)
        if config_error:
            result = self._base_result(entry, batch or self._default_batch(entry), status=RESULT_CONFIG_ERROR,
                                       message=f"稽核规则配置错误：{config_error}", passed=False)
            self._persist_result(rule_no, result)
            return {"ok": False, "message": result["message"], "result": result,
                    "interrupted": False}

        table = str(entry.get("校验表名") or "")
        field = str(entry.get("校验字段") or "")
        rows = store.table_rows(table)
        if not rows:
            from app.services.ods_layer import OdsLayerService
            rows = OdsLayerService().get_table_data(table)
        batch = batch or self._default_batch(entry)

        if not rows:
            result = self._base_result(entry, batch, status=RESULT_NO_DATA,
                                       message=f"目标表 {table or '（未配置）'} 没有数据，稽核未执行",
                                       passed=False)
            self._persist_result(rule_no, result)
            store.clear_quality_run(rule_no)
            return {"ok": True, "message": result["message"], "result": result, "interrupted": False}

        config = self._rule_config(entry)
        config["ratio"] = self._sample_ratio(entry)
        sample = self._sample_rows(rows, config)
        if not sample:
            result = self._base_result(entry, batch, status=RESULT_NO_DATA,
                                       message="采样比例为 0，没有可检查的数据行", passed=False)
            self._persist_result(rule_no, result)
            store.clear_quality_run(rule_no)
            return {"ok": True, "message": result["message"], "result": result, "interrupted": False}

        signature = [(index, self._value_signature(rows[index].get(field))) for index in sample]
        run = store.get_quality_run(rule_no)
        resuming = bool(
            not restart
            and run
            and run.get("status") == RESULT_INTERRUPTED
            and run.get("batch") == batch
            and run.get("signature") == signature
        )
        if resuming:
            processed = int(run.get("processed") or 0)
            failed_rows = list(run.get("failed_rows") or [])
        else:
            processed = 0
            failed_rows = []
        state: dict[str, Any] = {
            "entry_id": entry_id,
            "batch": batch,
            "table": table,
            "field": field,
            "processed": processed,
            "failed_rows": failed_rows,
            "signature": signature,
            "status": "运行中",
            "started_at": (run or {}).get("started_at") or datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        store.save_quality_run(rule_no, state)

        try:
            for position in range(processed, len(sample)):
                index = sample[position]
                row = rows[index]
                ok, reason = self._check_value(row.get(field), config)
                if not ok:
                    failed_rows.append(self._failed_row(index, row, row.get(field), reason, config))
                state["processed"] = position + 1
                state["failed_rows"] = failed_rows
                state["updated_at"] = datetime.now().isoformat()
                store.save_quality_run(rule_no, state)
        except Exception as exc:  # 中断：保留已完成部分与未完成游标
            state["status"] = RESULT_INTERRUPTED
            store.save_quality_run(rule_no, state)
            result = self._build_result(
                entry, batch, total=len(rows), checked=state["processed"], failed_rows=failed_rows,
                status=RESULT_INTERRUPTED, passed=False,
                message=(
                    f"稽核中断（{type(exc).__name__}）：已完成 {state['processed']}/{len(sample)} 行，"
                    "重试将只续写未完成部分，规则配置保持完整版本"
                ),
                resumed_from=processed,
            )
            self._persist_result(rule_no, result)
            return {"ok": False, "message": result["message"], "result": result, "interrupted": True}

        store.clear_quality_run(rule_no)
        result = self._build_result(
            entry, batch, total=len(rows), checked=len(sample), failed_rows=failed_rows,
            status=RESULT_COMPLETE, passed=not failed_rows,
            message=(
                f"稽核完成：检查{len(sample)}行，通过{len(sample) - len(failed_rows)}行，"
                f"失败{len(failed_rows)}行"
            ),
            resumed_from=processed if resuming else 0,
        )
        self._persist_result(rule_no, result)
        return {
            "ok": True,
            "message": result["message"],
            "result": result,
            "interrupted": False,
        }

    # ------------------------------------------------------------------
    # 规则口径与判定
    # ------------------------------------------------------------------

    def _rule_config(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "expr": str(entry.get("校验表达式") or "").strip(),
            "field": str(entry.get("校验字段") or "").strip(),
            "lower": entry.get("阈值下界", ""),
            "upper": entry.get("阈值上界", ""),
            "lower_inclusive": _truthy_option(entry.get("下界包含", "包含"), True),
            "upper_inclusive": _truthy_option(entry.get("上界包含", "包含"), True),
            "allow_blank": str(entry.get("空值判定") or "拒绝").strip() in {"忽略", "允许", "允许空值"},
            "blocking_level": str(entry.get("阻断级别") or DEFAULT_BLOCKING_LEVEL),
            "rule_order": entry.get("规则次序") or str(entry.get("id") or ""),
            "max_detail_rows": self._max_detail(entry),
        }

    def _max_detail(self, entry: dict[str, Any]) -> int:
        try:
            value = int(entry.get("最大明细行数") or DEFAULT_MAX_DETAIL_ROWS)
        except (TypeError, ValueError):
            return DEFAULT_MAX_DETAIL_ROWS
        return max(1, value)

    def _config_error(self, entry: dict[str, Any]) -> str:
        expr = str(entry.get("校验表达式") or "").strip()
        lower = entry.get("阈值下界", "")
        upper = entry.get("阈值上界", "")
        if not expr and str(lower).strip() == "" and str(upper).strip() == "":
            return "校验表达式与阈值边界均为空，无法判定数据是否合法"
        for label, value in (("阈值下界", lower), ("阈值上界", upper)):
            if str(value).strip() and _to_decimal(value) is None:
                return f"{label}「{value}」不是合法数值"
        low = _to_decimal(lower)
        high = _to_decimal(upper)
        if low is not None and high is not None and low > high:
            return f"阈值下界 {low} 大于阈值上界 {high}"
        ratio = self._sample_ratio(entry)
        if ratio is not None and ratio < 0:
            return f"采样比例「{entry.get('采样比例')}」不能为负数"
        raw_ratio = str(entry.get("采样比例") or "").strip()
        if raw_ratio:
            try:
                float(raw_ratio)
            except ValueError:
                return f"采样比例「{raw_ratio}」不是合法数值"
        return ""

    def _sample_ratio(self, entry: dict[str, Any]) -> float | None:
        raw = entry.get("采样比例")
        if raw is None or str(raw).strip() == "":
            return 1.0
        try:
            ratio = float(str(raw).strip())
        except (TypeError, ValueError):
            return 1.0
        if 1 < ratio <= 100:
            ratio = ratio / 100
        return ratio

    def _sample_rows(self, rows: list[dict[str, Any]], config: dict[str, Any]) -> list[int]:
        entry_ratio = config.get("ratio")
        ratio = 1.0 if entry_ratio is None else float(entry_ratio)
        if ratio >= 1:
            return list(range(len(rows)))
        if ratio <= 0:
            return []
        step = max(1, round(1 / ratio))
        return list(range(0, len(rows), step))

    def _default_batch(self, entry: dict[str, Any]) -> str:
        return str(entry.get("校验批次") or "全量")

    def _value_signature(self, value: Any) -> str:
        return "" if value is None else str(value)

    def _check_value(self, value: Any, config: dict[str, Any]) -> tuple[bool, str]:
        """返回 (是否合法, 失败原因)；非法数值绝不按 0 处理。"""
        # 允许空值的口径优先：空白不参与表达式与阈值比较。
        if _is_blank(value) and config.get("allow_blank"):
            return True, ""
        expr = config.get("expr", "")
        if expr:
            ok, reason = self._eval_expression(value, expr, config)
            if not ok:
                return False, reason
        if _is_blank(value):
            return False, "空值不允许参与判定"

        lower_raw = config.get("lower", "")
        upper_raw = config.get("upper", "")
        if str(lower_raw).strip() or str(upper_raw).strip():
            number = _to_decimal(value)
            if number is None:
                return False, f"非法数值「{value}」，不能当作 0 参与阈值比较"
            if str(lower_raw).strip():
                lower = _to_decimal(lower_raw)
                if lower is not None:
                    if config.get("lower_inclusive", True):
                        if number < lower:
                            return False, f"{number} 低于阈值下界 {lower}（包含边界）"
                    elif number <= lower:
                        return False, f"{number} 未高于阈值下界 {lower}（不包含边界）"
            if str(upper_raw).strip():
                upper = _to_decimal(upper_raw)
                if upper is not None:
                    if config.get("upper_inclusive", True):
                        if number > upper:
                            return False, f"{number} 高于阈值上界 {upper}（包含边界）"
                    elif number >= upper:
                        return False, f"{number} 未低于阈值上界 {upper}（不包含边界）"
        return True, ""

    def _eval_expression(self, value: Any, expr: str, config: dict[str, Any]) -> tuple[bool, str]:
        or_groups = _split_top_level(expr, "OR")
        if len(or_groups) > 1:
            reasons: list[str] = []
            for group in or_groups:
                ok, reason = self._eval_and_group(value, group, config)
                if ok:
                    return True, ""
                reasons.append(reason)
            return False, "；".join(reasons)
        return self._eval_and_group(value, expr, config)

    def _eval_and_group(self, value: Any, group: str, config: dict[str, Any]) -> tuple[bool, str]:
        clauses = _split_top_level(group, "AND")
        for clause in clauses:
            ok, reason = self._eval_clause(value, clause, config)
            if not ok:
                return False, reason
        return True, ""

    def _eval_clause(self, value: Any, clause: str, config: dict[str, Any]) -> tuple[bool, str]:
        text = clause.strip()
        if not text:
            return True, ""

        match = _NULL_RE.match(text)
        if match:
            is_null = _is_blank(value)
            expected = not bool(match.group("neg"))
            if is_null == expected:
                return True, ""
            return False, f"字段值「{value}」不满足 {text}"

        match = _IN_RE.match(text)
        if match:
            items = [item.strip().strip("'\"") for item in match.group("items").split(",") if item.strip()]
            present = str(value) if value is not None else ""
            included = present in items
            if bool(match.group("neg")):
                included = not included
            if included:
                return True, ""
            return False, f"「{present}」不在允许取值 {items} 内"

        match = _BETWEEN_RE.match(text)
        if match:
            number = _to_decimal(value)
            low = _to_decimal(match.group("low"))
            high = _to_decimal(match.group("high"))
            if number is None:
                return False, f"非法数值「{value}」，无法参与 BETWEEN 区间判定"
            if low is None or high is None:
                return False, f"BETWEEN 边界不是合法数值：{text}"
            if low <= number <= high:
                return True, ""
            return False, f"{number} 不在区间 [{low}, {high}] 内"

        match = _COMPARE_RE.match(text)
        if match:
            op = match.group("op")
            rhs = match.group("rhs").strip()
            # 无论表达式里写的是字段名还是裸值，都比对规则配置的校验字段，
            # 不认识的写法不做静默放行。
            return self._compare(value, op, rhs)

        # 不认识的判定条件不能默认为通过，否则脏数据会被放行。
        return False, f"无法解析判定条件「{text}」，按不通过处理"

    def _compare(self, value: Any, op: str, rhs: str) -> tuple[bool, str]:
        textual = (len(rhs) >= 2 and rhs[0] == rhs[-1] and rhs[0] in ("'", '"'))
        if textual:
            expected: Any = rhs[1:-1]
        else:
            expected = _to_decimal(rhs)
            if expected is None:
                expected = rhs

        if isinstance(expected, Decimal):
            actual = _to_decimal(value)
            if actual is None:
                return False, f"非法数值「{value}」，不能当作 0 参与 {op} {expected} 判定"
        else:
            actual = "" if value is None else str(value)
            expected = str(expected)

        result = {
            ">=": actual >= expected,
            "<=": actual <= expected,
            ">": actual > expected,
            "<": actual < expected,
            "=": actual == expected,
            "==": actual == expected,
            "!=": actual != expected,
            "<>": actual != expected,
        }.get(op)
        if result is None:
            return False, f"不支持的比较符「{op}」"
        if result:
            return True, ""
        return False, f"字段值「{value}」不满足 {op} {rhs}"

    # ------------------------------------------------------------------
    # 结果组装与持久化
    # ------------------------------------------------------------------

    def _base_result(
        self,
        entry: dict[str, Any],
        batch: str,
        *,
        status: str,
        message: str,
        passed: bool,
    ) -> dict[str, Any]:
        return self._build_result(
            entry, batch, total=0, checked=0, failed_rows=[], status=status,
            passed=passed, message=message, resumed_from=0,
        )

    def _build_result(
        self,
        entry: dict[str, Any],
        batch: str,
        *,
        total: int,
        checked: int,
        failed_rows: list[dict[str, Any]],
        status: str,
        passed: bool,
        message: str,
        resumed_from: int,
    ) -> dict[str, Any]:
        config = self._rule_config(entry)
        max_rows = config["max_detail_rows"]
        details = failed_rows[:max_rows]
        return {
            "result_id": f"{entry.get('规则编号', '')}@{batch}",
            "rule_entry_id": entry.get("id"),
            "rule_id": entry.get("规则编号", ""),
            "rule_order": config.get("rule_order", ""),
            "table": entry.get("校验表名", ""),
            "field": entry.get("校验字段", ""),
            "expression": config.get("expr", ""),
            "threshold_lower": entry.get("阈值下界", ""),
            "threshold_upper": entry.get("阈值上界", ""),
            "lower_inclusive": config.get("lower_inclusive", True),
            "upper_inclusive": config.get("upper_inclusive", True),
            "blocking_level": entry.get("阻断级别") or DEFAULT_BLOCKING_LEVEL,
            "batch": batch,
            "total": total,
            "checked": checked,
            "failed": len(failed_rows),
            "passed": max(checked - len(failed_rows), 0),
            "qualified": bool(passed),
            "status": status,
            "message": message,
            "failed_rows": details,
            "failed_rows_total": len(failed_rows),
            "detail_truncated": len(failed_rows) > len(details),
            "resumed_from": resumed_from,
            "checked_at": datetime.now().isoformat(),
        }

    def _failed_row(
        self,
        index: int,
        row: dict[str, Any],
        value: Any,
        reason: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "row_index": index + 1,
            "value": value,
            "reason": reason,
            "expression": config.get("expr", ""),
            "threshold_lower": config.get("lower", ""),
            "threshold_upper": config.get("upper", ""),
            "blocking_level": config.get("blocking_level", DEFAULT_BLOCKING_LEVEL),
            "row": dict(row),
        }

    def _persist_result(self, rule_no: str, result: dict[str, Any]) -> None:
        store.save_quality_result(rule_no, result)

    # ------------------------------------------------------------------
    # 供发布等下游模块调用
    # ------------------------------------------------------------------

    def ordered_rules_for_tables(self, tables: set[str]) -> list[dict[str, Any]]:
        """按阻断优先级与规则次序返回生效规则，作为跨模块消费的稳定口径。"""
        rules = [
            row for row in store.rows(MODULE)
            if row.get("status") == "已生效" and str(row.get("校验表名") or "") in tables
        ]

        def sort_key(row: dict[str, Any]) -> tuple[int, tuple[tuple[int, Any], ...], int]:
            level = str(row.get("阻断级别") or DEFAULT_BLOCKING_LEVEL)
            return (
                BLOCKING_LEVEL_RANK.get(level, len(BLOCKING_LEVEL_RANK)),
                batch_sort_key(row.get("规则次序") or row.get("id") or ""),
                int(row.get("id") or 0),
            )

        return sorted(rules, key=sort_key)

    def run_rule(self, entry: dict[str, Any], batch: str | None = None) -> dict[str, Any]:
        """执行单条规则并直接返回本次结果对象，调用方不得改用历史结果。"""
        execution = self.execute_check(int(entry.get("id") or 0), batch=batch, restart=True)
        result = execution.get("result") or {}
        return result
