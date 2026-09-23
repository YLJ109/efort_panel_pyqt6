# -*- coding: utf-8 -*-
"""通用控件：卡片 / 状态胶囊 / 关节行（含位移色条）/ 日志视图。

全部从 tokens 取色与尺寸，**不出现裸色值**。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .tokens import FS, FONT, M, MONO, P, STEPS


# ------------------------------------------------------------------ 基础
def label(text: str, role: str = "", parent=None) -> QLabel:
    lb = QLabel(text, parent)
    if role:
        lb.setObjectName(role)
    return lb


def hint(text: str, parent=None, wrap: int = 0) -> QLabel:
    lb = QLabel(text, parent)
    lb.setObjectName("hint")
    lb.setWordWrap(True)
    if wrap:
        lb.setMaximumWidth(wrap)
    return lb


def button(text: str, kind: str = "default", parent=None, name: str = "") -> QPushButton:
    b = QPushButton(text, parent)
    b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if name:
        b.setObjectName(name)
    return b


class Card(QFrame):
    """带标题栏的卡片。内容加到 self.body_layout。"""

    def __init__(self, title: str, hint_text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(M.card_pad, M.card_pad - 3, M.card_pad, M.card_pad - 2)
        v.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(label(title, "cardTitle"))
        head.addStretch(1)
        if hint_text:
            head.addWidget(label(hint_text, "cardHint"))
        v.addLayout(head)

        line = QFrame()
        line.setObjectName("line")
        line.setFrameShape(QFrame.Shape.NoFrame)
        v.addSpacing(6)
        v.addWidget(line)
        v.addSpacing(M.gap - 1)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(M.gap - 1)
        v.addWidget(self.body)


class Pill(QLabel):
    """状态胶囊。set_on(kind) 传 None 表示熄灭。"""

    def __init__(self, text: str, kind: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("pill")
        self._kind = kind
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_on(None)

    def set_on(self, kind: str | None) -> None:
        self.setProperty("on", kind or "")
        self.style().unpolish(self)
        self.style().polish(self)


class DisplaceBar(QWidget):
    """位移色条：中线=基线零点，左右各半个量程；超量程夹持并变红。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(M.bar_h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.delta: float | None = None
        self.limit = 10.0

    def set_value(self, delta: float | None, limit: float) -> None:
        self.delta, self.limit = delta, max(limit, 1e-6)
        self.update()

    def _color(self) -> str:
        if self.delta is None:
            return P.accent
        r = abs(self.delta) / self.limit
        return P.danger if r > 1.0 else (P.warn if r > 0.6 else P.accent)

    def paintEvent(self, _ev) -> None:                    # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(P.panel2))
        half = w / 2.0
        p.setPen(QPen(QColor(P.border), 1))
        p.drawLine(int(half), 0, int(half), h)            # 零点刻度
        if self.delta is None or w < 4:
            p.end()
            return
        frac = max(-1.0, min(1.0, self.delta / self.limit))
        x0, x1 = (half, half + frac * half) if frac >= 0 else (half + frac * half, half)
        p.fillRect(int(round(x0)), 1, max(1, int(round(x1 - x0))), max(1, h - 2),
                   QColor(self._color()))
        p.end()


class JointRow(QWidget):
    """一行 = 一个轴：**坐标 + ± 步进（示教器那 12 个键）+ 在动指示 + 相对基线位移**。

    2026-09-21 改造（用户要求"关节点动删掉，只需要 6 个点坐标和哪个点在动那种；
    还要示教器 J1~J6 的 ± 控制命令"）：
      · 删掉原来每轴 8 个按钮的栅格（6×8 = 48 个）与位移色条
      · 每轴只留 **− / +** 两个按钮 → 6 轴 × 2 = **12 个**，与示教器一一对应
      · 步长由卡片头部的【步长】下拉统一决定（0.1 / 0.5 / 1 / 5 / 10°）
    """

    stepRequested = pyqtSignal(int, float)                 # (轴号 1..6, 方向 +1 / -1)

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.buttons: list[QPushButton] = []

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        lb = label("J%d" % (index + 1))
        lb.setFont(QFont(MONO, FS["mono_l"], QFont.Weight.Bold))
        lb.setStyleSheet("color:%s;" % P.muted)
        lb.setFixedWidth(30)
        lay.addWidget(lb)

        self.v_deg = label("--", "mono")
        self.v_deg.setFont(QFont(MONO, FS["mono"], QFont.Weight.Bold))
        self.v_deg.setFixedWidth(92)
        self.v_deg.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.v_deg)
        lay.addSpacing(10)

        self.b_neg = button("−", "neg", self, "jogBtn")
        self.b_neg.setFixedWidth(48)
        self.b_neg.setToolTip("J%d 负向走一个步长" % (index + 1))
        self.b_neg.clicked.connect(lambda _=False: self.stepRequested.emit(self.index + 1, -1.0))
        lay.addWidget(self.b_neg)
        lay.addSpacing(4)

        self.b_pos = button("+", "primary", self, "jogBtn")
        self.b_pos.setFixedWidth(48)
        self.b_pos.setToolTip("J%d 正向走一个步长" % (index + 1))
        self.b_pos.clicked.connect(lambda _=False: self.stepRequested.emit(self.index + 1, 1.0))
        lay.addWidget(self.b_pos)
        self.buttons = [self.b_neg, self.b_pos]
        lay.addSpacing(12)

        self.v_mov = label("", "hint")
        self.v_mov.setFixedWidth(54)
        lay.addWidget(self.v_mov)
        lay.addSpacing(6)

        self.v_dlt = label("", "monoDim")
        self.v_dlt.setFixedWidth(64)
        self.v_dlt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.v_dlt)
        lay.addStretch(1)

    def update_deg(self, deg: float, moving: bool, delta: float | None) -> None:
        self.v_deg.setText("%+.3f" % deg)
        if moving:
            self.v_mov.setText("● 在动")
            self.v_mov.setStyleSheet("color:%s;" % P.accent)
            self.v_deg.setStyleSheet("color:%s;" % P.accent)
        else:
            self.v_mov.setText("")
            self.v_deg.setStyleSheet("")
        if delta is None:
            self.v_dlt.setText("")
            return
        self.v_dlt.setText("Δ%+.2f" % delta)
        self.v_dlt.setStyleSheet("color:%s;" % (P.warn if abs(delta) > 0.02 else P.dim))


class LogView(QPlainTextEdit):
    """只读日志：按级别着色 + 行数上限。"""

    COLORS = {"info": P.muted, "ok": P.ok, "warn": P.warn, "err": P.danger,
              "ts": P.dim, "head": P.accent}
    MAX_BLOCKS = 600

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("log")
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setFont(QFont(MONO, FS["mono_s"]))

    def append_line(self, level: str, text: str, ts: str = "") -> None:
        col = self.COLORS.get(level, P.text)
        esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        html = ('<span style="color:%s">%s</span>'
                '<span style="color:%s">%s</span>' % (P.dim, ts, col, esc))
        self.appendHtml(html)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
