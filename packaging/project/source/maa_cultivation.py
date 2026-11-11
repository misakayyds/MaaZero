#!/usr/bin/env python3
"""Pure domain planning for operator cultivation goals (elite/level/materials).

The module is deliberately side-effect free: it only reads a pre-built offline
index (``maa-cultivation-data.json``) plus caller supplied inventory, and never
talks to the device, the game or the network.

Contract
--------
``load_catalog(path) -> dict``
``validate_goal(catalog, goal) -> dict``
``calculate_plan(catalog, goals, inventory) -> dict``

Data provenance: ``yuanyan3060/ArknightsGameResource`` pinned to one commit by
``tools/build_cultivation_data.py``; every source file carries sha256 + bytes.
Level exp / level LMD come from ``gamedata_const.characterExpMap`` and
``characterUpgradeCostMap`` (indexed by elite phase, level transitions from 1).
Promotion cost and materials come from ``character_table.phases[..].evolveCost``
+ ``evolveGoldCost``; recipes come from ``building_data`` (加工站 + 制造站).
Stages are restricted to permanently available, conservatively navigable ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
EXP_KEY = "EXP"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_BY_INDEX = {index + 1: name for index, name in enumerate(WEEKDAYS)}

STATUS_COMPLETED = "completed"
STATUS_DISABLED = "disabled"
STATUS_INVALID = "invalid"
STATUS_READY = "ready"
STATUS_MISSING = "missing"
STATUS_BLOCKED = "blocked"

MAX_RECIPE_DEPTH = 12

STANDARD_NOTES = (
    "EXP 为统一虚拟经验单位(来源 characterExpMap);仓库不可见,需用作战记录换算",
    "关卡映射仅取常驻保守可导航关卡(主线 NORMAL/资源本/芯片本),不承诺最优",
    "首版不模拟源石与采购中心自动兑换",
)


class CatalogError(ValueError):
    """Raised when the offline index is absent, unreadable or malformed."""


def load_catalog(path: str | Path) -> dict[str, Any]:
    """Read and validate the offline cultivation index.

    Raises :class:`CatalogError` (a ``ValueError``) on any structural problem so
    callers can degrade to "unavailable" instead of planning with bad data.
    """

    catalog_path = Path(path)
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise CatalogError(f"培养索引不存在:{catalog_path}") from error
    except OSError as error:
        raise CatalogError(f"培养索引不可读:{catalog_path}({error})") from error
    except UnicodeDecodeError as error:
        raise CatalogError(f"培养索引编码无效:{catalog_path}") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CatalogError(f"培养索引不是合法 JSON:{catalog_path}") from error
    return validate_catalog(value)


def _require_mapping(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(message)
    return value


def _require_positive_int(value: Any, message: str) -> int:
    if type(value) is not int or value < 1:
        raise CatalogError(message)
    return value


def _require_non_negative_int(value: Any, message: str) -> int:
    if type(value) is not int or value < 0:
        raise CatalogError(message)
    return value


def _check_acyclic(recipes: Mapping[str, Any]) -> None:
    """Reject recipe graphs that contain a cycle (chips are excluded upstream)."""

    state: dict[str, int] = {}

    def visit(node: str) -> None:
        state[node] = 1
        recipe = recipes.get(node)
        if isinstance(recipe, dict):
            inputs = recipe.get("inputs")
            if isinstance(inputs, dict):
                for child in inputs:
                    if child not in recipes:
                        continue
                    child_state = state.get(child, 0)
                    if child_state == 1:
                        raise CatalogError(f"配方存在循环:{child} -> {node}")
                    if child_state == 0:
                        visit(child)
        state[node] = 2

    for node in recipes:
        if state.get(node, 0) == 0:
            visit(node)


def validate_catalog(value: Any) -> dict[str, Any]:
    """Validate the offline index and return it unchanged."""

    catalog = _require_mapping(value, "培养索引结构无效")
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError("培养索引版本无效")
    source = _require_mapping(catalog.get("source"), "培养索引缺少 source")
    if not isinstance(source.get("repository"), str) or not isinstance(
        source.get("commit"), str
    ):
        raise CatalogError("培养索引 source 缺少 repository/commit")
    currency = _require_mapping(catalog.get("currency"), "培养索引缺少 currency")
    if not isinstance(currency.get("gold_item_id"), str):
        raise CatalogError("培养索引缺少 currency.gold_item_id")

    leveling = _require_mapping(catalog.get("leveling"), "培养索引缺少 leveling")
    for key in ("exp_per_level", "gold_per_level"):
        rows = leveling.get(key)
        if not isinstance(rows, list) or not rows:
            raise CatalogError(f"培养索引 leveling.{key} 无效")
        for row in rows:
            if not isinstance(row, list) or not row:
                raise CatalogError(f"培养索引 leveling.{key} 行无效")
            for cell in row:
                _require_non_negative_int(cell, f"培养索引 leveling.{key} 数值无效")

    operators = _require_mapping(catalog.get("operators"), "培养索引缺少 operators")
    max_by_phase: list[int] = []
    for operator_id, operator in operators.items():
        entry = _require_mapping(operator, f"培养索引干员结构无效:{operator_id}")
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            raise CatalogError(f"培养索引干员缺少名称:{operator_id}")
        rarity = entry.get("rarity")
        if type(rarity) is not int or not 1 <= rarity <= 6:
            raise CatalogError(f"培养索引干员星级无效:{operator_id}")
        max_levels = entry.get("max_levels")
        if not isinstance(max_levels, list) or not 1 <= len(max_levels) <= 3:
            raise CatalogError(f"培养索引干员阶段上限无效:{operator_id}")
        for index, limit in enumerate(max_levels):
            _require_positive_int(limit, f"培养索引干员等级上限无效:{operator_id}")
            while len(max_by_phase) <= index:
                max_by_phase.append(0)
            max_by_phase[index] = max(max_by_phase[index], limit)
        phase_costs = _require_mapping(
            entry.get("phase_costs"), f"培养索引干员缺少 phase_costs:{operator_id}"
        )
        for elite_key, cost in phase_costs.items():
            if not isinstance(elite_key, str) or not elite_key.isdigit():
                raise CatalogError(f"培养索引精英化阶段键无效:{operator_id}")
            elite = int(elite_key)
            if not 1 <= elite <= len(max_levels) - 1:
                raise CatalogError(f"培养索引精英化阶段越界:{operator_id}/{elite_key}")
            cost_entry = _require_mapping(
                cost, f"培养索引精英化成本无效:{operator_id}/{elite_key}"
            )
            _require_non_negative_int(
                cost_entry.get("gold", 0), f"培养索引精英化龙门币无效:{operator_id}"
            )
            items = _require_mapping(
                cost_entry.get("items"), f"培养索引精英化材料无效:{operator_id}"
            )
            for item_id, count in items.items():
                if not isinstance(item_id, str) or not item_id:
                    raise CatalogError(f"培养索引材料键无效:{operator_id}")
                _require_positive_int(count, f"培养索引材料数量无效:{operator_id}/{item_id}")

    for index, limit in enumerate(max_by_phase):
        rows = leveling["exp_per_level"]
        if index >= len(rows) or len(rows[index]) < limit - 1:
            raise CatalogError(f"培养索引经验表长度不足,阶段 {index}")
        gold_rows = leveling["gold_per_level"]
        if index >= len(gold_rows) or len(gold_rows[index]) < limit - 1:
            raise CatalogError(f"培养索引龙门币表长度不足,阶段 {index}")

    items = _require_mapping(catalog.get("items"), "培养索引缺少 items")
    for item_id, item in items.items():
        entry = _require_mapping(item, f"培养索引物品结构无效:{item_id}")
        if not isinstance(entry.get("name"), str) or not entry["name"]:
            raise CatalogError(f"培养索引物品缺少名称:{item_id}")
        stages = entry.get("stages", [])
        if not isinstance(stages, list) or not all(
            isinstance(stage, str) and stage for stage in stages
        ):
            raise CatalogError(f"培养索引物品关卡列表无效:{item_id}")

    recipes = _require_mapping(catalog.get("recipes"), "培养索引缺少 recipes")
    for item_id, recipe in recipes.items():
        entry = _require_mapping(recipe, f"培养索引配方结构无效:{item_id}")
        if entry.get("item_id") != item_id:
            raise CatalogError(f"培养索引配方 item_id 不一致:{item_id}")
        _require_positive_int(entry.get("output_count"), f"培养索引配方产量无效:{item_id}")
        _require_non_negative_int(entry.get("gold", 0), f"培养索引配方龙门币无效:{item_id}")
        inputs = _require_mapping(entry.get("inputs"), f"培养索引配方材料无效:{item_id}")
        if not inputs:
            raise CatalogError(f"培养索引配方没有材料:{item_id}")
        for input_id, count in inputs.items():
            if not isinstance(input_id, str) or not input_id:
                raise CatalogError(f"培养索引配方材料键无效:{item_id}")
            _require_positive_int(count, f"培养索引配方材料数量无效:{item_id}/{input_id}")
    _check_acyclic(recipes)

    stages = catalog.get("stages")
    if not isinstance(stages, list):
        raise CatalogError("培养索引缺少 stages")
    for stage in stages:
        entry = _require_mapping(stage, "培养索引关卡结构无效")
        if not isinstance(entry.get("stage"), str) or not entry["stage"]:
            raise CatalogError("培养索引关卡缺少 stage")
        weekdays = entry.get("open_weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            raise CatalogError(f"培养索引关卡开放日无效:{entry['stage']}")
        for weekday in weekdays:
            if weekday not in WEEKDAYS:
                raise CatalogError(f"培养索引关卡开放日无效:{entry['stage']}")
        _require_positive_int(entry.get("ap_cost"), f"培养索引关卡理智无效:{entry['stage']}")
        if not isinstance(entry.get("items"), list):
            raise CatalogError(f"培养索引关卡掉落无效:{entry['stage']}")

    for key in ("virtual_resources", "exp_items", "chip_exchange"):
        if key in catalog and not isinstance(catalog[key], dict):
            raise CatalogError(f"培养索引 {key} 结构无效")
    for item_id, entry in (catalog.get("exp_items") or {}).items():
        value = _require_mapping(entry, f"培养索引作战记录结构无效:{item_id}")
        if not isinstance(value.get("name"), str) or not value["name"]:
            raise CatalogError(f"培养索引作战记录缺少名称:{item_id}")
        _require_positive_int(value.get("exp"), f"培养索引作战记录经验无效:{item_id}")
    if "blocked_items" in catalog and not isinstance(catalog["blocked_items"], list):
        raise CatalogError("培养索引 blocked_items 结构无效")
    return catalog


def _check_catalog_shape(catalog: Any) -> dict[str, Any]:
    """Cheap guard used by the hot path (planning)."""

    value = _require_mapping(catalog, "培养索引结构无效")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError("培养索引版本无效")
    for key in ("operators", "items", "recipes", "stages", "leveling", "currency"):
        if key not in value:
            raise CatalogError(f"培养索引缺少 {key}")
    return value


def _error(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _as_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def validate_goal(catalog: Any, goal: Any) -> dict[str, Any]:
    """Validate one goal against the catalog.

    Returns ``{"valid", "errors", "goal", "operator"}``. Missing
    ``current_elite``/``current_level`` default to ``0``/``1`` (from scratch),
    ``enabled`` defaults to true and ``completed`` to false.
    """

    catalog_value = _check_catalog_shape(catalog)
    operators = catalog_value["operators"]
    errors: list[dict[str, str]] = []

    payload = goal if isinstance(goal, dict) else {}
    goal_id = payload.get("id")
    if not isinstance(goal_id, str) or not goal_id:
        errors.append(_error("missing_id", "目标缺少 id"))
        goal_id = ""
    operator_id = payload.get("operator_id")
    if not isinstance(operator_id, str) or not operator_id:
        errors.append(_error("missing_operator", "目标缺少 operator_id"))
        operator_id = ""

    operator = operators.get(operator_id) if operator_id else None
    if operator is None and not any(error["code"] == "missing_operator" for error in errors):
        errors.append(_error("unknown_operator", f"索引中没有干员:{operator_id}"))

    current_elite = payload.get("current_elite", 0)
    current_level = payload.get("current_level", 1)
    current_elite_value = _as_int(current_elite)
    current_level_value = _as_int(current_level)
    target_elite_value = _as_int(payload.get("target_elite"))
    target_level_value = _as_int(payload.get("target_level"))

    if target_elite_value is None:
        errors.append(_error("missing_target_elite", "目标缺少 target_elite"))
    if target_level_value is None:
        errors.append(_error("missing_target_level", "目标缺少 target_level"))

    if isinstance(operator, dict):
        max_levels = operator.get("max_levels") or []
        if current_elite_value is None or not 0 <= current_elite_value < len(max_levels):
            errors.append(_error("current_elite_out_of_range", "当前精英阶段越界"))
        if target_elite_value is not None and not 0 <= target_elite_value < len(max_levels):
            errors.append(_error("target_elite_out_of_range", "目标精英阶段越界"))
        if (
            current_elite_value is not None
            and 0 <= current_elite_value < len(max_levels)
            and (
                current_level_value is None
                or not 1 <= current_level_value <= max_levels[current_elite_value]
            )
        ):
            errors.append(_error("current_level_out_of_range", "当前等级越界"))
        if (
            target_elite_value is not None
            and 0 <= target_elite_value < len(max_levels)
            and (
                target_level_value is None
                or not 1 <= target_level_value <= max_levels[target_elite_value]
            )
        ):
            errors.append(_error("target_level_out_of_range", "目标等级越界"))
        if (
            not errors
            # service 复核实际练度后,已达标的目标允许 target < current(仍校验练度合法)
            and not bool(payload.get("completed", False))
            and (target_elite_value, target_level_value) < (current_elite_value, current_level_value)
        ):
            errors.append(_error("target_before_current", "目标低于当前培养度"))

    normalized = {
        "id": goal_id,
        "operator_id": operator_id,
        "current_elite": current_elite_value if current_elite_value is not None else 0,
        "current_level": current_level_value if current_level_value is not None else 1,
        "target_elite": target_elite_value,
        "target_level": target_level_value,
        "enabled": bool(payload.get("enabled", True)),
        "completed": bool(payload.get("completed", False)),
    }
    return {
        "valid": not errors,
        "errors": errors,
        "goal": normalized,
        "operator": (
            {
                "id": operator_id,
                "name": operator["name"],
                "rarity": operator["rarity"],
                "max_levels": list(operator["max_levels"]),
            }
            if isinstance(operator, dict)
            else None
        ),
    }


def _level_segments(max_levels: list[int], current_elite: int, current_level: int,
                    target_elite: int, target_level: int) -> list[tuple[int, int, int]]:
    if current_elite == target_elite:
        return [(current_elite, current_level, target_level)] if target_level > current_level else []
    segments = [(current_elite, current_level, max_levels[current_elite])]
    for elite in range(current_elite + 1, target_elite):
        segments.append((elite, 1, max_levels[elite]))
    segments.append((target_elite, 1, target_level))
    return segments


def _level_cost(leveling: Mapping[str, Any], elite: int, from_level: int, to_level: int) -> tuple[int, int]:
    if to_level <= from_level:
        return 0, 0
    exp_row = leveling["exp_per_level"][elite]
    gold_row = leveling["gold_per_level"][elite]
    return (
        sum(exp_row[from_level - 1: to_level - 1]),
        sum(gold_row[from_level - 1: to_level - 1]),
    )


def _item_entry(catalog: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    entry = catalog["items"].get(item_id)
    return entry if isinstance(entry, dict) else {}


def _try_cover(
    recipes: Mapping[str, Any], key: str, need: int, pools: dict[str, int], depth: int = 0
) -> bool:
    """Can ``need`` of ``key`` be satisfied from ``pools`` using recipes only?

    Used to decide whether an item that is *also* a conservative stage drop should
    be crafted right now (low tier materials already in stock) instead of being
    reported as missing. ``pools`` is mutated on success and simply discarded on
    failure, so a negative answer changes nothing.
    """

    if need <= 0:
        return True
    if depth > MAX_RECIPE_DEPTH:
        return False
    available = pools.get(key, 0)
    if available > 0:
        taken = min(available, need)
        pools[key] = available - taken
        need -= taken
    if need <= 0:
        return True
    recipe = recipes.get(key)
    if not isinstance(recipe, dict):
        return False
    crafts = -(-need // recipe["output_count"])
    for input_id, count in recipe["inputs"].items():
        if not _try_cover(recipes, str(input_id), int(count) * crafts, pools, depth + 1):
            return False
    surplus = crafts * recipe["output_count"] - need
    if surplus:
        pools[key] = pools.get(key, 0) + surplus
    return True


def _merge(add_to: dict[str, int], key: str, amount: int) -> None:
    if amount:
        add_to[key] = add_to.get(key, 0) + amount


def _merge_craft(steps: dict[str, dict[str, Any]], recipe: Mapping[str, Any], crafts: int) -> None:
    item_id = recipe["item_id"]
    step = steps.get(item_id)
    if step is None:
        step = {
            "item_id": item_id,
            "quantity": 0,
            "craft_count": 0,
            "produced": 0,
            "inputs": dict(recipe["inputs"]),
            "station": dict(recipe.get("station") or {}),
        }
        steps[item_id] = step
    step["craft_count"] += crafts
    step["produced"] += crafts * recipe["output_count"]
    step["quantity"] = step["produced"]


def _merge_step(target: dict[str, dict[str, Any]], step: Mapping[str, Any]) -> None:
    item_id = step["item_id"]
    current = target.get(item_id)
    if current is None:
        target[item_id] = {
            "item_id": item_id,
            "quantity": step["quantity"],
            "craft_count": step["craft_count"],
            "produced": step["produced"],
            "inputs": dict(step["inputs"]),
            "station": dict(step["station"]),
        }
        return
    current["craft_count"] += step["craft_count"]
    current["produced"] += step["produced"]
    current["quantity"] = current["produced"]


def _plan_goal(
    catalog: Mapping[str, Any],
    operator: Mapping[str, Any],
    goal: Mapping[str, Any],
    pool: dict[str, int],
    books: dict[str, int],
    stages_by_code: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    gold_key = catalog["currency"]["gold_item_id"]
    recipes = catalog["recipes"]
    leveling = catalog["leveling"]
    max_levels = list(operator["max_levels"])
    current_elite = goal["current_elite"]
    current_level = goal["current_level"]
    target_elite = goal["target_elite"]
    target_level = goal["target_level"]

    direct: dict[str, int] = {}
    lmd = {"elite": 0, "level": 0, "craft": 0, "overshoot": 0, "total": 0}
    exp_total = 0
    lmd_base = 0
    phases: list[dict[str, Any]] = []
    from_books_total: list[dict[str, Any]] = []
    granted_total = 0
    missing_total = 0
    leftover_total = 0

    for elite in range(current_elite + 1, target_elite + 1):
        cost = (operator["phase_costs"] or {}).get(str(elite)) or {}
        gold = int(cost.get("gold", 0) or 0)
        _merge(direct, gold_key, gold)
        lmd["elite"] += gold
        for item_id, count in (cost.get("items") or {}).items():
            _merge(direct, str(item_id), int(count))

    for elite, from_level, to_level in _level_segments(
        max_levels, current_elite, current_level, target_elite, target_level
    ):
        need, base_gold = _level_cost(leveling, elite, from_level, to_level)
        exp_total += need
        lmd_base += base_gold
        # 每个精英阶段独立整张分配:一本书不能拆到两个阶段。
        phase_allocation = _allocate_exp_for_phase(catalog, need, books)
        granted_total += phase_allocation["granted"]
        missing_total += phase_allocation["missing"]
        leftover_total += phase_allocation["leftover"]
        from_books_total.extend(phase_allocation["from_books"])
        if phase_allocation["missing"]:
            _merge(direct, EXP_KEY, phase_allocation["missing"])
        # 整张书会继续把干员喂上去:龙门币按实际走到的等级算,并且封顶在本阶段满级。
        walked_budget = max(phase_allocation["allocated"], need)
        walked_to = _walk_level(leveling, elite, from_level, walked_budget, max_levels[elite])
        walked_gold = _level_cost(leveling, elite, from_level, walked_to)[1]
        lmd["level"] += walked_gold
        _merge(direct, gold_key, walked_gold)
        phases.append(
            {
                "elite": elite,
                "from_level": from_level,
                "target_level": to_level,
                "needed": need,
                "allocated_exp": phase_allocation["allocated"],
                "granted": phase_allocation["granted"],
                "missing": phase_allocation["missing"],
                "walked_to_level": walked_to,
                "lmd_base": base_gold,
                "lmd": walked_gold,
                "from_books": phase_allocation["from_books"],
                "leftover_credit": phase_allocation["leftover"],
            }
        )

    lmd["overshoot"] = lmd["level"] - lmd_base

    requirements = dict(direct)
    if exp_total:
        requirements[EXP_KEY] = exp_total
    allocation = {
        "unit": EXP_KEY,
        "needed": exp_total,
        "granted": granted_total,
        "from_books": from_books_total,
        "leftover_credit": leftover_total,
        "leftover_usable": False,
        "missing": missing_total,
        "phases": phases,
    }
    expanded: dict[str, int] = {}
    missing: dict[str, int] = {}
    blocked: list[dict[str, Any]] = []
    farm: list[dict[str, Any]] = []
    craft_steps: dict[str, dict[str, Any]] = {}
    craft_surplus: dict[str, int] = {}
    # 合成余量只在当前目标内可用:原料还没拿到时不能凭“虚拟余量”让后续目标就绪。
    pending: dict[str, int] = {}
    virtual_resources = catalog.get("virtual_resources") or {}

    layer = dict(direct)
    guard = 0
    while layer:
        guard += 1
        if guard > 64:
            raise CatalogError("配方展开层数异常,疑似循环")
        next_layer: dict[str, int] = {}
        for key in layer:
            need = layer[key]
            if need <= 0:
                continue
            if key == EXP_KEY:
                _merge(missing, key, need)
                exp_entry = virtual_resources.get(EXP_KEY)
                if isinstance(exp_entry, dict):
                    _record_farm(farm, exp_entry, key, need, stages_by_code)
                continue
            available = pool.get(key, 0)
            if available > 0:
                taken = min(available, need)
                pool[key] = available - taken
                need -= taken
            available_pending = pending.get(key, 0)
            if need > 0 and available_pending > 0:
                taken = min(available_pending, need)
                pending[key] = available_pending - taken
                need -= taken
            if need <= 0:
                continue
            entry = _item_entry(catalog, key)
            source_kind = entry.get("source_kind")
            recipe = recipes.get(key)
            if not isinstance(recipe, dict):
                recipe = None
            if source_kind == "stage_primary" and recipe is not None:
                snapshot = dict(pool)
                for pending_key, pending_amount in pending.items():
                    snapshot[pending_key] = snapshot.get(pending_key, 0) + pending_amount
                if not _try_cover(recipes, key, need, snapshot):
                    recipe = None
            if source_kind == "stage_primary" and recipe is None:
                _merge(missing, key, need)
                _record_farm(farm, entry, key, need, stages_by_code)
                continue
            if isinstance(recipe, dict):
                output_count = recipe["output_count"]
                crafts = -(-need // output_count)
                surplus = crafts * output_count - need
                if surplus:
                    craft_surplus[key] = craft_surplus.get(key, 0) + surplus
                    pending[key] = pending.get(key, 0) + surplus
                _merge_craft(craft_steps, recipe, crafts)
                gold_cost = crafts * int(recipe.get("gold", 0) or 0)
                if gold_cost:
                    lmd["craft"] += gold_cost
                    _merge(next_layer, gold_key, gold_cost)
                    _merge(expanded, gold_key, gold_cost)
                for input_id, count in recipe["inputs"].items():
                    total = int(count) * crafts
                    _merge(next_layer, str(input_id), total)
                    _merge(expanded, str(input_id), total)
                continue
            if source_kind == "stage_bonus":
                _merge(missing, key, need)
                _record_farm(farm, entry, key, need, stages_by_code)
                continue
            _merge(missing, key, need)
            blocked.append(_blocked_entry(catalog, entry, key, need))
        layer = next_layer

    lmd["total"] = lmd["elite"] + lmd["level"] + lmd["craft"]
    if blocked:
        status = STATUS_BLOCKED
    elif missing:
        status = STATUS_MISSING
    else:
        status = STATUS_READY
    return {
        "id": goal["id"],
        "operator_id": goal["operator_id"],
        "name": operator["name"],
        "status": status,
        "errors": [],
        "target": {
            "current_elite": current_elite,
            "current_level": current_level,
            "target_elite": target_elite,
            "target_level": target_level,
        },
        "requirements": requirements,
        "expanded": expanded,
        "missing": missing,
        "crafts": list(craft_steps.values()),
        "farm": farm,
        "blocked": blocked,
        "lmd": lmd,
        "exp": allocation,
        "craft_surplus": craft_surplus,
    }


def _record_farm(
    farm: list[dict[str, Any]],
    entry: Mapping[str, Any],
    item_id: str,
    count: int,
    stages_by_code: Mapping[str, Mapping[str, Any]],
) -> None:
    for existing in farm:
        if existing["item_id"] == item_id:
            existing["count"] += count
            return
    stage_code = None
    stage_entry: Mapping[str, Any] = {}
    for candidate in entry.get("stages") or []:
        if candidate in stages_by_code:
            stage_code = candidate
            stage_entry = stages_by_code[candidate]
            break
    if stage_code is None:
        stages = entry.get("stages") or []
        stage_code = stages[0] if stages else None
    farm.append(
        {
            "item_id": item_id,
            "count": count,
            "stage": stage_code,
            "zone_id": stage_entry.get("zone_id"),
            "ap_cost": stage_entry.get("ap_cost"),
            "open_weekdays": list(stage_entry.get("open_weekdays") or []),
        }
    )


def _blocked_entry(
    catalog: Mapping[str, Any], entry: Mapping[str, Any], item_id: str, count: int
) -> dict[str, Any]:
    acquisition = entry.get("acquisition")
    if isinstance(acquisition, dict):
        reason = acquisition.get("kind") or "unavailable"
        detail = acquisition.get("detail") or "无自动获取途径"
    elif item_id == EXP_KEY:
        reason = "virtual"
        detail = "经验为虚拟单位"
    else:
        reason = "unavailable"
        detail = "索引中没有该物资的可刷关卡或配方"
    name = entry.get("name") or item_id
    return {
        "item_id": item_id,
        "name": name,
        "count": count,
        "reason": reason,
        "detail": detail,
    }


def _normalize_inventory(
    catalog: Mapping[str, Any], inventory: Any
) -> tuple[dict[str, int], dict[str, int], list[dict[str, Any]], list[str]]:
    """Split inventory into physical item pool, physical exp books and ignored keys.

    Battle records stay whole units: they are never converted into a shared EXP
    number, because one record cannot be split between two operators. The virtual
    ``EXP`` key is a derived unit and is explicitly rejected as external stock.
    """

    pool: dict[str, int] = {}
    books: dict[str, int] = {}
    ignored: list[dict[str, Any]] = []
    invalid: list[str] = []
    exp_items = catalog.get("exp_items") or {}
    if inventory is None:
        return pool, books, ignored, invalid
    if not isinstance(inventory, dict):
        return pool, books, ignored, ["库存不是键值对象,已按空库存处理"]
    for key, value in inventory.items():
        if not isinstance(key, str) or not key:
            invalid.append(f"库存键无效:{key!r}")
            continue
        amount = _as_int(value)
        if amount is None or amount < 0:
            invalid.append(f"库存 {key} 数值无效,已按 0 处理")
            continue
        if amount == 0:
            continue
        if key == EXP_KEY:
            ignored.append(
                {"key": key, "value": amount, "reason": "virtual_unit_not_accepted"}
            )
            continue
        exp_entry = exp_items.get(key)
        if isinstance(exp_entry, dict):
            exp_value = _as_int(exp_entry.get("exp"))
            if exp_value is None or exp_value <= 0:
                invalid.append(f"库存 {key} 缺少经验换算值,已按 0 处理")
                continue
            _merge(books, key, amount)
            continue
        _merge(pool, key, amount)
    return pool, books, ignored, invalid


def _exp_book_value(catalog: Mapping[str, Any], item_id: str) -> int:
    entry = (catalog.get("exp_items") or {}).get(item_id)
    if isinstance(entry, dict):
        value = entry.get("exp")
        if type(value) is int and value > 0:
            return value
    return 0


def _allocate_exp_for_phase(
    catalog: Mapping[str, Any],
    needed: int,
    books: dict[str, int],
) -> dict[str, Any]:
    """Allocate whole battle records for ONE elite phase; leftover is informational.

    Small records first (conservative, not optimised). A record is consumed as a
    whole unit and can never be split across elite phases, and whatever exp the
    phase does not use is only *reported*: v1 never reuses it in a later phase or
    goal because there is no evidence that unused record exp survives a promotion.
    """

    remaining = max(0, needed)
    from_books: list[dict[str, Any]] = []
    allocated = 0
    if remaining > 0:
        order = sorted(
            (book_id for book_id in books if _exp_book_value(catalog, book_id) > 0),
            key=lambda book_id: (_exp_book_value(catalog, book_id), book_id),
        )
        for book_id in order:
            if remaining <= 0:
                break
            per_book = _exp_book_value(catalog, book_id)
            count = books.get(book_id, 0)
            if count <= 0:
                continue
            need_books = -(-remaining // per_book)
            take = min(need_books, count)
            if take >= need_books:
                granted = remaining
                leftover = take * per_book - remaining
                remaining = 0
            else:
                granted = take * per_book
                leftover = 0
                remaining -= granted
            books[book_id] = count - take
            allocated += take * per_book
            from_books.append(
                {
                    "item_id": book_id,
                    "count": take,
                    "exp": per_book,
                    "exp_granted": granted,
                    "exp_leftover": leftover,
                }
            )
    granted = sum(book["exp_granted"] for book in from_books)
    return {
        "allocated": allocated,
        "granted": granted,
        "from_books": from_books,
        "missing": remaining,
        "leftover": sum(book["exp_leftover"] for book in from_books),
    }


def _walk_level(
    leveling: Mapping[str, Any],
    elite: int,
    from_level: int,
    exp_budget: int,
    max_level: int,
) -> int:
    """Level reached after spending ``exp_budget`` inside one elite phase."""

    row = leveling["exp_per_level"][elite]
    level = from_level
    spent = 0
    while level < max_level:
        cost = row[level - 1]
        if spent + cost > exp_budget:
            break
        spent += cost
        level += 1
    return level


def calculate_plan(catalog: Any, goals: Any, inventory: Any) -> dict[str, Any]:
    """Plan cultivation goals against a fixed inventory.

    Inventory is consumed in goal order and never reused; missing keys count as
    zero. Direct demands (promotion materials + level LMD + exp) are registered
    before recipe expansion so a low tier material is never double counted as
    both a goal requirement and a recipe input. Craft counts round up, craft LMD
    is charged, and items without a farmable stage, recipe or automatic
    exchange stay blocked instead of being reported as ready.

    Result semantics per goal:

    ``requirements``
        direct finished-product demand (promotion materials, elite/level LMD,
        exp) *before* recipe expansion.
    ``expanded``
        extra demand produced by recipe expansion (craft inputs + craft LMD).
    ``missing``
        leaf-only shortfall after inventory and crafting: items the user must
        still obtain (stage drops, blocked exchanges, virtual exp). Crafted
        intermediates never appear here, so ``missing`` alone decides readiness.
    ``crafts``
        rows shaped ``{item_id, quantity, craft_count, produced, inputs,
        station}`` where ``quantity == produced`` and ``inputs`` are the per-craft
        recipe inputs (multiply by ``craft_count`` for total consumption).
    ``farm``
        leaf shortfall with a conservative permanently-open stage. An ``EXP``
        entry points at the LS family; the runtime runs that stage and converts
        the 作战记录 it drops instead of matching ``EXP`` as a drop item.

    ``exp``
        whole-record allocation detail: ``needed``, ``granted``, ``from_books``
        (per record id: count / exp / exp_granted / exp_leftover),
        ``leftover_credit``, ``leftover_usable``, ``missing`` and ``phases``.
        Battle records are never converted into one shared EXP number, and each
        elite phase allocates whole records independently (a record never spans
        two phases, and v1 reports leftover exp instead of reusing it in a later
        phase/goal, because unused record exp surviving a promotion is unproven).
        A missing ``EXP`` value routes to an LS stage. Virtual ``EXP`` is a
        derived unit only and is rejected if a caller passes it as real
        inventory.

    LMD is a conservative upper bound of what a level-up can actually cost: when
    whole records overshoot the target level, ``lmd.level`` charges every level
    the operator walks through (capped at the phase max level) and the extra is
    exposed as ``lmd.overshoot``; ``exp.phases[*].walked_to_level`` / ``lmd``
    show it per phase. When a phase has no records at all the table cost to the
    target level is charged, i.e. overshoot from *farmed* records is not modelled.

    Crafted surplus stays local to one goal: it never lets a later goal look
    ready before the materials for that craft are actually available.
    """

    catalog_value = _check_catalog_shape(catalog)
    stages_by_code = {
        entry["stage"]: entry
        for entry in catalog_value["stages"]
        if isinstance(entry, dict) and isinstance(entry.get("stage"), str)
    }
    pool, books, ignored_inventory, invalid_inventory = _normalize_inventory(
        catalog_value, inventory
    )
    initial_pool = dict(pool)
    initial_books = dict(books)
    gold_key = (catalog_value.get("currency") or {}).get("gold_item_id")

    goal_list = list(goals) if isinstance(goals, (list, tuple)) else []
    results: list[dict[str, Any]] = []
    aggregate_missing: dict[str, int] = {}
    aggregate_requirements: dict[str, int] = {}
    aggregate_expanded: dict[str, int] = {}
    aggregate_crafts: dict[str, dict[str, Any]] = {}
    aggregate_farm: dict[str, dict[str, Any]] = {}
    aggregate_surplus: dict[str, int] = {}
    aggregate_blocked: list[dict[str, Any]] = []
    invalid_goals: list[str] = []

    for raw_goal in goal_list:
        validation = validate_goal(catalog_value, raw_goal)
        goal = validation["goal"]
        if not validation["valid"]:
            invalid_goals.append(goal["id"])
            results.append(
                {
                    "id": goal["id"],
                    "operator_id": goal["operator_id"],
                    "name": (validation["operator"] or {}).get("name"),
                    "status": STATUS_INVALID,
                    "errors": validation["errors"],
                    "target": {
                        "current_elite": goal["current_elite"],
                        "current_level": goal["current_level"],
                        "target_elite": goal["target_elite"],
                        "target_level": goal["target_level"],
                    },
                    "requirements": {},
                    "expanded": {},
                    "missing": {},
                    "crafts": [],
                    "farm": [],
                    "blocked": [],
                    "lmd": {"elite": 0, "level": 0, "craft": 0, "total": 0},
                    "exp": _empty_exp(),
                    "craft_surplus": {},
                }
            )
            continue
        if goal["completed"]:
            results.append(_skip_result(goal, validation["operator"], STATUS_COMPLETED))
            continue
        if not goal["enabled"]:
            results.append(_skip_result(goal, validation["operator"], STATUS_DISABLED))
            continue
        operator_entry = catalog_value["operators"].get(goal["operator_id"])
        if not isinstance(operator_entry, dict):
            raise CatalogError(f"索引中没有干员:{goal['operator_id']}")
        entry = _plan_goal(
            catalog_value, operator_entry, goal, pool, books, stages_by_code
        )
        for key, amount in entry["missing"].items():
            _merge(aggregate_missing, key, amount)
        for key, amount in entry["requirements"].items():
            _merge(aggregate_requirements, key, amount)
        for key, amount in entry["expanded"].items():
            _merge(aggregate_expanded, key, amount)
        for step in entry["crafts"]:
            _merge_step(aggregate_crafts, step)
        for item in entry["farm"]:
            merged = aggregate_farm.get(item["item_id"])
            if merged is None:
                aggregate_farm[item["item_id"]] = dict(item)
            else:
                merged["count"] += item["count"]
        for key, amount in entry["craft_surplus"].items():
            _merge(aggregate_surplus, key, amount)
        aggregate_blocked.extend(entry["blocked"])
        results.append(entry)

    used: dict[str, int] = {}
    remaining: dict[str, int] = {}
    for key, amount in initial_pool.items():
        left = pool.get(key, 0)
        remaining[key] = left
        if amount != left:
            used[key] = amount - left
    books_used: dict[str, int] = {}
    for key, amount in initial_books.items():
        left = books.get(key, 0)
        if amount != left:
            books_used[key] = amount - left

    exp_allocation: list[dict[str, Any]] = []
    for entry in results:
        if entry["status"] in (STATUS_COMPLETED, STATUS_DISABLED, STATUS_INVALID):
            continue
        exp = entry["exp"]
        farm_stage = next(
            (item["stage"] for item in entry["farm"] if item["item_id"] == EXP_KEY),
            None,
        )
        exp_allocation.append(
            {
                "goal_id": entry["id"],
                "operator_id": entry["operator_id"],
                "needed": exp["needed"],
                "granted": exp["granted"],
                "from_books": exp["from_books"],
                "leftover_credit": exp["leftover_credit"],
                "leftover_usable": exp["leftover_usable"],
                "missing": exp["missing"],
                "farm_stage": farm_stage,
            }
        )

    notes = list(STANDARD_NOTES)
    book_values = sorted(
        {
            _exp_book_value(catalog_value, book_id)
            for book_id in initial_books
            if _exp_book_value(catalog_value, book_id) > 0
        }
    )
    if book_values:
        notes.append(
            "作战记录虚拟经验 " + "/".join(str(value) for value in book_values)
            + " 为稳定常量来源(不在 excel 数据表内);按整张分配,余经验只作说明,"
            "首版不跨目标复用(跨精英是否保留无证据)"
        )
    if aggregate_missing.get(EXP_KEY):
        notes.append(
            "EXP 缺口的 farm 条目指向 LS 关卡(作战记录);runtime 只需刷该关卡并换算经验,"
            "不要把 EXP 当作掉落物做阈值匹配"
        )
    if gold_key and any(entry.get("lmd", {}).get("total") for entry in results):
        notes.append(
            f"{gold_key} 龙门币余额无法由 Depot 自动确认(classifyType=NORMAL,非 MATERIAL);"
            "需求照常计算,余额需服务层按 unknown 处理,不能当作 0"
        )
    if aggregate_surplus:
        notes.append("合成余量只属于当次合成轮次,不跨目标共享,不会让后续目标提前就绪")
    for item in ignored_inventory:
        if item.get("reason") == "virtual_unit_not_accepted":
            notes.append(
                f"库存 {item['key']}={item['value']} 不接受外部输入(虚拟派生单位),已忽略"
            )
        else:
            notes.append(f"库存 {item['key']}={item['value']} 被忽略({item.get('reason')})")
    for message in invalid_inventory:
        notes.append(message)
    for item in aggregate_blocked:
        notes.append(f"{item['name']}({item['item_id']}) 无自动获取途径,已阻塞:{item['detail']}")
    if invalid_goals:
        notes.append("存在无效目标,未计入就绪判断:" + ", ".join(invalid_goals))

    blocking_statuses = {STATUS_MISSING, STATUS_BLOCKED, STATUS_INVALID}
    ready = all(result["status"] not in blocking_statuses for result in results)

    source = catalog_value.get("source") or {}
    return {
        "ready": ready,
        "catalog_source": {
            "schema_version": catalog_value.get("schema_version"),
            "repository": source.get("repository"),
            "commit": source.get("commit"),
        },
        "goals": results,
        "missing": aggregate_missing,
        "requirements": aggregate_requirements,
        "expanded": aggregate_expanded,
        "crafts": list(aggregate_crafts.values()),
        "farm": list(aggregate_farm.values()),
        "blocked": aggregate_blocked,
        "invalid_goals": invalid_goals,
        "inventory": {
            "used": used,
            "remaining": remaining,
            "books": initial_books,
            "books_used": books_used,
            "books_remaining": dict(books),
            "ignored": ignored_inventory,
        },
        "exp_allocation": exp_allocation,
        "craft_surplus": aggregate_surplus,
        "notes": notes,
    }


def _skip_result(
    goal: Mapping[str, Any], operator: Mapping[str, Any] | None, status: str
) -> dict[str, Any]:
    return {
        "id": goal["id"],
        "operator_id": goal["operator_id"],
        "name": (operator or {}).get("name"),
        "status": status,
        "errors": [],
        "target": {
            "current_elite": goal["current_elite"],
            "current_level": goal["current_level"],
            "target_elite": goal["target_elite"],
            "target_level": goal["target_level"],
        },
        "requirements": {},
        "expanded": {},
        "missing": {},
        "crafts": [],
        "farm": [],
        "blocked": [],
        "lmd": {"elite": 0, "level": 0, "craft": 0, "total": 0},
        "exp": _empty_exp(),
        "craft_surplus": {},
    }


def _empty_exp() -> dict[str, Any]:
    return {
        "unit": EXP_KEY,
        "needed": 0,
        "granted": 0,
        "from_books": [],
        "leftover_credit": 0,
        "leftover_usable": False,
        "missing": 0,
        "phases": [],
    }


def summarize_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Small helper for logs/WebUI: counts per status plus blocked item ids."""

    counts: dict[str, int] = {}
    for entry in plan.get("goals") or []:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {
        "ready": bool(plan.get("ready")),
        "goals": len(plan.get("goals") or []),
        "statuses": counts,
        "blocked_items": sorted({item["item_id"] for item in plan.get("blocked") or []}),
        "missing_items": sorted(plan.get("missing") or {}),
    }


def iter_catalog_items(catalog: Any) -> Iterable[str]:
    """Deterministic item-id iteration helper (used by tooling/tests)."""

    value = _check_catalog_shape(catalog)
    return iter(sorted(value["items"]))
