# -*- coding: utf-8 -*-
"""埃夫特 EFORT (RP-2 控制器) Modbus TCP 客户端 —— 纯协议层，不含任何 UI 依赖。

设计依据全部来自实机验证（详见 README.md）：
  · 从站号 1（0 无应答）；FC3 读 / FC6 写；MBAP 7 字节头。
  · 一次读 **32** 个寄存器（地址 0 起）最稳 —— J1~J6 无法单读，只能大块"夹带"。
    读数量白名单 1..8,10,12,16,20,24,32,48,64；**23 确定性失败**。
  · FC6 响应体 [fc][addr_hi][addr_lo][val_hi][val_lo] → **取值在 body[3:5]**，不是 [2:4]。
  · 关节角 FLOAT 占 2 寄存器、**CDAB 字序**。
  · 命令字必须"同沿"单条写入，且始终保留 Bit0(上伺服) + Bit12(伺服使能)。
  · 伺服吸合延迟 ≈0.55s（三次实测）→ 采样必须 ≤100ms 才不会误判"没上电"。
  · 点动走官方手册 15.4.5 的用户变量区：40139~44(目标角)/40135.Bit0(触发)/
    40035.Bit0(完成) —— 对应 RPL 侧 fidbus.mtcp_ro_* / mtcp_wo_*（见 200.XPL）。
"""
from __future__ import annotations

import socket
import struct
import time

# ---------------------------------------------------------------- 寄存器地址
ADDR_STATUS = 0            # 40001 状态位
ADDR_SPEED = 2             # 40003 运行速度
ADDR_ALARM1 = 3            # 40004 报警代码 1
ADDR_ALARM2 = 4            # 40005 报警代码 2
ADDR_PROG = 5              # 40006 程序号
ADDR_JOINT1 = 10           # 40011 J1 关节角起点（6 轴 × 2 寄存器）
ADDR_CMD = 100             # 40101 指令位
ADDR_SET_SPEED = 102       # 40103 速度设定
ADDR_SET_PROG = 103        # 40104 目标程序号

#: 点动参数区 —— **官方手册《C30 RPL 指令手册》15.4.5 ModbusTCP 映射表**（RP-2 实测吻合）：
#:
#:   接收端用户变量（PC 写 → 机器人 RPL 读 `fidbus.mtcp_ro_*`）：
#:     40135~40138  BOOL 位区  → fidbus.mtcp_ro_b[0..63]    （Bit0 = 点动触发）
#:     40139~40144  I16        → fidbus.mtcp_ro_i[0..5]     （J1~J6 目标绝对角 ×100）
#:   发送端用户变量（机器人写 `fidbus.mtcp_wo_*` → PC 读）：
#:     40035~40038  BOOL 位区  → fidbus.mtcp_wo_b[0..63]    （Bit0 = 点动完成）
#:
#: ⚠ 历史教训：40105~40110 **不是**用户寄存器（40105=附加轴轴号选择、40106=附加轴
#:   速度、40107~40110 系统预留），往那里写目标角是无效设计 —— 已废弃。
#:
#: 设计要点：EFORT 的运动指令是 `MJOINT(POINTJ(j1..j6), v速度, fine, tool)`，
#: 目标是**绝对关节角**而非增量。PC 端本来就能实时读到 J1~J6（40011~40022，CDAB），
#: 于是由 **PC 自己算好"当前角 + 增量"的绝对目标**写下来，控制器上的常驻点动程序
#: （200.XPL）就退化成 "WAIT 触发 → 读 6 个数 → POINTJ → MJOINT → 置完成位"。
ADDR_JOG_ANG = 138         # 40139~40144 = fidbus.mtcp_ro_i[0..5]（6 轴目标角）
JOG_ANG_COUNT = 6
ANG_SCALE = 100            # 角度 ×100 存整数；int16 范围 ±327.67°（覆盖全部关节行程）
#    量化 0.01°，点动档位最小 1° —— 无感。落在合法单读窗口 40135~40144，可回读验证。
ADDR_RO_TRIG = 134         # 40135 = fidbus.mtcp_ro_b 位区（Bit0 = 点动触发，电平）
ADDR_WO_STAT = 34          # 40035 = fidbus.mtcp_wo_b 位区（Bit0 = 点动完成）
JOG_TRIG_MASK = 0x0001     # 40135 触发位掩码（Bit0；Bit1~15 预留扩展）
#: 点动服务程序号 —— **0 = 未指定**。
#:
#: ⚠ 这里**故意不预设任何号**（2026-09-23 用户要求："不要猜程序名称也要可能是英文"）：
#:   现场扫过一次，1~999 里只有 410 / 411 存在，从来没有过 200 —— 之前硬编码 200 只是
#:   凭空假设，只会让【一键就绪】每次都白报一次 5005。谁在控制器上有程序、它叫什么，
#:   只有用户自己知道 → 由界面上的【程序清单】维护（app/progstore.py）。
DEFAULT_JOG_PROG = 0

