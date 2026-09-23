# -*- coding: utf-8 -*-
"""埃夫特 EFORT 远程控制台 · PyQt6 版 —— 入口。

用法：
    python main.py                        正常启动（默认 IP 192.168.1.12）
    python main.py --host 192.168.1.12    指定控制器 IP
    python main.py --selftest             离屏冒烟：只建界面不连机器人（不带字体观感）
    python main.py --shot [目录]          出三张界面截图（原生字形，但不映射到屏幕）
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt                        # noqa: E402
from PyQt6.QtGui import QFont                       # noqa: E402
from PyQt6.QtWidgets import QApplication            # noqa: E402

from app.tokens import FONT                          # noqa: E402


def _log_path() -> str | None:
    """审计日志：<项目>/logs/audit-YYYYMMDD.log（--no-audit 可关）。"""
    if "--no-audit" in sys.argv:
        return None
    return os.path.join(ROOT, "logs", "audit-%s.log" % time.strftime("%Y%m%d"))


def build_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("EFORT Remote Console")
    app.setFont(QFont(FONT, 9))
    return app


def main() -> int:
    argv = sys.argv[1:]
    host = "192.168.1.12"
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]

    # ⚠ 平台插件必须在 QApplication **创建之前**决定 —— 之后再 setdefault 已经晚了，
    #   结果是"以为在离屏，其实真弹了一个窗口"。（曾经就踩过：--shot 会闪窗）
    if "--selftest" in argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = build_app()

    from app.window import MainWindow
    win = MainWindow(host=host, log_path=_log_path())

    if "--selftest" in argv:
        win.show()
        for _ in range(12):
            app.processEvents()
        win.close()
        print("✅ 界面冒烟通过：控件树构建 + 刷新无异常")
        return 0

    if "--shot" in argv:
        out = argv[argv.index("--shot") + 1] if len(argv) > argv.index("--shot") + 1 else "shots"
        out = os.path.join(ROOT, out)
        os.makedirs(out, exist_ok=True)
        # 截图要看**真实字形**（离屏平台没有系统字体，中文会全变方框），
        # 所以走原生平台 + 不映射到屏幕：布局与绘制照做，但不会弹窗打扰你。
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        win.show()
        for _ in range(10):
            app.processEvents()
        n = 0
        for name in ("01_initial", "02_ready", "03_jogging"):
            app.processEvents()
            win.grab().save(os.path.join(out, name + ".png"))
            n += 1
            # 让三个状态略有差异：第 2 张勾选安全确认，第 3 张再模拟一次点动
            if name == "01_initial":
                win.c_gate.setChecked(True)
            elif name == "02_ready":
                win._on_jog(4, 5.0)
            for _ in range(6):
                app.processEvents()
        win.close()
        print("已保存 %d 张截图 -> %s" % (n, out))
        return 0

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
