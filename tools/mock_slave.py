# -*- coding: utf-8 -*-
"""本地 mock Modbus 从站 —— 复刻埃夫特 RP-2 的"脾气"，供离线自检用。

语义依据：官方《C30 RPL 指令手册》15.4.5 映射表 + 现场实测（见 docs/）：
  · 伺服吸合有 **0.55s 延迟**（验证界面不会误判"没上电"）
  · 点动服务程序 = **常驻**：运行后挂在 WAIT（run 恒真但**不动**），
    等 40135.Bit0 触发 → 朝 40139~44 的绝对目标走（5°/s）→ 到位置 40035.Bit0=1
    → 撤触发（40135=0）后回到挂起
  · `rogue=True` 且点动执行中：额外让 J2 以 3°/s 溜出去（验证熔断"其他轴越界"分支）
  · `missing` 里的程序号"不存在"：加载位不置起；**硬要加载则报报警 5005**（真机实测）
  · 停止位 0x1005 生效后关节立即冻结
"""
from __future__ import annotations

import socket
import struct
import threading
import time

from efort_client import BIT

JOG_PROG = 200
LOAD_DELAY = 0.5      # 程序加载位置起的实测延迟（真机 ≈0.50s，见 tools/probe_load.py）
JOG_RATE = 5.0        # 度/秒（贴近真机 5% 速度量级）
ROGUE_RATE = 3.0      # 度/秒（仅 rogue 模式）
ANG0 = 138            # 40139 -> 0 基地址（fidbus.mtcp_ro_i[0]）
TRIG_ADDR = 134       # 40135（触发位区，Bit0 = 触发）
ACK_ADDR = 34         # 40035（完成位区，Bit0 = 完成）
JOINTS0 = [73.340, -49.490, -38.397, 12.456, 107.722, -0.927]


class MockRobot:
    def __init__(self):
        self.lock = threading.Lock()
        self.manual, self.auto, self.remote = 0, 1, 0
        self.servo = self.alarm = self.estop = self.run = self.loaded = False
        self.alarm1 = self.alarm2 = 0
        self.speed, self.prog, self.set_speed, self.set_prog = 5, 123, 5, 123
        self.cmd = 0
        self.joints = list(JOINTS0)
        self.p_ang = [0] * 6            # 40139~40144：J1~J6 目标角 ×100
        self.trig = 0                   # 40135.Bit0 触发（电平）
        self.p_ack = 0                  # 40035.Bit0 完成（点动程序写）
        self.engage_at = None
        self.run_t0 = None
        self.anchor = None
        self.aim = None
        self.stop_cmds = 0
        self.run_cmds = 0
        self.rogue = False
        self.missing: set[int] = set()
        #: 现场那个"点动服务程序"的号 —— **由被测端告知**（真机上也一样：号是人定的）。
        #: 默认 200 只是 mock 自己的初值，测试里会改成别处。
        self.jog_prog = JOG_PROG
        #: None = 除 missing 外都存在；给定集合 = **只有这些号存在**（用于扫描自检）
        self.existing: set[int] | None = None
        self.load_at: float | None = None
        #: 非点动程序运行时 J2 的移动速率（度/秒）—— 用来复现"作业程序大幅运动"
        self.job_rate = 0.0
        #: 非点动程序的运行时长（秒）—— 调长一点，给熔断判定留出稳定窗口
        self.job_secs = 0.6

    def _exists(self, n: int) -> bool:
        if self.existing is not None:
            return n in self.existing
        return n not in self.missing

    # ------------------------------------------------ 内部
    def tick(self) -> None:
        now = time.time()
        with self.lock:
            if self.load_at is not None and now >= self.load_at:
                self.loaded = True          # ← 实测：加载位置位有 ≈0.5s 延迟
                self.prog = self.set_prog
                self.load_at = None
            if self.engage_at is not None and now >= self.engage_at:
                self.servo = True
                self.engage_at = None
            if not self.run or self.run_t0 is None:
                return
            dt = now - self.run_t0
            if self.aim is None:                    # 非点动程序：job_secs 后自然结束
                if self.job_rate:                   # 模拟"作业程序本就要大幅运动"
                    self.joints[1] = self.anchor[1] + self.job_rate * min(dt, self.job_secs)
                if dt > self.job_secs:
                    self.run = False
                return
            done = True
            for i in range(6):
                span = self.aim[i] - self.anchor[i]
                step = min(JOG_RATE * dt, abs(span)) * (1 if span >= 0 else -1)
                self.joints[i] = self.anchor[i] + step
                if abs(step) < abs(span) - 1e-9:
                    done = False
            if self.rogue:
                self.joints[1] = self.anchor[1] + ROGUE_RATE * dt
                done = False
            if done:
                self.p_ack = 1
                self.run_t0 = None

    def status_word(self) -> int:
        st = 0
        for name, on in (("manual", self.manual), ("auto", self.auto), ("remote", self.remote),
                         ("servo", self.servo), ("alarm", self.alarm), ("estop", self.estop),
                         ("run", self.run), ("prog_loaded", self.loaded),
                         ("servo_ready", self.servo)):
            if on:
                st |= 1 << BIT[name]
        return st

    def registers(self, addr: int, count: int) -> list[int]:
        self.tick()
        with self.lock:
            row = [0] * max(addr + count, 144)
            row[0] = self.status_word()
            row[2] = self.speed
            row[3] = self.alarm1
            row[4] = self.alarm2
            row[5] = self.prog
            for i, v in enumerate(self.joints):
                a, b, c, d = struct.pack(">f", v)
                row[10 + i * 2] = (c << 8) | d          # CDAB：低字在前
                row[11 + i * 2] = (a << 8) | b
            row[100] = self.cmd
            row[102] = self.set_speed
            row[103] = self.set_prog
            row[ACK_ADDR] = 1 if self.p_ack else 0
            row[TRIG_ADDR] = 1 if self.trig else 0
            for i in range(6):
                row[ANG0 + i] = self.p_ang[i] & 0xFFFF
            return [row[addr + i] for i in range(count)]

    def write(self, addr: int, val: int) -> None:
        now = time.time()
        sval = val - 0x10000 if val > 0x7FFF else val
        with self.lock:
            if addr == 102:
                self.set_speed = val
                return
            if addr == 103:
                self.set_prog = val            # 40104 只是"目标"；40006 在加载成功时才更新
                return
            if ANG0 <= addr < ANG0 + 6:
                self.p_ang[addr - ANG0] = sval
                return
            if addr == TRIG_ADDR:
                self.trig = val & 1
                if self.trig and self.run and self.prog == self.jog_prog:
                    self.anchor = list(self.joints)     # WAIT 放行 → 朝目标走
                    self.aim = [v / 100.0 for v in self.p_ang]
                    self.run_t0 = now
                    self.p_ack = 0
                else:
                    self.p_ack = 0
                    self.run_t0 = None
                return
            if addr != 100:
                return
            self.cmd = val
            if val & 0x2000:                       # Bit13 取消使能
                self.servo = False
                self.engage_at = None
                return
            if val & 0x0004:                       # Bit2 停止
                self.run = False
                self.run_t0 = None
                self.loaded = False             # ← 实测：停止后程序加载位回落
                self.load_at = None
                self.stop_cmds += 1
                return
            if val & 0x0008:                       # Bit3 清报警
                self.alarm = False
                self.alarm1 = self.alarm2 = 0
            if val & 0x0010:                       # Bit4 加载
                if not self._exists(self.set_prog):
                    self.loaded = False
                    self.load_at = None
                    self.alarm = True              # ← 真机实测：光是「加载」就报 5005
                    self.alarm1 = 5005
                else:
                    self.loaded = False
                    self.load_at = now + LOAD_DELAY   # ← ≈0.5s 后才置位
            if val & 0x0002:                       # Bit1 运行
                self.run_cmds += 1
                if not self._exists(self.set_prog):
                    self.alarm = True
                    self.alarm1 = 5005
                    self.run = False
                    return
                self.run = True
                self.loaded = True
                if self.prog == self.jog_prog:
                    self.aim = [v / 100.0 for v in self.p_ang]
                    self.p_ack = 0
                    self.run_t0 = None             # 常驻挂起
                else:
                    self.aim = None
                    self.anchor = list(self.joints)
                    self.run_t0 = now
            if (val & 0x1001) == 0x1001 and not self.servo and self.engage_at is None:
                self.engage_at = now + 0.55        # ← 真机实测延迟


