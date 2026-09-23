# -*- coding: utf-8 -*-
"""在桌面（或指定目录）生成「一键启动」快捷方式 —— 按钮形式启动控制台。

为什么不用 PowerShell：本机安全策略**禁止 COM 实例化**（`New-Object -ComObject WScript.Shell`
被直接拦掉），所以改用纯 Python 的 `pylnk3` 直接写 .lnk 二进制。

默认指向 `pythonw.exe` ⇒ **双击只出界面、没有黑窗口**；
加 `--console` 则指向 `启动面板_诊断.bat`（出错时留在屏幕上，排错用）。

用法：
    python tools/make_shortcut.py                  # 桌面：EFORT 远程控制台.lnk
    python tools/make_shortcut.py --console        # 上面那个改成带控制台的版本
    python tools/make_shortcut.py --dir "D:\\"     # 放到别处
    python tools/make_shortcut.py --name "EFORT 点动台"
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYW = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\efort_pyqt6\Scripts\pythonw.exe"
BAT = os.path.join(ROOT, "启动面板.bat")
BAT_CONSOLE = os.path.join(ROOT, "启动面板_诊断.bat")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成一键启动快捷方式")
    ap.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "Desktop"),
                    help="放哪儿（默认桌面）")
    ap.add_argument("--name", default="EFORT 远程控制台", help="快捷方式显示名")
    ap.add_argument("--console", action="store_true",
                    help="指向带控制台的启动器（排错用；平时不要加）")
    a = ap.parse_args()

    try:
        from pylnk3 import Lnk
    except ImportError:
        print("缺少 pylnk3：pip install pylnk3")
        return 1

    target = BAT_CONSOLE if a.console else (PYW if os.path.exists(PYW) else BAT)
    args = "" if target.lower().endswith(".bat") else "main.py"

    out = os.path.join(os.path.abspath(a.dir), a.name + ".lnk")
    if os.path.exists(out):
        print("已存在，将覆盖：%s" % out)

    lnk = Lnk()
    # ⚠ pylnk3 的 `path` 是**只读属性**，目标必须用 specify_local_location 设
    lnk.specify_local_location(target)
    lnk.arguments = args
    lnk.work_dir = ROOT
    lnk.description = "埃夫特 EFORT RP-2 远程控制台（PyQt6）"
    lnk.icon = target if target.lower().endswith(".exe") else (
        PYW if os.path.exists(PYW) else target)
    lnk.save(out)

    ok = os.path.exists(out)
    print("%s 目标=%s 参数=%s 工作目录=%s"
          % ("✅ 已创建" if ok else "❌ 创建失败", target, args or "(无)", ROOT))
    print("   快捷方式：%s" % out)
    if not ok:
        return 1
    if target == PYW and not os.path.exists(PYW):
        print("   ⚠ 找不到 pythonw.exe，已回退到 .bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
