# -*- coding: utf-8 -*-
"""安全预置：把「点动目标角」40139~40144 写成机器人**当前实际角度**。

为什么需要它（2026-09-23）：
  第一次运行一个**自己写的**点动服务程序时，我们无法从 Modbus 侧读它内部到底写了什么。
  万一程序里**漏了 WAIT / 写错了流程**，它会一被运行就直接 MJOINT ——
  目标就是 40139~44 里当时的值。若那里残留着旧值（或全是 0），机器人会立刻冲向那个姿态。

  对策：运行前把 40139~44 预置成「当前位置」。
  这样即使程序缺 WAIT，MJOINT 的目标 = 当前姿态 → **机器人一步都不会动**。
  之后面板每次点动都会重写这 6 个寄存器，所以预置值不会影响正常使用。

本脚本**只写数据寄存器，绝不置 40135.Bit0 触发位** → 不会引发任何运动。

用法：
    python tools/presafe_jog_target.py                # 预置为当前角
    python tools/presafe_jog_target.py --dry-run      # 只看现值，不写
    python tools/presafe_jog_target.py --host 192.168.1.12
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efort_client import (  # noqa: E402
    ADDR_JOG_ANG, JOG_ANG_COUNT, EfortClient, ModbusError,
)


def main() -> int:
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "192.168.1.12"
    dry = "--dry-run" in argv

    c = EfortClient(host)
    print("连接:", c.connect())
    s = c.snapshot()
    if s is None:
        print("读状态失败")
        c.close()
        return 1
    b = s["bits"]
    cur = s["joints"]
    print("控制器状态: 自动=%d 伺服=%d 报警=%d/%d 运行=%d 当前程序=%d"
          % (b["auto"], b["servo"], s["alarm1"], s["alarm2"], b["run"], s["prog"]))
    print("机器人当前位置: " + "  ".join("J%d=%+.3f" % (i + 1, v) for i, v in enumerate(cur)))

    cur40035 = s.get("jog_wo")
    print("40035 回执位现值: %s" % (("0x%04X" % cur40035[0]) if cur40035 else "读不到"))

    raw = c._read(ADDR_JOG_ANG, JOG_ANG_COUNT)
    if raw:
        old = [v - 65536 if v > 32767 else v for v in raw]
        print("40139~44 现值（×100 的整数）: %s" % old)
        print("  折成角度: %s" % "  ".join("%+.2f" % (v / 100.0) for v in old))
        if any(abs(v / 100.0 - j) > 2.0 for v, j in zip(old, cur)):
            print("  ⚠ 与当前位置差 >2° —— 这是**残留的危险目标**；若程序缺 WAIT，运行时机器人会冲向它")
    else:
        print("40139~44 读失败")

    if dry:
        print("\n--dry-run：未写入。")
        c.close()
        return 0

    txt = c.write_jog_angles(list(cur))
    print("\n已写入 40139~44 = 当前角：%s" % txt)
    raw2 = c._read(ADDR_JOG_ANG, JOG_ANG_COUNT)
    if raw2:
        new = [v - 65536 if v > 32767 else v for v in raw2]
        print("读回确认: %s" % "  ".join("%+.2f" % (v / 100.0) for v in new))
    print("✅ 预置完成 —— 未置 40135.Bit0，机器人不会动。"
          "现在即使点动服务程序漏写 WAIT，运行它也不会产生位移。")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
