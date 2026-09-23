# -*- coding: utf-8 -*-
"""主题令牌 —— **禁止在其它文件里写裸色值 / 裸字号**。

所有颜色、字号、圆角、间距都从这里取；改主题只改这一个文件。
（沿用原 tkinter 版的深色工业 HMI 观感：冷灰底 + 蓝主色 + 三色语义。）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    # 背景层次（由深到浅）
    bg: str = "#11151b"          # 窗口底
    panel: str = "#1a212a"       # 卡片
    panel2: str = "#232c38"      # 卡内次级块 / 输入框按钮底
    panel3: str = "#2b3644"      # 悬停
    entry: str = "#0e1218"       # 输入框 / 日志底
    border: str = "#2e3946"      # 描边
    border_lo: str = "#232c38"   # 弱描边/分隔

    # 文本
    text: str = "#e7edf4"
    muted: str = "#8b98a8"
    dim: str = "#5f6b7a"

    # 语义色
    accent: str = "#3d9df0"
    ok: str = "#27b57a"
    warn: str = "#e0a038"
    danger: str = "#e04b4f"

    # 语义色（实底 + 其上文字），用于胶囊/主按钮
    on_accent: str = "#ffffff"
    accent_bg: str = "#1f5a86"
    ok_bg: str = "#1b7a52"
    warn_bg: str = "#b57d24"
    danger_bg: str = "#b8343a"
    neutral_bg: str = "#39465a"
    danger_bg_hi: str = "#cf4047"


@dataclass(frozen=True)
class Metrics:
    radius: int = 6
    radius_sm: int = 4
    pad: int = 7
    gap: int = 8
    card_pad: int = 11
    bar_h: int = 9


P = Palette()
M = Metrics()

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

FS = {"h1": 15, "h2": 11, "body": 9, "small": 8, "mono": 10, "mono_s": 9, "mono_l": 11,
      "estop": 15, "ready": 12}

#: 点动档位（度）
STEPS: tuple[int, ...] = (10, 5, 2, 1)


def qss(p: Palette = P, m: Metrics = M) -> str:
    """全局样式表。控件一律靠 objectName / class 选择器取样式，不再内联设色。"""
    return f"""
QWidget {{
    background: {p.bg}; color: {p.text};
    font-family: "{FONT}";
    font-size: {FS['body']}pt;
}}
QToolTip {{
    background: {p.panel2}; color: {p.text};
    border: 1px solid {p.border}; padding: 4px 6px;
}}

/* ---------- 卡片 ---------- */
QFrame#card {{ background: {p.panel}; border: 1px solid {p.border}; border-radius: {m.radius}px; }}
QLabel#cardTitle {{ color: {p.accent}; font-weight: bold; font-size: {FS['body']}pt; }}
QLabel#cardHint  {{ color: {p.dim}; font-size: {FS['small']}pt; }}
QLabel#hint      {{ color: {p.dim}; font-size: {FS['small']}pt; }}
QLabel#muted     {{ color: {p.muted}; }}
QLabel#mono      {{ font-family: "{MONO}"; font-size: {FS['mono']}pt; font-weight: bold; }}
QLabel#monoDim   {{ font-family: "{MONO}"; font-size: {FS['mono_s']}pt; color: {p.dim}; }}
QFrame#line {{ background: {p.border_lo}; max-height: 1px; min-height: 1px; border: none; }}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {p.panel2}; color: {p.text};
    border: 1px solid {p.border}; border-radius: {m.radius_sm}px;
    padding: 5px 10px; min-height: 16px;
}}
QPushButton:hover:enabled   {{ background: {p.panel3}; }}
QPushButton:pressed:enabled {{ background: {p.border}; }}
QPushButton:disabled        {{ color: {p.dim}; background: {p.panel}; border-color: {p.border_lo}; }}

