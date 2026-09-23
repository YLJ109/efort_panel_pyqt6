# -*- coding: utf-8 -*-
"""点动链路自检 —— 判断「点了不动」到底卡在哪一环。

默认 **只读**（不触发、机器人不动）。加 `--fire` 才做一次**受控**触发：

  · 触发前先把 `40139~44` 预置成**机器人当前实际角** ⇒ 即使程序真的执行了 MJOINT，
    目标也只是"现在这个姿态" → **原地不动**（防冲）；
  · 置 `40135.Bit0 = 1`，轮询 6 秒看 `40035.Bit0`（完成位）何时变 1；
  · **无论结果如何都撤触发**（`40135 = 0`），程序回到 WAIT 挂起。

判据（2026-09-23 现场实测总结）：
  ✅ 收到 40035 回执                → 程序正常、链路通（看用了多久）
  ❌ 未收到 + 运行位常亮            → 程序**卡在 WAIT** 上（几乎都是 WAIT 条件变量选错）
  ❌ 未收到 + 运行位=0              → 程序没在运行 → 去点【一键就绪】

用法：
    python tools/diag_jog.py                 # 只读体检（安全，随时可跑）
    python tools/diag_jog.py --fire          # 现场确认安全后，做一次受控触发
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efort_client import EfortClient, ModbusError  # noqa: E402


def report(c: EfortClient, s: dict) -> None:
    b = s["bits"]
    print("  自动=%d 伺服=%d 运行=%d 报警=%d/%d 当前程序=%d 速度=%d"
          % (b["auto"], b["servo"], b["run"], s["alarm1"], s["alarm2"], s["prog"], s["speed"]))
    print("  40135 触发位 = %s | 40035 完成位 = %s"
          % (s["jog_trig"], s["jog_done"]))
    ro = s.get("jog_ro")
    if ro:
        ang = [(v - 65536 if v > 32767 else v) / 100.0 for v in ro]
        print("  40139~44 目标角 = %s"
              % "  ".join("J%d=%+.2f" % (i + 1, v) for i, v in enumerate(ang)))
    print("  机器人当前角     = %s" % "  ".join("J%d=%+.2f" % (i + 1, v) for i, v in enumerate(s["joints"])))


def main() -> int:
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "192.168.1.12"
    fire = "--fire" in argv
    hold = float(argv[argv.index("--hold") + 1]) if "--hold" in argv else 6.0

    c = EfortClient(host)
    print("连接:", c.connect())
    s = c.snapshot()
    if s is None:
        print("读状态失败")
        c.close()
        return 1

    print("\n── 触发前链路状态 ──")
    report(c, s)

    if not fire:
        print("\n（只读模式。要实测触发：确认现场安全后加 --fire）")
        c.close()
        return 0

    if not s["bits"]["servo"]:
        # 实机经验：**只要进过示教器的程序编辑态，伺服就会被下使能**，
        # 于是"运行位起不来"会被误当成程序问题。这里自动走一次官方重吸合流程。
        print("\n伺服未上电 → 自动上伺服（0x0000 → 等 0.6s → 0x1001）…")
        try:
            c.servo_reengage()
        except ModbusError:
            pass
        t0 = time.time()
        while time.time() - t0 < 3.5:
            s = c.snapshot()
            if s is None or s["bits"]["servo"]:
                break
            time.sleep(0.15)
        print("  伺服=%s（%.2fs）" % (s["bits"]["servo"] if s else "读失败", time.time() - t0))
        if not s or not s["bits"]["servo"]:
            print("  ⚠ 伺服仍未上电 —— 请在示教器上使能 / 检查安全回路，再点面板【一键就绪】。")
            c.close()
            return 1
    if s["alarm1"] or s["alarm2"] or s["bits"]["alarm"]:
        print("\n⚠ 有报警 —— 先清报警再测。")
        c.close()
        return 1
    if not s["bits"]["run"]:
        print("\n⚠ 运行位=0 —— 点动服务程序没在运行，先点【一键就绪】。")
        c.close()
        return 1

    # ★ 防冲：把目标角预置成当前位置（即使程序里缺 WAIT / 目标写错，也不会动）
    cur = list(s["joints"])
    print("\n── 防冲预置 ──")
    print(" ", c.write_jog_angles(cur))
    print("  （只写数据寄存器，不置触发位 → 此刻不动）")

    base = list(cur)
    done0 = c.read_jog_done()
    print("\n── 触发 ── 40135 = 0x0001（最多等 %.1fs）" % hold)
    t0 = time.time()
    try:
        c.trigger_jog()
    except ModbusError as e:
        print("  触发写失败:", e)
        c.close()
        return 1

    done_at = None
    seen_trig = set()
    peak = [0.0] * 6
    while time.time() - t0 < hold:
        w = c.jog_window()
        if w is not None:
            seen_trig.add(w[0] & 0x0001)
        d = c.read_jog_done()
        st = c.snapshot()
        if st is not None:
            for i in range(6):
                peak[i] = max(peak[i], abs(st["joints"][i] - base[i]))
        if d and done_at is None:
            done_at = time.time() - t0
            break
        time.sleep(0.15)

    try:
        c.clear_trigger()
        print("  已撤触发（40135 = 0）")
    except ModbusError as e:
        print("  !! 撤触发失败:", e, "—— 请手动确认 40135 已归零")

    st = c.snapshot()
    moved = max(peak) if peak else 0.0
    print("\n── 结果 ──")
    print("  40035 完成位：%s" % ("✅ 在 %.2fs 置 1" % done_at if done_at else "❌ 全程为 0"))
    print("  触发位读数出现过的值: %s" % sorted(seen_trig))
    print("  最大位移: %.3f°（%s）" % (moved, "有动作" if moved > 0.05 else "零位移"))
    if st:
        print("  触发后状态：运行=%d 报警=%d/%d" % (st["bits"]["run"], st["alarm1"], st["alarm2"]))

    print("\n── 结论 ──")
    if done_at:
        print("  ✅ 程序正常：接住了触发、执行了 MJOINT、回了完成位。链路全通。")
        print("     （本次目标角=当前位置，所以没有位移 —— 这是防冲预置的预期效果）")
    elif st and st["bits"]["run"]:
        print("  ❌ 程序**在运行却没接住触发** → 它卡在 WAIT 上。")
        print("     最常见原因：WAIT 的条件变量选错了。必须是【读区】")
        print("       fidbus.mtcp_ro_b[0]     （= 40135.Bit0）✅")
        print("     若写成【写区】")
        print("       fidbus.mtcp_wo_b[0]     ❌ 程序自己第一句刚把它置 false，永远等不到")
        print("     核对：示教器打开程序看【当前执行行】是否停在 WAIT；")
        print("           确认前缀 fidbus. 没漏、下标是 [0]。改完【保存】再【一键就绪】。")
    else:
        print("  ❌ 程序没在运行 → 去点【一键就绪】（或重新加载运行该程序）。")

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
