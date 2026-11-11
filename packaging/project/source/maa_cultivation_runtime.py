#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""培养目标运行编排 runtime（只做运行编排，不做领域计算，也不直接读写状态文件）。

上游接口
========

* ``maa_cultivation_service.py``（唯一状态/计划来源，关键字调用）::

      load_state(config_dir) -> dict              # settings.enabled / revision / goals / inventory
      save_inventory(config_dir, items, *, scanned_at=None) -> dict   # 只成功替换，不累加
      set_runtime(config_dir, status, message) -> None
      pending_notifications(config_dir, catalog_path) -> list[{key, text}]
      mark_notified(config_dir, key) -> None
      get_payload(config_dir, catalog_path) -> dict  # plan / catalog / currency 的唯一来源
      record_lmd_drops(config_dir, event_id, amount, *, confirmed_at=None) -> bool
      invalidate_balance(config_dir, reason) -> None

  runtime 不再自己调用 domain 计算计划：每批之前重新取 get_payload()，直接用其中的 plan
  （service 已按 currency 规则剔除/阻塞龙门币目标）与 catalog。runtime 从 plan 的 farm 列表
  读取 (item_id, stage, open_weekdays, ap_cost) 并按游戏日（UTC+8 04:00 换日）复核开放日；
  索引里没有的关卡、或 verified=false / navigable=false / expired=true 的关卡一律不自动导航。

* 龙门币：成功 Depot 返回的 4001 余额为准，并替换旧余额和掉落事件；逐战 INFO 行 Drops:
  龙门币 × N 仅在同一余额 epoch 内补记，旧 epoch 不跨扫描重复累加。成功的龙门币本若未
  返回 Depot 龙门币余额，invalidate_balance 并停止刷钱。可能扣款的 phase 在执行前失效余额：
  白名单（Mall/Award/Fight/StartUp/CloseDown/
  Depot/OperBox）不扣；Recruit 看 catalog.currency.recruitment_free；Infrast 只看 params.replenish；
  其余未知类型（含 Custom）保守失效。未接管路径由 --mode invalidate-for-task 在运行前处理。

执行语义（--mode execute）
=========================

1. 读取 runner 已做过活动守门 / 基建 takeover 的 effective TOML，保留任务顺序、
   非目标 Fight 语义、剿灭任务以及非 tasks 顶层字段；前置段必须已含 StartUp，
   否则不接管、按原始基线原样执行并提示（不替用户发明启动，也不给后缀补 StartUp）。
2. 第一段前置日常附加 Depot / OperBox 扫描。只有子进程 exit=0、回调 what=DepotInfo、
   details.done=true 且 details.data 非空的完整快照才算真实库存，绝不用估算值。
3. 第一个需要理智的 Fight（剿灭除外）被接管：按 plan.farm 缺料分批插入
   times=5 / series=1 的小批量 Fight，每批之前重读 settings.enabled 与 revision，
   每批之后 Depot 重扫并按真实库存重算；中途关闭或目标变更立即按新状态处理。
4. 仅在“目标已齐料”或“没有开放可刷缺口”时才用 settings.fallback_stage 消耗剩余理智。
5. 连续无库存增益、失败批次过多、批次/次数上限、StartUp/设备失败都会安全停止。
6. 首版无药无石：runtime 生成的每个 Fight 都显式 medicine=0 / stone=0 /
   expiring_medicine=0，防止从原任务 params 或 variants 泄漏。
7. 运行始终在原 runner 的外层任务锁、设备预检、看门狗与日志路径之下；失败一律不自动
   重跑（由 maa-run.sh 保证），避免药剂与副产统计重复。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_DIR = Path("/root/.config/maa")
CATALOG_FILENAME = "maa-cultivation-data.json"
RUNNER_SCRIPT = SCRIPT_DIR / "maa-run.sh"
SCAN_TASK_NAME = "cultivation-scan"
MASTERY_TASK_NAME = "cultivation-mastery"
# MaaCore debug 日志：maa-cli 回调不全时从这里补取本轮最终 OperBox（与 maa-operator-scan.py 同源）。
DEFAULT_ASST_LOG = Path("/root/.local/state/maa/debug/asst.log")
OPERATOR_SNAPSHOT_NAME = "operator-box.json"
OPERATOR_STATE_NAME = "operator-box-state.json"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DEPOT_MARKER = "Depot: "
OPER_PROGRESS_MARKERS = (
    "Assistant::append_callback | SubTaskExtraInfo ",
    "SubTaskExtraInfo, details: ",
    "OperProgress: ",
)

# DepotInfo / OperBoxInfo 是 maa-cli 的 info! 回调：子进程日志级别低于 info 时回调根本不会出现。
MAA_LOG_FLOOR = "info"
MAA_CLI = "maa"
LOCK_BUSY_EXIT = 75

# 龙门币逐战掉落行（maa-cli StageDrops 的 info! 回调）。收尾汇总是小写 "total drops:"，这里不匹配。
DROP_LINE_MARKER = "Drops: "
DROP_PAIR_RE = re.compile(r"([^,×]+?)\s*×\s*([0-9]+)")
# 领域层的虚拟经验单位：仓库里没有这个 key，必须用作战记录按 exp_items 换算。
EXP_ITEM_ID = "EXP"
# 明确不消耗龙门币的任务类型白名单（MAA 语义固定，不依赖 catalog）。
NO_LMD_SPEND_TASK_TYPES = (
    "Mall",
    "Award",
    "Fight",
    "StartUp",
    "CloseDown",
    "Depot",
    "OperBox",
)
# Infrast 只在这两个参数为 true 时才可能用源石补货（InfrastTask.cpp replenish）。
INFRAST_REPLENISH_KEY = "replenish"
INFRAST_PRODUCT_MARKER = "ProductOfFacility: "
# MaaCore 用 OriginStone 表示源石碎片产线（InfrastProductionTask.cpp 里 m_product=OriginStone，
# ReplenishOriginiumShardTaskPlugin 也按 OriginStone 判定）；ProductChanged 分支写 OriginiumShard。
# 只有这类产物才真的消耗龙门币（纯金/经验/芯片等不消耗）。
INFRAST_SHARD_PRODUCTS = ("OriginStone", "OriginiumShard")

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
GAME_DAY_OFFSET_HOURS = 8
GAME_DAY_ROLLOVER_HOURS = 4

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_DISABLED = 3
EXIT_TIMEOUT = 124
EXIT_STARTUP_FAILED = 91

STARTUP_TASK: dict[str, Any] = {
    "name": "开始唤醒",
    "type": "StartUp",
    "params": {"client_type": "Official", "start_game_enabled": True},
}
DEPOT_TASK: dict[str, Any] = {"name": "仓库扫描", "type": "Depot"}
OPERBOX_TASK: dict[str, Any] = {"name": "干员扫描", "type": "OperBox"}

# runtime 生成的 Fight 必须显式关掉药/石，防止从原任务泄漏。
NO_CONSUMABLE_FIGHT_DEFAULTS: dict[str, int] = {"medicine": 0, "stone": 0, "expiring_medicine": 0}


class CultivationError(Exception):
    """runtime 内部可预期错误。"""


class StartupFailure(CultivationError):
    """StartUp 或设备级失败：必须立刻停止，不得重跑或继续后续任务。"""


class CallbackError(CultivationError):
    """回调缺失或结构不合法。"""


@dataclass(frozen=True)
class Limits:
    """刷取上限；默认值即首版稳定默认。"""

    batch_times: int = 5
    series: int = 1
    max_total_battles: int = 60
    max_batches: int = 12
    max_no_gain_streak: int = 2
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    output: str = ""
    name: str = ""
    started_ns: int = 0


@dataclass(frozen=True)
class BatchOutcome:
    """一次培养批次结果：只有完整 Depot 快照才算真实库存。"""

    stage: str
    exit_code: int
    items: dict[str, int] | None
    startup_failed: bool
    fight_error: bool = False
    device_failure: bool = False
    sanity: tuple[int, int] | None = None
    lmd_amounts: tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# 日志文本与回调解析
# ---------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """去掉日志中的 ANSI 转义，保留原始换行。"""

    return ANSI_RE.sub("", text)


def extract_messages(text: str, marker: str) -> list[dict[str, Any]]:
    """按 maa-cli 回调前缀抽取 JSON 消息，支持 ANSI 与多行 pretty JSON。"""

    if not isinstance(text, str) or not text:
        return []
    cleaned = strip_ansi(text)
    decoder = json.JSONDecoder()
    messages: list[dict[str, Any]] = []
    position = 0
    while True:
        index = cleaned.find(marker, position)
        if index < 0:
            break
        start = index + len(marker)
        try:
            value, end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            position = start
            continue
        position = end
        if isinstance(value, dict):
            messages.append(value)
    return messages


def validate_depot_message(message: Mapping[str, Any]) -> dict[str, int]:
    """校验 MaaCore DepotInfo 回调并返回 {item_id: 数量}。

    真实格式（Task/Miscellaneous/DepotRecognitionTask.cpp）::

        {"what": "DepotInfo", "details": {"done": true, "data": "{\"30012\": 12}"}}

    ``details.data`` 是 JSON 字符串（旧 core 可能是对象）。done=true 但 data 为空视为
    "扫描失败"，抛 CallbackError，绝不写入库存。
    """

    if message.get("what") != "DepotInfo":
        raise CallbackError("不是 DepotInfo 回调")
    details = message.get("details")
    if not isinstance(details, Mapping):
        raise CallbackError("DepotInfo 缺少 details")
    if details.get("done") is not True:
        raise CallbackError("Depot 扫描未完成")
    data: Any = details.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as error:
            raise CallbackError("Depot 数据不是合法 JSON") from error
    if not isinstance(data, Mapping):
        raise CallbackError("Depot 数据不是对象")
    items: dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise CallbackError("Depot 物品 ID 无效")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CallbackError(f"Depot 物品数量无效: {key}")
        items[key] = value
    if not items:
        raise CallbackError("Depot 数据为空，按扫描失败处理")
    return items


def final_depot_items(text: str) -> dict[str, int] | None:
    """日志里最后一个完整 Depot 快照；没有完整快照时返回 None。"""

    result: dict[str, int] | None = None
    for message in extract_messages(text, DEPOT_MARKER):
        try:
            result = validate_depot_message(message)
        except CallbackError:
            continue
    return result