QPushButton[kind="primary"] {{ background: {p.accent_bg}; border-color: {p.accent}; color: {p.on_accent}; }}
QPushButton[kind="primary"]:hover:enabled {{ background: #26699b; }}
QPushButton[kind="ok"]      {{ background: {p.ok_bg}; border-color: {p.ok}; color: {p.on_accent}; }}
QPushButton[kind="ok"]:hover:enabled {{ background: #229967; }}
QPushButton[kind="warn"]    {{ background: {p.warn_bg}; border-color: {p.warn}; color: {p.on_accent}; }}
QPushButton[kind="warn"]:hover:enabled {{ background: #c98d2c; }}
QPushButton[kind="danger"]  {{ background: {p.danger_bg}; border-color: {p.danger}; color: {p.on_accent}; }}
QPushButton[kind="danger"]:hover:enabled {{ background: {p.danger_bg_hi}; }}
QPushButton[kind="ghost"]   {{ background: {p.panel2}; color: {p.muted}; }}
QPushButton[kind="ghost"]:hover:enabled {{ background: {p.panel3}; color: {p.text}; }}
QPushButton[kind="neg"]     {{ color: #d3dde9; }}

QPushButton#jogBtn {{ padding: 3px 0; min-width: 34px; min-height: 14px; }}
QPushButton#runArmed {{
    background: {p.danger_bg}; border-color: {p.danger}; color: {p.on_accent}; font-weight: bold;
}}
QPushButton#estop {{
    background: {p.danger_bg}; border-color: {p.danger}; color: {p.on_accent};
    font-size: {FS['estop']}pt; font-weight: bold; padding: 14px 10px;
    border-radius: {m.radius}px;
}}
QPushButton#estop:hover:enabled {{ background: {p.danger_bg_hi}; }}
QPushButton#ready {{
    background: {p.ok_bg}; border-color: {p.ok}; color: {p.on_accent};
    font-size: {FS['ready']}pt; font-weight: bold; padding: 9px 10px;
}}

/* ---------- 输入 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {p.entry}; color: {p.text};
    border: 1px solid {p.border}; border-radius: {m.radius_sm}px;
    padding: 4px 6px; font-family: "{MONO}"; font-size: {FS['mono']}pt;
    selection-background-color: {p.accent}; selection-color: {p.on_accent};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {p.dim}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {p.muted}; margin-right: 6px; }}
QComboBox QAbstractItemView {{
    background: {p.panel2}; color: {p.text};
    border: 1px solid {p.border}; selection-background-color: {p.accent_bg};
    outline: none;
}}
/* ---------- ⑥ 点位清单 ---------- */
QListWidget#ptList {{
    background: {p.entry}; color: {p.text};
    border: 1px solid {p.border}; border-radius: {m.radius_sm}px;
    font-family: "{MONO}"; font-size: {FS['mono_s']}pt;
    outline: none;
}}
QListWidget#ptList::item {{ padding: 3px 6px; border-bottom: 1px solid {p.border}; }}
QListWidget#ptList::item:selected {{ background: {p.accent_bg}; color: #dceeff; }}
QListWidget#ptList::item:hover {{ background: {p.panel2}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; background: {p.panel2};
    border: none; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid {p.muted}; width: 0; height: 0; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {p.muted}; width: 0; height: 0; }}

/* ---------- 勾选 ---------- */
QCheckBox {{ color: {p.warn}; font-size: {FS['small']}pt; spacing: 7px; }}
QCheckBox::indicator {{
    width: 13px; height: 13px; border-radius: 3px;
    border: 1px solid {p.border}; background: {p.entry};
}}
QCheckBox::indicator:checked {{ background: {p.warn}; border-color: {p.warn}; }}
QCheckBox::indicator:hover {{ border-color: {p.warn}; }}

/* ---------- 状态胶囊 ---------- */
QLabel#pill {{
    background: {p.panel2}; color: {p.dim};
    border: 1px solid {p.border_lo}; border-radius: {m.radius_sm}px;
    padding: 2px 6px; font-size: {FS['small']}pt;
}}
QLabel#pill[on="bad"]  {{ background: {p.danger_bg}; color: #ffe4e5; border-color: {p.danger}; }}
QLabel#pill[on="good"] {{ background: {p.ok_bg};     color: #d9f7e8; border-color: {p.ok}; }}
QLabel#pill[on="info"] {{ background: {p.accent_bg}; color: #dceeff; border-color: {p.accent}; }}
QLabel#pill[on="neutral"] {{ background: {p.neutral_bg}; color: {p.text}; border-color: {p.border}; }}

/* ---------- 日志 ---------- */
QPlainTextEdit#log {{
    background: {p.entry}; color: {p.text}; border: 1px solid {p.border};
    border-radius: {m.radius_sm}px; font-family: "{MONO}"; font-size: {FS['mono_s']}pt;
    padding: 5px 7px; selection-background-color: {p.accent};
}}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{ background: {p.entry}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.panel2}; min-height: 24px; border-radius: 5px; }}
QScrollBar::handle:vertical:hover {{ background: {p.border}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: {p.entry}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.panel2}; min-width: 24px; border-radius: 5px; }}

/* ---------- 标签页（帮助/模板弹窗） ---------- */
QTabWidget::pane {{ border: 1px solid {p.border}; border-radius: {m.radius_sm}px; top: -1px; }}
QTabBar::tab {{
    background: {p.panel}; color: {p.muted}; border: 1px solid {p.border};
    border-bottom: none; padding: 6px 14px; margin-right: 2px;
    border-top-left-radius: {m.radius_sm}px; border-top-right-radius: {m.radius_sm}px;
}}
QTabBar::tab:selected {{ background: {p.panel2}; color: {p.text}; }}
QTabBar::tab:hover {{ color: {p.text}; }}

/* ---------- 对话框 ---------- */
QMessageBox {{ background: {p.panel}; }}
QMessageBox QLabel {{ color: {p.text}; }}
"""
