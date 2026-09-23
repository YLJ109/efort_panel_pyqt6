# -*- coding: utf-8 -*-
"""扫描控制器上"存在哪些程序号" —— **只加载、绝不运行**。

用法: python tools/scan_programs.py [起=1] [止=300] [--host IP] [--budget 1.2] [--json 路径]

判据（2026-09-21 现场实测，见 tools/probe_load.py 时间线）：
  · **存在**：`40001.Bit11` 程序加载位置位 —— 实测 **≈0.50s** 才拉高（是真脉冲，必须等得够久）
  · **不存在**：报警 **5005** 出现 —— 实测 **≈0.08s**（快且锁存，比等脉冲可靠）
  · 40006（当前程序号）**不随加载变化**，不能当判据（已实测：加载 123 失败后仍是 411）

⚠ 历史 bug：旧版把等待窗口设成 0.5s，而真实加载要 0.50~0.61s —— **临界漏判**，
   结果 411 明明存在却被报成"（无）"。现在窗口 ≥1.2s，并改成"报警/置位谁先到就停"。

全程不碰 Bit1/Bit2，**不会让机器人动**。
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efort_client import (  # noqa: E402
    ADDR_SET_PROG, LOAD_WORD, STOP_WORD, EfortClient, ModbusError,
)

LIGHT_BLOCK = 6            # 一次读 40001~40006（在合法白名单 1..8 内，最省往返）


def _light(c: EfortClient):
    """轻量读：一次拿 [40001 状态, ..., 40004 报警1, 40005 报警2, 40006 程序号]。"""
    return c._read(0, LIGHT_BLOCK)


def _wait_loaded_bit_clear(c: EfortClient, timeout: float = 1.5) -> bool:
    """确保 Bit11 归零后再探下一个号 —— 否则残留位会让下一个号假阳性。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        w = _light(c)
        if w and not ((w[0] >> 11) & 1):
            return True
        try:
            c.cmd(STOP_WORD)
        except ModbusError:
            pass
        time.sleep(0.1)
    return False


def probe(c: EfortClient, n: int, budget: float) -> tuple[str, float, str]:
    """探一个号。返回 (kind, 秒, 说明)，kind ∈ exists / missing / timeout / error。"""
    try:
        c.write_reg(ADDR_SET_PROG, n)
        c.cmd(LOAD_WORD)
    except ModbusError as e:
        return "error", 0.0, str(e)
    t0 = time.time()
    while True:
        dt = time.time() - t0
        w = _light(c)
        if w is not None:
            if (w[0] >> 11) & 1:
                return "exists", dt, "Bit11 置位"
            if w[3] or w[4]:
                return "missing", dt, "报警 %d/%d" % (w[3], w[4])
        if dt >= budget:
            return "timeout", dt, "无置位也无报警"
        time.sleep(0.04)


def main() -> int:
    argv = sys.argv[1:]
    opts = {"host": "192.168.1.12", "budget": 1.2, "json": None}
    nums: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--host" and i + 1 < len(argv):
            opts["host"] = argv[i + 1]
            i += 2
            continue
        if a == "--budget" and i + 1 < len(argv):
            opts["budget"] = float(argv[i + 1])
            i += 2
            continue
        if a == "--fast":
            opts["budget"] = 1.2           # 保留旧开关，但不再低于安全窗口
            i += 1
            continue
        if a == "--json" and i + 1 < len(argv):
            opts["json"] = argv[i + 1]
            i += 2
            continue
        nums.append(a)
        i += 1
    lo = int(nums[0]) if nums else 1
    hi = int(nums[1]) if len(nums) > 1 else 300
    budget = opts["budget"]

    c = EfortClient(opts["host"])
    print("连接:", c.connect())
    s = c.snapshot()
    b = s["bits"]
    print("状态: 自动=%d 手动=%d 伺服=%d 报警=%d/%d 运行=%d 当前程序=%s"
          % (b["auto"], b["manual"], b["servo"], s["alarm1"], s["alarm2"], b["run"], s["prog"]))
    if b["run"]:
        print("!! 当前有程序在运行 —— 先停掉再扫描")
        return 1
    orig = s["prog"]
    _wait_loaded_bit_clear(c)
    print("扫描 %d~%d（只加载不运行；存在≈0.5s / 不存在≈0.1s，窗口 %.1fs）…" % (lo, hi, budget))
    print()

    found: list[dict] = []
    timeouts: list[int] = []
    errors: list[int] = []
    t0 = time.time()
    for n in range(lo, hi + 1):
        kind, dt, why = probe(c, n, budget)
        if kind == "exists":
            found.append({"prog": n, "secs": round(dt, 2)})
            print("  ✅ %-4d 存在（%.2fs，%s）" % (n, dt, why), flush=True)
            try:
                c.cmd(STOP_WORD)
            except ModbusError:
                pass
            _wait_loaded_bit_clear(c)
        elif kind == "missing":
            try:
                c.clear_alarm()            # 5005 不清会累积，影响后面判断
            except ModbusError:
                pass
            time.sleep(0.2)
            _wait_loaded_bit_clear(c, 0.6)
        elif kind == "timeout":
            timeouts.append(n)
            print("  ⚠ %-4d 无明确应答（%.2fs 内既未置位也无报警）" % (n, dt), flush=True)
        else:
            errors.append(n)
            print("  × %-4d 通信异常：%s" % (n, why), flush=True)
        if (n - lo + 1) % 25 == 0:
            print("  … 进度 %d/%d，已用 %.0fs，命中 %d 个"
                  % (n - lo + 1, hi - lo + 1, time.time() - t0, len(found)), flush=True)

    print()
    print("=== 扫描完成，用时 %.0fs ===" % (time.time() - t0))
    if found:
        print("存在的程序号（%d 个）：%s"
              % (len(found), "  ".join("%d(%.2fs)" % (f["prog"], f["secs"]) for f in found)))
    else:
        print("存在的程序号: （无）")
    if timeouts:
        print("无明确应答: %s" % timeouts)
    if errors:
        print("通信异常: %s" % errors)

    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "host": opts["host"],
           "range": [lo, hi], "found": found, "timeout": timeouts, "error": errors}
    jp = opts["json"] or os.path.join(ROOT, "logs", "scan-%s.json" % time.strftime("%Y%m%d"))
    try:
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("结果已存: %s" % jp)
    except OSError as e:
        print("结果存盘失败:", e)

    try:
        c.set_program(orig)
        c.cmd(0x1001)
        st = c.snapshot()
        print("收尾: 40104 恢复为 %d；报警=%d/%d 伺服=%d"
              % (orig, st["alarm1"], st["alarm2"], st["bits"]["servo"]))
    except ModbusError as e:
        print("收尾失败:", e)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
