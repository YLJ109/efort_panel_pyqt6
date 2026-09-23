# -*- coding: utf-8 -*-
"""诊断：加载程序时控制器各信号的时间线 —— 用来确定"程序是否存在"的可靠判据。

用法: python tools/probe_load.py 411 123 [--host 192.168.1.12] [--secs 3]

对每个程序号打印 (t, Bit11 程序加载位, 40006 当前程序号, 报警1, 报警2) 时间线，
**只加载不运行**。用来区分：
  · Bit11 是"瞬时脉冲"还是"锁存"  → 决定扫描器能不能靠它
  · 40006 是否随成功加载而变成该号 → 若会，则是**持久状态**，比脉冲可靠得多
  · 加载不存在的程序时报警何时出现（5005 延迟）
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efort_client import (  # noqa: E402
    ADDR_SET_PROG, LOAD_WORD, STOP_WORD, EfortClient, ModbusError,
)


def probe(c: EfortClient, n: int, secs: float) -> None:
    print("\n================ 探测程序号 %d ================" % n)
    before = c._read(0, 6)
    if before is None:
        print("  读失败")
        return
    print("  加载前: Bit11=%d 40006=%d 报警=%d/%d"
          % ((before[0] >> 11) & 1, before[5], before[3], before[4]))
    try:
        echo = c.write_reg(ADDR_SET_PROG, n)
        print("  40104 写入 %d（回显 %d）" % (n, echo))
        c.cmd(LOAD_WORD)
    except ModbusError as e:
        print("  写入/命令失败:", e)
        return
    t0 = time.time()
    seen_loaded = False
    first_alarm = None
    first_progchg = None
    rows = []
    while time.time() - t0 < secs:
        w = c._read(0, 6)
        dt = time.time() - t0
        if w is None:
            rows.append((dt, None, None, None, None))
        else:
            b11 = (w[0] >> 11) & 1
            rows.append((dt, b11, w[5], w[3], w[4]))
            if b11:
                seen_loaded = True
            if (w[3] or w[4]) and first_alarm is None:
                first_alarm = (dt, w[3], w[4])
            if w[5] == n and first_progchg is None:
                first_progchg = dt
        time.sleep(0.06)
    # 打印（压缩：只打变化点）
    last = None
    for dt, b11, prog, a1, a2 in rows:
        key = (b11, prog, a1, a2)
        if key != last:
            print("   t=%.2fs  Bit11=%s 40006=%s 报警=%s/%s" % (dt, b11, prog, a1, a2))
            last = key
    print("  ── 小结：Bit11 见过置位=%s；40006 变成 %d 的时刻=%s；报警首次出现=%s"
          % (seen_loaded, n, ("%.2fs" % first_progchg) if first_progchg else "从未",
             ("%.2fs %d/%d" % first_alarm) if first_alarm else "无"))
    try:
        c.cmd(STOP_WORD)
    except ModbusError:
        pass
    time.sleep(0.2)
    if first_alarm:
        try:
            c.clear_alarm()
        except ModbusError:
            pass
        time.sleep(0.3)
        st = c._read(0, 6)
        print("  清报警后: 报警=%s/%s" % (st[3], st[4]) if st else "  清报警后读失败")


def main() -> int:
    argv = sys.argv[1:]
    opts = {"host": "192.168.1.12", "secs": 3.0}
    nums: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--host" and i + 1 < len(argv):
            opts["host"] = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--secs" and i + 1 < len(argv):
            opts["secs"] = float(argv[i + 1])
            i += 2
            continue
        nums.append(argv[i])
        i += 1
    nums_int = [int(a) for a in nums] or [411, 123]
    host, secs = opts["host"], opts["secs"]

    c = EfortClient(host)
    print("连接:", c.connect())
    s = c.snapshot()
    b = s["bits"]
    print("初始状态: 自动=%d 伺服=%d 报警=%d/%d 运行=%d 当前程序=%d"
          % (b["auto"], b["servo"], s["alarm1"], s["alarm2"], b["run"], s["prog"]))
    if b["run"]:
        print("!! 有程序在运行 —— 先停再探测")
        return 1
    orig = s["prog"]
    for n in nums_int:
        probe(c, n, secs)
    try:
        c.set_program(orig)
        c.cmd(0x1001)
    except ModbusError:
        pass
    st = c.snapshot()
    if st:
        print("\n收尾: 40104=%d 报警=%d/%d 伺服=%d"
              % (orig, st["alarm1"], st["alarm2"], st["bits"]["servo"]))
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