def _extract_oper_progress_from_text(
    text: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """从单份日志文本按出现顺序提取 OperProgressDetail 列表与 OperProgressSummary。"""

    if not isinstance(text, str) or not text:
        return [], None
    cleaned = strip_ansi(text)
    starts: list[int] = []
    for marker in OPER_PROGRESS_MARKERS:
        pos = 0
        while True:
            idx = cleaned.find(marker, pos)
            if idx < 0:
                break
            start = idx + len(marker)
            starts.append(start)
            pos = start
    if not starts:
        return [], None
    decoder = json.JSONDecoder()
    details_list: list[dict[str, Any]] = []
    summary: dict[str, Any] | None = None
    seen_starts: set[int] = set()
    for start in sorted(starts):
        if start in seen_starts:
            continue
        seen_starts.add(start)
        try:
            value, _end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        what = value.get("what")
        details = value.get("details")
        if what == "OperProgressDetail" and isinstance(details, Mapping):
            details_list.append(dict(details))
        elif what == "OperProgressSummary" and isinstance(details, Mapping):
            summary = dict(details)
    return details_list, summary


def extract_oper_progress_callbacks(
    output: str, core_log: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """优先从本轮新鲜 MaaCore debug 日志提取 OperProgress 回调，回退至子进程 stdout。"""

    core_details, core_summary = _extract_oper_progress_from_text(core_log)
    if core_details or core_summary is not None:
        return core_details, core_summary
    return _extract_oper_progress_from_text(output)


def oper_progress_task_from_state(
    state: Mapping[str, Any], *, require_enabled: bool = False
) -> dict[str, Any] | None:
    """从 state['mastery'] 组装 MaaCore OperProgress 任务；无有效待执行项时返回 None。"""

    mastery = state.get("mastery")
    if not isinstance(mastery, Mapping):
        return None
    if require_enabled and mastery.get("enabled") is not True:
        return None
    raw_plans = mastery.get("plans")
    if not isinstance(raw_plans, list):
        return None
    task_plans: list[dict[str, Any]] = []
    for item in raw_plans:
        if not isinstance(item, Mapping):
            continue
        if not item.get("enabled", True) or item.get("completed", False):
            continue
        role = str(item.get("role") or "").strip()
        name = str(item.get("operator_name") or "").strip()
        targets = item.get("skill_mastery")
        current = item.get("current_mastery") or [0, 0, 0]
        if (
            not role
            or not name
            or not isinstance(targets, list)
            or len(targets) != 3
        ):
            continue
        effective = [
            0
            if (
                idx < len(current)
                and isinstance(current[idx], int)
                and isinstance(tgt, int)
                and current[idx] >= tgt
            )
            else (int(tgt) if isinstance(tgt, int) and 0 <= tgt <= 3 else 0)
            for idx, tgt in enumerate(targets)
        ]
        if all(v == 0 for v in effective):
            continue
        need_skill_7 = bool(
            item.get("auto_skill_level_7", True)
            and not item.get("skill_level_7_verified", False)
        )
        # 将同一干员的多个未完成专精技能拆成按技能独立的 plans 子项：
        # 1. 若前一个技能启动训练或训练室忙，MaaCore 会在子项循环入口 0ms 跳过后续子项；
        # 2. 若前一个技能刚好在训练室领完满专三（MasterySelectSkillMaxAlready），拆成独立子项可强制
        #    下一技能先走 find_and_open_operator -> OperProgress@ReturnToOperBox 退回档案页，
        #    绕开上游 MaaCore 在训练室直接调 OperFilesImageAnalyzer::elite_level 报 RecognitionFailed 的 Bug。
        first_subplan = True
        for skill_idx, skill_target in enumerate(effective):
            if skill_target <= 0:
                continue
            single_vec = [0, 0, 0]
            single_vec[skill_idx] = skill_target
            entry: dict[str, Any] = {
                "role": role,
                "name": name,
                "skill_mastery": single_vec,
            }
            if first_subplan and need_skill_7:
                entry["skill_level"] = 7
                first_subplan = False
            task_plans.append(entry)
    if not task_plans:
        return None
    return {
        "name": "技能专精",
        "type": "OperProgress",
        "params": {"plans": task_plans},
    }


def output_reports_startup_failure(text: str) -> bool:
    """日志中是否出现 StartUp 失败。"""

    return "StartUp Error" in strip_ansi(text or "")


DEVICE_FAILURE_MARKERS = (
    "ScreencapFailed",
    "Unable to connect",
    "unable to connect",
    "failed to connect",
    "Connection refused",
    "connection refused",
    "device offline",
    "DeviceNotFound",
    "adb: ",
    "连接失败",
)


def output_reports_device_failure(text: str) -> bool:
    """设备 / 连接级失败：不能当作普通关卡失败继续刷其他本。"""

    cleaned = strip_ansi(text or "")
    if "StartUp Error" in cleaned:
        return True
    return any(marker in cleaned for marker in DEVICE_FAILURE_MARKERS)


def output_reports_fight_error(text: str) -> bool:
    """战斗本自身失败：可以熔断该关卡并继续处理其他缺口。"""

    cleaned = strip_ansi(text or "")
    return "Fight Error" in cleaned or "Fight Stopped" in cleaned


def settlement_count(text: str) -> int:
    """Count actual StageDrops log records, not requested limits or starts.

    Generated cultivation/fallback tasks use series=1. For arbitrary prefix
    tasks this remains a settlement-record count, not a claim about battle count.
    """
    return sum(bool(re.search(r"(?:^|\s)Drops:\s", line))
               for line in strip_ansi(text or "").splitlines())


def effective_maa_log(value: Any) -> str:
    """把继承的 MAA_LOG 提升到至少 info：否则 Depot/OperBox 回调不会出现在日志里。

    比 info 更详细的 debug/trace 保持原样，warn/error/off/未设置一律提升为 info。
    """

    level = str(value or "").strip().lower()
    if level in ("trace", "debug"):
        return level
    return MAA_LOG_FLOOR


def lmd_drops_from_output(text: str, item_name: str) -> list[int]:
    """解析逐战 INFO "Drops: <名称> × N" 行，只返回龙门币条目（不含 total 汇总）。

    一行可能同时包含多种掉落（"龙门币 × 1200, 聚酸酯组 × 2"），逐个匹配物品名。
    """

    if not item_name:
        return []
    amounts: list[int] = []
    for line in strip_ansi(text or "").splitlines():
        index = line.find(DROP_LINE_MARKER)
        if index < 0:
            continue
        payload = line[index + len(DROP_LINE_MARKER):]
        for name, count in DROP_PAIR_RE.findall(payload):
            if name.strip() == item_name:
                amounts.append(int(count))
    return amounts


def experience_total(catalog: Mapping[str, Any], items: Mapping[str, Any]) -> int:
    """把仓库里的作战记录按 catalog.exp_items 换算成总经验（EXP 是虚拟物品）。"""

    exp_items = catalog.get("exp_items")
    if not isinstance(exp_items, Mapping) or not isinstance(items, Mapping):
        return 0
    total = 0
    for item_id, count in items.items():
        entry = exp_items.get(item_id)
        if not isinstance(entry, Mapping):
            continue
        value = entry.get("exp")
        if isinstance(value, int) and value > 0 and isinstance(count, int) and count > 0:
            total += value * count
    return total


CORE_LOG_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[.,](\d{1,3}))?")


def filter_core_log_since(text: str, since_ns: int) -> str:
    """只保留本次执行开始之后写入的 MaaCore 日志行（按行首时间戳过滤）。

    MaaCore 的 asst.log 会累积多次任务：文件 mtime 变新不代表每行都新，所以逐行比较时间戳，
    避免把上一轮的最终 OperBox 回调当成本轮结果；没有时间戳的行一律丢弃。
    """

    if since_ns <= 0:
        return ""
    since = datetime.fromtimestamp(since_ns / 1_000_000_000)
    kept: list[str] = []
    for line in (text or "").splitlines():
        match = CORE_LOG_TIMESTAMP_RE.search(line[:96])
        if not match:
            continue
        fraction = (match.group(2) or "0").ljust(3, "0")
        try:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        stamp = stamp.replace(microsecond=int(fraction) * 1000)
        if stamp >= since:
            kept.append(line)
    return "\n".join(kept)


def infrast_replenishes(task: Mapping[str, Any]) -> bool:
    """Infrast 是否开启了源石补货：params.replenish 或任一 variants.params.replenish。

    continue_training 只消耗材料不消耗龙门币（最终 Depot 会刷新库存），因此不算扣款。
    """

    def enabled(params: Any) -> bool:
        return isinstance(params, Mapping) and params.get(INFRAST_REPLENISH_KEY) is True

    if enabled(task.get("params")):
        return True
    for variant in task.get("variants") or []:
        if isinstance(variant, Mapping) and enabled(variant.get("params")):
            return True
    return False


def lmd_risk_types(catalog: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    """返回可能扣龙门币的任务类型标记（用于失效已确认余额）。

    规则：白名单类型不扣；Recruit 在 catalog.currency.recruitment_free 为 true 时视为免费，
    缺失或 false 一律保守失效；Infrast 仅在 replenish=true 时失效；其余未知类型（含 Custom）
    保守失效。
    """

    free_recruit = (catalog.get("currency") or {}).get("recruitment_free") is True
    risky: set[str] = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        kind = str(task.get("type") or "")
        if not kind or kind in NO_LMD_SPEND_TASK_TYPES:
            continue
        if kind == "Recruit":
            if not free_recruit:
                risky.add(kind)
            continue
        if kind == "Infrast":
            # 接管路径不在这里失效：只有运行后看到源石碎片产物才真扣龙门币，
            # 由 infrast_phase_risk() 在 phase 结束后判定。
            continue
        risky.add(kind)
    return sorted(risky)


def infrast_runs_mfg(task: Mapping[str, Any]) -> bool:
    """未指定 facility 时 MAA 默认会跑制造站；显式列表里含 Mfg 也算。"""

    facility = (task.get("params") or {}).get("facility")
    if not isinstance(facility, (list, tuple)) or not facility:
        return True
    return any(str(item) == "Mfg" for item in facility)


def infrast_may_replenish(tasks: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(task, Mapping)
        and task.get("type") == "Infrast"
        and infrast_replenishes(task)
        and infrast_runs_mfg(task)
        for task in tasks
    )


def lmd_conservative_risk_types(
    catalog: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
) -> list[str]:
    """未接管路径的整段运行前判定：拿不到逐 phase INFO，Infrast(replenish) 直接算风险。"""

    risky = set(lmd_risk_types(catalog, tasks))
    if infrast_may_replenish(tasks):
        risky.add("Infrast:" + INFRAST_REPLENISH_KEY)
    return sorted(risky)


def infrast_phase_risk(
    output: str, tasks: Sequence[Mapping[str, Any]], *, failed: bool
) -> str | None:
    """Infrast phase 结束后判断余额是否失效（只有源石碎片产线真扣龙门币）。

    出现 ProductOfFacility: OriginStone/OriginiumShard → 失效；开启 replenish 且会跑制造站，
    但没有任何可靠 ProductOfFacility 记录或本次执行失败 → 保守失效；正常 Gold/Exp 记录保持余额。
    """

    if not any(isinstance(task, Mapping) and task.get("type") == "Infrast" for task in tasks):
        return None
    text = strip_ansi(output or "")
    if any(INFRAST_PRODUCT_MARKER + product in text for product in INFRAST_SHARD_PRODUCTS):
        return "Infrast:originium_shard"
    if not infrast_may_replenish(tasks):
        return None
    if failed or INFRAST_PRODUCT_MARKER not in text:
        return "Infrast:replenish_unverified"
    return None


# ---------------------------------------------------------------------------
# TOML 读写（保留任务顺序与非 tasks 顶层字段）
# ---------------------------------------------------------------------------


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(
            f"{key} = {toml_value(item)}" for key, item in value.items()
        ) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def clean_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def inline_params_ok(params: Mapping[str, Any]) -> bool:
    if len(params) > 4:
        return False
    for value in params.values():
        if isinstance(value, Mapping):
            return False
        if isinstance(value, (list, tuple)) and len(value) > 5:
            return False
    return True


def format_task(task: Mapping[str, Any], array_name: str) -> list[str]:
    lines = [f"[[{array_name}]]"]
    if task.get("name"):
        lines.append(f"name = {toml_value(task['name'])}")
    lines.append(f"type = {toml_value(task.get('type', ''))}")
    if task.get("strategy"):
        lines.append(f"strategy = {toml_value(task['strategy'])}")
    for key, value in task.items():
        if key in {"name", "type", "strategy", "params", "variants"}:
            continue
        if value is None:
            continue
        lines.append(f"{key} = {toml_value(value)}")

    params = clean_mapping(task.get("params") or {})
    variants = task.get("variants") or []
    if params and not variants and inline_params_ok(params):
        lines.append(f"params = {toml_value(params)}")
    elif params:
        lines.append("")
        lines.append(f"[{array_name}.params]")
        for key, value in params.items():
            lines.append(f"{key} = {toml_value(value)}")

    for variant in variants:
        lines.append("")
        lines.append(f"[[{array_name}.variants]]")
        condition = variant.get("condition")
        if condition:
            lines.append(f"condition = {toml_value(condition)}")
        if variant.get("strategy"):
            lines.append(f"strategy = {toml_value(variant['strategy'])}")
        variant_params = clean_mapping(variant.get("params") or {})
        if variant_params:
            lines.append(f"params = {toml_value(variant_params)}")
    lines.append("")
    return lines


def emit_table(prefix: str, mapping: Mapping[str, Any], lines: list[str]) -> None:
    lines.append(f"[{prefix}]")
    nested: dict[str, Mapping[str, Any]] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            nested[key] = value
        else:
            lines.append(f"{key} = {toml_value(value)}")
    for key, value in nested.items():
        lines.append("")
        emit_table(f"{prefix}.{key}", value, lines)


def dump_document(document: Mapping[str, Any], array_names: Sequence[str] = ("tasks", "disabled_tasks")) -> str:
    """写回 TOML：先顶层标量、再其它顶层表、最后任务数组（保持数组内顺序）。"""

    lines: list[str] = []
    scalars: dict[str, Any] = {}
    tables: dict[str, Mapping[str, Any]] = {}
    for key, value in document.items():
        if key in array_names:
            continue
        if isinstance(value, Mapping):
            tables[key] = value
        elif value is not None:
            scalars[key] = value
    for key, value in scalars.items():
        lines.append(f"{key} = {toml_value(value)}")
    if scalars:
        lines.append("")
    for key, value in tables.items():
        emit_table(key, value, lines)
        lines.append("")
    for name in array_names:
        for task in document.get(name) or []:
            lines.extend(format_task(task, name))
    return "\n".join(lines).rstrip() + "\n"


def read_task_document(path: Path | str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    data = tomllib.loads(raw)
    if not isinstance(data, dict):
        raise CultivationError("任务配置不是 TOML 文档")
    return data


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# 任务分片与关卡选择
# ---------------------------------------------------------------------------


def is_annihilation_fight(task: Mapping[str, Any]) -> bool:
    """剿灭作战：stage 为 Annihilation 或以 @Annihilation 结尾（与 WebUI 判定一致）。"""

    if task.get("type") != "Fight":
        return False
    stage = str((task.get("params") or {}).get("stage") or "")
    if stage == "Annihilation" or stage.endswith("@Annihilation"):
        return True
    for variant in task.get("variants") or []:
        variant_stage = str((variant.get("params") or {}).get("stage") or "")
        if variant_stage.endswith("@Annihilation"):
            return True
    return False


def sanity_fight_index(tasks: Sequence[Mapping[str, Any]]) -> int | None:
    """第一个需要消耗理智的 Fight 任务下标；剿灭不算。"""

    for index, task in enumerate(tasks):
        if task.get("type") != "Fight":
            continue
        if is_annihilation_fight(task):
            continue
        return index
    return None


def game_day_start(now: datetime) -> datetime:
    zone = timezone(timedelta(hours=GAME_DAY_OFFSET_HOURS))
    local = now.astimezone(zone) if now.tzinfo else now.replace(tzinfo=zone)
    shifted = local - timedelta(hours=GAME_DAY_ROLLOVER_HOURS)
    return shifted.replace(hour=0, minute=0, second=0, microsecond=0)


def game_weekday_key(now: datetime) -> str:
    return WEEKDAY_KEYS[game_day_start(now).weekday()]


def weekdays_include(weekdays: Any, weekday_key: str) -> bool:
    if not weekdays:
        return True
    if not isinstance(weekdays, (list, tuple)):
        return False
    normalized = {str(day).strip().lower()[:3] for day in weekdays}
    return weekday_key in normalized


def stage_is_navigable(entry: Mapping[str, Any]) -> bool:
    """索引自带的保守可导航标记；显式否定的关卡不自动导航。"""

    if entry.get("verified") is False or entry.get("navigable") is False:
        return False
    if entry.get("expired") is True:
        return False
    return True


def catalog_stage_index(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for entry in catalog.get("stages") or []:
        if not isinstance(entry, Mapping):
            continue
        stage = entry.get("stage")
        if isinstance(stage, str) and stage:
            index[stage] = entry
    return index


def usable_stage(
    catalog: Mapping[str, Any],
    stage: Any,
    weekday_key: str,
    blocked: Sequence[str] = (),
    weekdays: Any = None,
) -> Mapping[str, Any] | None:
    """共享关卡判定：索引内、可自动导航、游戏日开放、本轮未熔断。

    培养批次与备用关卡必须共用该判定，避免再打已关闭或本轮已熔断的本。
    """

    if not isinstance(stage, str) or not stage or stage in set(blocked):
        return None
    entry = catalog_stage_index(catalog).get(stage)
    if entry is None or not stage_is_navigable(entry):
        return None
    if weekdays is None:
        weekdays = entry.get("open_weekdays")
    if not weekdays_include(weekdays, weekday_key):
        return None
    return entry


def farm_candidates(
    plan: Mapping[str, Any],
    catalog: Mapping[str, Any],
    weekday_key: str,
    blocked: Sequence[str] = (),
) -> list[tuple[str, str, Mapping[str, Any]]]:
    """按 plan.farm 顺序返回今天开放、可自动导航且未熔断的 (item_id, stage, entry)。"""

    candidates: list[tuple[str, str, Mapping[str, Any]]] = []
    for item in plan.get("farm") or []:
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("item_id")
        stage = item.get("stage")
        if not isinstance(item_id, str) or not item_id:
            continue
        entry = usable_stage(catalog, stage, weekday_key, blocked, item.get("open_weekdays"))
        if entry is None:
            continue
        candidates.append((item_id, stage, entry))
    return candidates


def pick_farm_target(
    plan: Mapping[str, Any],
    catalog: Mapping[str, Any],
    weekday_key: str,
    blocked: Sequence[str] = (),
) -> tuple[str, str] | None:
    """按缺料选 (item_id, stage)；没有开放可刷缺口时返回 None。"""

    candidates = farm_candidates(plan, catalog, weekday_key, blocked)
    if not candidates:
        return None
    item_id, stage, _entry = candidates[0]
    return item_id, stage


def missing_items(plan: Mapping[str, Any]) -> dict[str, int]:
    """plan.missing 归一化成 {item_id: 正数缺口}。"""

    missing = plan.get("missing")
    if not isinstance(missing, Mapping):
        return {}
    result: dict[str, int] = {}
    for item, count in missing.items():
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            continue
        if int(count) > 0:
            result[str(item)] = int(count)
    return result


def fight_task(
    stage: str,
    *,
    times: int,
    series: int = 1,
    name: str = "培养作战",
    drops: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """生成一个无药无石的小批量 Fight 任务。"""

    params: dict[str, Any] = {"stage": stage, "times": int(times), "series": int(series)}
    if drops:
        params["drops"] = {item: int(count) for item, count in drops.items() if count > 0}
    params.update(NO_CONSUMABLE_FIGHT_DEFAULTS)
    return {"name": name, "type": "Fight", "params": params}


# ---------------------------------------------------------------------------
# 兄弟模块加载与适配
# ---------------------------------------------------------------------------


def load_sibling(name: str) -> Any:
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    return importlib.import_module(name)


class ServiceAdapter:
    """把 service 收敛成 runtime 需要的最小集合；读状态失败一律向上抛，不静默降级。"""

    def __init__(self, module: Any) -> None:
        self._module = module

    def load_state(self, config_dir: Path) -> dict[str, Any]:
        state = self._module.load_state(str(config_dir))
        if not isinstance(state, dict):
            raise CultivationError("service.load_state 返回非对象")
        return state

    def settings(self, config_dir: Path) -> dict[str, Any]:
        settings = self.load_state(config_dir).get("settings")
        if not isinstance(settings, Mapping):
            raise CultivationError("培养状态缺少 settings")
        return dict(settings)

    def get_payload(self, config_dir: Path, catalog_path: Path) -> dict[str, Any]:
        """service.get_payload：plan/catalog/currency 的唯一来源（runtime 不再自己算 plan）。"""

        payload = self._module.get_payload(str(config_dir), str(catalog_path))
        if not isinstance(payload, Mapping):
            raise CultivationError("service.get_payload 返回非对象")
        return dict(payload)

    def enabled(self, config_dir: Path) -> bool:
        return self.settings(config_dir).get("enabled") is True

    def save_inventory(self, config_dir: Path, items: Mapping[str, int]) -> None:
        self._module.save_inventory(str(config_dir), dict(items))

    def set_runtime(self, config_dir: Path, status: str, message: str) -> None:
        try:
            self._module.set_runtime(str(config_dir), status, message)
        except Exception:  # 运行状态写入失败不影响任务结果
            pass

    def set_mastery_runtime(self, config_dir: Path, status: str, message: str) -> None:
        setter = getattr(self._module, "set_mastery_runtime", None)
        if not callable(setter):
            return
        try:
            setter(str(config_dir), status, message)
        except Exception:
            pass

    def record_mastery_results(
        self,
        config_dir: Path,
        catalog_path: Path,
        details_list: Sequence[Mapping[str, Any]],
        *,
        summary: Mapping[str, Any] | None = None,
    ) -> bool:
        recorder = getattr(self._module, "record_mastery_results", None)
        if not callable(recorder):
            return False
        recorder(
            str(config_dir),
            str(catalog_path),
            [dict(item) for item in details_list],
            summary=dict(summary) if isinstance(summary, Mapping) else None,
        )
        return True

    def pending_notifications(
        self,
        config_dir: Path,
        catalog_path: Path,
        *,
        include_goals: bool = True,
        include_uncompleted_goals: bool = True,
    ) -> list[dict[str, str]]:
        try:
            pending = self._module.pending_notifications(
                str(config_dir),
                str(catalog_path),
                include_goals=include_goals,
                include_uncompleted_goals=include_uncompleted_goals,
            )
        except TypeError:
            pending = self._module.pending_notifications(str(config_dir), str(catalog_path))
        if not isinstance(pending, list):
            return []
        result: list[dict[str, str]] = []
        for entry in pending:
            if isinstance(entry, Mapping) and entry.get("key") and entry.get("text"):
                result.append({"key": str(entry["key"]), "text": str(entry["text"])})
        return result

    def mark_notified(self, config_dir: Path, key: str) -> None:
        self._module.mark_notified(str(config_dir), str(key))


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def posix_popen_kwargs() -> dict[str, Any]:
    return {"start_new_session": True} if os.name == "posix" else {}


class MaaCliRunner:
    """写临时任务文件并执行 maa run <name> --batch，实时回显原始输出。"""

    def __init__(
        self,
        *,
        config_dir: Path,
        maa_bin: str = "maa",
        env_extra: Mapping[str, str] | None = None,
        process_holder: dict[str, int] | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.maa_bin = maa_bin
        self.env_extra = dict(env_extra or {})
        self.process_holder = process_holder if process_holder is not None else {}
        self._sequence = 0

    def run(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        name_prefix: str,
        document: Mapping[str, Any] | None = None,
        env_extra: Mapping[str, str] | None = None,
        ) -> RunResult:
        self._sequence += 1
        name = f"{name_prefix}-{os.getpid()}-{self._sequence}"
        doc: dict[str, Any] = dict(document or {})
        doc["tasks"] = [dict(task) for task in tasks]
        path = self.config_dir / "tasks" / f"{name}.toml"
        write_text_atomic(path, dump_document(doc))
        started_ns = time.time_ns()
        try:
            result = self.invoke(["run", name, "--batch"], env_extra=env_extra)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return RunResult(result.exit_code, result.output, name, started_ns)

    def invoke(self, args: Sequence[str], *, env_extra: Mapping[str, str] | None = None) -> RunResult:
        env = dict(os.environ)
        env.setdefault("MAA_LOG", "info")
        env.update(self.env_extra)
        if env_extra:
            env.update(env_extra)
        # 普通 cron 会 export MAA_LOG=warn；这里必须强制到 info 以上，否则读不到 Depot 回调。
        env["MAA_LOG"] = effective_maa_log(env.get("MAA_LOG"))
        command = [shutil.which(self.maa_bin) or self.maa_bin, *args]
        if env.get("MAA_RUN_SESSION") and args[:1] == ["run"]:
            command = [
                sys.executable, str(Path(__file__).with_name("maa_run_session.py")), "exec",
                "--session", env["MAA_RUN_SESSION"], "--config-dir", str(self.config_dir),
                "--maa", command[0], "--", *args,
            ]
        buffer: list[str] = []
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **posix_popen_kwargs(),
        )
        self.process_holder["pid"] = process.pid
        try:
            assert process.stdout is not None
            for line in process.stdout:
                buffer.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
        finally:
            self.process_holder.pop("pid", None)
            exit_code = process.wait()
        print(f"[CULTIVATION_PHASE] child_exit={exit_code} settlements={settlement_count(''.join(buffer))}", flush=True)
        return RunResult(exit_code, "".join(buffer))


class RunnerScript:
    """调用既有 maa-run.sh（保持任务锁与设备预检），用于 scan / run 入口。"""

    def __init__(
        self,
        script: Path = RUNNER_SCRIPT,
        process_holder: dict[str, int] | None = None,
    ) -> None:
        self.script = Path(script)
        self.process_holder = process_holder if process_holder is not None else {}

    def run(self, task_name: str, extra_args: Sequence[str] = ()) -> RunResult:
        command = ["/bin/bash", str(self.script), task_name, *extra_args]
        buffer: list[str] = []
        started_ns = time.time_ns()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **posix_popen_kwargs(),
        )
        self.process_holder["pid"] = process.pid
        try:
            assert process.stdout is not None
            for line in process.stdout:
                buffer.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
        finally:
            self.process_holder.pop("pid", None)
            exit_code = process.wait()
        return RunResult(exit_code, "".join(buffer), task_name, started_ns)


def telegram_sender(
    text: str,
    *,
    config_dir: Path,
    script_dir: Path = SCRIPT_DIR,
    retries: int = 3,
    retry_delay_s: float = 3.0,
) -> bool:
    """按既有 tg.txt 双行格式发送通知（支持最多 3 次重试）；任何异常都不外泄（可能含 token/URL）。"""

    import time
    import urllib.parse
    import urllib.request

    candidate = Path(script_dir) / "tg.txt"
    if not candidate.is_file():
        candidate = Path(config_dir) / "tg.txt"
    if not candidate.is_file():
        return False
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if len(lines) < 2:
        return False
    token = lines[0].strip()
    chat_id = lines[1].strip()
    if not token or not chat_id:
        return False
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    total_attempts = max(1, int(retries))
    for attempt in range(total_attempts):
        request = urllib.request.Request(url, data=payload)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if 200 <= int(response.status) < 300:
                    return True
        except Exception:
            pass
        if attempt + 1 < total_attempts and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    return False


SANITY_RE = re.compile(r"Current sanity:\s*(\d+)\s*/\s*(\d+)")


def last_sanity(text: str) -> tuple[int, int] | None:
    """读取 maa-cli SanityBeforeStage 输出的最后一次 当前/上限 理智。"""

    matches = SANITY_RE.findall(strip_ansi(text or ""))
    if not matches:
        return None
    current, total = matches[-1]
    return int(current), int(total)


def complete_depot(result: "RunResult") -> dict[str, int] | None:
    """完整 Depot 快照 = 子进程 exit=0 且回调 done=true 且 data 非空。"""

    if result.exit_code != 0:
        return None
    return final_depot_items(result.output)


# ---------------------------------------------------------------------------
# 编排主体
# ---------------------------------------------------------------------------


class CultivationRuntime:
    def __init__(
        self,
        *,
        config_dir: Path,
        catalog_path: Path | None = None,
        service_module: Any | None = None,
        maa_runner: Any | None = None,
        runner_script: Any | None = None,
        sender: Callable[[str], bool] | None = None,
        now: Callable[[], datetime] | None = None,
        log: Callable[[str], None] | None = None,
        limits: Limits | None = None,
        maa_bin: str = "maa",
        env_extra: Mapping[str, str] | None = None,
        asst_log_path: Path | None = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        # 真实索引与 WebUI/service 一样位于脚本目录（部署清单 /root/maa-scripts/maa-cultivation-data.json）；
        # 默认绝不能落到 config_dir，否则实机会读不到索引。
        self.catalog_path = (
            Path(catalog_path) if catalog_path else (SCRIPT_DIR / CATALOG_FILENAME)
        )
        self.limits = limits or Limits()
        self._log = log or self.default_log
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._process_holder: dict[str, int] = {}
        self._service_module = service_module
        self._service = ServiceAdapter(self.require_service())
        self._event_seq = 0
        self._instance_id = uuid.uuid4().hex[:12]
        self.asst_log_path = Path(
            asst_log_path
            or os.environ.get("MAA_ASST_LOG")
            or DEFAULT_ASST_LOG
        )
        self._maa_runner = maa_runner
        if self._maa_runner is None:
            self._maa_runner = MaaCliRunner(
                config_dir=self.config_dir,
                maa_bin=maa_bin,
                env_extra=env_extra,
                process_holder=self._process_holder,
            )
        self._runner_script = runner_script
        if self._runner_script is None:
            self._runner_script = RunnerScript(process_holder=self._process_holder)
        self._sender = sender or (lambda text: telegram_sender(text, config_dir=self.config_dir))

    # -- 基础设施 ---------------------------------------------------------

    @staticmethod
    def default_log(message: str) -> None:
        sys.stdout.write(f"[CULTIVATION] {message}\n")
        sys.stdout.flush()

    def require_service(self) -> Any:
        if self._service_module is None:
            self._service_module = load_sibling("maa_cultivation_service")
        if not hasattr(self._service_module, "load_state"):
            raise CultivationError("maa_cultivation_service 缺少 load_state")
        return self._service_module

    def install_signal_forwarding(self) -> None:
        if os.name != "posix":
            return
        holder = self._process_holder

        def handler(signum, frame):  # pragma: no cover - 依赖真实信号
            pid = holder.get("pid")
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    pass
            raise SystemExit(EXIT_TIMEOUT)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    # -- 状态查询 ---------------------------------------------------------

    def enabled(self) -> bool:
        return self._service.enabled(self.config_dir)

    def _payload(self) -> dict[str, Any]:
        """plan / catalog / currency / settings 的唯一来源（runtime 不再自己算 plan）。"""

        return self._service.get_payload(self.config_dir, self.catalog_path)

    def _catalog(self) -> dict[str, Any]:
        """只取 catalog（余额判定用），避免为了判断任务类型重算整份 plan。"""

        loader = getattr(self._service_module, "load_catalog", None)
        if callable(loader):
            try:
                catalog = loader(str(self.catalog_path))
            except Exception as error:
                raise CultivationError(f"培养索引不可读: {type(error).__name__}") from error
            if isinstance(catalog, Mapping):
                return dict(catalog)
        catalog = self._payload().get("catalog")
        return dict(catalog) if isinstance(catalog, Mapping) else {}

    @staticmethod
    def _currency(payload: Mapping[str, Any]) -> dict[str, Any]:
        currency = payload.get("currency")
        return dict(currency) if isinstance(currency, Mapping) else {}

    def _next_event_base(self, run_name: str) -> str:
        self._event_seq += 1
        # 事件 base 带本进程实例 uuid：PID 复用后 run 名可能重复，新掉落不能当重复事件。
        prefix = run_name or f"cultivation-{os.getpid()}-{self._event_seq}"
        return f"{prefix}#{self._instance_id}"

    @staticmethod
    def _gold_item(catalog: Mapping[str, Any]) -> tuple[str, str]:
        gold_id = str((catalog.get("currency") or {}).get("gold_item_id") or "")
        if not gold_id:
            return "", ""
        name = str(((catalog.get("items") or {}).get(gold_id) or {}).get("name") or "")
        return gold_id, name

    def _record_lmd_from_output(
        self,
        output: str,
        *,
        run_name: str,
        catalog: Mapping[str, Any],
        confirmed_at: Any,
    ) -> tuple[int, ...]:
        """解析本阶段所有逐战 Drops 行并记账（prefix/批次/tail 共用同一实现）。

        返回本次观察到的龙门币掉落；epoch 由调用方在“失效处理之后、运行之前”读取，
        避免中途人工重确认导致重复累加。
        """

        _gold_id, gold_name = self._gold_item(catalog)
        if not gold_name:
            return ()
        amounts = tuple(lmd_drops_from_output(output, gold_name))
        if amounts:
            self._record_lmd_drops(amounts, run_name=run_name, confirmed_at=confirmed_at)
        return amounts

    def _record_lmd_drops(
        self, amounts: Sequence[int], *, run_name: str, confirmed_at: Any
    ) -> None:
        """按唯一事件 ID 记账逐战观察到的龙门币掉落；事件幂等，绝不猜收益。"""

        recorder = getattr(self._service_module, "record_lmd_drops", None)
        if not callable(recorder):
            self._log("WARN service 未提供 record_lmd_drops，本轮龙门币掉落未记账")
            return
        base = self._next_event_base(run_name)
        for index, amount in enumerate(amounts):
            event_id = f"{base}#lmd-{index}"
            try:
                counted = recorder(
                    str(self.config_dir), event_id, int(amount), confirmed_at=confirmed_at
                )
            except Exception as error:
                self._log(f"WARN 龙门币掉落记账失败: {type(error).__name__}")
                continue
            if counted:
                self._log(f"龙门币掉落已记账: +{amount}（{event_id}）")
            else:
                self._log(f"WARN 龙门币掉落未记账（余额待确认或事件重复）: {event_id}")

    def _invalidate_balance(self, reason: str) -> None:
        """标记余额待重新确认；接管路径 strict=True：写失败必须停目标，不能假 ready。"""

        invalidation = getattr(self._service_module, "invalidate_balance", None)
        if not callable(invalidation):
            message = "service 未提供 invalidate_balance，无法标记龙门币待确认"
            raise CultivationError(message)
        try:
            invalidation(str(self.config_dir), str(reason)[:200])
        except Exception as error:
            raise CultivationError(
                f"invalidate_balance 失败，无法确认余额状态: {type(error).__name__}"
            ) from error
        self._log(f"龙门币余额已标记为待重新确认: {reason}")

    def _invalidate_balance_best_effort(self, reason: str) -> None:
        """未接管路径：状态写失败只能记录，绝不能挡住正常 MAA 运行。"""

        try:
            self._invalidate_balance(reason)
        except Exception as error:
            self._log(
                f"WARN 龙门币余额失效失败（本轮仍按原任务执行，状态可能不准确）: {type(error).__name__}"
            )

    def _invalidate_after_lmd_spending(
        self,
        tasks: Sequence[Mapping[str, Any]],
        phase: str,
        catalog: Mapping[str, Any] | None = None,
    ) -> None:
        """phase 即将执行前调用：出现可能扣龙门币的任务就把余额标记为待重新确认。

        判定见 lmd_risk_types：白名单类型不扣；Recruit 看 recruitment_free；Infrast 只看
        replenish；其余未知类型保守失效。startup/plain/tail 各 phase 独立判定。
        """

        risky = lmd_risk_types(catalog or {}, tasks)
        if risky:
            self._invalidate_balance(phase + ':lmd_spending:' + ','.join(risky))

    def _apply_infrast_phase_risk(
        self, output: str, tasks: Sequence[Mapping[str, Any]], *, failed: bool
    ) -> None:
        """Infrast 阶段结束后按实测产物判断余额：只有源石碎片产线真扣龙门币。"""

        reason = infrast_phase_risk(output, tasks, failed=failed)
        if reason:
            self._invalidate_balance(reason)

    def invalidate_for_task(self, task_file: Path) -> int:
        """未接管路径（普通/定时/单任务）在运行前调用：只有培养 state 存在才操作。

        永远返回成功：余额失效只是保守标记，不能阻断 MAA 运行。
        """

        state_file = self.config_dir / "cultivation" / "state.json"
        if not state_file.is_file():
            return EXIT_OK
        try:
            document = read_task_document(task_file)
        except Exception as error:
            self._log(f"WARN 无法读取任务配置，跳过余额失效检查: {type(error).__name__}")
            return EXIT_OK
        tasks = document.get("tasks")
        if isinstance(tasks, list):
            risky = lmd_conservative_risk_types(self._catalog(), tasks)
            if risky:
                self._invalidate_balance_best_effort('task_list:lmd_spending:' + ','.join(risky))
        return EXIT_OK

    def check(self) -> int:
        try:
            return EXIT_OK if self.enabled() else EXIT_DISABLED
        except Exception as error:
            self._log(f"培养状态不可读，按关闭处理: {type(error).__name__}")
            return EXIT_DISABLED

    # -- execute ----------------------------------------------------------

    def execute(self, task_file: Path) -> int:
        try:
            document = read_task_document(task_file)
        except Exception as error:
            self._log(f"无法读取任务配置: {type(error).__name__}")
            return EXIT_USAGE
        tasks = document.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            self._log("effective 任务列表为空，拒绝接管")
            return EXIT_USAGE
        try:
            payload = self._payload()
            settings = payload.get("settings")
            if not isinstance(settings, Mapping):
                raise CultivationError("service.get_payload 缺少 settings")
            settings = dict(settings)
        except Exception as error:
            self._log(f"培养状态不可读，本轮不接管: {type(error).__name__}")
            return EXIT_FAILED
        self._service.set_runtime(self.config_dir, "running", "培养运行已接管本轮日常")
        try:
            return self._execute_tasks(document, tasks, settings)
        except StartupFailure as error:
            self._log(f"StartUp 失败，立即停止且不重跑: {error}")
            self._service.set_runtime(self.config_dir, "failed", "startup_failed")
            return EXIT_STARTUP_FAILED
        except Exception as error:
            detail = str(error).strip().replace("\n", " ")[:200]
            self._log(f"培养编排异常中止，不触发备用关卡: {type(error).__name__}: {detail}")
            self._service.set_runtime(self.config_dir, "failed", f"error:{type(error).__name__}")
            return EXIT_FAILED

    def _execute_tasks(
        self,
        document: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
        settings: Mapping[str, Any],
    ) -> int:
        index = sanity_fight_index(tasks)
        catalog = self._payload().get("catalog") or {}
        if index is None:
            self._log("未发现需要理智的 Fight，本轮按原始任务原样执行")
            # 接管标志会让 runner 跳过全局失效检查，这里必须自己按任务列表失效余额。
            self._invalidate_after_lmd_spending(tasks, "plain", catalog)
            confirmed_at = self._currency(self._payload()).get("confirmed_at")
            result = self._maa_runner.run(tasks, name_prefix="cultivation-plain", document=document)
            self._record_lmd_from_output(
                result.output,
                run_name=result.name,
                catalog=catalog,
                confirmed_at=confirmed_at,
            )
            self._apply_infrast_phase_risk(
                result.output, tasks, failed=result.exit_code != 0
            )
            return self._finish(result.exit_code, "未接管理智作战")

        head = [dict(task) for task in tasks[:index]]
        tail = [dict(task) for task in tasks[index + 1:]]
        if not any(task.get("type") == "StartUp" for task in head):
            # 不替用户发明启动、也不整轮失败：按原始基线原样执行并明确提示未接管。
            self._log("WARN 前置段没有 StartUp，本轮不接管培养，按原始任务基线执行")
            baseline = [dict(task) for task in tasks]
            self._invalidate_after_lmd_spending(baseline, "baseline", catalog)
            baseline_epoch = self._currency(self._payload()).get("confirmed_at")
            result = self._maa_runner.run(
                baseline, name_prefix="cultivation-baseline", document=document
            )
            if output_reports_startup_failure(result.output):
                raise StartupFailure("基线执行检测到 StartUp 失败")
            self._record_lmd_from_output(
                result.output,
                run_name=result.name,
                catalog=catalog,
                confirmed_at=baseline_epoch,
            )
            self._apply_infrast_phase_risk(
                result.output, baseline, failed=result.exit_code != 0
            )
            return self._finish(result.exit_code, "未接管：前置段缺少 StartUp")

        phase1 = head + [dict(DEPOT_TASK), dict(OPERBOX_TASK)]
        self._invalidate_after_lmd_spending(phase1, "phase1", catalog)
        phase1_epoch = self._currency(self._payload()).get("confirmed_at")
        result = self._maa_runner.run(phase1, name_prefix="cultivation-scan", document=document)
        if output_reports_startup_failure(result.output):
            raise StartupFailure("前置段检测到 StartUp 失败")
        self._record_lmd_from_output(
            result.output,
            run_name=result.name,
            catalog=catalog,
            confirmed_at=phase1_epoch,
        )
        self._apply_infrast_phase_risk(phase1, result.output, failed=result.exit_code != 0)
        if result.exit_code != 0:
            return self._finish(result.exit_code, "前置日常失败")
        inventory = complete_depot(result)
        if inventory is None:
            self._log("WARN 第一段未取得完整 Depot 快照（需 exit=0 且 data 非空），本轮不刷培养材料")
            self._mark_inventory_failed("前置段 Depot 扫描不完整，保留上一次库存")
            self._persist_operbox(
                result.output,
                started_ns=result.started_ns,
                mark_stale_on_failure=True,
            )
            return self._run_tail(document, tail, fallback=None, force_failure=True)
        self._service.save_inventory(self.config_dir, inventory)
        self._log(f"库存快照已更新: {len(inventory)} 种物品")

        # 开始刷取前必须同时拿到 Depot 与 OperBox：仅凭旧 goal 练度可能已被手动升级改过。
        if not self._persist_operbox(
            result.output, started_ns=result.started_ns, mark_stale_on_failure=True
        ):
            self._log("WARN 前置段 OperBox 快照不完整，本轮不刷取培养材料")
            self._service.set_runtime(
                self.config_dir, "idle", "前置 OperBox 不完整：本轮不刷取培养材料"
            )
            return self._run_tail(document, tail, fallback=None, force_failure=True)
        try:
            self._reconcile_snapshot(strict=True, fresh_inventory=inventory)
        except CultivationError as error:
            self._log(f"WARN {error}，本轮不刷取培养材料")
            return self._run_tail(document, tail, fallback=None, force_failure=True)
        # 复核可能把目标标记为已完成：先发本轮新达标/齐料提醒，再进入刷取或兜底。
        self._deliver_notifications()

        farming = self._farm(document, inventory)
        fallback = self._fallback_task(settings, farming)
        self._log(f"培养刷取结束: {farming['reason']}（批次={farming['batches']} 作战额度={farming['battles']}，不含备用本；实际结算见各阶段记录）")
        return self._run_tail(
            document,
            tail,
            fallback=fallback,
            force_failure=bool(farming.get("degraded")),
        )

    def _farm(self, document: Mapping[str, Any], inventory: dict[str, int]) -> dict[str, Any]:
        payload = self._payload()
        catalog = payload.get("catalog") or {}
        plan = payload.get("plan") or {}
        blocked: list[str] = []
        batches = 0
        battles = 0
        no_gain_streak = 0
        consecutive_failures = 0
        disabled = False
        degraded = False
        reason = "goals_ready"

        while True:
            # 每批之前重新取 payload：设置、目标、余额变化都按 service 的最新结果处理。
            payload = self._payload()
            catalog = payload.get("catalog") or {}
            plan = payload.get("plan") or {}
            currency = self._currency(payload)
            if (payload.get("settings") or {}).get("enabled") is not True:
                disabled = True
                reason = "disabled_mid_run"
                break
            if plan.get("ready") is True:
                reason = "goals_ready"
                # 齐料/新达标提醒必须在兜底开始前发出（服务端按 key 幂等去重）。
                self._deliver_notifications()
                break
            if batches >= self.limits.max_batches:
                reason = "batch_cap"
                break
            if battles + self.limits.batch_times > self.limits.max_total_battles:
                reason = "battle_cap"
                break
            target = pick_farm_target(plan, catalog, game_weekday_key(self._now()), blocked)
            if target is None:
                reason = "no_farmable_gap"
                self._deliver_notifications()
                break
            item_id, stage = target
            gap = missing_items(plan).get(item_id) or 0
            gold_id, _gold_name = self._gold_item(catalog)
            is_money = bool(gold_id) and item_id == gold_id
            confirmed_at = currency.get("confirmed_at")
            self._log(f"培养批次 {batches + 1}: {stage}（缺 {item_id} × {gap}）")
            outcome = self._run_batch(
                document,
                stage,
                item_id=item_id,
                gap=gap,
                catalog=catalog,
                confirmed_at=confirmed_at,
            )
            batches += 1
            battles += self.limits.batch_times
            if outcome.startup_failed:
                raise StartupFailure(f"培养批次检测到 StartUp 失败（{stage}）")
            if outcome.device_failure or (outcome.exit_code != 0 and not outcome.fight_error):
                # 设备 / 连接级失败不得当成普通关卡失败继续重试其他本。
                raise StartupFailure(f"培养批次疑似设备/连接失败 exit={outcome.exit_code}（{stage}）")
            if outcome.fight_error:
                blocked.append(stage)
                consecutive_failures += 1
                self._log(f"WARN 关卡 {stage} 战斗失败（exit={outcome.exit_code}），本轮熔断该关卡")
                if consecutive_failures >= self.limits.max_consecutive_failures:
                    reason = "stage_failures"
                    break
                continue
            if outcome.items is None:
                # exit=0 但 Depot 不完整：旧库存不可信，停止培养刷取且不使用备用关卡。
                degraded = True
                reason = "depot_incomplete"
                self._log(f"WARN 关卡 {stage} 未取得完整 Depot 快照，本轮停止培养刷取")
                break
            if is_money and gold_id not in outcome.items:
                # 完整 Depot 未识别龙门币时不能拿掉落日志代替当前余额。
                self._service.save_inventory(self.config_dir, outcome.items)
                self._invalidate_balance("ce_without_depot_balance:" + stage)
                reason = "lmd_unconfirmed"
                break
            consecutive_failures = 0
            # 增益按目标物品真实计量单位判断：龙门币比较前后真实仓库余额，
            # EXP 看作战记录换算出的总经验，其他材料看仓库数量。
            before_items = dict(inventory)
            before_exp = experience_total(catalog, before_items)
            before_balance = currency.get("balance")
            inventory.clear()
            inventory.update(outcome.items)
            self._service.save_inventory(self.config_dir, inventory)
            after_payload = self._payload()
            plan = after_payload.get("plan") or {}
            if is_money:
                after_currency = self._currency(after_payload)
                depot_confirmed = (
                    after_currency.get("available") is True
                    and after_currency.get("balance") == outcome.items.get(gold_id)
                )
                if not depot_confirmed:
                    self._log("WARN 龙门币余额与本轮仓库不一致，本轮余额增益无法确认")
                gained = depot_confirmed and int(after_currency.get("balance") or 0) > int(
                    before_balance or 0
                )
            elif item_id == EXP_ITEM_ID:
                after_exp = experience_total(catalog, outcome.items)
                gained = after_exp > before_exp
                self._log(f"目标经验换算: {before_exp} -> {after_exp}")
            else:
                gained = outcome.items.get(item_id, 0) > before_items.get(item_id, 0)
                if item_id not in outcome.items:
                    self._log(f"WARN 目标材料 {item_id} 不在仓库快照中，按缺料处理")
            if gained:
                no_gain_streak = 0
                self._log(f"关卡 {stage} 完成，目标材料 {item_id} 已增加，按真实 Depot 重算")
            else:
                no_gain_streak += 1
                self._log(f"关卡 {stage} 未让目标材料 {item_id} 增加（连续 {no_gain_streak} 次）")
            if self._sanity_exhausted(outcome, catalog, stage):
                reason = "sanity_exhausted"
                break
            if no_gain_streak >= self.limits.max_no_gain_streak:
                reason = "no_inventory_gain"
                break

        return {
            "reason": reason,
            "batches": batches,
            "battles": battles,
            "plan": plan,
            "blocked": blocked,
            "disabled": disabled,
            "degraded": degraded,
        }

    def _sanity_exhausted(
        self, outcome: BatchOutcome, catalog: Mapping[str, Any], stage: str
    ) -> bool:
        if outcome.sanity is None:
            return False
        current, _total = outcome.sanity
        entry = catalog_stage_index(catalog).get(stage) or {}
        ap_cost = entry.get("ap_cost")
        if isinstance(ap_cost, int) and ap_cost > 0:
            return current < ap_cost
        return current <= 0

    def _run_batch(
        self,
        document: Mapping[str, Any],
        stage: str,
        *,
        item_id: str = "",
        gap: int = 0,
        catalog: Mapping[str, Any] | None = None,
        confirmed_at: Any = None,
    ) -> BatchOutcome:
        drops = {item_id: int(gap)} if item_id and gap > 0 else None
        task = fight_task(
            stage, times=self.limits.batch_times, series=self.limits.series, drops=drops
        )
        result = self._maa_runner.run(
            [task, dict(DEPOT_TASK)], name_prefix="cultivation-batch", document=document
        )
        self._log(f"培养批次结算记录={settlement_count(result.output)} stage={stage} exit={result.exit_code}")
        lmd_amounts = self._record_lmd_from_output(
            result.output,
            run_name=result.name,
            catalog=catalog or {},
            confirmed_at=confirmed_at,
        )
        return BatchOutcome(
            stage=stage,
            exit_code=result.exit_code,
            items=complete_depot(result),
            startup_failed=output_reports_startup_failure(result.output),
            fight_error=output_reports_fight_error(result.output),
            device_failure=output_reports_device_failure(result.output),
            sanity=last_sanity(result.output),
            lmd_amounts=lmd_amounts,
        )

    def _fallback_task(
        self, settings: Mapping[str, Any], farming: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """只在目标已齐料、无开放可刷缺口或关卡全部熔断时使用 settings.fallback_stage。"""

        if farming.get("disabled"):
            self._log("培养已中途关闭，不再使用备用关卡")
            return None
        reason = farming.get("reason")
        if reason not in ("goals_ready", "no_farmable_gap", "stage_failures"):
            return None
        stage = settings.get("fallback_stage")
        if not isinstance(stage, str) or not stage.strip():
            return None
        stage = stage.strip()
        payload = self._payload()
        catalog = payload.get("catalog") or {}
        # 与培养批次共用同一套判定：索引内、可导航、游戏日开放、本轮未熔断。
        if usable_stage(
            catalog, stage, game_weekday_key(self._now()), farming.get("blocked") or []
        ) is None:
            self._log(f"WARN 备用关卡 {stage} 今日不可用或本轮已熔断，跳过")
            return None
        # 用户显式选的兜底关卡照刷（含龙门币本）：未知余额只影响 service 的缺口计算，
        # 绝不当成 0 参与缺口，也不因此禁止用户的兜底意图；复核失败才禁止兜底。
        remaining = self.limits.max_total_battles - int(farming.get("battles") or 0)
        if remaining <= 0:
            return None
        self._log(f"使用备用关卡 {stage}（上限 {remaining} 次，无药无石）")
        return fight_task(stage, times=remaining, series=self.limits.series, name="备用作战")

    def _oper_progress_task(self, *, require_enabled: bool = True) -> dict[str, Any] | None:
        try:
            state = self._service.load_state(self.config_dir)
        except Exception:
            return None
        return oper_progress_task_from_state(state, require_enabled=require_enabled)

    def _record_oper_progress(self, output: str, *, started_ns: int = 0) -> bool:
        try:
            core_log = self._read_fresh_core_log(started_ns)
            details_list, summary = extract_oper_progress_callbacks(output, core_log)
            if not details_list and summary is None:
                return False
            recorded = self._service.record_mastery_results(
                self.config_dir,
                self.catalog_path,
                details_list,
                summary=summary,
            )
            if recorded:
                self._log(
                    f"技能专精回调已更新: 明细={len(details_list)} 条"
                    + (f" 汇总={summary}" if summary else "")
                )
            return recorded
        except Exception as error:
            self._log(f"WARN 技能专精回调记录失败: {type(error).__name__}")
            return False

    def _run_tail(
        self,
        document: Mapping[str, Any],
        tail: Sequence[Mapping[str, Any]],
        *,
        fallback: Mapping[str, Any] | None,
        force_failure: bool = False,
    ) -> int:
        phase: list[dict[str, Any]] = []
        if fallback is not None:
            phase.append(dict(fallback))
        phase.extend(dict(task) for task in tail)
        oper_task = self._oper_progress_task(require_enabled=True)
        # 收尾双扫描必须插在首个 CloseDown 之前：CloseDown 之后游戏已关闭，扫描必然失败。
        closing_tasks = (
            [oper_task, dict(DEPOT_TASK), dict(OPERBOX_TASK)]
            if oper_task is not None
            else [dict(DEPOT_TASK), dict(OPERBOX_TASK)]
        )
        close_index = next(
            (
                index
                for index, task in enumerate(phase)
                if str(task.get("type") or "") == "CloseDown"
            ),
            None,
        )
        if close_index is None:
            phase.extend(closing_tasks)
        else:
            # 其它任务相对次序保持原样，只把双扫描提前到 CloseDown 前。
            phase[close_index:close_index] = closing_tasks
        tail_catalog = self._payload().get("catalog") or {}
        self._invalidate_after_lmd_spending(phase, "tail", tail_catalog)
        tail_epoch = self._currency(self._payload()).get("confirmed_at")
        result = self._maa_runner.run(phase, name_prefix="cultivation-tail", document=document)
        self._log(f"收尾作战结算记录={settlement_count(result.output)}（含备用本） exit={result.exit_code}")
        if output_reports_startup_failure(result.output):
            raise StartupFailure("后续日常检测到 StartUp 失败")
        if oper_task is not None:
            self._record_oper_progress(result.output, started_ns=result.started_ns)
        self._record_lmd_from_output(
            result.output,
            run_name=result.name,
            catalog=tail_catalog,
            confirmed_at=tail_epoch,
        )
        self._apply_infrast_phase_risk(phase, result.output, failed=result.exit_code != 0)
        items = complete_depot(result)
        if items is not None:
            self._service.save_inventory(self.config_dir, items)
        else:
            self._log("WARN 最终 Depot 扫描不完整，本轮不发送通知且不覆盖库存")
            self._mark_inventory_failed("tail Depot 扫描不完整，保留上一次库存")
            self._mark_operator_stale("tail_depot_incomplete", result.output)
        trusted = False
        if self._persist_operbox(
            result.output, started_ns=result.started_ns, mark_stale_on_failure=True
        ):
            # 接管路径的收尾同样复核练度，保证前端看到最新缺口。
            trusted = self._reconcile_snapshot(strict=False, fresh_inventory=items)
        # 复核不可信（未拥有/并发修改）或本轮已记失败时，绝不按旧 goal 发齐料通知。
        allow_goals = bool(items is not None and trusted and not force_failure)
        if allow_goals or oper_task is not None:
            self._deliver_notifications(
                include_goals=allow_goals,
                include_uncompleted_goals=self.enabled(),
            )
        exit_code = result.exit_code
        if exit_code == 0 and force_failure:
            # 库存不可信 / 培养刷取未完成：即使后续日常跑通也必须明确失败，不得报 ALL DONE。
            exit_code = EXIT_FAILED
        message = "后续日常完成" if exit_code == 0 else "后续日常失败"
        if force_failure:
            message = "培养扫描未完成，本轮记失败"
        return self._finish(exit_code, message)

    def _mark_operator_stale(self, code: str, source_log: str = "") -> None:
        """扫描不完整时保留旧 snapshot，只写 failed state 让 UI 判定过期。"""

        try:
            module = load_sibling("maa_operator_box")
        except Exception as error:
            self._log(f"WARN 无法加载 maa_operator_box: {type(error).__name__}")
            return
        try:
            module.save_failed_attempt(
                self.config_dir / OPERATOR_STATE_NAME,
                code=code,
                message="收尾扫描未完成，保留上一次完整快照",
                source_log=source_log or "",
            )
        except Exception as error:
            self._log(f"WARN 干员快照过期标记写入失败: {type(error).__name__}")

    def _mark_inventory_failed(self, message: str) -> None:
        """库存扫描失败：保留 items/scanned_at，只把 available 置 false 并记 last_error。"""

        marker = getattr(self._service_module, "mark_inventory_failed", None)
        if not callable(marker):
            self._log("WARN service 未提供 mark_inventory_failed，仅记录日志")
            return
        try:
            marker(str(self.config_dir), str(message)[:200])
        except Exception as error:
            self._log(f"WARN 库存过期标记写入失败: {type(error).__name__}")

    def _read_fresh_core_log(self, started_ns: int) -> str:
        """只读本次执行开始之后更新过的 MaaCore 日志，避免把旧回调当成本轮结果。"""

        if started_ns <= 0:
            return ""
        try:
            stat = self.asst_log_path.stat()
        except OSError:
            return ""
        if stat.st_mtime_ns < started_ns:
            self._log("WARN MaaCore 日志早于本次执行，忽略旧回调")
            return ""
        try:
            text = self.asst_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        fresh = filter_core_log_since(text, started_ns)
        if not fresh:
            self._log("WARN MaaCore 日志里没有本次执行之后的回调记录")
        return fresh

    def _persist_operbox(
        self,
        output: str,
        *,
        started_ns: int = 0,
        mark_stale_on_failure: bool = False,
    ) -> bool:
        try:
            module = load_sibling("maa_operator_box")
        except Exception as error:
            self._log(f"WARN 无法加载 maa_operator_box: {type(error).__name__}")
            return False
        try:
            # maa-cli stdout 只给 done=false 时，用本轮更新过的 MaaCore 日志补最终回调。
            core_log = self._read_fresh_core_log(started_ns)
            callback, source = module.extract_final_callback_from_sources(output, core_log)
            source_log = output
            if source == "maacore_debug":
                # 与 maa-operator-scan.py 一致：把最终 core 回调追加进证据文本，
                # 让快照的 source_sha256 能追溯到真正的来源而不是只有 stdout。
                source_log = output + (
                    "\n--- MAA_CORE_FINAL_CALLBACK ---\n"
                    + json.dumps(
                        callback, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                )
            module.save_successful_scan(
                self.config_dir / OPERATOR_SNAPSHOT_NAME,
                self.config_dir / OPERATOR_STATE_NAME,
                callback,
                source_log=source_log,
                core_version="",
                resource_version="",
            )
            self._log(f"干员快照已更新（来源={source}）")
            return True
        except Exception as error:
            self._log(f"WARN 干员快照未更新: {type(error).__name__}")
            if mark_stale_on_failure:
                self._mark_operator_stale("operbox_callback_incomplete", output)
            return False

    def _reconcile_snapshot(self, *, strict: bool,
                            fresh_inventory: Mapping[str, int] | None = None) -> bool:
        """用新鲜 OperBox 快照校正目标练度（service.reconcile_operator_snapshot）。

        strict=True（开始刷取前）：接口缺失或校验收到的 ValueError 都直接停止本轮培养目标，
        绝不沿用旧 goal 练度继续刷取；strict=False（收尾/独立扫描刷新）只记录日志。

        返回 True 表示本轮练度复核可信（调用方以此作为发送“齐料/达标”通知的前提）；
        返回 False（未拥有、并发修改、接口缺失等）时必须跳过旧 goal 的通知。
        """

        reconcile = getattr(self._service_module, "reconcile_operator_snapshot", None)
        if not callable(reconcile):
            if strict:
                raise CultivationError(
                    "service 未提供 reconcile_operator_snapshot，无法复核目标练度"
                )
            self._log("WARN service 未提供 reconcile_operator_snapshot，跳过练度复核")
            return False
        try:
            outcome = reconcile(str(self.config_dir), str(self.catalog_path))
            changed = outcome.get("changed") if isinstance(outcome, Mapping) else None
            count = len(changed) if isinstance(changed, list) else 0
            if count:
                self._log(f"已按最新干员快照校正 {count} 个目标练度（达标自动标记完成）")
                # These consecutive Depot/OperBox callbacks belong to this same
                # successful scan. Reconciliation invalidates historical money;
                # restore only this scan's warehouse, never a stored old snapshot.
                if fresh_inventory is not None:
                    self._service.save_inventory(self.config_dir, fresh_inventory)
            else:
                self._log("目标练度与最新干员快照一致，无需校正")
            return True
        except ValueError as error:
            if strict:
                raise CultivationError(f"目标练度校正失败: {error}") from error
            self._log(f"WARN 目标练度校正失败（保留旧数据）: {error}")
            return False
        except Exception as error:
            if strict:
                raise CultivationError(
                    f"目标练度校正异常: {type(error).__name__}"
                ) from error
            self._log(f"WARN 目标练度校正异常: {type(error).__name__}")
            return False

    def _deliver_notifications(
        self,
        *,
        include_goals: bool = True,
        include_uncompleted_goals: bool = True,
    ) -> None:
        try:
            try:
                pending = self._service.pending_notifications(
                    self.config_dir,
                    self.catalog_path,
                    include_goals=include_goals,
                    include_uncompleted_goals=include_uncompleted_goals,
                )
            except TypeError:
                pending = self._service.pending_notifications(self.config_dir, self.catalog_path)
        except Exception as error:
            self._log(f"WARN 待通知读取失败: {type(error).__name__}")
            return
        for entry in pending:
            try:
                delivered = bool(self._sender(entry["text"]))
            except Exception:
                delivered = False
            if not delivered:
                self._log("WARN 通知发送失败，保留待通知")
                continue
            try:
                self._service.mark_notified(self.config_dir, entry["key"])
            except Exception as error:
                self._log(f"WARN 通知标记失败: {type(error).__name__}")

    def _finish(self, exit_code: int, message: str) -> int:
        status = "idle" if exit_code == 0 else "failed"
        self._service.set_runtime(self.config_dir, status, message)
        self._log(f"runtime_exit={exit_code} status={status} {message}")
        return exit_code

    def mark_interrupted(self, reason: str) -> None:
        """被信号或 Ctrl-C 打断时落盘 interrupted，避免 runtime 永久停在 running。"""

        self._service.set_runtime(self.config_dir, "interrupted", f"运行被中断: {reason}")

    # -- scan / run 入口 --------------------------------------------------

    def scan(self) -> int:
        ensure_scan_task_file(self.config_dir)
        result = self._runner_script.run(SCAN_TASK_NAME, ["--debug"])
        if result.exit_code == LOCK_BUSY_EXIT:
            self._log("任务锁被其他 MAA 任务占用（exit=75），本次扫描未执行")
            return LOCK_BUSY_EXIT
        items = complete_depot(result)
        if items is None:
            self._log("扫描未取得完整 Depot 快照（需 exit=0 且 data 非空），库存未更新")
            self._mark_inventory_failed("独立扫描 Depot 不完整，保留上一次库存")
            return result.exit_code or EXIT_FAILED
        self._service.save_inventory(self.config_dir, items)
        if self._persist_operbox(
            result.output, started_ns=result.started_ns, mark_stale_on_failure=True
        ):
            # 独立扫描同样复核目标练度，供前端立即看到最新缺口。
            self._reconcile_snapshot(strict=False, fresh_inventory=items)
            self._log(f"库存扫描完成: {len(items)} 种物品")
            return EXIT_OK
        # 库存已保存，但干员扫描没完成：不能报“扫描成功”。
        self._log("WARN 库存已保存，但干员快照未更新，本次扫描按失败返回")
        return EXIT_FAILED

    def run_daily(self) -> int:
        if not self.enabled():
            self._log("培养未启用，拒绝运行")
            return EXIT_DISABLED
        result = self._runner_script.run("daily", ["--debug"])
        if result.exit_code == LOCK_BUSY_EXIT:
            self._log("任务锁被其他 MAA 任务占用（exit=75），本次未启动日常")
        return result.exit_code

    def run_mastery(self) -> int:
        oper_task = self._oper_progress_task(require_enabled=False)
        if oper_task is None:
            self._log("没有待执行的技能专精计划")
            self._service.set_mastery_runtime(
                self.config_dir, "idle", "没有待执行的技能专精计划"
            )
            return EXIT_DISABLED
        ensure_mastery_task_file(self.config_dir, oper_task)
        self._service.set_mastery_runtime(
            self.config_dir, "running", "正在执行技能专精与加工站合成"
        )
        result = self._runner_script.run(MASTERY_TASK_NAME, ["--debug"])
        if result.exit_code == LOCK_BUSY_EXIT:
            self._log("任务锁被其他 MAA 任务占用（exit=75），本次技能专精未执行")
            self._service.set_mastery_runtime(
                self.config_dir, "idle", "任务锁忙（其他 MAA 任务运行中），稍后重试"
            )
            return LOCK_BUSY_EXIT
        if output_reports_startup_failure(result.output):
            self._log("WARN 技能专精期间检测到 StartUp 失败")
            self._service.set_mastery_runtime(
                self.config_dir, "failed", "StartUp 启动失败，未进入专精流程"
            )
            return result.exit_code or EXIT_STARTUP_FAILED
        recorded = self._record_oper_progress(
            result.output, started_ns=result.started_ns
        )
        items = complete_depot(result)
        if items is not None:
            self._service.save_inventory(self.config_dir, items)
            self._log(f"专精后库存快照已更新: {len(items)} 种物品")
        if self._persist_operbox(
            result.output, started_ns=result.started_ns, mark_stale_on_failure=False
        ):
            self._reconcile_snapshot(strict=False, fresh_inventory=items)
        self._deliver_notifications(include_goals=False)
        if result.exit_code != 0:
            if not recorded:
                self._service.set_mastery_runtime(
                    self.config_dir,
                    "failed",
                    f"技能专精任务执行失败（exit={result.exit_code}）",
                )
            return result.exit_code
        if not recorded:
            self._service.set_mastery_runtime(
                self.config_dir, "idle", "技能专精任务已执行（未收到进度变更回调）"
            )
        return EXIT_OK

    # -- 每轮收尾扫描 ------------------------------------------------------

    def closing_scan(self, task_file: Path) -> int:
        """每轮正常游戏任务收尾一次：追加 Depot + OperBox 并持久化快照。

        只在一轮游戏任务成功后由 runner 调用（失败/断链/健康检查不会走到这里）。
        扫描失败保留旧快照并标记过期，绝不重跑整轮、也不改变本轮退出码。
        """

        try:
            document = read_task_document(task_file)
        except Exception as error:
            self._log(f"WARN 无法读取任务配置，跳过收尾扫描: {type(error).__name__}")
            return EXIT_OK
        tasks = document.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return EXIT_OK
        if Path(task_file).stem.startswith(MASTERY_TASK_NAME) or any(
            str(t.get("type") or "") == "OperProgress"
            for t in tasks
            if isinstance(t, Mapping)
        ):
            self._log("跳过收尾扫描：当前任务本身已包含技能专精与收尾扫描")
            return EXIT_OK
        oper_task = self._oper_progress_task(require_enabled=True)
        # 只扫一遍收尾（StartUp 负责把游戏带回主界面，游戏若仍在前台则直接续接）；
        # 绝不重跑本轮任务链。
        if oper_task is not None:
            phase = [
                dict(STARTUP_TASK),
                oper_task,
                dict(DEPOT_TASK),
                dict(OPERBOX_TASK),
            ]
            self._log("收尾扫描: StartUp + OperProgress + Depot + OperBox（随日常收尾自动推进专精）")
        else:
            phase = [dict(STARTUP_TASK), dict(DEPOT_TASK), dict(OPERBOX_TASK)]
            self._log("收尾扫描: StartUp + Depot + OperBox（每轮一次，不重跑整轮）")
        result = self._maa_runner.run(
            phase, name_prefix="cultivation-closing", document=document
        )
        if output_reports_startup_failure(result.output):
            self._log("WARN 收尾扫描期间检测到 StartUp 失败，保留旧快照")
            self._mark_inventory_failed("收尾扫描期间 StartUp 失败，保留上一次库存")
            self._mark_operator_stale("closing_startup_failed", result.output)
            return EXIT_OK
        if oper_task is not None:
            self._record_oper_progress(result.output, started_ns=result.started_ns)
        items = complete_depot(result)
        if items is not None:
            self._service.save_inventory(self.config_dir, items)
            self._log(f"收尾库存快照已更新: {len(items)} 种物品")
        else:
            self._log("WARN 收尾 Depot 扫描不完整，保留旧库存并标记过期")
            self._mark_inventory_failed("收尾 Depot 扫描不完整，保留上一次库存")
            self._mark_operator_stale("closing_depot_incomplete", result.output)
            self._service.set_runtime(
                self.config_dir, "idle", "收尾扫描不完整：库存/干员快照保留旧值并标记过期"
            )
        reconciled = False
        if self._persist_operbox(
            result.output, started_ns=result.started_ns, mark_stale_on_failure=True
        ):
            # 收尾后同步校正目标练度（手动升级过就顺势改缺口；达标记完成）。
            reconciled = self._reconcile_snapshot(strict=False, fresh_inventory=items)
        if reconciled or oper_task is not None:
            self._deliver_notifications(
                include_goals=reconciled,
                include_uncompleted_goals=self.enabled(),
            )
        return EXIT_OK


def ensure_scan_task_file(config_dir: Path) -> Path:
    """写入 StartUp + Depot + OperBox 扫描任务文件（内容一致则不重写）。"""

    path = Path(config_dir) / "tasks" / f"{SCAN_TASK_NAME}.toml"
    text = dump_document({"tasks": [dict(STARTUP_TASK), dict(DEPOT_TASK), dict(OPERBOX_TASK)]})
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    write_text_atomic(path, text)
    return path


def ensure_mastery_task_file(config_dir: Path, oper_task: Mapping[str, Any]) -> Path:
    """写入 StartUp + OperProgress + Depot + OperBox 专精任务文件。"""

    path = Path(config_dir) / "tasks" / f"{MASTERY_TASK_NAME}.toml"
    text = dump_document(
        {
            "tasks": [
                dict(STARTUP_TASK),
                dict(oper_task),
                dict(DEPOT_TASK),
                dict(OPERBOX_TASK),
            ]
        }
    )
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except OSError:
        pass
    write_text_atomic(path, text)
    return path


def resolve_config_dir(task_file: Path | None = None, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env_dir = os.environ.get("MAA_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        completed = subprocess.run(
            [shutil.which(MAA_CLI) or MAA_CLI, "dir", "config"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        resolved = completed.stdout.strip()
        if resolved:
            return Path(resolved)
    except (OSError, subprocess.SubprocessError):
        pass
    if task_file is not None:
        path = Path(task_file)
        if path.parent.name == "tasks":
            return path.parent.parent
        return path.parent
    return DEFAULT_CONFIG_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="培养目标运行编排")
    parser.add_argument(
        "--mode",
        required=True,
        choices=(
            "check",
            "execute",
            "scan",
            "run",
            "mastery",
            "invalidate-for-task",
            "closing-scan",
        ),
        help=(
            "check=是否启用；execute=runner 接管执行；scan=扫描库存；run=立即运行日常；"
            "mastery=立即执行技能专精与加工站合成；"
            "invalidate-for-task=未接管路径运行前保守失效龙门币余额；"
            "closing-scan=每轮游戏任务收尾刷新干员与库存快照"
        ),
    )
    parser.add_argument("--task-file", help="execute / invalidate-for-task 读取的 effective TOML")
    parser.add_argument("--config-dir", help="MAA 配置目录（默认 maa dir config）")
    parser.add_argument(
        "--catalog-path",
        help="培养索引 JSON（默认脚本目录下的 maa-cultivation-data.json）",
    )
    parser.add_argument("--debug", action="store_true", help="以 debug 级别运行 MAA")
    return parser


def main(argv: Sequence[str] | None = None, **overrides: Any) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    config_dir = resolve_config_dir(
        Path(args.task_file) if args.task_file else None, args.config_dir
    )
    options: dict[str, Any] = {"config_dir": config_dir}
    if args.catalog_path:
        options["catalog_path"] = args.catalog_path
    if args.debug:
        options["env_extra"] = {"MAA_LOG": "debug"}
    options.update(overrides)

    if args.mode == "invalidate-for-task":
        if not args.task_file:
            return EXIT_USAGE
        # 非培养用户零成本：state 不存在就直接返回，不导入 service、不读 catalog。
        if not (Path(config_dir) / "cultivation" / "state.json").is_file():
            return EXIT_OK
        try:
            return CultivationRuntime(**options).invalidate_for_task(Path(args.task_file))
        except Exception as error:
            sys.stderr.write(
                f"[CULTIVATION] 余额失效检查跳过: {type(error).__name__}\n"
            )
            return EXIT_OK

    try:
        runtime = CultivationRuntime(**options)
    except Exception as error:
        sys.stderr.write(f"[CULTIVATION] 初始化失败: {type(error).__name__}\n")
        return EXIT_DISABLED if args.mode == "check" else EXIT_FAILED
    if args.mode == "check":
        return runtime.check()
    runtime.install_signal_forwarding()
    try:
        if args.mode == "execute":
            if not args.task_file:
                sys.stderr.write("[CULTIVATION] execute 需要 --task-file\n")
                return EXIT_USAGE
            return runtime.execute(Path(args.task_file))
        if args.mode == "scan":
            return runtime.scan()
        if args.mode == "mastery":
            return runtime.run_mastery()
        if args.mode == "closing-scan":
            if not args.task_file:
                return EXIT_USAGE
            return runtime.closing_scan(Path(args.task_file))
        return runtime.run_daily()
    except SystemExit:
        runtime.mark_interrupted("signal")
        raise
    except KeyboardInterrupt:
        runtime.mark_interrupted("keyboard_interrupt")
        return EXIT_TIMEOUT


if __name__ == "__main__":
    raise SystemExit(main())
