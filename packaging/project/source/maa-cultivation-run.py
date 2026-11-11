#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""培养目标运行编排的薄入口（逻辑全部在 maa_cultivation_runtime.py）。

    --mode check    是否启用：exit 0 启用 / exit 3 关闭；runner 用它决定是否接管
    --mode execute  由 maa-run.sh 在 run_maa_task 中调用，读取 effective TOML 接管本轮日常
    --mode scan     经既有 runner 执行 StartUp+Depot+OperBox 扫描并保存真实库存
    --mode run      经既有 runner 立即运行日常；未启用时拒绝执行

退出码：0 成功；2 用法/配置错误；3 未启用；91 StartUp 或设备级失败；其余透传 MAA/runner。
    """

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from maa_cultivation_runtime import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
