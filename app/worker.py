# -*- coding: utf-8 -*-
"""机器人工作线程 —— 独占唯一一条 Modbus 连接，UI 线程永不碰 socket。

设计（沿用并改进 tkinter 版）：
  · UI 线程只通过 queue 收发消息；本线程串行处理命令 + 周期轮询。
  · **不再忙等**：用 Condition 定时唤醒（空闲时 CPU 占用 ~0）。
  · **熔断重入保护**（修复原版缺陷）：熔断武装期间拒绝新的点动/运行，
    否则后一次会覆盖 fuse，前一次的越界就失去保护。
  · **断线自动重连**：轮询失败进入退避重连（1→2→4→8→10s 封顶），
    重连后自动恢复基线并提示"需要重新点【一键就绪】"。
  · 熔断状态随快照一起 emit 给 UI —— 不再让 UI 跨线程读 worker 内部字段。
"""
from __future__ import annotations

import os
import queue
import threading
import time

from efort_client import DEFAULT_JOG_PROG, LOAD_WORD, STOP_WORD, EfortClient, ModbusError

#: 已现场核实的报警码解释 —— **不编造**，没核实的不放进来。
ALARM_TEXT = {
    5005: "远程加载程序错误 -3：当前加载的程序不存在",
    4902: "XPL 文件 API 错误：程序文件内容损坏",
    1812: "安全门报警：检查安全门是否关闭、作业区内是否有人",
}


