# -*- coding: utf-8 -*-
"""主窗口 —— 埃夫特 EFORT 远程控制台（PyQt6 版）。

与 tkinter 版功能对齐，并做以下修复/增强：
  · 熔断武装期间**禁用点动按钮**（配合 worker 侧重入保护），避免覆盖 fuse
  · **Esc = 急停**（不经安全闸门）；退出前若有连接/伺服则二次确认
  · 链路状态三态：已连接 / 重连中 / 未连接；重连中给【立即重连】按钮
  · 熔断状态由 worker 消息带回，UI 不再跨线程读 worker 内部字段
  · 日志可落盘（审计）+ 一键清空 / 打开日志文件
  · 数字输入一律用 SpinBox 限幅，杜绝非法值
  · **程序清单**（2026-09-23 用户要求）：程序号 / 名字 / 备注由**用户自己维护**、
    落盘到 programs.json，可增删改名；哪一个是"点动服务程序"也由用户声明 ——
    **绝不猜**程序号，也**不假设**程序名是中文还是英文。
"""
from __future__ import annotations

import os
import queue
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from efort_client import EfortClient

from .pointstore import DEFAULT_FILE as POINT_FILE, PointStore
from .progstore import DEFAULT_FILE, ProgramStore
from .tokens import FS, FONT, M, MONO, P, qss
from .widgets import Card, LogView, Pill, button, hint, label
from .worker import ALARM_TEXT, RobotWorker

APP_TITLE = "埃夫特 EFORT · 远程控制台"
SUBTITLE = "RP-2 / Modbus TCP · 关节 ± 步进 + 点位记录/回访 + 程序测试跑（定时）/ 执行"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PILLS = [
    ("manual", "手 动", "neutral"), ("auto", "自 动", "info"), ("remote", "远 程", "neutral"),
    ("servo", "伺 服", "good"), ("alarm", "报 警", "bad"), ("estop", "急 停", "bad"),
    ("run", "程序运行", "info"), ("prog_loaded", "程序加载", "info"),
    ("servo_ready", "伺服准备", "good"),
]

HELP_TEXT = """【怎么用】五步就够
────────────────────────────────────────
第 0 步   先把你的程序登记进来（只需做一次，之后随时可增删）
          ⑤ 卡【程序清单】→ 点【添加】→ 填「程序号」+「名字」（名字随便写，可能是英文）
          · 程序号 = 示教器上那个程序在 Modbus 40104 里的编号（只能填数字）
          · 名字只用于你自己在界面上认，不会下发给机器人
          · 清单存到项目目录的 {FILE}，重启自动读回
          不想手填就先点【扫描】——扫到的程序号会自动并入清单（扫描只加载、不运行）。
          建好的清单里，选中哪个点【设为点动服务】，它就是下面点动要用的那个常驻程序。

第 1 步   点【一 键 就 绪】
          自动完成：连接 → 有报警就清报警 → 伺服上电 → 拉起点动服务
          （加载并运行点动服务程序，它会挂在 WAIT 上待命）。
          日志出现「就绪完成」就可以动了。
          没设点动服务程序也不会报错 —— 只是 J1~J6 的 ± 按钮不可用，跑程序照常。

第 2 步   手动挪关节：④ 卡 J1~J6 每行【−】【+】
          右上角先选【步长】（0.1 / 0.5 / 1 / 5 / 10°），点一下走一个步长。
          这就是示教器那 12 个 J± 键的等价物；轴在动时坐标变蓝、显示「● 在动」。
          速度由 ⑤ 卡的「速度 %」决定：5% ≈ 1.3°/s（慢慢挪），100% ≈ 60~90°/s。

第 3 步   记录点位：⑥ 卡
          摇到位 → 点【记录当前点】→ 起个名字（如 P1）。存进 {POINTS}，重启自动读回。
          选中某个点会显示「当前 → 该点」各轴差多少；点【去这个点】就自己走回去。
          · 「去这个点」真的会动，会先弹一张各轴位移的确认表；
          · Modbus 读不到示教器上的点位，所以点位是**本软件自己记**的；
          · 想换名字点【改名】，不要了点【删除】。

第 4 步   先【测试跑】，再【执行】
          · 测试跑：强制 5% 低速 + **跑 N 秒自动停**（右上角选 2/3/5/10 秒…）
                    + 越界熔断（任一关节位移超上限也立即停）。
                    第一次跑新程序、试点位就用它；跑完**不**自动恢复点动（停在现场便于检查）。
          · 执行：用设定速度、**不做越界检测**，直接一次跑完
                  （作业程序本就要让轴大幅运动，14:52 那次就是这样被误掐停的）。
                  跑完按勾选自动恢复点动服务，可以接着点动，中间不用再来一遍。
          两者都是**一键下发**，所以手要一直放在急停上。
          · 去点位同样是"不做越界检测"（6 轴一起走，位移本来就大），
            只保报警 / 急停 / 通信 / 超时四道底线。

出任何异常 → 点【紧 急 停 止】（或直接按 Esc）
          任何时候都可用：撤点动触发位 + 下发停止字 0x1005。
────────────────────────────────────────
关于"示教器的 12 个 J± 命令"
  已经把 Modbus 能枚举的指令位翻到底了（40101 Bit0~Bit13），**没有**原生点动命令；
  示教器走的是控制器内部链路（ETH3A），PC 在 ETH3B，抓不到也不该抓。
  ⇒ 本项目不再试图去"获取"它，改用**等价实现**：12 个 ± 按钮 + 控制器上的常驻程序。
  「去这个点」也复用同一条通道（写 40139~44 六个绝对角 → 置 40135.Bit0）。
────────────────────────────────────────
安全提醒
  · 真机会动，动手前确认作业区没人
  · 第一次先用 0.1° 步长、5% 速度、2 秒测试跑试
  · 大位移（去点位）先降速，手放急停
  · 软件熔断是第二道防线，不是唯一防线
  · 熔断武装期间 ± 按钮会被锁住（防误触覆盖保护阈值）
  · 急停（Esc）**绝不**自动恢复任何东西 —— 必须人工确认后重新【一键就绪】
""".replace("{FILE}", DEFAULT_FILE).replace("{POINTS}", POINT_FILE)