def _handle(conn: socket.socket, robot: MockRobot) -> None:
    conn.settimeout(10.0)
    with conn:
        while True:
            try:
                head = conn.recv(7)
            except OSError:
                return
            if len(head) < 7:
                return
            txn, _proto, length, unit = struct.unpack(">HHHB", head)
            body = b""
            while len(body) < length - 1:
                chunk = conn.recv(length - 1 - len(body))
                if not chunk:
                    return
                body += chunk
            fc = body[0]
            if fc == 3:
                addr, count = struct.unpack(">HH", body[1:5])
                data = b"".join(struct.pack(">H", v) for v in robot.registers(addr, count))
                pdu = bytes([3, len(data)]) + data
            elif fc == 6:
                addr, val = struct.unpack(">HH", body[1:5])
                robot.write(addr, val)
                pdu = struct.pack(">BHH", 6, addr, val)
            else:
                pdu = bytes([fc | 0x80, 0x01])
            conn.sendall(struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu)


def start(host: str = "127.0.0.1", port: int = 15022):
    """启动 mock 从站，返回 (robot, stop_fn)。

    ⚠ stop_fn 会**同时关闭已建立的连接** —— 只 close 监听 socket 的话，
    已有连接仍然活着，被测程序根本感知不到"断链"。
    """
    robot = MockRobot()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    stop_evt = threading.Event()
    conns: list[socket.socket] = []
    conns_lock = threading.Lock()

    def serve():
        while not stop_evt.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conns_lock:
                conns.append(conn)
            threading.Thread(target=_handle, args=(conn, robot), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.2)

    def stop():
        stop_evt.set()
        try:
            srv.close()
        except OSError:
            pass
        with conns_lock:
            for c in conns:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    c.close()
                except OSError:
                    pass
            conns.clear()

    return robot, stop


if __name__ == "__main__":
    import sys
    r, _stop = start(port=int(sys.argv[1]) if len(sys.argv) > 1 else 15022)
    print("mock 从站已启动（Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