class RobotWorker(threading.Thread):
    POLL_MS = 200                 # 轮询周期
    JOG_TIMEOUT = 30.0            # 点动熔断超时（秒）
    TEST_TIMEOUT = 300.0          # **测试跑**兜底超时（秒）—— 正常由"定时"先到，这只是保险丝
    TEST_SPEED = 5                # **测试跑**强制速度（%）—— 忽略界面设定，永远低速
    TEST_SECS_DEFAULT = 3.0       # **测试跑**默认时长（秒）—— 用户要求"跑 3 秒、2 秒这种"
    TEST_SECS_MAX = 3600.0
    SCAN_BUDGET = 1.2             # 扫描单号等待窗口（实测：存在需 ≈0.5s 才置位）
    RECONNECT_DELAYS = (1, 2, 4, 8, 10)   # 重连退避（秒），最后一项为封顶重复值

    def __init__(self, out: queue.Queue, log_path: str | None = None):
        super().__init__(daemon=True, name="RobotWorker")
        self.out = out
        self.inbox: queue.Queue = queue.Queue()
        self._cv = threading.Condition()      # 取代 20ms 忙等
        self._stop = threading.Event()
        self.cli = EfortClient()
        self.log_path = log_path
        self.audit = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                self.audit = open(log_path, "a", encoding="utf-8", buffering=1)
            except OSError:
                self.audit = None

        # 状态
        self.base: list[float] | None = None
        self.maxd = [0.0] * 6
        self.prev_j: list[float] | None = None
        self.fuse: dict | None = None
        self.fuse_t0 = 0.0
        self.last_move = 0.0
        self.moved = False
        self.run_seen = False
        self.seq = 0
        self.frames = 0
        self.speed = 5
        self.jog_prog = DEFAULT_JOG_PROG
        self.jog_prog_bad: int | None = None
        self.jog_status: tuple[str, int] = ("unset" if DEFAULT_JOG_PROG <= 0 else "unknown",
                                            DEFAULT_JOG_PROG)
        #: 跑作业程序前点动服务是否处于"运行中" —— 用于跑完**自动恢复**（体验上不中断）
        self.jog_was_armed = False
        self.auto_restore_jog = True
        #: 程序扫描状态机 —— 在主循环里**逐号推进**，绝不长时间独占工作线程
        #: （否则 300 个号 × 0.5s = 150s 不轮询，UI 快照与链路看门狗全瞎）
        self._scan: dict | None = None
        # 连接
        self.want_conn = False                 # 期望处于连接态（供自动重连判断）
        self.auto_reconnect = True
        self._link_down = False
        self._retry_i = 0
        #: 熔断后"仅保持伺服"的补发时刻。
        #: ⚠ 原 tkinter 版用 threading.Timer 在**另一个线程**里写 socket ——
        #:   会和轮询线程争用同一条连接，导致响应错配（偶发 Modbus 错误、急停写不进去）。
        #:   改为在**本线程**的主循环里定时执行，socket 永远只被本线程碰。
        self._restore_due: float | None = None

    # ------------------------------------------------------------ 对外
    def post(self, *msg) -> None:
        self.inbox.put(msg)
        with self._cv:
            self._cv.notify()

    def emit(self, kind, *payload) -> None:
        self.out.put((kind,) + payload)

    def _audit_write(self, level: str, text: str) -> None:
        """只落审计文件、不回发 UI（回发会造成 LogView 里重复一行）。"""
        if self.audit is None:
            return
        try:
            self.audit.write("%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, text))
        except OSError:
            pass

    def log(self, level: str, text: str) -> None:
        self.emit("log", level, text)
        self._audit_write(level, text)

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify()

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        last_poll = 0.0
        while not self._stop.is_set():
            drained = False
            while True:
                try:
                    msg = self.inbox.get_nowait()
                except queue.Empty:
                    break
                drained = True
                try:
                    self._handle(msg)
                except ModbusError as e:
                    self.log("err", "Modbus 错误：%s" % e)
                except Exception as e:                              # noqa: BLE001
                    self.log("err", "命令异常：%s: %s" % (type(e).__name__, e))

            if self._scan is not None:
                try:
                    self._scan_step()
                except ModbusError as e:
                    self._scan = None
                    self.log("err", "扫描中断：%s" % e)
                    self.emit("scan_done", False, [], 0, 0)

            now = time.time()
            if self._restore_due is not None and now >= self._restore_due:
                self._restore_due = None
                self._restore_servo()
            due = (now - last_poll) * 1000.0 >= self.POLL_MS
            if self.cli.is_open and due:
                last_poll = now
                self._poll()
            elif self.want_conn and not self.cli.is_open and due:
                last_poll = now
                self._try_reconnect()

            with self._cv:
                self._cv.wait(0.05 if (self.cli.is_open or self.want_conn) else 0.5)
        if self.audit is not None:
            try:
                self.audit.close()
            except OSError:
                pass

    # ------------------------------------------------------------ 命令
    def _handle(self, msg) -> None:
        op = msg[0]
        if op == "ui_log":
            # 窗口侧的日志（勾安全确认 / 记录点位 / 去点位 / 拦截提示…）也**必须落盘**，
            # 否则审计文件里只有 worker 说过的话，看不到"人做了什么"。
            # 文件由本线程独占持有 ⇒ 窗口只投递、不自己写，避免两个写者交错切行。
            self._audit_write(msg[1], msg[2])
        elif op == "connect":
            self._connect(msg[1], msg[2])
        elif op == "disconnect":
            self.fuse = None
            self.want_conn = False
            self.cli.close()
            self.emit("conn", False, "已断开")
        elif op == "reconnect_now":
            self._retry_i = 0
            self._try_reconnect(force=True)
        elif op == "set_base":
            s = self.cli.snapshot()
            if s:
                self.base = list(s["joints"])
                self.maxd = [0.0] * 6
                self.log("info", "已把当前姿态设为位移基线")
        elif op == "speed":
            self.speed = int(msg[1])
            r = self.cli.set_speed(self.speed)
            self.log("warn", "40103 = %s（速度设定 %d%%，同时用于点动）" % (r, self.speed))
        elif op == "jog_prog":
            n = int(msg[1])
            self.jog_prog = n
            self.jog_prog_bad = None
            if n <= 0:
                self._set_jog_status("unset", 0)
                self.log("warn", "已取消点动服务程序 —— 点动将不可用（点动必须靠一个常驻程序）")
            else:
                self._set_jog_status("unknown", n)
                self.log("info", "点动服务程序设为 %d（已解除之前的失败锁存）"
                                 "—— 现在点【一键就绪】验证并拉起它" % n)
        elif op == "servo_on":
            r = self.cli.servo_on()
            self.log("warn", "40101 = 0x%04X（伺服上电）→ 等 ≈0.55s 吸合…" % r)
        elif op == "servo_reengage":
            r = self.cli.servo_reengage()
            self.log("warn", "已按【全清 → 等 0.6s → 0x%04X】重新吸合" % r)
        elif op == "servo_off":
            self.cli.servo_disable()
            self.fuse = None
            self.log("warn", "已下发 0x2000 → 0x0000（取消使能）")
        elif op == "clear_alarm":
            a, b = self.cli.clear_alarm_full()
            self.log("warn", "40101 = 0x%04X（清报警）" % a
                     + (" → 伺服已重新吸合 0x%04X" % b if b else ""))
            st = self.cli.snapshot()
            if st:
                self.log("ok" if not (st["alarm1"] or st["alarm2"]) else "err",
                         "  清后状态：伺服=%d 报警=%d/%d"
                         % (st["bits"]["servo"], st["alarm1"], st["alarm2"]))
        elif op == "ready":
            self._do_ready(msg[1], msg[2])
        elif op == "param_test":
            self.log("info", "参数区写回自检：写哨兵 → 读回 → 还原（不触发运动）…")
            for addr, ok, txt in self.cli.param_selftest():
                self.log("ok" if ok else "err", "  %s %s %s" % (addr, "可写" if ok else "不可写", txt))
            self.log("info", "参数区自检结束（已全部还原为 0）")
        elif op == "jog":
            self._do_jog(msg[1], msg[2], msg[3])
        elif op == "goto":
            # 去点位：走的是**和点动完全相同的通道**（写 40139~44 → 置 40135.Bit0），
            # 区别只是 6 个轴一起给、位移可能很大 ⇒ 不做越界熔断（见 _do_goto）。
            self._do_goto(msg[1], msg[2])
        elif op == "load":
            ok, txt = self.cli.load_program(msg[1], msg[2])
            self.log("ok" if ok else "warn", txt)
        elif op == "run":
            # ("run", mode, prog, tlim, olim, secs)
            #   mode ∈ {"test"(测试跑：低速+越界+**定时**), "run"(执行：直接跑)}
            self._do_run(msg[1], msg[2], msg[3], msg[4], msg[5])
        elif op == "scan":
            self._start_scan(int(msg[1]), int(msg[2]))
        elif op == "scan_cancel":
            if self._scan is not None:
                done = self._scan["next"] - self._scan["lo"]
                self._scan = None
                self.log("warn", "扫描已取消（已走过 %d 个号，结果不保留）" % done)
                self.emit("scan_done", False, [], 0, 0)
        elif op == "auto_restore":
            self.auto_restore_jog = bool(msg[1])
            self.log("info", "「跑完自动恢复点动服务」=%s" % ("开" if self.auto_restore_jog else "关"))
        elif op == "stop":
            r = self.cli.stop()
            self.fuse = None
            self.log("warn", "40101 = 0x%04X（停止）" % r)
            self._maybe_restore_jog("停止")
        elif op == "estop":
            self._do_estop()

    # ------------------------------------------------------------ 连接
    def _connect(self, host: str, port: int) -> None:
        self.cli.host, self.cli.port = host, port
        info = self.cli.connect()
        s = self.cli.snapshot()
        if s is None:
            raise ModbusError("连接后读快照失败")
        self.base = list(s["joints"])
        self.maxd = [0.0] * 6
        self.want_conn = True
        self._link_down = False
        self._retry_i = 0
        self.emit("conn", True, info)
        self.log("ok", "已连接 %s（只读状态，未上电）" % info)
        self.emit("snap", s, [0.0] * 6, list(self.maxd), None)

    def _try_reconnect(self, force: bool = False) -> None:
        if not self.auto_reconnect and not force:
            return
        delays = self.RECONNECT_DELAYS
        delay = delays[min(self._retry_i, len(delays) - 1)]
        if not force and self._link_down and (time.time() - getattr(self, "_down_t", 0)) < delay:
            return
        try:
            info = self.cli.connect()
        except Exception as e:                                      # noqa: BLE001
            self._link_down = True
            self._down_t = time.time()
            self._retry_i += 1
            if self._retry_i in (1, 3, 6):                          # 只提示 3 次，避免刷屏
                self.log("warn", "重连失败（%s），%ds 后重试…"
                         % (type(e).__name__, delays[min(self._retry_i, len(delays) - 1)]))
            self.emit("conn", None, "链路中断，自动重连中…")
            return
        # 成功
        self._link_down = False
        self._retry_i = 0
        s = self.cli.snapshot()
        self.base = list(s["joints"]) if s else None
        self.maxd = [0.0] * 6
        self.emit("conn", True, info)
        self.log("ok", "链路已恢复：%s" % info)
        self.log("warn", "  注意：点动服务可能已中断 —— 请重新点【一键就绪】。")
        self._set_jog_status("unknown", self.jog_prog)

    # ------------------------------------------------------------ 一键就绪
    def _do_ready(self, host: str, port: int) -> None:
        self.log("info", "── 一键就绪开始 ──")
        if not self.cli.is_open:
            self.log("info", "① 连接…")
            self._connect(host, port)
        else:
            self.log("info", "① 已连接，跳过")

        s = self.cli.snapshot()
        if s is None:
            raise ModbusError("就绪流程：读状态失败")
        if s["alarm1"] or s["alarm2"] or s["bits"]["alarm"]:
            self.log("warn", "② 有报警 %d/%d → 清报警" % (s["alarm1"], s["alarm2"]))
            _a, b = self.cli.clear_alarm_full()
            self.log("ok", "② 报警已清除" + ("（并已重新吸合伺服）" if b else ""))
        else:
            self.log("info", "② 无报警，跳过")

        s = self.cli.snapshot()
        if s and not s["bits"]["servo"]:
            self.log("warn", "③ 伺服未上电 → 上电（含 ≈0.55s 吸合延迟）")
            self.cli.servo_on()
            if not self._wait_servo(3.0):
                self.log("warn", "③ 3s 未见置位 → 改用【全清 → 等 0.6s → 吸合】重试")
                self.cli.servo_reengage()
                if not self._wait_servo(3.0):
                    self.log("err", "③ 伺服仍未上电 —— 请在示教器上手动使能并检查安全回路")
        else:
            self.log("info", "③ 伺服已在上电，跳过")

        # ④ 点动服务拉起
        prog = self.jog_prog
        s = self.cli.snapshot() or s
        if prog <= 0:
            # 不猜程序号：没指定就**明确跳过**，而不是瞎试一个号、白报一次 5005
            self._set_jog_status("unset", 0)
            self.log("warn", "④ 未指定点动服务程序 → 跳过这一步（连接 / 报警 / 伺服都已完成）")
            self.log("warn", "   处理：在 ⑤ 卡【程序清单】里 [添加] 你在示教器上建好的程序号，"
                             "选中后点 [设为点动服务]。")
            self.log("warn", "   没有它也能【测试跑】/【执行】作业程序；只是 J1~J6 的 ± 按钮不可用。")
            self.emit("ready_done", True, "unset")
            return
        if self.jog_status[0] == "armed":
            self.log("info", "④ 点动服务 %d 已在运行，跳过" % prog)
            self.emit("ready_done", True, "armed")
            return
        if not s["bits"]["auto"]:
            self.log("err", "④ 点动服务未拉起：控制器**不在【自动】模式**（手动模式下外部控制信号被禁用）")
            self.log("warn", "  处理：示教器把模式切到【自动】，然后重新点【一键就绪】。")
            self.emit("ready_done", False, "auto")
            return
        self.log("info", "④ 拉起点动服务：加载并运行程序 %d…" % prog)
        try:
            ok, txt = self.cli.load_program(prog, None)
        except ModbusError as e:
            self.log("err", "④ 点动服务拉起失败：%s" % e)
            self.emit("ready_done", False, "error")
            return
        if ok:
            self.jog_prog_bad = None
            self._set_jog_status("ok", prog)
            self.log("ok", "④ %s" % txt)
            self.cli.run_program()
            self._set_jog_status("armed", prog)
            self.log("ok", "④ 点动服务 %d 已挂起等待指令（程序运行位常亮是正常的）" % prog)
            self.emit("ready_done", True, "armed")
            return
        # 失败 → 对照诊断
        self.jog_prog_bad = prog
        self._set_jog_status("bad", prog)
        self.log("err", "④ 点动服务程序 %d 加载失败：%s" % (prog, txt))
        self.log("warn", "  40104 目标程序号 = %d，控制器上没有该程序（或编译不通过）。" % prog)
        ref = s["prog"]
        if ref and ref != prog:
            try:
                rok, rtxt = self.cli.load_program(ref, None)
            except ModbusError:
                rok, rtxt = False, "异常"
            if rok:
                self.log("info", "  对照：已加载程序 %d → %s" % (ref, rtxt))
                self.log("warn", "  ⇒ 加载机制正常，问题就在 %d 号本身（示教器上没有它 / 没保存 / 编译不通过）" % prog)
                self.cli.set_program(ref)
            else:
                self.log("err", "  对照：连程序 %d 也加载不了（%s）" % (ref, rtxt))
                self.log("err", "  ⇒ 问题在机制侧：检查示教器是否【自动】模式、有无安全回路报警/未登录")
        self.log("warn", "  处理：确认示教器上真有程序 %d 且**已保存**（【程序模板】给的是点动服务程序的"
                         "官方逐字录入步骤）；若只是号填错了，在 ⑤ 卡【程序清单】里用"
                         "[改名/备注] 改成正确的号。" % prog)
        self._recover_after_bad_load("加载失败")
        self.emit("ready_done", False, "bad")

    def _wait_servo(self, timeout: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.cli.snapshot()
            if st and st["bits"]["servo"]:
                self.log("ok", "③ 伺服已上电（%.2fs）" % (time.time() - t0))
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------ 前置校验
    def _precheck(self, what: str):
        s = self.cli.snapshot()
        if s is None:
            raise ModbusError("%s前读状态失败" % what)
        b = s["bits"]
        if not b["servo"]:
            self.log("warn", "%s被拒：伺服未上电" % what)
            return None
        if b["alarm"] or b["estop"] or s["alarm1"] or s["alarm2"]:
            self.log("warn", "%s被拒：有报警/急停（码 %d/%d）" % (what, s["alarm1"], s["alarm2"]))
            self.log("warn", "  → 点【清报警 (0x1009)】清除；报警不会自动消失")
            return None
        if not b["auto"]:
            self.log("warn", "%s被拒：不在自动模式" % what)
            return None
        return s

    def _arm(self, s, mode: str, axis: int, tlim: float, olim: float, deg: float = 0.0,
             timeout: float | None = None, duration: float | None = None) -> None:
        self.base = list(s["joints"])
        self.maxd = [0.0] * 6
        self.prev_j = None
        self.moved = False
        self.last_move = time.time()
        self.fuse_t0 = self.last_move
        self.fuse = {"mode": mode, "axis": axis, "deg": deg, "tlim": tlim, "olim": olim,
                     "timeout": timeout, "duration": duration}

    def _recover_after_bad_load(self, why: str) -> None:
        st = self.cli.snapshot()
        if st is None:
            return
        if st["alarm1"] or st["alarm2"] or st["bits"]["alarm"]:
            meaning = ALARM_TEXT.get(st["alarm1"], "")
            self.log("warn", "  %s已引发报警 %d/%d%s → 自动清除"
                     % (why, st["alarm1"], st["alarm2"], ("（%s）" % meaning) if meaning else ""))
            _a, b = self.cli.clear_alarm_full()
            self.log("ok", "  报警已清除" + ("，伺服已重新吸合" if b else ""))
            st = self.cli.snapshot() or st
        good = st["prog"]
        if good and st.get("set_prog") != good:
            try:
                self.cli.set_program(good)
                self.log("info", "  40104 已恢复为控制器当前程序号 %d" % good)
            except ModbusError:
                pass
        self.log("warn", "  后续点动不会再重试 %s 号（避免反复触发报警）。改程序号或先在示教器里建好它。"
                 % self.jog_prog_bad)

    # ------------------------------------------------------------ 点动
    #: 写目标角后**必须**静置这么久才能置触发位 —— 见 `_verify_jog_target` 的说明。
    JOG_SETTLE = 0.15

    def _jog_service_gate(self, what: str) -> int:
        """点动 / 去点位**共用**的前置校验：必须有已就绪的点动服务程序。

        两条通道都要经控制器上的常驻程序做 `MJOINT` 并回执，所以门槛完全一样。
        通过 → 返回点动服务程序号（>0）；不通过 → 返回 0，并已把原因写进日志。
        """
        prog = self.jog_prog
        if prog <= 0:
            self.log("err", "%s被拒：还没指定**点动服务程序**。" % what)
            self.log("warn", "  点动必须靠控制器上一个常驻程序来回执（Modbus 没有原生点动命令）——")
            self.log("warn", "  在 ⑤ 卡【程序清单】里 [添加] 你按【程序模板】在示教器上建好的那个程序号，"
                             "选中后点 [设为点动服务]，再点【一键就绪】。")
            return 0
        if self.jog_status[0] == "armed":
            self.jog_prog_bad = None
        if self.jog_prog_bad == prog:
            self.log("err", "%s被拒：点动服务程序 %d 之前加载失败过，不再重复重试。" % (what, prog))
            self.log("warn", "  处理：确认示教器上确实有这个程序且已保存（点左【程序模板】拿步骤），"
                             "然后在【程序清单】里重新点 [设为点动服务] + 点【一键就绪】。")
            return 0
        if self.jog_status[0] != "armed":
            self.log("warn", "%s被拒：点动服务未在运行（程序 %d 未加载/未运行）。" % (what, prog))
            self.log("warn", "  处理：先点【一键就绪】拉起点动服务；若刚改过程序号，也需重新就绪。")
            return 0
        return prog

    def _verify_jog_target(self, target: list) -> bool:
        """写完 `40139~44` 后**回读校验 + 静置**，再允许触发。

        ★ 2026-09-23 现场实测的竞态（真机 6 步里中招 1 次）：
          程序正挂在 `WAIT` 上时，若**写完目标角立刻**置 `40135.Bit0`，
          控制器还没把 Modbus 读区同步进程序变量空间 → 程序放行后 `MJOINT`
          拿到的是**上一轮的旧目标角**：`40035` 完成位秒回、实测位移 `0.000°`、
          人看着就是"点了不走"。
        ⇒ 对策：先回读目标寄存器确认写落地，再静置 `JOG_SETTLE` 秒才触发。

        返回 `False` = 目标没可信地写下去（调用方应放弃本次点动、**不要触发**）。
        """
        ok = False
        t0 = time.time()
        while time.time() - t0 < 1.0:
            s = self.cli.snapshot()
            rb = s.get("jog_ro") if s else None
            if rb and len(rb) == 6:
                ang = [((v - 65536 if v > 32767 else v) / 100.0) for v in rb]
                if all(abs(ang[i] - target[i]) < 0.005 for i in range(6)):
                    ok = True
                    break
            time.sleep(0.03)
        if self.JOG_SETTLE > 0:
            time.sleep(self.JOG_SETTLE)
        return ok

    def _do_jog(self, axis: int, deg: float, olim: float) -> None:
        # ★ 重入保护：熔断武装期间绝不允许再点 —— 否则会覆盖 fuse，前一次失去保护
        if self.fuse is not None:
            f = self.fuse
            self.log("warn", "点动被拒：上一次动作仍在进行中（%s J%d，熔断已武装，已 %.1fs）"
                     % ("点动" if f["mode"] == "jog" else "跑程序", f["axis"],
                        time.time() - self.fuse_t0))
            self.log("warn", "  请等它结束（看到「点动完成」或「熔断解除」后再点）。")
            return
        s = self._precheck("点动")
        if s is None:
            return
        prog = self._jog_service_gate("点动")
        if prog <= 0:
            return
        cur = s["joints"][axis - 1]
        target = list(s["joints"])
        target[axis - 1] = cur + deg
        self.cli.set_speed(self.speed)
        self.cli.write_jog_angles(target)
        self.log("info", "目标：J%d %+.3f° → %+.3f°（绝对角）→ 40139~44"
                 % (axis, cur, target[axis - 1]))
        # ★ 写完必须**回读校验 + 静置**再触发，否则会踩"程序读旧目标角"的竞态
        #   （完成位秒回、零位移，看着就是"点了不走"）—— 详见 _verify_jog_target。
        if not self._verify_jog_target(target):
            self.log("err", "点动已放弃：目标角写入后**回读不一致**（40139~44 没落地）"
                            "—— 未置触发位，机器人不动。")
            self.log("warn", "  多为通信抖动。稍等一秒重按一次即可；"
                             "若反复出现，检查网线/交换机，或把速度调低再试。")
            return
        tlim = max(abs(deg) * 2.0, 2.0)
        s2 = self.cli.snapshot() or s
        self._arm(s2, "jog", axis, tlim, olim, deg)
        self.log("info", "熔断：J%d≤%g° / 其他≤%g°，等 40035.Bit0 完成位…" % (axis, tlim, olim))
        r = self.cli.trigger_jog()
        self.log("warn", "40135 = 0x%04X（触发 → 程序 %d 的 WAIT 放行）" % (r, prog))

    # ------------------------------------------------------------ 去点位
    #: 去点位**不做越界熔断**（6 轴一起走、位移本来就大，拿点动的尺子量必然误杀 ——
    #: 与【执行】档同一口径），只保留 报警 / 急停 / 通信 / 超时 四道底线。
    GOTO_MIN_TIMEOUT = 30.0
    GOTO_MAX_TIMEOUT = 300.0

    def _do_goto(self, name: str, joints) -> None:
        """走到记录下来的点位 —— 走**和点动完全相同的通道**，只是 6 轴一起给。

        通道：写 `40139~44`=该点位的 6 个绝对角 → 置 `40135.Bit0` → 常驻程序 `MJOINT`
        → 回执 `40035.Bit0` → 撤触发。（Modbus 没有"去点位"这种指令，全靠这条路。）
        """
        if self.fuse is not None:
            f = self.fuse
            self.log("warn", "去点位被拒：上一次动作仍在进行中（%s，已 %.1fs）"
                     % ("点动" if f["mode"] == "jog" else "跑程序", time.time() - self.fuse_t0))
            self.log("warn", "  请等它结束再点。")
            return
        s = self._precheck("去点位")
        if s is None:
            return
        if self._jog_service_gate("去点位") <= 0:
            return

        try:
            tgt = [float(v) for v in joints]
        except (TypeError, ValueError):
            self.log("err", "去点位被拒：点位数据不是数字")
            return
        if len(tgt) != 6 or any(v != v or abs(v) > 1000.0 for v in tgt):
            self.log("err", "去点位被拒：点位需要 6 个有限角度值（J1~J6）")
            return

        cur = list(s["joints"])
        d = [tgt[i] - cur[i] for i in range(6)]
        maxd = max(abs(x) for x in d)
        if maxd < 0.02:
            self.log("warn", "已在点位「%s」上（最大差 %.3f°）—— 不需要动作。" % (name, maxd))
            return

        # 超时只是**兜底**，不是熔断阈值：实测 40103=5% 时约 1.3°/s，随速度档放大。
        rate = max(1.0, self.speed / 5.0)
        timeout = min(self.GOTO_MAX_TIMEOUT, max(self.GOTO_MIN_TIMEOUT, maxd / rate * 3.0 + 10.0))
        self.log("info", "── 去点位「%s」：最大单轴位移 %.2f° ──"
                 % (name, maxd))
        self.log("info", "  各轴位移 " + " ".join("J%d %+.2f°" % (i + 1, d[i]) for i in range(6)))
        if self.speed <= 20:
            self.log("warn", "  当前速度 %d%%（≈%.1f°/s）—— 大位移会比较慢，预计 %.0fs 量级。"
                     % (self.speed, rate, maxd / rate))

        self.cli.set_speed(self.speed)
        self.cli.write_jog_angles(tgt)
        # ★ 与点动同源：写后必须回读校验 + 静置，否则会踩"程序读旧目标角"的竞态
        if not self._verify_jog_target(tgt):
            self.log("err", "去点位已放弃：目标角写入后**回读不一致**（40139~44 没落地）"
                            "—— 未置触发位，机器人不动。")
            return
        s2 = self.cli.snapshot() or s
        self._arm(s2, "goto", 1, 0.0, 0.0, maxd, timeout=timeout)
        self.log("info", "熔断：报警 / 急停 / 通信 / %.0fs 超时（**不做越界检测**）；"
                         "等 40035.Bit0 完成位…" % timeout)
        r = self.cli.trigger_jog()
        self.log("warn", "40135 = 0x%04X（触发 → 程序 %d 的 WAIT 放行）" % (r, self.jog_prog))

    # ------------------------------------------------------------ 跑程序
    def _do_run(self, mode: str, prog: int, tlim: float, olim: float,
                secs: float = 0.0) -> None:
        """跑程序。**测试跑 / 执行 两档彻底分离**（2026-09-21 / 09-23 用户要求）：

        · `mode="test"` 测试跑 —— 低速（强制 5%）+ **定时**（跑 N 秒自动停，默认 3s）
          + 越界熔断（任一位移超限即停）。第一次跑新程序、试点位用它。
          跑完**不自动恢复点动**（停在现场便于检查）。
        · `mode="run"`  执行   —— 生产速度 + **越界检测关**，直接跑完
          （只保报警/急停/通信/程序结束）。作业程序本就要让轴大幅运动，
          拿点动的尺子量它必然误杀（14:52 那次 J2 走 5.63° > 5° 被掐停就是这么来的）。
        """
        if self.fuse is not None:
            self.log("warn", "运行被拒：上一次动作仍在进行中，熔断已武装。")
            return
        s = self._precheck("运行")
        if s is None:
            return
        prog = prog or s["set_prog"] or s["prog"]
        if prog <= 0:
            self.log("err", "运行被拒：程序号无效（先从 ⑤ 卡【程序清单】里选一个，或[添加]它）")
            return
        if self.jog_prog > 0 and prog == self.jog_prog:
            self.log("warn", "注意：%d 是**点动服务程序**，不是作业程序。" % prog)
            self.log("warn", "  跑它只会让它常驻挂起，不会干活。请从【程序清单】里选作业程序。")
        test = (mode == "test")
        speed = self.TEST_SPEED if test else self.speed
        if test:
            secs = float(secs or self.TEST_SECS_DEFAULT)
            secs = max(0.5, min(secs, self.TEST_SECS_MAX))

        # 手册：程序运行过程中不可加载 → 必须先把点动服务停掉；跑完再按需拉回来
        self.jog_was_armed = self.jog_status[0] == "armed"
        if self.jog_was_armed:
            r = self.cli.stop()
            self._set_jog_status("ok", self.jog_prog)
            self.log("warn", "已暂停点动服务 %d（0x%04X）%s"
                     % (self.jog_prog, r,
                        "" if test else "—— 程序结束后按开关自动恢复"))

        self.cli.set_speed(speed)
        self.cli.set_program(prog)
        ok, txt = self.cli.load_program(prog, None)
        if not ok:
            if self.jog_prog > 0 and prog == self.jog_prog:
                self.jog_prog_bad = prog
            self.log("err", "运行已放弃（未触发运行）：%s" % txt)
            self.log("warn", "  程序号 %d 不存在或无法加载 —— 不触发运行，避免报报警。" % prog)
            self.log("warn", "  提示：点【扫描】可列出控制器上真实存在的程序号，"
                             "扫到的会自动进【程序清单】。")
            self._recover_after_bad_load("加载失败")
            return
        self.log("ok", txt)
        s2 = self.cli.snapshot() or s

        if test:
            self.log("info", "── 测试跑：程序 %d @ **%d%% 低速**（忽略设定速度 %d%%），"
                             "**跑 %.1f 秒自动停** ──" % (prog, speed, self.speed, secs))
            self.log("info", "   越界熔断：任一关节位移 > %g° 也立即停（兜底 %.0fs）"
                     % (tlim, self.TEST_TIMEOUT))
            self._arm(s2, "test", 0, tlim, olim, 0.0,
                      timeout=self.TEST_TIMEOUT, duration=secs)
        else:
            self.log("info", "── 执行：程序 %d @ %d%%，直接跑完 ──" % (prog, speed))
            self.log("warn", "   越界检测已**关闭**（作业程序本就大幅运动）；"
                             "仍保报警 / 急停 / 通信 / 程序结束 四道保护")
            self._arm(s2, "run", 0, tlim, olim, 0.0, timeout=None, duration=None)

        self.run_seen = False
        r = self.cli.run_program()
        self.log("warn", "40101 = 0x%04X（运行程序 %d）" % (r, prog))

    def _do_estop(self) -> None:
        """急停：撤触发（防程序重跑残留触发自行运动）→ 停止 → 撤熔断 → 中止扫描。

        ⚠ 急停**绝不自动恢复**任何东西 —— 必须由人确认现场后重新点【一键就绪】。
        """
        self._scan = None
        try:
            self.cli.clear_trigger()
        except Exception:                                          # noqa: BLE001
            pass
        self.fuse = None
        self.jog_was_armed = False
        r = self.cli.stop()
        self.log("err", "!! 急停：已撤点动触发位 + 下发 0x%04X 停止字" % r)
        self.emit("estop_done", None)

    def _maybe_restore_jog(self, why: str = "程序结束") -> None:
        """作业程序结束后，把之前暂停的点动服务自动拉回来（"中间不要停"）。

        安全前提：必须仍在【自动】模式、无报警、无急停 —— 否则只提示、不自动拉起。
        """
        if not (self.auto_restore_jog and self.jog_was_armed):
            return
        self.jog_was_armed = False
        st = self.cli.snapshot()
        if st is None:
            return
        if st["bits"]["estop"] or st["alarm1"] or st["alarm2"] or st["bits"]["alarm"]:
            self.log("warn", "  ↺ 自动恢复点动服务已跳过：当前有报警/急停（码 %d/%d）——"
                             "清完报警后点【一键就绪】"
                     % (st["alarm1"], st["alarm2"]))
            return
        if not st["bits"]["auto"]:
            self.log("warn", "  ↺ 自动恢复点动服务已跳过：不在【自动】模式")
            return
        prog = self.jog_prog
        if prog <= 0:
            self.log("info", "  ↺ 跳过自动恢复点动服务：尚未指定点动服务程序")
            return
        try:
            ok, txt = self.cli.load_program(prog, None)
        except ModbusError as e:
            ok, txt = False, str(e)
        if not ok:
            self._set_jog_status("bad", prog)
            self.log("err", "  ↺ 自动恢复点动服务失败：%s" % txt)
            self.log("warn", "    处理：在示教器上确认程序 %d 存在并已保存，然后点【一键就绪】。" % prog)
            return
        self._set_jog_status("ok", prog)
        self.cli.run_program()
        self._set_jog_status("armed", prog)
        self.log("ok", "  ↺ %s → 已自动恢复点动服务 %d，可以继续点 J1~J6" % (why, prog))

    # ------------------------------------------------------------ 轮询
    def _poll(self) -> None:
        try:
            s = self.cli.snapshot()
        except Exception:                                          # noqa: BLE001
            s = None
        if s is None:
            if self.want_conn and self.auto_reconnect:
                self.cli.close()
                self._link_down = True
                self._down_t = time.time()
                self.log("warn", "轮询失败 —— 链路可能已断，进入自动重连…")
            self.emit("conn", None, "轮询失败")
            return
        self.frames += 1
        j = s["joints"]
        d = [0.0] * 6
        if self.base:
            for i in range(6):
                d[i] = j[i] - self.base[i]
                self.maxd[i] = max(self.maxd[i], abs(d[i]))
        self.emit("snap", s, d, list(self.maxd), dict(self.fuse) if self.fuse else None)
        if self.fuse:
            self._check_fuse(s, j)

    def _check_fuse(self, s, j) -> None:
        f = self.fuse
        if f is None:
            return
        ti = f["axis"] - 1
        b = s["bits"]
        now = time.time()
        others = [self.maxd[i] for i in range(6) if i != ti]
        if self.prev_j is not None and any(abs(j[i] - self.prev_j[i]) > 0.02 for i in range(6)):
            self.last_move = now
            self.moved = True
        elif not self.moved and max(self.maxd) > 0.05:
            self.moved = True
            self.last_move = now
        self.prev_j = list(j)

        timed = False
        why = None
        if b["alarm"] or b["estop"] or s["alarm1"] or s["alarm2"]:
            why = "报警/急停（码 %d/%d）" % (s["alarm1"], s["alarm2"])
        elif f["mode"] == "test" and max(self.maxd) > f["tlim"]:
            k = self.maxd.index(max(self.maxd))
            why = "测试跑越界 J%d 位移 %.2f° > %g°" % (k + 1, max(self.maxd), f["tlim"])
        elif f["mode"] == "jog" and self.maxd[ti] > f["tlim"]:
            why = "J%d 越界 %.2f° > %g°" % (f["axis"], self.maxd[ti], f["tlim"])
        elif f["mode"] == "jog" and others and max(others) > f["olim"]:
            k = [i for i in range(6) if i != ti]
            jj = k[others.index(max(others))]
            why = "其他轴越界 J%d %.2f° > %g°" % (jj + 1, max(others), f["olim"])
        elif (f.get("duration") and now - self.fuse_t0 >= f["duration"]):
            timed = True                                    # ← 定时测试跑到点（正常结束，非故障）
        elif f["timeout"] is not None and now - self.fuse_t0 > f["timeout"]:
            why = "%.0fs 超时" % f["timeout"]
        if timed or why:
            r = self.cli.stop()
            if f["mode"] in ("jog", "goto"):
                try:
                    self.cli.clear_trigger()
                    self.log("warn", "  已撤点动触发位（40135=0）")
                except ModbusError:
                    pass
            self.fuse = None
            if timed:
                self.log("ok", "⏱ 测试跑到设定时长 %.1fs → 已下发 0x%04X 停止（按计划结束）"
                         % (f["duration"], r))
            else:
                self.log("err", "熔断触发（%s）→ 下发 0x%04X 停止" % (why, r))
            if self.jog_was_armed:
                self.jog_was_armed = False
                if f["mode"] == "test":
                    self.log("warn", "  点动服务在测试跑前已暂停 —— 要继续点动请点【一键就绪】。")
                else:
                    # 熔断是异常中断：**不自动恢复**，明确让人重新就绪
                    self.log("warn", "  点动服务已被中断 —— 确认现场后重新点【一键就绪】。")
            if why and "报警" in why:
                self.log("warn", "  → 报警不会自己消失，请点【清报警 (0x1009)】；"
                                 "否则后续点动都会被拒（伺服/报警前置校验不通过）")
            self._restore_due = time.time() + 0.5      # 本线程稍后补发（勿用 Timer 跨线程写 socket）
            return

        if f["mode"] in ("jog", "goto"):
            if s.get("jog_done"):
                try:
                    self.cli.clear_trigger()
                except ModbusError:
                    pass
                self.fuse = None
                if f["mode"] == "goto":
                    mv = max(abs(s["joints"][i] - self.base[i]) for i in range(6))
                    self.log("ok", "点位到位（程序回执 40035.Bit0=1）：最大单轴位移 %.2f°"
                                   "→ 已撤触发、基线已归零" % mv)
                else:
                    self.log("ok", "点动完成（程序回执 40035.Bit0=1）：J%d 实际 %+.2f°（请求 %+.0f°）"
                                   "→ 已撤触发、基线已归零"
                             % (f["axis"], s["joints"][ti] - self.base[ti], f["deg"]))
                self.base = list(j)
                self.maxd = [0.0] * 6
            elif not self.moved and now - self.fuse_t0 > 8.0:
                # ★ 必须撤触发！不撤的话程序会一直满足 WAIT → 无限循环 MJOINT
                #   （2026-09-23 现场实测：这条路径漏撤，测完 40135 一直停在 1）
                try:
                    self.cli.clear_trigger()
                except ModbusError:
                    pass
                self.fuse = None
                if b["run"] and not s.get("jog_done"):
                    # 现场实测最典型的一种：程序**在运行**（运行位常亮）、但 40035 恒 0、
                    # 零位移 ⇒ 程序**卡在 WAIT 上**，触发根本没被接住。别再用"是否已加载运行"
                    # 这种笼统提示 —— 程序明明在跑，问题在 WAIT 的条件上。
                    self.log("err", "点动无响应：程序 %d **在运行、却没接住触发**"
                                    "（运行位=1 常亮，40035 完成位始终 0）→ 它卡在 WAIT 上了。"
                             % self.jog_prog)
                    self.log("warn", "  最常见原因：WAIT 的**条件变量选错**。要等的是【读区】"
                                     "fidbus.mtcp_ro_b[0]（= 40135.Bit0）；")
                    self.log("warn", "  若写成【写区】fidbus.mtcp_wo_b[0]，程序自己第一句刚把它置成 "
                                     "false → 永远等不到。")
                    self.log("warn", "  核对：示教器打开程序 %d，看**当前执行行**是否停在 WAIT；"
                                     "并确认变量前缀 fidbus. 没漏、下标是 [0]。改完【保存】再【一键就绪】。"
                             % self.jog_prog)
                    self.log("warn", "  已撤触发（40135=0）—— 程序回到 WAIT 挂起，可改完直接再试。")
                else:
                    self.log("warn", "8s 内未观察到运动、也未收到完成位 —— 检查：程序 %d 是否已加载运行"
                                     "（重新点【一键就绪】）、代码是否按模板录入（点左【程序模板】对照）"
                             % self.jog_prog)
        else:                                       # test / run 两档
            if b["run"]:
                self.run_seen = True
            elif self.run_seen:
                self.fuse = None
                self.log("ok", "程序运行位回落，程序结束（%.1fs）→ 熔断解除"
                         % (now - self.fuse_t0))
                if f["mode"] == "test":
                    self.jog_was_armed = False
                    self.log("warn", "  测试跑结束 —— **不自动恢复点动**（保持现场便于检查）。"
                                     "要继续点动请点【一键就绪】。")
                else:
                    self._maybe_restore_jog("程序结束")
            elif not self.run_seen and now - self.fuse_t0 > 12.0:
                self.fuse = None
                self.log("warn", "12s 内程序运行位未置起 —— 程序可能没真正启动"
                                 "（检查示教器是否在【自动】模式）→ 已撤熔断")
                if self.jog_was_armed:
                    self._maybe_restore_jog("程序未启动")

    # ------------------------------------------------------------ 程序扫描
    def _probe_prog(self, n: int) -> tuple[str, float, str]:
        """探一个程序号是否存在 —— **只加载，绝不运行**（不碰 Bit1/Bit2，机器人不动）。

        判据来自现场实测（`tools/probe_load.py` 时间线，2026-09-21）：
          · **存在**   → `40001.Bit11` 程序加载位置位，实测 **≈0.50s** 才出现（真脉冲，必须等够）
          · **不存在** → 报警 **5005**，实测 **≈0.08s** 出现（快且锁存，比等脉冲可靠）
          · `40006`（当前程序号）**不随加载变化**（加载 123 失败后仍是 411）→ 当不了判据

        ⚠ 旧版把等待窗口设成 0.5s，正好卡在 0.50~0.61s 的临界上 → 411 明明存在却被判"不存在"。
        """
        try:
            self.cli.set_program(n)
            self.cli.cmd(LOAD_WORD)
        except ModbusError as e:
            return "error", 0.0, str(e)
        t0 = time.time()
        while True:
            dt = time.time() - t0
            w = self.cli._read(0, 6)                 # 40001~40006 一次读完，最省往返
            if w is not None:
                if (w[0] >> 11) & 1:
                    return "exists", dt, "程序加载位置位"
                if w[3] or w[4]:
                    return "missing", dt, "报警 %d/%d" % (w[3], w[4])
            if dt >= self.SCAN_BUDGET:
                return "timeout", dt, "既无置位也无报警"
            time.sleep(0.04)

    def _reject_scan(self, reason: str, level: str = "warn") -> None:
        """拒绝扫描 —— **必须回一个 scan_reject**，否则界面会永远卡在「扫描中…」。"""
        self.log(level, "扫描被拒：%s" % reason)
        self.emit("scan_reject", reason)

    def _start_scan(self, lo: int, hi: int) -> None:
        """启动扫描（**状态机**，真正的推进在 _scan_step）。"""
        if self._scan is not None:
            self._reject_scan("已有一个扫描在进行中（已走到 %d）" % (self._scan["next"] - 1))
            return
        if self.fuse is not None:
            self._reject_scan("熔断已武装（有动作在进行）")
            return
        if not self.cli.is_open:
            self._reject_scan("尚未连接")
            return
        s = self.cli.snapshot()
        if s is None:
            self._reject_scan("读状态失败")
            return
        if lo < 1 or hi < lo:
            self._reject_scan("扫描范围非法：%d ~ %d" % (lo, hi), "err")
            return
        if s["bits"]["run"]:
            # ⚠ 点动服务程序是**常驻**的：它挂在 WAIT 上时运行位本身就是 1。
            #   所以"有程序在运行"不能一刀切 —— 只有**不是**我们自己的点动服务时才拒绝。
            own = (self.jog_prog > 0 and self.jog_status[0] == "armed"
                   and s["prog"] == self.jog_prog)
            if not own:
                self._reject_scan("当前有作业程序在运行（程序号 %s）—— 先点【停止】"
                                  % (s["prog"] or "?"))
                return
            self.log("info", "当前运行的是点动服务程序 %d（常驻挂起）—— 扫描前先停掉它"
                     % self.jog_prog)
        # 加载会打断点动服务 → 先记下来，扫完自动拉回
        self.jog_was_armed = self.jog_status[0] == "armed"
        if self.jog_was_armed:
            try:
                self.cli.stop()
            except ModbusError:
                pass
            self._set_jog_status("ok", self.jog_prog)
            self.log("warn", "扫描前已暂停点动服务 %d（扫完自动恢复）" % self.jog_prog)
        self._scan = {"lo": lo, "hi": hi, "next": lo, "found": [],
                      "orig": s["prog"], "wait": 0.0, "t0": time.time()}
        self.log("info", "── 扫描程序 %d~%d（**只加载、不运行**；存在≈0.5s / 不存在≈0.1s）──"
                 % (lo, hi))

    def _scan_step(self) -> None:
        """在主循环里推进**一个**号 —— 保持轮询 / UI / 急停始终可用。

        （反例：把 300 个号一次性循环完 = 最长 150s 不轮询，UI 快照和链路看门狗全瞎。）
        """
        sc = self._scan
        if sc is None or time.time() < sc["wait"]:
            return
        n = sc["next"]
        if n > sc["hi"]:
            found = sc["found"]
            self._scan = None
            self.log("ok", "── 扫描完成：%d~%d 共命中 **%d** 个程序（用时 %.0fs）──"
                     % (sc["lo"], sc["hi"], len(found), time.time() - sc["t0"]))
            self.log("ok" if found else "warn",
                     "   %s" % ("  ".join(str(x) for x in found) if found
                               else "一个都没找到 —— 确认示教器上确实建过程序、并且**保存过**。"))
            try:
                self.cli.set_program(sc["orig"])
                self.cli.servo_on()
            except ModbusError:
                pass
            self.emit("scan_done", True, found, sc["lo"], sc["hi"])
            self._maybe_restore_jog("扫描完成")
            return
        sc["next"] = n + 1
        kind, dt, why = self._probe_prog(n)
        if kind == "exists":
            sc["found"].append(n)
            self.log("ok", "  ✅ %d 存在（%.2fs）" % (n, dt))
            try:
                self.cli.cmd(STOP_WORD)               # 卸载，免得影响下一个号的判断
            except ModbusError:
                pass
            sc["wait"] = time.time() + 0.25
        elif kind == "missing":
            try:
                self.cli.clear_alarm()                # 5005 不清会累积
            except ModbusError:
                pass
            sc["wait"] = time.time() + 0.15
        elif kind == "timeout":
            self.log("warn", "  ⚠ %d 无明确应答（%.2fs）：既未置位也无报警" % (n, dt))
        else:
            self.log("err", "  × %d 通信异常：%s" % (n, why))
        if sc["next"] % 25 == 0:
            self.emit("scan_progress", sc["next"] - sc["lo"], sc["hi"] - sc["lo"] + 1,
                      len(sc["found"]))

    def _restore_servo(self) -> None:
        try:
            if self.cli.is_open:
                r = self.cli.servo_on()
                self.emit("log", "info", "已恢复 0x%04X（仅保持伺服）" % r)
        except Exception:                                          # noqa: BLE001
            pass

    def _set_jog_status(self, state: str, prog: int) -> None:
        self.jog_status = (state, prog)
        self.emit("jog_status", state, prog)