class MainWindow(QMainWindow):
    def __init__(self, host: str = "192.168.1.12", log_path: str | None = None,
                 store_path: str | None = None, points_path: str | None = None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setStyleSheet(qss())
        self.setMinimumSize(1240, 800)
        self.resize(1400, 950)

        self.q: queue.Queue = queue.Queue()
        self.w = RobotWorker(self.q, log_path=log_path)
        self.w.start()

        self.connected = False
        self.link_state = "off"          # off / on / retry
        self.gate = False
        self.manual_warned = False
        self.fuse: dict | None = None
        self.scanning = False
        self._prev_j: list[float] | None = None      # 上一帧关节角（用来判"哪个轴在动"）
        self._mov_until = [0.0] * 6                  # 每轴"在动"指示的到期时刻
        self._closing = False
        self._log_path = log_path

        #: 程序清单（用户自己维护、落盘）—— **绝不猜程序号 / 程序名**。
        #: 里面存的既是"要跑哪些程序"，也是"哪个程序是点动服务程序"。
        self.store_path = (os.path.abspath(store_path) if store_path
                           else os.path.join(PROJECT_ROOT, DEFAULT_FILE))
        self.store = ProgramStore(self.store_path)

        #: 点位清单（**示教点**：摇到位 → 记录当前点 → 以后一键去这个点）。
        #: Modbus 没有任何"读示教点"的接口，所以只能**上位机自己记**（方案 A）。
        self.points_path = (os.path.abspath(points_path) if points_path
                            else os.path.join(PROJECT_ROOT, POINT_FILE))
        self.points = PointStore(self.points_path)
        self._last_j: list[float] | None = None      # 最近一帧关节角（录点的数据源）
        self._pt_buttons: list = []                  # 点位卡里需要"有连接才可用"的按钮

        self._build()
        self._wire()
        self._gate_refresh()
        self._refresh_prog_combo()
        # 启动即按**清单里的真实状态**渲染 ③ 卡 —— 否则它会一直停在 `_build` 里的初始
        # 占位文本，用户就看不到「未设置 → J1~J6 点不动」这条关键警告（本次实测踩到）。
        self._on_jog_status("unknown" if self.store.jog > 0 else "unset", self.store.jog)
        # 把上次记住的"点动服务程序"告诉 worker（0 = 没设过 → 不用发，worker 默认就是 unset）
        if self.store.jog > 0:
            self.w.post("jog_prog", self.store.jog)

        self.log("info", "控制台已启动。最简单：点【一 键 就 绪】→ 用 J1~J6 的 ± 按钮点动。")
        self.log("info", "不熟悉就点右上角【怎么用？】看四步说明。")
        if self.store.jog > 0:
            self.log("info", "点动服务程序（上次记住的）：%s" % self.store.label(self.store.jog))
        else:
            self.log("warn", "还没指定**点动服务程序** —— 在 ⑤ 卡【程序清单】里[添加]你的程序号，"
                             "选中后点[设为点动服务]（点动必须靠控制器上一个常驻程序）。")
        self.log("info", "程序清单文件：%s（可随时增删，重启自动读回）" % self.store_path)
        self.log("info", "跑作业程序：⑤ 卡【扫描】列出控制器上真实存在的程序（自动进清单）→ "
                         "选一个 → 先【测试跑】（低速+定时），再【执行】。")
        self.log("info", "记录点位：⑥ 卡 —— 用 ± 把机器人摇到位，点【记录当前点】，"
                         "以后选中它点【去这个点】就自己走回去（已记 %d 个，存 %s）。"
                 % (len(self.points.items()), self.points_path))
        if log_path:
            self.log("info", "审计日志落盘：%s" % log_path)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._pump)
        self.timer.start(40)

    # ================================================================ 构建
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 8)
        root.setSpacing(10)

        # ---- 顶栏 ----
        head = QHBoxLayout()
        head.setSpacing(10)
        t = label("埃夫特 EFORT RP-2")
        t.setFont(QFont(FONT, FS["h1"], QFont.Weight.Bold))
        head.addWidget(t)
        sub = label(SUBTITLE, "cardHint")
        sub.setContentsMargins(0, 6, 0, 0)
        head.addWidget(sub)
        head.addStretch(1)
        self.p_link = label("●  未连接")
        self.p_link.setFont(QFont(FONT, FS["h2"], QFont.Weight.Bold))
        head.addWidget(self.p_link)
        b_help = button("怎么用？", "ghost", self)
        b_help.clicked.connect(self._show_help)
        head.addWidget(b_help)
        root.addLayout(head)

        # ---- 主体：左窄（控制）/ 右宽（点动+监视）----
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(M.gap)
        lv.addWidget(self._card_conn())
        lv.addWidget(self._card_perm())
        lv.addWidget(self._card_backend())
        lv.addWidget(self._card_estop())
        lv.addStretch(1)
        left.setMinimumWidth(360)
        left.setMaximumWidth(460)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(M.gap)
        rv.addWidget(self._card_jog())
        rv.addWidget(self._card_points())
        rv.addWidget(self._card_run())
        rv.addWidget(self._card_status())
        rv.addWidget(self._card_log(), 1)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([400, 940])
        root.addWidget(split, 1)

        # ---- 底栏 ----
        bar = QHBoxLayout()
        self.l_stat = label("就绪 · 未连接（只读模式）", "hint")
        bar.addWidget(self.l_stat)
        bar.addStretch(1)
        self.l_tick = label("", "hint")
        bar.addWidget(self.l_tick)
        root.addLayout(bar)

    # ---------------------------------------------------------- ① 连接
    def _card_conn(self) -> Card:
        c = Card("① 连接", "只读")
        b = c.body_layout
        row = QHBoxLayout()
        row.addWidget(label("控制器 IP", "muted"))
        self.e_host = QLineEdit("192.168.1.12")
        self.e_host.setMaxLength(15)
        row.addWidget(self.e_host, 1)
        b.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(label("端口", "muted"))
        self.e_port = QSpinBox()
        self.e_port.setRange(1, 65535)
        self.e_port.setValue(502)
        self.e_port.setFixedWidth(84)
        row2.addWidget(self.e_port)
        row2.addWidget(label("从站号 1", "hint"))
        row2.addStretch(1)
        b.addLayout(row2)

        row3 = QHBoxLayout()
        self.b_conn = button("连  接", "primary", self)
        self.b_disc = button("断开", "ghost", self)
        self.b_base = button("设基线", "ghost", self)
        for w in (self.b_conn, self.b_disc, self.b_base):
            row3.addWidget(w)
        row3.addStretch(1)
        b.addLayout(row3)
        self.b_disc.setEnabled(False)
        self.b_base.setEnabled(False)
        return c

    # ---------------------------------------------------------- ② 使能与安全
    def _card_perm(self) -> Card:
        c = Card("② 使能与安全", "写入")
        b = c.body_layout
        self.b_ready = button("一 键 就 绪", "ok", self, "ready")
        b.addWidget(self.b_ready)
        b.addWidget(hint("自动完成：连接 → 有报警就清报警 → 伺服上电 → 拉起点动服务（约 2 秒）"))

        self.c_gate = QCheckBox("现场安全确认：作业区无人、有急停按钮、速度已降")
        b.addWidget(self.c_gate)

        g = QHBoxLayout()
        self.b_son = button("伺服上电", "ok", self)
        self.b_re = button("重新吸合", "ghost", self)
        self.b_soff = button("取消使能", "ghost", self)
        for w in (self.b_son, self.b_re, self.b_soff):
            g.addWidget(w)
        g.addStretch(1)
        b.addLayout(g)

        self.b_clr = button("清报警 (0x1009)", "warn", self)
        b.addWidget(self.b_clr)
        b.addWidget(hint("吸合约 0.55s 置位；已持 0x1001 须先造下降沿才能重吸合。"))

        self.gated = [self.b_son, self.b_re, self.b_soff, self.b_clr]
        return c

    # ---------------------------------------------------------- ③ 点动服务程序
    def _card_backend(self) -> Card:
        c = Card("③ 点动服务程序", "控制器上的常驻程序")
        b = c.body_layout

        self.l_jprog = label("○ 点动服务程序未设置", "hint")
        self.l_jprog.setFont(QFont(FONT, FS["small"], QFont.Weight.Bold))
        b.addWidget(self.l_jprog)

        g = QHBoxLayout()
        self.b_ptest = button("参数区写回自检", "ghost", self)
        self.b_tpl = button("程序模板", "ghost", self)
        g.addWidget(self.b_ptest)
        g.addWidget(self.b_tpl)
        g.addStretch(1)
        b.addLayout(g)
        self.gated.append(self.b_ptest)
        self.b_tpl.clicked.connect(self._show_template)

        b.addWidget(hint("点动没有原生 Modbus 命令 —— 只能靠控制器上一个常驻程序来回执。"
                         "它的号/名只有你知道 → 在 ⑤ 卡【程序清单】里自己 [添加]，"
                         "选中后点 [设为点动服务]。【程序模板】给的是官方逐字录入步骤。"))
        b.addWidget(hint("参数区（官方手册 15.4.5）：40139~44 = J1~J6 目标绝对角 ×100（0.01° 分辨率）"
                         "→ 40135.Bit0 触发；程序回执 40035.Bit0 = 完成。"))
        return c

    # ---------------------------------------------------------- 急停
    def _card_estop(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.b_estop = button("紧 急 停 止", "danger", self, "estop")
        self.b_estop.setToolTip("直接下发 40101 = 0x1005（并撤点动触发位）· 快捷键 Esc")
        v.addWidget(self.b_estop)
        v.addWidget(hint("直接下发 40101 = 0x1005，任何时候可用（快捷键 Esc）"))
        return w

    # ---------------------------------------------------------- ④ 关节
    def _card_jog(self) -> Card:
        from .widgets import JointRow
        c = Card("④ 关节", "6 个坐标 · J1~J6 的 ± 控制（对应示教器那 12 个键）")
        b = c.body_layout

        head = QHBoxLayout()
        self.l_moving = label("当前在动：静止", "monoDim")
        self.l_moving.setFont(QFont(FONT, FS["h2"], QFont.Weight.Bold))
        head.addWidget(self.l_moving)
        head.addStretch(1)
        head.addWidget(label("步长", "muted"))
        self.cb_step = QComboBox()
        self.cb_step.addItems(["0.1°", "0.5°", "1°", "5°", "10°"])
        self.cb_step.setCurrentIndex(2)
        self.cb_step.setFixedWidth(74)
        self.cb_step.setToolTip("点一下 ± 按钮走的度数")
        head.addWidget(self.cb_step)
        head.addSpacing(10)
        head.addWidget(label("其他轴限", "muted"))
        self.sp_olim = QDoubleSpinBox()
        self.sp_olim.setRange(0.1, 180.0)
        self.sp_olim.setSingleStep(0.5)
        self.sp_olim.setDecimals(1)
        self.sp_olim.setValue(1.5)
        self.sp_olim.setSuffix(" °")
        self.sp_olim.setFixedWidth(86)
        self.sp_olim.setToolTip("点动时「其他轴」允许的漂移上限 —— 超了立即停")
        head.addWidget(self.sp_olim)
        b.addLayout(head)

        self.jrows: list[JointRow] = []
        for i in range(6):
            jr = JointRow(i)
            jr.setFixedHeight(26)          # 别让布局把行走压到比 ± 按钮还矮（会裁到按钮）
            b.addWidget(jr)
            self.jrows.append(jr)
        b.addWidget(hint("【−】/【+】= 该轴走一个步长；「Δ」= 自【设基线】以来的位移；在动时坐标变蓝。"))
        return c

    # ---------------------------------------------------------- ⑥ 点位（记录示教点）
    def _card_points(self) -> Card:
        c = Card("⑥ 点位", "摇到位 → 【记录当前点】→ 以后一键【去这个点】· 存 %s" % POINT_FILE)
        b = c.body_layout

        self.lw_pt = QListWidget()
        self.lw_pt.setObjectName("ptList")
        self.lw_pt.setFixedHeight(104)
        self.lw_pt.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.lw_pt.setToolTip("每行：点名 ｜ J1~J6（记录当时的绝对角）｜ 记录时间")
        self.lw_pt.currentItemChanged.connect(lambda *_: self._on_pt_sel())
        b.addWidget(self.lw_pt)

        row = QHBoxLayout()
        self.b_ptr = button("记录当前点", "primary", self)
        self.b_ptr.setToolTip("把机器人此刻的 J1~J6 角度存成一个点位。\n"
                              "只读当前角度，不产生任何运动。")
        self.b_ptg = button("去这个点", "warn", self)
        self.b_ptg.setToolTip("让机器人走到选中的点位 —— 真的会动！\n"
                              "需先勾选现场安全确认；速度用 ⑤ 卡那个「速度 %」。")
        self.b_ptn = button("改名", "ghost", self)
        self.b_ptd = button("删除", "ghost", self)
        for w in (self.b_ptr, self.b_ptg, self.b_ptn, self.b_ptd):
            row.addWidget(w)
        row.addStretch(1)
        b.addLayout(row)

        self.l_pt = label("", "hint")
        self.l_pt.setWordWrap(True)
        b.addWidget(self.l_pt)
        b.addWidget(hint("「去这个点」走的是和点动**完全相同**的通道（6 轴一起走）"
                         "—— 大位移请先把速度调低、手放急停。"))
        self._refresh_pt_list()
        return c

    # ---- ⑥ 点位：数据与交互 ----
    def _pt_no(self) -> int:
        it = self.lw_pt.currentItem() if hasattr(self, "lw_pt") else None
        return int(it.data(Qt.ItemDataRole.UserRole)) if it is not None else 0

    def _refresh_pt_list(self, keep: int | None = None) -> None:
        """按清单重建点位列表（点位号存在 item data 里，不从文本抠数字）。"""
        if not hasattr(self, "lw_pt"):
            return
        keep = self._pt_no() if keep is None else keep
        self.lw_pt.clear()
        for it in self.points.items():
            js = it["joints"]
            txt = "%s ｜ %s" % (it["name"], " ".join("J%d %+.2f" % (i + 1, js[i])
                                                     for i in range(6)))
            if it["ts"]:
                txt += " ｜ %s" % it["ts"]
            row = QListWidgetItem(txt)
            row.setData(Qt.ItemDataRole.UserRole, it["no"])
            self.lw_pt.addItem(row)
            if it["no"] == keep:
                self.lw_pt.setCurrentItem(row)
        if self.lw_pt.currentRow() < 0 and self.lw_pt.count():
            self.lw_pt.setCurrentRow(0)
        self._on_pt_sel()

    def _on_pt_sel(self) -> None:
        """选中点位 → 显示各轴位移预演（当前 → 目标）。"""
        no = self._pt_no()
        it = self.points.get(no) if no else None
        if it is None or self._last_j is None:
            self.l_pt.setText("还没记录点位：用 J1~J6 的 ± 把机器人摇到位，点【记录当前点】。"
                              if it is None else "还没读到机器人姿态 —— 先点【一键就绪】。")
            self.l_pt.setStyleSheet("color:%s;" % P.dim)
        else:
            d = [it["joints"][i] - self._last_j[i] for i in range(6)]
            maxd = max(abs(x) for x in d)
            same = maxd < 0.02
            way = ("当前 → %s：" % it["name"]
                   + "  ".join("J%d %+.2f→%+.2f" % (i + 1, self._last_j[i], it["joints"][i])
                               for i in range(6)))
            back = "  【已在该点】" if same else "  最大位移 %.2f°" % maxd
            self.l_pt.setText(("✅ " if same else "→ ") + way + back)
            self.l_pt.setStyleSheet("color:%s;" % (P.ok if same else P.warn))
        self._pt_refresh_enabled()

    def _pt_refresh_enabled(self) -> None:
        """点位按钮的可用性：去点位 = 已连接 + 现场安全确认 + 熔断未武装。"""
        if not hasattr(self, "b_ptg"):
            return
        has = self._pt_no() > 0
        self.b_ptr.setEnabled(bool(self.connected) and self._last_j is not None)
        self.b_ptn.setEnabled(has)
        self.b_ptd.setEnabled(has)
        self.b_ptg.setEnabled(has and self.connected and self.gate and self.fuse is None)

    def _on_pt_record(self) -> None:
        """记录当前点 —— 纯读角度，不产生任何运动。"""
        if not self.connected or self._last_j is None:
            self.log("warn", "记录点位被拒：还没连上控制器 / 还没读到当前姿态。")
            self.log("warn", "  处理：点【一键就绪】。")
            return
        no = self.points.next_no()
        name, ok = QInputDialog.getText(
            self, "记录当前点", "给这个点起个名字（方便以后认得它）：",
            text="P%d" % no)
        if not ok:
            self.log("info", "已取消：记录点位")
            return
        ok2, msg = self.points.add(name, self._last_j)
        self.log("ok" if ok2 else "err", msg)
        if ok2:
            self._refresh_pt_list(keep=self.points.next_no() - 1)
            self.log("info", "提示：想让它自动走回来，选中它点【去这个点】（会真的动）。")

    def _on_pt_rename(self) -> None:
        no = self._pt_no()
        it = self.points.get(no) if no else None
        if it is None:
            self.log("warn", "改名被拒：先在⑥卡里选中一个点位")
            return
        name, ok = QInputDialog.getText(self, "点位改名", "新名字：", text=it["name"])
        if not ok or not name.strip():
            return
        ok2, msg = self.points.update(no, name=name)
        self.log("ok" if ok2 else "err", msg)
        if ok2:
            self._refresh_pt_list(keep=no)

    def _on_pt_del(self) -> None:
        no = self._pt_no()
        it = self.points.get(no) if no else None
        if it is None:
            self.log("warn", "删除被拒：先在⑥卡里选中一个点位")
            return
        if QMessageBox.question(self, "删除点位", "确定删除点位「%s」？" % it["name"]) \
                != QMessageBox.StandardButton.Yes:
            return
        ok2, msg = self.points.remove(no)
        self.log("ok" if ok2 else "err", msg)
        if ok2:
            self._refresh_pt_list()

    def _on_pt_goto(self) -> None:
        """去这个点 —— 真的会动，所以先弹预演确认。"""
        no = self._pt_no()
        it = self.points.get(no) if no else None
        if it is None:
            self.log("warn", "去点位被拒：先在⑥卡里选中一个点位")
            return
        if not self.connected:
            self.log("warn", "去点位被拒：尚未连接")
            return
        if not self.gate:
            self.log("warn", "已被拦截：请先勾选【现场安全确认】")
            return
        if self.fuse is not None:
            self.log("warn", "去点位被拒：上一次动作仍在进行中，等它结束再点。")
            return
        if self._last_j is None:
            self.log("warn", "去点位被拒：还没读到当前姿态")
            return
        d = [it["joints"][i] - self._last_j[i] for i in range(6)]
        maxd = max(abs(x) for x in d)
        if maxd < 0.02:
            self.log("warn", "已在点位「%s」上（最大差 %.3f°）—— 不需要动作。" % (it["name"], maxd))
            return
        detail = "\n".join("J%d   %+9.3f  →  %+9.3f    Δ %+.3f"
                           % (i + 1, self._last_j[i], it["joints"][i], d[i]) for i in range(6))
        detail += "\n\n最大单轴位移 %.2f°　速度 %d%%" % (maxd, self.sp_speed.value())
        if not self._confirm_goto(it["name"], detail):
            self.log("info", "已取消：去点位「%s」" % it["name"])
            return
        self.w.post("goto", it["name"], list(it["joints"]))
        self.log("warn", "→ 去点位「%s」已下发（最大位移 %.2f°）" % (it["name"], maxd))

    def _confirm_goto(self, name: str, detail: str) -> bool:
        """去点位的二次确认（各轴位移明细）。抽成方法 → 自检里可覆盖，免弹窗卡住。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("去点位 · 确认")
        box.setText("机器人将真的运动到点位「%s」。" % name)
        box.setInformativeText("先确认作业区无人、手已放在急停上。")
        box.setDetailedText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Ok

    # ---------------------------------------------------------- ⑤ 程序清单（测试跑 / 执行）
    def _card_run(self) -> Card:
        c = Card("⑤ 程序清单 · 测试跑 / 执行",
                 "清单存 %s（可增删，重启读回）· 扫到的自动加进来" % DEFAULT_FILE)
        b = c.body_layout

        # --- 选程序 + 清单操作（同一行，省高度）---
        row = QHBoxLayout()
        row.addWidget(label("程序", "muted"))
        self.cb_prog = QComboBox()
        self.cb_prog.setMinimumWidth(150)
        self.cb_prog.setToolTip("清单里的程序，显示成「号 · 名字」。\n"
                                "号 = 下发给控制器 40104 的数字；名字只是给你自己认（可以是英文）。")
        row.addWidget(self.cb_prog, 1)
        self.b_padd = button("添加", "ghost", self)
        self.b_pedit = button("改名", "ghost", self)
        self.b_pedit.setToolTip("改程序号 / 名字 / 备注")
        self.b_pdel = button("删除", "ghost", self)
        self.b_pjog = button("设为点动服务", "ghost", self)
        self.b_pjog.setToolTip("把当前选中的程序声明为「点动服务程序」\n"
                               "（点动必须靠它在控制器上常驻回执）")
        for w in (self.b_padd, self.b_pedit, self.b_pdel, self.b_pjog):
            row.addWidget(w)
        b.addLayout(row)

        # --- 扫描（可选）+ 状态（同一行）---
        row2 = QHBoxLayout()
        row2.addWidget(label("扫描范围", "muted"))
        self.sp_lo = QSpinBox()
        self.sp_lo.setRange(1, 99999)
        self.sp_lo.setValue(1)
        self.sp_lo.setFixedWidth(72)
        row2.addWidget(self.sp_lo)
        row2.addWidget(label("~", "muted"))
        self.sp_hi = QSpinBox()
        self.sp_hi.setRange(1, 99999)
        self.sp_hi.setValue(600)
        self.sp_hi.setFixedWidth(72)
        row2.addWidget(self.sp_hi)
        self.b_scan = button("扫描", "ghost", self)
        row2.addWidget(self.b_scan)
        self.b_scan_abort = button("中止", "ghost", self)
        self.b_scan_abort.setEnabled(False)
        row2.addWidget(self.b_scan_abort)
        self.l_scan = label("未扫描（只加载、不运行，机器人不动）", "hint")
        row2.addWidget(self.l_scan, 1)
        b.addLayout(row2)

        # --- 参数：速度 / 测试时长 / 越界上限 ---
        row3 = QHBoxLayout()
        row3.addWidget(label("速度 %", "muted"))
        self.sp_speed = QSpinBox()
        self.sp_speed.setRange(1, 100)
        self.sp_speed.setValue(5)
        self.sp_speed.setSuffix(" %")
        self.sp_speed.setFixedWidth(80)
        row3.addWidget(self.sp_speed)
        self.b_speed = button("应用", "primary", self)
        row3.addWidget(self.b_speed)
        row3.addSpacing(10)
        row3.addWidget(label("测试时长", "muted"))
        self.cb_secs = QComboBox()
        self.cb_secs.setEditable(True)
        self.cb_secs.addItems(["2", "3", "5", "10", "30", "60"])
        self.cb_secs.setCurrentText("3")
        self.cb_secs.setFixedWidth(54)
        self.cb_secs.setToolTip("【测试跑】跑多少秒后自动停 —— 可自己输数字（0.5 ~ 3600）")
        row3.addWidget(self.cb_secs)
        row3.addWidget(label("秒", "muted"))
        row3.addSpacing(10)
        row3.addWidget(label("越界上限", "muted"))
        self.sp_testlim = QDoubleSpinBox()
        self.sp_testlim.setRange(1.0, 360.0)
        self.sp_testlim.setSingleStep(5.0)
        self.sp_testlim.setDecimals(1)
        self.sp_testlim.setValue(45.0)
        self.sp_testlim.setSuffix(" °")
        self.sp_testlim.setFixedWidth(84)
        self.sp_testlim.setToolTip("【测试跑】时任一关节相对起点的位移超过它 → 立即停")
        row3.addWidget(self.sp_testlim)
        row3.addStretch(1)
        b.addLayout(row3)
        self.gated.append(self.b_speed)

        # --- 两个动作 + 自动恢复开关（同一行）---
        row4 = QHBoxLayout()
        self.b_test = button("▶ 测 试 跑", "warn", self)
        self.b_test.setToolTip("强制 5% 低速 + 跑够设定秒数自动停 + 越界熔断；跑完不自动恢复点动")
        self.b_exec = button("▶ 执  行", "ok", self)
        self.b_exec.setToolTip("生产速度；不做越界检测，直接一次跑完")
        self.b_stop = button("停  止", "warn", self)
        for w in (self.b_test, self.b_exec, self.b_stop):
            row4.addWidget(w)
        self.c_restore = QCheckBox("执行结束后自动恢复点动服务")
        self.c_restore.setChecked(True)
        row4.addSpacing(6)
        row4.addWidget(self.c_restore)
        row4.addStretch(1)
        b.addLayout(row4)
        self.gated += [self.b_test, self.b_exec, self.b_stop]

        self.l_fuse = label("○ 熔断未武装", "hint")
        self.l_fuse.setFont(QFont(FONT, FS["small"], QFont.Weight.Bold))
        b.addWidget(self.l_fuse)
        return c

    # ---------------------------------------------------------- ⑥ 状态
    def _card_status(self) -> Card:
        c = Card("⑥ 状态", "40001~40006")
        b = c.body_layout
        grid = QGridLayout()
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(4)
        self.pills: dict[str, Pill] = {}
        for i, (key, text, kind) in enumerate(PILLS):
            pl = Pill(text, kind)
            grid.addWidget(pl, i // 3, i % 3)
            self.pills[key] = pl
        b.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.v_speed = self._stat(row, "运行速度", "0")
        self.v_prog = self._stat(row, "程序号", "--")
        self.v_alarm = self._stat(row, "报警码", "--")
        row.addSpacing(6)
        self.v_cmd = self._stat(row, "40101", "--")
        self.v_jog = self._stat(row, "40139~44", "--")
        row.addStretch(1)
        b.addLayout(row)

        self.l_alarm = label("", "hint")
        b.addWidget(self.l_alarm)
        return c

    # ---------------------------------------------------------- ⑦ 日志
    def _card_log(self) -> Card:
        c = Card("⑦ 事件日志", "审计")
        b = c.body_layout
        self.log_view = LogView()
        b.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        b_clear = button("清空", "ghost", self)
        b_clear.clicked.connect(lambda: self.log_view.clear())
        row.addWidget(b_clear)
        if self._log_path:
            b_open = button("打开日志文件", "ghost", self)
            b_open.clicked.connect(self._open_log_file)
            row.addWidget(b_open)
        row.addStretch(1)
        b.addLayout(row)
        return c

    def _stat(self, row: QHBoxLayout, name: str, init: str) -> QLabel:
        row.addWidget(label(name, "hint"))
        v = label(init, "mono")
        row.addWidget(v)
        return v

    # ================================================================ 事件
    def _wire(self) -> None:
        self.b_conn.clicked.connect(self._on_connect)
        self.b_disc.clicked.connect(self._on_disconnect)
        self.b_base.clicked.connect(lambda: self.w.post("set_base"))
        self.b_ready.clicked.connect(self._on_ready)
        self.c_gate.toggled.connect(self._on_gate)
        self.b_son.clicked.connect(lambda: self._gated(self.w.post, "servo_on"))
        self.b_soff.clicked.connect(lambda: self._gated(self.w.post, "servo_off"))
        self.b_re.clicked.connect(lambda: self._gated(self.w.post, "servo_reengage"))
        self.b_clr.clicked.connect(lambda: self._gated(self.w.post, "clear_alarm"))
        self.b_ptest.clicked.connect(lambda: self._gated(self.w.post, "param_test"))
        self.b_speed.clicked.connect(
            lambda: self._gated(self.w.post, "speed", self.sp_speed.value()))
        self.b_scan.clicked.connect(self._on_scan)
        self.b_scan_abort.clicked.connect(lambda: self.w.post("scan_cancel"))
        self.b_padd.clicked.connect(self._on_prog_add)
        self.b_pedit.clicked.connect(self._on_prog_edit)
        self.b_pdel.clicked.connect(self._on_prog_del)
        self.b_pjog.clicked.connect(self._on_set_jog)
        self.b_test.clicked.connect(lambda: self._on_run("test"))
        self.b_exec.clicked.connect(lambda: self._on_run("run"))
        self.c_restore.toggled.connect(lambda v: self.w.post("auto_restore", bool(v)))
        self.b_stop.clicked.connect(lambda: self.w.post("stop"))
        self.b_estop.clicked.connect(self._on_estop)
        for jr in self.jrows:
            jr.stepRequested.connect(self._on_jog)
        self.b_ptr.clicked.connect(self._on_pt_record)
        self.b_ptg.clicked.connect(self._on_pt_goto)
        self.b_ptn.clicked.connect(self._on_pt_rename)
        self.b_ptd.clicked.connect(self._on_pt_del)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._on_estop)

    # ---------------------------------------------------------- 交互
    def _gated(self, fn, *args) -> None:
        if not self.gate:
            self.log("warn", "已被拦截：请先勾选【现场安全确认】")
            return
        fn(*args)

    def _on_gate(self, checked: bool) -> None:
        self.gate = bool(checked)
        self.log("warn" if self.gate else "info",
                 "【现场安全确认】已勾选 → 写入类操作解锁" if self.gate
                 else "【现场安全确认】已取消 → 写入类操作锁定")
        self._gate_refresh()

    def _gate_refresh(self) -> None:
        on = self.gate and self.connected
        for b in getattr(self, "gated", []):
            b.setEnabled(on)
        self._refresh_jog_enabled()

    def _refresh_jog_enabled(self) -> None:
        """熔断武装期间锁住点动按钮（防覆盖保护阈值）。"""
        on = self.gate and self.connected and self.fuse is None
        for jr in getattr(self, "jrows", []):
            for b in jr.buttons:
                b.setEnabled(on)
        self._pt_refresh_enabled()          # ⑥ 卡的【去这个点】同理

    def _on_connect(self) -> None:
        host = self.e_host.text().strip() or "192.168.1.12"
        self.b_conn.setEnabled(False)
        self.l_stat.setText("正在连接 %s:%d …" % (host, self.e_port.value()))
        self.w.post("connect", host, self.e_port.value())

    def _on_disconnect(self) -> None:
        self.w.post("disconnect")

    def _on_ready(self) -> None:
        if not self.gate:
            self.c_gate.setChecked(True)          # 一键就绪同时视为一次现场安全确认
        host = self.e_host.text().strip() or "192.168.1.12"
        self.b_ready.setEnabled(False)
        self.b_ready.setText("就 绪 中 …")
        self.w.post("ready", host, self.e_port.value())

    def _on_jog(self, axis: int, direction: float) -> None:
        """J1~J6 的 ± 步进 —— 就是示教器那 12 个控制键的等价物。"""
        if not self.connected:
            self.log("warn", "点动被拒：尚未连接")
            return
        if not self.gate:
            self.log("warn", "已被拦截：请先勾选【现场安全确认】")
            return
        step = self._step_size()
        if step <= 0:
            return
        self.w.post("jog", axis, direction * step, self.sp_olim.value())

    def _step_size(self) -> float:
        try:
            return float(self.cb_step.currentText().rstrip("°").strip())
        except ValueError:
            self.log("err", "步长解析失败")
            return 0.0

    # ---------------------------------------------------------- 程序清单
    def _refresh_prog_combo(self, keep: int | None = None) -> None:
        """按清单重建下拉（显示「号 · 名字」）。

        ⚠ 程序号存在 item **data** 里，而不是从文本里抠数字 ——
          名字里带数字（如 `PICK_A2`）时才不会解析错。
        """
        items = self.store.items()
        self.cb_prog.blockSignals(True)
        self.cb_prog.clear()
        for it in items:
            self.cb_prog.addItem(self.store.label(it["no"]), it["no"])
        if keep is None:
            keep = self.store.jog or (items[0]["no"] if items else 0)
        if keep:
            i = self.cb_prog.findData(keep)
            if i >= 0:
                self.cb_prog.setCurrentIndex(i)
        self.cb_prog.blockSignals(False)
        for b in (self.b_pedit, self.b_pdel, self.b_pjog):
            b.setEnabled(bool(items))

    def _cur_prog(self) -> int:
        d = self.cb_prog.currentData()
        return int(d) if d else 0

    def _on_prog_add(self) -> None:
        d = ProgDialog(self, title="添加程序")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        no, name, note = d.values()
        ok, msg = self.store.add(no, name, note)
        self.log("ok" if ok else "err", msg)
        self._refresh_prog_combo(keep=no)
        if ok and self.store.jog == 0:
            self.log("info", "  提示：若这个就是点动要用的常驻程序，点 [设为点动服务] 声明它。")

    def _on_prog_edit(self) -> None:
        no = self._cur_prog()
        it = self.store.get(no)
        if it is None:
            self.log("warn", "先在下拉里选一个程序")
            return
        d = ProgDialog(self, no=it["no"], name=it["name"], note=it["note"], title="改名 / 备注")
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        no2, name, note = d.values()
        if no2 != it["no"]:
            was_jog = self.store.jog == it["no"]
            self.store.remove(it["no"])
            ok, msg = self.store.add(no2, name, note)
            if was_jog:
                self.store.set_jog(no2)
                msg += "（点动服务标记已跟着改成 %d）" % no2
            self.log("ok" if ok else "err", msg)
            self._refresh_prog_combo(keep=no2)
            self.w.post("jog_prog", self.store.jog)
            return
        ok, msg = self.store.update(no2, name, note)
        self.log("ok" if ok else "err", msg)
        self._refresh_prog_combo(keep=no2)

    def _on_prog_del(self) -> None:
        no = self._cur_prog()
        if not no:
            self.log("warn", "先在下拉里选一个程序")
            return
        if QMessageBox.question(
                self, "删除清单条目",
                "从清单里删除 %s ？\n\n（只删本地这一条清单记录，"
                "不会动控制器上的程序文件）" % self.store.label(no),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        ok, msg = self.store.remove(no)
        self.log("ok" if ok else "err", msg)
        self._refresh_prog_combo()
        self.w.post("jog_prog", self.store.jog)

    def _on_set_jog(self) -> None:
        no = self._cur_prog()
        if not no:
            self.log("warn", "先在下拉里选一个程序，再点【设为点动服务】")
            return
        ok, msg = self.store.set_jog(no)
        self.log("ok" if ok else "err", msg)
        if not ok:
            return
        self._refresh_prog_combo(keep=no)
        self.w.post("jog_prog", no)          # 让 worker 重新验证并去掉旧的失败锁存

    def _on_scan(self) -> None:
        """扫描控制器上**真实存在**的程序号 —— 只加载、不运行，机器人不动。"""
        if not self.connected:
            self.log("warn", "扫描被拒：尚未连接（先点【连接】或【一键就绪】）")
            return
        if self.scanning:
            return
        self.scanning = True
        self.b_scan.setEnabled(False)
        self.b_scan.setText("扫描中…")
        self.b_scan_abort.setEnabled(True)
        self.w.post("scan", self.sp_lo.value(), self.sp_hi.value())

    def _on_scan_done(self, ok: bool, found: list, lo: int, hi: int) -> None:
        self._scan_ui_reset()
        if not ok:
            self.l_scan.setText("扫描已中止")
            self.l_scan.setStyleSheet("color:%s;" % P.dim)
            return
        added, _ = self.store.merge_scan([int(n) for n in found])
        self._refresh_prog_combo(keep=added[0] if added else None)
        if found:
            msg = ("扫描完成：%d~%d 命中 %d 个（新加进清单 %d 条）→ 下拉里选一个开跑"
                   % (lo, hi, len(found), len(added)))
            self.log("ok", "   " + msg + "　清单现共 %d 条" % len(self.store.items()))
        else:
            msg = ("扫描完成：%d~%d 一个都没找到 —— "
                   "确认示教器上确实建过程序、并且**保存过**" % (lo, hi))
            self.log("warn", "   " + msg)
        self.l_scan.setText(msg)
        self.l_scan.setStyleSheet("color:%s;" % (P.ok if found else P.warn))

    def _scan_ui_reset(self) -> None:
        self.scanning = False
        self.b_scan.setEnabled(True)
        self.b_scan.setText("扫描")
        self.b_scan_abort.setEnabled(False)

    def _on_scan_reject(self, reason: str) -> None:
        """worker 拒绝了扫描 —— 必须把界面从「扫描中…」解出来，否则按钮永远卡住。"""
        self._scan_ui_reset()
        self.l_scan.setText("扫描被拒：%s" % reason)
        self.l_scan.setStyleSheet("color:%s;" % P.warn)

    def _secs(self) -> float:
        """测试跑时长（秒）—— 界面上可下拉选、也可手输。"""
        try:
            v = float(self.cb_secs.currentText().strip())
        except ValueError:
            self.log("warn", "测试时长不是数字，按 3 秒处理")
            return 3.0
        return max(0.5, min(v, 3600.0))

    def _on_run(self, mode: str) -> None:
        """跑程序：`test` = 测试跑（5% 低速 + 定时 + 越界熔断）/ `run` = 执行（直接跑完）。

        两者都**一键下发**（用户要求"直接执行、中间不要停"）；
        保护来自：现场安全确认闸门 + 急停 + 报警/急停/通信熔断（执行档全部保留）。
        """
        if not self.gate:
            self.log("warn", "已被拦截：请先勾选【现场安全确认】")
            return
        prog = self._cur_prog()
        if not prog:
            self.log("err", "程序号无效 —— 先在【程序清单】里 [添加]，或点【扫描】把实有程序加进来")
            return
        lim = self.sp_testlim.value()
        secs = self._secs()
        if mode == "test":
            self.log("warn", "▶ 测试跑程序 %d：强制 5%% 低速 + 跑 %.1f 秒自动停 + 越界上限 %g°"
                     % (prog, secs, lim))
        else:
            self.log("warn", "▶ 执行程序 %d：生产速度，直接跑完；不做越界检测%s"
                     % (prog, "；结束后自动恢复点动服务" if self.c_restore.isChecked() else ""))
        self.w.post("run", mode, prog, lim, lim, secs)

    def _on_estop(self) -> None:
        self.w.post("estop")
        self.l_fuse.setText("○ 已急停")
        self.l_fuse.setStyleSheet("color:%s;" % P.danger)

    # ---------------------------------------------------------- 弹窗
    def _show_help(self) -> None:
        QMessageBox.information(self, "怎么用", HELP_TEXT)

    def _show_template(self) -> None:
        from .template import JOG_TEMPLATE
        box = QMessageBox(self)
        box.setWindowTitle("点动服务程序 · 完整代码")
        box.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        box.setText("在示教器上新建程序并录入以下代码（语法依据官方《C30 RPL 指令手册》"
                    "/《新版程序编辑器》操作手册）。\n展开「Show Details」查看，可直接选中复制。")
        box.setDetailedText(JOG_TEMPLATE)
        box.setStandardButtons(QMessageBox.StandardButton.Close)
        box.exec()

    def _open_log_file(self) -> None:
        if self._log_path and os.path.exists(self._log_path):
            os.startfile(os.path.dirname(self._log_path))       # noqa: S606
        else:
            self.log("warn", "日志文件尚未生成")

    # ================================================================ 消息泵
    def log(self, level: str, text: str) -> None:
        self.log_view.append_line(level, text, time.strftime("%H:%M:%S "))
        # ★ 窗口侧的话也要落审计文件（worker 独占文件句柄，这里只投递）——
        #   否则"人做了什么"（勾安全确认 / 记录点位 / 去点位 / 被拦截）在磁盘上查不到。
        try:
            self.w.post("ui_log", level, text)
        except Exception:                                          # noqa: BLE001
            pass

    def _pump(self) -> None:
        try:
            while True:
                self._dispatch(self.q.get_nowait())
        except queue.Empty:
            pass

    def _dispatch(self, m) -> None:
        kind = m[0]
        if kind == "log":
            self.log(m[1], m[2])
        elif kind == "conn":
            self._on_conn(m[1], m[2])
        elif kind == "ready_done":
            self.b_ready.setEnabled(self.connected)
            self.b_ready.setText("一 键 就 绪")
            sub = m[2] if len(m) > 2 else ""
            if m[1] and sub == "unset":
                self.log("warn", "就绪完成（连接 / 报警 / 伺服都好了）—— 但**还没指定点动服务程序**，"
                                 "J1~J6 的 ± 按钮暂不可用；跑程序不受影响。")
            elif m[1]:
                self.log("ok", "就绪完成：现在可以直接点 J1~J6 的点动按钮了 ✅")
            else:
                self.log("warn", "就绪未完成，请看上面日志")
        elif kind == "jog_status":
            self._on_jog_status(m[1], m[2])
        elif kind == "scan_progress":
            self.l_scan.setText("扫描中… %d/%d，已命中 %d 个" % (m[1], m[2], m[3]))
            self.l_scan.setStyleSheet("color:%s;" % P.warn)
        elif kind == "scan_done":
            self._on_scan_done(m[1], m[2], m[3], m[4])
        elif kind == "scan_reject":
            self._on_scan_reject(m[1])
        elif kind == "estop_done":
            pass
        elif kind == "snap":
            self._on_snap(m[1], m[2], m[3], m[4])

    def _on_conn(self, ok, info) -> None:
        if ok is None:
            self.link_state = "retry"
            self.p_link.setText("●  链路中断 · 自动重连中")
            self.p_link.setStyleSheet("color:%s;" % P.warn)
            self.l_stat.setText("轮询失败，正在自动重连…")
            return
        self.connected = bool(ok)
        self.link_state = "on" if ok else "off"
        if ok:
            self.p_link.setText("●  已连接（只读）")
            self.p_link.setStyleSheet("color:%s;" % P.ok)
            self.l_stat.setText(str(info))
            self.b_conn.setEnabled(False)
            self.b_disc.setEnabled(True)
            self.b_base.setEnabled(True)
        else:
            self.p_link.setText("●  未连接")
            self.p_link.setStyleSheet("color:%s;" % P.dim)
            self.l_stat.setText("就绪 · 未连接（只读模式）")
            self.b_conn.setEnabled(True)
            self.b_disc.setEnabled(False)
            self.b_base.setEnabled(False)
            self.b_ready.setEnabled(True)
        self._gate_refresh()

    def _on_jog_status(self, state: str, prog: int) -> None:
        txt = {
            "armed": ("● 点动服务 %d：运行中，挂起等指令 ✅\n  现在点 J1~J6 按钮就会动" % prog, P.ok),
            "ok": ("● 点动服务程序 %d：可加载（还没跑，点【一键就绪】拉起）" % prog, P.warn),
            "bad": ("× 点动服务程序 %d：控制器上不存在\n  → 去示教器建它（点【程序模板】拿步骤），"
                    "建好并保存后重新【一键就绪】" % prog, P.danger),
            "unset": ("× 点动服务程序未设置 → J1~J6 的 ± 按钮点不动就是因为它\n"
                      "  → 去 ⑤ 卡【添加】你在示教器上建好的那个程序号（不知道怎么写点左【程序模板】），"
                      "选中它点 [设为点动服务]，再点【一键就绪】。", P.danger),
        }.get(state, ("○ 点动服务程序 %d：未验证（点【一键就绪】验证并拉起）" % prog, P.dim))
        self.l_jprog.setText(txt[0])
        self.l_jprog.setStyleSheet("color:%s;" % txt[1])

    def _on_snap(self, s, d, maxd, fuse) -> None:
        b = s["bits"]
        if not b.get("auto", 0):
            if not self.manual_warned:
                self.manual_warned = True
                self.log("warn", "⚠ 控制器处于【手动】模式 —— 外部控制信号被禁用，"
                                 "点动/运行都不会生效（你只会看到指令被下发）。")
                self.log("warn", "  → 请在示教器把模式切到【自动】，再点【一键就绪】。")
        else:
            self.manual_warned = False

        for key, (lb, kind) in [(k, (self.pills[k], k2)) for k, _, k2 in PILLS]:
            lb.set_on(kind if b.get(key, 0) else None)

        self.v_speed.setText("%d%%" % s["speed"])
        self.v_prog.setText(str(s["prog"]))
        self.v_alarm.setText("%d/%d" % (s["alarm1"], s["alarm2"]))
        self.v_alarm.setStyleSheet(
            "color:%s;" % (P.danger if (s["alarm1"] or s["alarm2"]) else P.ok))
        tips = [ALARM_TEXT[c] for c in (s["alarm1"], s["alarm2"]) if c in ALARM_TEXT]
        if tips:
            self.l_alarm.setText("⚠ 报警 " + "；".join(tips) + " → 点【清报警】")
            self.l_alarm.setStyleSheet("color:%s;" % P.danger)
        elif s["alarm1"] or s["alarm2"]:
            self.l_alarm.setText("⚠ 有报警（本地无解释，请到示教器看报警详情）")
            self.l_alarm.setStyleSheet("color:%s;" % P.warn)
        else:
            self.l_alarm.setText("")
        self.v_cmd.setText("0x%04X" % s["cmd_word"] if s["cmd_word"] is not None else "--")
        ro = s.get("jog_ro")
        self.v_jog.setText(
            " ".join("%+.1f" % a for a in [EfortClient.decode_angle(x) for x in ro[:6]])
            if ro else "--")

        was_fuse = self.fuse
        self.fuse = fuse
        if (was_fuse is None) != (fuse is None):
            self._refresh_jog_enabled()

        # ---- 关节行：6 个坐标 + "哪个轴在动" ----
        now = time.time()
        j = s["joints"]
        self._last_j = list(j)          # ⑥ 卡【记录当前点】的数据源（只读角度，无运动）
        if self._prev_j is not None:
            for i in range(6):
                if abs(j[i] - self._prev_j[i]) > 0.01:
                    self._mov_until[i] = now + 0.6        # 0.6s 内算"在动"
        self._prev_j = list(j)
        mov = [i for i in range(6) if now < self._mov_until[i]]
        for i, jr in enumerate(self.jrows):
            jr.update_deg(j[i], i in mov, d[i] if self.connected else None)
        if mov:
            self.l_moving.setText("当前在动：" + " ".join("J%d" % (i + 1) for i in mov))
            self.l_moving.setStyleSheet("color:%s;" % P.accent)
        else:
            self.l_moving.setText("当前在动：静止")
            self.l_moving.setStyleSheet("color:%s;" % P.dim)

        if b["servo"]:
            self.l_stat.setText("%s:%d · 伺服已上电 · 自动=%d 急停=%d"
                                % (self.e_host.text(), self.e_port.value(), b["auto"], b["estop"]))
        if fuse is not None:
            m = fuse["mode"]
            if m == "jog":
                txt = "点动：J%d ≤ %g° / 其他轴 ≤ %g°" % (fuse["axis"], fuse["tlim"], fuse["olim"])
            elif m == "goto":
                txt = ("去点位：最大位移 %.2f° / %.0fs 超时（不做越界检测）"
                       % (fuse.get("deg") or 0.0, fuse.get("timeout") or 0))
            elif m == "test":
                d = fuse.get("duration")
                txt = ("测试跑：%.0fs 定时自动停 / 任一位移 ≤ %g°" % (d, fuse["tlim"])
                       if d else "测试跑：任一位移 ≤ %g°" % fuse["tlim"])
            else:
                txt = "执行：不做越界检测（保报警/急停/通信）"
            self.l_fuse.setText("● 熔断已武装（%s）" % txt)
            self.l_fuse.setStyleSheet("color:%s;" % P.danger)
        else:
            self.l_fuse.setText("○ 熔断未武装")
            self.l_fuse.setStyleSheet("color:%s;" % P.dim)
        self._on_pt_sel()          # ⑥ 卡：选中点位的位移预演随姿态实时更新
        self.l_tick.setText("帧 %d · %s" % (self.w.frames, time.strftime("%H:%M:%S")))

    # ================================================================ 关闭
    def closeEvent(self, ev) -> None:                          # noqa: N802
        if self._closing:
            ev.accept()
            return
        risky = self.connected or (self.fuse is not None)
        if risky:
            r = QMessageBox.question(
                self, "确认退出",
                "机器人连接仍处于活动状态%s。\n退出会断开连接并停止轮询。\n\n确定退出吗？"
                % ("，且熔断已武装" if self.fuse is not None else ""),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        self._closing = True
        try:
            self.timer.stop()
            self.w.post("disconnect")
            time.sleep(0.12)
            self.w.stop()
        finally:
            ev.accept()


class ProgDialog(QDialog):
    """添加 / 修改一条程序清单条目：程序号（必填）+ 名字（可英文）+ 备注。

    为什么要有"名字"：Modbus 只认数字号，控制器不会把程序名告诉我们，
    而程序文件很可能叫 `PICK_A.XPL` 这种英文名 —— 名字纯属本地备注，
    方便你自己在清单里认出来，**不会**下发给机器人。
    """

    def __init__(self, parent=None, no: int = 0, name: str = "", note: str = "",
                 title: str = "程序"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        self.sp_no = QSpinBox()
        self.sp_no.setRange(1, 99999)
        self.sp_no.setValue(no if no > 0 else 1)
        self.sp_no.setToolTip("控制器上该程序在 Modbus 40104 里的编号（只能是数字）")
        form.addRow("程序号 *", self.sp_no)

        self.e_name = QLineEdit(name)
        self.e_name.setPlaceholderText("例：PICK_A / 上料 / 随便你怎么叫（可留空）")
        form.addRow("名字", self.e_name)

        self.e_note = QLineEdit(note)
        self.e_note.setPlaceholderText("备注（可留空）")
        form.addRow("备注", self.e_note)
        v.addLayout(form)

        v.addWidget(hint("「程序号」是唯一会下发给机器人的东西；名字和备注只存在本地清单里。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def values(self) -> tuple[int, str, str]:
        return (self.sp_no.value(), self.e_name.text().strip(), self.e_note.text().strip())
