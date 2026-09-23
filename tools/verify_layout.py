# -*- coding: utf-8 -*-
"""布局几何验收（离屏）—— 输出**人可读**的数值结论，不依赖看图。

检查项：
  1. 卡片/主要容器在窗口内的实际几何（宽高是否被挤压）
  2. 有无控件超出父容器（被裁）或可见却尺寸为 0
  3. 关节行 2 个 ± 按钮的宽度、角度列宽度是否够显示
  4. 日志卡实际高度（历史踩坑：日志卡被挤出窗口只剩几十像素）
  5. 主题令牌覆盖：抽查关键控件是否吃到 QSS（尺寸/可见性）

用法: python tools/verify_layout.py [宽 高]
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QPushButton  # noqa: E402

from app.tokens import P                                              # noqa: E402
from app.window import MainWindow                                     # noqa: E402

W, H = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (1360, 920)

app = QApplication(sys.argv[:1])
win = MainWindow(host="192.168.1.12", log_path=None)
win.resize(W, H)
win.show()
for _ in range(8):
    app.processEvents()

print("=== 窗口 %dx%d（实际 %dx%d）===" % (W, H, win.width(), win.height()))

# ---- 主要容器 ----
def g(w):
    r = w.geometry()
    return "%dx%d @(%d,%d)" % (r.width(), r.height(), r.x(), r.y())

cards = [w for w in win.findChildren(QFrame) if w.objectName() == "card"]
print("\n=== 卡片（%d 张）===" % len(cards))
for c in cards:
    t = c.findChild(QLabel, "cardTitle")
    print("   %-22s %s" % ((t.text() if t else "?"), g(c)))

print("\n=== 关节行（6 轴 × −/+ = 12 个按钮）===")
for jr in win.jrows:
    btns = [b.width() for b in jr.buttons]
    print("   J%d  %s | −/+ 按钮宽 %s (最小 %d) | 角度列 %d | 在动列 %d | 位移列 %d"
          % (jr.index + 1, g(jr), btns, min(btns), jr.v_deg.width(),
             jr.v_mov.width(), jr.v_dlt.width()))

print("\n=== 关键读数控件 ===")
for name, w in (("日志", win.log_view), ("状态胶囊区", win.pills["auto"]),
                ("熔断标签", win.l_fuse), ("点动服务标签", win.l_jprog),
                ("底部状态", win.l_stat), ("急停按钮", win.b_estop),
                ("当前在动", win.l_moving), ("扫描状态", win.l_scan),
                ("程序下拉", win.cb_prog), ("步长下拉", win.cb_step),
                ("测试时长", win.cb_secs), ("添加按钮", win.b_padd),
                ("设为点动按钮", win.b_pjog)):
    print("   %-12s %s  可见=%s" % (name, g(w), w.isVisible()))

# ---- 健康检查 ----
from PyQt6.QtWidgets import QWidget  # noqa: E402

problems: list[str] = []


def each(w, cb):
    for ch in w.children():
        if isinstance(ch, QWidget):
            cb(ch, w)
            each(ch, cb)


def check(ch, par) -> None:
    if not ch.isVisible():
        return
    r = ch.geometry()
    if not isinstance(par, QWidget):
        return
    pr = par.rect()
    if r.width() <= 0 or r.height() <= 0:
        problems.append("可见但尺寸为 0: %s %s %s" % (type(ch).__name__,
                                                 ch.objectName() or "-", g(ch)))
    # 超出父容器右下 2px 以上 = 被裁
    if r.right() > pr.width() + 2 or r.bottom() > pr.height() + 2:
        problems.append("超出父容器(可能被裁): %s %s 子%s 父%s"
                        % (type(ch).__name__, ch.objectName() or "-", g(ch),
                           "%dx%d" % (pr.width(), pr.height())))


each(win, check)

print("\n=== 日志卡高度 ===")
lv = win.log_view.height()
print("   %d px  %s" % (lv, "OK" if lv >= 80 else "⚠ 被挤压"))

print("\n=== 结论 ===")
if problems:
    for p in problems[:40]:
        print("   ⚠ " + p)
    print("   共 %d 处待查" % len(problems))
else:
    print("   ✅ 未发现裁切 / 零尺寸问题")

# 顺便打印令牌抽查（确认 QSS 生效：按钮应有圆角样式属性）
b = win.b_ready
print("\n=== 令牌抽查 ===")
print("   主色 accent=%s  ok=%s  danger=%s" % (P.accent, P.ok, P.danger))
print("   一键就绪按钮: kind=%r 尺寸=%s 可见=%s"
      % (b.property("kind"), g(b), b.isVisible()))
print("   ± 按钮总数: %d（应为 12）" % sum(len(r.buttons) for r in win.jrows))
print("   测试跑/执行按钮: %s / %s"
      % (g(win.b_test), g(win.b_exec)))
print("\n=== 程序清单 ===")
print("   文件: %s（存在=%s）" % (win.store_path, os.path.exists(win.store_path)))
print("   条目: %s  点动服务程序=%s"
      % (win.store.numbers() or "（空 —— 启动后自己 [添加] 或 [扫描]）", win.store.jog or "未设置"))
print("   清单按钮: 添加=%s 改名=%s 删除=%s 设为点动=%s"
      % (win.b_padd.isEnabled(), win.b_pedit.isEnabled(),
         win.b_pdel.isEnabled(), win.b_pjog.isEnabled()))
sys.exit(0)