READ_BLOCK = 32            # 系统区一次读 32（≥22 即可，32 最稳，勿改 23）
CMD_BLOCK = 10             # 指令区窗口 40101~40110

# ---------------------------------------------------------------- 命令字
SERVO_WORD = 0x1001        # Bit0 上伺服 + Bit12 伺服使能（同沿吸合的保持字）
LOAD_WORD = SERVO_WORD | 0x0010     # + Bit4 加载程序
RUN_WORD = SERVO_WORD | 0x0013      # + Bit4 + Bit1 运行
STOP_WORD = SERVO_WORD | 0x0004     # + Bit2 停止
CLEAR_WORD = SERVO_WORD | 0x0008    # + Bit3 清报警
DISABLE_WORD = 0x2000      # Bit13 取消伺服使能（用于造下降沿）
ZERO_WORD = 0x0000         # 全清

# ---------------------------------------------------------------- 状态位
BIT = {
    "manual": 0, "auto": 1, "remote": 2, "servo": 3, "alarm": 4,
    "estop": 5, "run": 6, "safe1": 7, "safe2": 8, "safe3": 9, "safe4": 10,
    "prog_loaded": 11, "servo_ready": 12, "prog_reserve": 13, "prog_reset": 14,
}
CMD_BIT = {
    "servo_on": 0, "run": 1, "stop": 2, "clear_alarm": 3, "load": 4,
    "servo_enable": 12, "servo_disable": 13,
}


def bit(word: int, name: str) -> int:
    return (word >> BIT[name]) & 1


def f32_cdab(hi: int, lo: int) -> float:
    """CDAB 字序还原 FLOAT（已实证）。"""
    return struct.unpack(">f", ((lo << 16) | hi).to_bytes(4, "big"))[0]


class ModbusError(Exception):
    pass


class EfortClient:
    """一次连接、顺序访问。非线程安全 —— 请由单个工作线程持有。"""

    def __init__(self, host: str = "192.168.1.12", port: int = 502, timeout: float = 1.5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.unit = 1
        self._txn = 0

    # ------------------------------------------------------------ 连接
    @property
    def is_open(self) -> bool:
        return self.sock is not None

    def connect(self) -> str:
        """返回人类可读的结果字符串；失败抛 ModbusError。"""
        self.close()
        t0 = time.time()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self._txn = 0
        # 从站号探测：1 优先，0 兜底
        for u in (1, 0):
            self.unit = u
            if self._read(ADDR_STATUS, 1) is not None:
                return (f"{self.host}:{self.port} 从站号 {u} "
                        f"({(time.time() - t0) * 1000:.0f} ms)")
        self.close()
        raise ModbusError(f"TCP 已连上 {self.host}:{self.port}，但 Modbus 无应答（从站号 1/0 均无响应）")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    # ------------------------------------------------------------ 底层收发
    def _mbap(self, pdu: bytes) -> bytes:
        self._txn = (self._txn + 1) & 0xFFFF
        return struct.pack(">HHHB", self._txn, 0, len(pdu) + 1, self.unit) + pdu

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            try:
                c = self.sock.recv(n - len(buf))
            except (socket.timeout, OSError):
                return None
            if not c:
                return None
            buf += c
        return buf

    def _read(self, addr: int, count: int) -> list[int] | None:
        if self.sock is None:
            return None
        try:
            self.sock.sendall(self._mbap(struct.pack(">BHH", 3, addr, count)))
        except OSError:
            return None
        h = self._recv_exact(7)
        if h is None:
            return None
        _t, _p, ln, _u = struct.unpack(">HHHB", h)
        b = self._recv_exact(max(ln - 1, 0))
        if b is None or len(b) < 2 or (b[0] & 0x80) or b[0] != 3:
            return None
        n = b[1]
        d = b[2:2 + n]
        if len(d) < n:
            return None
        return [struct.unpack(">H", d[i:i + 2])[0] for i in range(0, n, 2)]

    def write_reg(self, addr: int, value: int) -> int:
        """FC6 写单寄存器，返回控制器回显值（校验用）。"""
        if self.sock is None:
            raise ModbusError("未连接")
        try:
            self.sock.sendall(self._mbap(struct.pack(">BHH", 6, addr, value & 0xFFFF)))
        except OSError as e:
            raise ModbusError(f"发送失败: {e}") from e
        h = self._recv_exact(7)
        if h is None:
            raise ModbusError("写超时（无响应）")
        _t, _p, ln, _u = struct.unpack(">HHHB", h)
        b = self._recv_exact(max(ln - 1, 0))
        if b is None or len(b) < 2:
            raise ModbusError("写响应不完整")
        if b[0] & 0x80:
            raise ModbusError(f"Modbus 异常码 {b[1]}")
        if b[0] != 6:
            raise ModbusError(f"意外功能码 {b[0]}")
        return struct.unpack(">H", b[3:5])[0]      # ← 实测取值位置

    # ------------------------------------------------------------ 语义接口
    def snapshot(self) -> dict | None:
        """读一帧完整快照（系统区 32 + 指令区 10 + 点动用户区 10+1）。"""
        regs = self._read(0, READ_BLOCK)
        if regs is None:
            return None
        cmd = self._read(ADDR_CMD, CMD_BLOCK)
        ro = self._read(ADDR_RO_TRIG, 10)          # 40135~40144
        wo = self._read(ADDR_WO_STAT, 1)           # 40035
        st = regs[ADDR_STATUS]
        return {
            "t": time.time(),
            "st": st,
            "bits": {k: bit(st, k) for k in BIT},
            "speed": regs[ADDR_SPEED],
            "alarm1": regs[ADDR_ALARM1],
            "alarm2": regs[ADDR_ALARM2],
            "prog": regs[ADDR_PROG],
            "joints": [f32_cdab(regs[ADDR_JOINT1 + i * 2], regs[ADDR_JOINT1 + i * 2 + 1])
                       for i in range(6)],
            "cmd_word": cmd[ADDR_CMD - ADDR_CMD] if cmd else None,
            "set_speed": cmd[ADDR_SET_SPEED - ADDR_CMD] if cmd else None,
            "set_prog": cmd[ADDR_SET_PROG - ADDR_CMD] if cmd else None,
            "jog_trig": (ro[0] & JOG_TRIG_MASK) if ro else None,      # 40135.Bit0
            "jog_ro": ro[4:10] if ro else None,                        # 40139~44 目标角×100
            "jog_done": (wo[0] & JOG_TRIG_MASK) if wo else None,       # 40035.Bit0
        }

    def cmd(self, word: int) -> int:
        """写 40101 指令字。"""
        return self.write_reg(ADDR_CMD, word)

    def servo_on(self) -> int:
        return self.cmd(SERVO_WORD)

    def servo_disable(self) -> None:
        """取消使能 + 全清命令字。

        ⚠ 实测：写 `0x0000` 控制器常常**不回正常应答**（超时/异常），但命令是生效的，
        所以这里必须容忍异常，否则会把一次成功的取消使能记成失败。
        """
        self.cmd(DISABLE_WORD)
        try:
            self.cmd(ZERO_WORD)
        except ModbusError:
            pass

    def servo_reengage(self) -> int:
        """伺服实际为 0 时的重新吸合：**0x0000（全清）→ 等 0.6s → 0x1001**。

        实测（2026-09-21）：
          · 用 `0x2000` 造下降沿后只等 0.3s 就重发 `0x1001` → **吸不上**（0.3s < 0.55s 吸合延迟）；
          · 全清后等 0.6s 再写 `0x1001` → **t≈0.56s 吸合成功**。
        所以这里的等待时间必须 ≥ 吸合延迟，取 0.6s。
        """
        try:
            self.cmd(ZERO_WORD)
        except ModbusError:
            pass
        time.sleep(0.6)
        return self.cmd(SERVO_WORD)

    def clear_alarm_full(self) -> tuple[int, int | None]:
        """清报警 → 等 0.4s → **若伺服掉了就自动重新吸合**。

        返回 (清报警回显, 吸合回显或 None)。
        """
        a = self.clear_alarm()
        time.sleep(0.4)
        s = self.snapshot()
        if s is not None and not s["bits"]["servo"]:
            return a, self.servo_reengage()
        self.cmd(SERVO_WORD)
        return a, None

    def clear_alarm(self) -> int:
        return self.cmd(CLEAR_WORD)

    def stop(self) -> int:
        return self.cmd(STOP_WORD)

    def set_speed(self, v: int) -> int:
        return self.write_reg(ADDR_SET_SPEED, v)

    def set_program(self, n: int) -> int:
        return self.write_reg(ADDR_SET_PROG, n)

    def load_program(self, n: int, speed: int | None = None,
                     wait: float = 2.0) -> tuple[bool, str]:
        """速度兜底 → 目标程序号 → 0x1011 加载 → 等 Bit11 程序加载位。"""
        if speed is not None:
            self.set_speed(speed)
        self.set_program(n)
        self.cmd(LOAD_WORD)
        t0 = time.time()
        while time.time() - t0 < wait:
            s = self.snapshot()
            if s and s["bits"]["prog_loaded"]:
                return True, f"程序 {n} 已加载（{(time.time() - t0):.2f}s）"
            time.sleep(0.1)
        return False, f"程序加载位 {wait:.0f}s 内未置位"

    def run_program(self) -> int:
        return self.cmd(RUN_WORD)

    # ------------------------------------------------------------ 点动用户区（40135~40144 / 40035）
    def jog_window(self) -> list[int] | None:
        """读 40135~40144（合法单读窗口）：[0]=触发位区，[4..9]=J1~J6 目标角×100。"""
        return self._read(ADDR_RO_TRIG, 10)

    def read_jog_done(self) -> bool | None:
        """读 40035.Bit0 —— 点动程序写的「上一发已到位」完成标志。None=读失败。"""
        w = self._read(ADDR_WO_STAT, 1)
        return None if w is None else bool(w[0] & JOG_TRIG_MASK)

    @staticmethod
    def encode_angle(deg: float) -> int:
        """角度 → 有符号 16 位整数（×100，补码）。"""
        return int(round(deg * ANG_SCALE)) & 0xFFFF

    @staticmethod
    def decode_angle(word: int) -> float:
        v = word - 0x10000 if word > 0x7FFF else word
        return v / ANG_SCALE

    def write_jog_angles(self, angles: list[float]) -> str:
        """写 J1~J6 的**绝对目标角**到 40139~40144（6 发 FC6）。不置触发位。

        触发位（40135.Bit0）由 trigger_jog() 单独置 —— 保证「角度先落位、
        后触发」的顺序，点动程序绝不会读到半截数据。
        """
        for i, a in enumerate(angles[:JOG_ANG_COUNT]):
            self.write_reg(ADDR_JOG_ANG + i, self.encode_angle(a))
        txt = " ".join(f"J{i + 1}={a:+.2f}" for i, a in enumerate(angles[:JOG_ANG_COUNT]))
        return f"目标姿态 {txt}"

    def trigger_jog(self) -> int:
        """置 40135.Bit0 = 1 —— 点动程序 WAIT 到后开始 MJOINT。"""
        return self.write_reg(ADDR_RO_TRIG, JOG_TRIG_MASK)

    def clear_trigger(self) -> int:
        """撤 40135 触发位（写 0x0000）—— 点动程序回到 WAIT 挂起。"""
        return self.write_reg(ADDR_RO_TRIG, 0x0000)

    def param_selftest(self) -> list[tuple[str, bool, str]]:
        """探测点动用户区是否真的可写（写哨兵 → 读回 → 还原 0）。

        **可逆的非动作类写入**：只碰 40139~40144（目标角数据）和 40135
        （且触发位只写 **0x0000** —— 绝不写 0x0001，否则若 200 号程序正在
        WAIT 会被立即触发运动）。不碰 40101 指令位。
        """
        out: list[tuple[str, bool, str]] = []
        sentinels = [0x0101, 0x0202, 0x0303, 0x0404, 0x0505, 0x0606]
        addrs = [ADDR_JOG_ANG + i for i in range(JOG_ANG_COUNT)]
        for a, v in zip(addrs, sentinels):
            try:
                self.write_reg(a, v)
            except ModbusError as e:
                out.append((f"40{a + 1:03d}", False, f"写失败 {e}"))
        back = self.jog_window()
        for i, (a, v) in enumerate(zip(addrs, sentinels)):
            got = back[4 + i] if back and 4 + i < len(back) else None
            out.append((f"40{a + 1:03d}", got == v,
                        f"写 0x{v:04X} → 读 0x{got:04X}" if got is not None else "读回失败"))
        try:                                   # 触发位寄存器：只验证可写 0（安全值）
            self.write_reg(ADDR_RO_TRIG, 0x0000)
            back = self.jog_window() or []
            got = back[0] if back else None
            out.append(("40135", got == 0, f"写 0x0000 → 读 0x{got or 0:04X}（触发位只测 0，防误触发）"))
        except ModbusError as e:
            out.append(("40135", False, f"写失败 {e}"))
        for a in [ADDR_RO_TRIG] + addrs:       # 全部还原
            try:
                self.write_reg(a, 0)
            except ModbusError:
                pass
        return out

    # ------------------------------------------------------------ 只读自检
    @staticmethod
    def selftest(host: str) -> int:
        c = EfortClient(host)
        print("=== 协议层只读自检（仅 FC3）===")
        print("连接:", c.connect())
        s = c.snapshot()
        if s is None:
            print("❌ 读快照失败")
            return 1
        b = s["bits"]
        print(f"状态: 自动={b['auto']} 远程={b['remote']} 伺服={b['servo']} "
              f"报警={b['alarm']}({s['alarm1']}/{s['alarm2']}) 急停={b['estop']} "
              f"程序运行={b['run']} 程序加载={b['prog_loaded']}")
        print(f"速度={s['speed']}  程序号={s['prog']}  40101=0x{s['cmd_word']:04X}")
        print("关节角: " + "  ".join(f"J{i + 1}={v:+.3f}" for i, v in enumerate(s["joints"])))
        c.close()
        print("✅ 自检完成（无任何写入）")
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(EfortClient.selftest(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.12"))
