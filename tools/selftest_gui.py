# -*- coding: utf-8 -*-
"""端到端自检：本地 mock Modbus 从站 + 真实 PyQt6 界面（不碰真机）。

覆盖链路：界面按钮 → RobotWorker 命令队列 → EfortClient(FC3/FC6) → 控制器语义。
用法: python tools/selftest_gui.py

2026-09-23 改造后新增/重写的用例：
  · [11b]       测试跑**定时**：跑 2 秒自动停（不依赖越界）
  · [14]        扫描结果**自动并入**程序清单（而不是覆盖下拉）
  · [17]        程序清单：添加 / 英文名 / 落盘 / 重载 / 删除 / 设为点动服务
  · [18]        点位记录（示教点）：记录 / 校验 / 落盘 / 改名 / 删除 / 去点位全链路
  · [6] / [11]  点动服务程序的号由**用户清单**决定（不再硬编码 200）
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QShortcut                                    # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox                # noqa: E402

from app.pointstore import PointStore                                # noqa: E402
from app.progstore import ProgramStore                               # noqa: E402
from app.window import MainWindow                                    # noqa: E402
from mock_slave import JOINTS0, JOG_PROG, start                      # noqa: E402

HOST, PORT = "127.0.0.1", 15022
TMP_STORE = os.path.join(ROOT, "tools", "_tmp_programs.json")
TMP_POINTS = os.path.join(ROOT, "tools", "_tmp_points.json")
JOB_A, JOB_B = 410, 411

results: list[bool] = []


def check(name: str, cond, extra: str = "") -> None:
    results.append(bool(cond))
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))


def main() -> int:
    for p in (TMP_STORE, TMP_STORE + ".bad", TMP_POINTS, TMP_POINTS + ".bad"):
        if os.path.exists(p):
            os.remove(p)

    robot, stop_mock = start(HOST, PORT)
    app = QApplication(sys.argv[:1])
    # ⚠ 用临时清单文件，绝不碰用户真实的 programs.json / points.json
    win = MainWindow(host=HOST, log_path=None, store_path=TMP_STORE,
                     points_path=TMP_POINTS)
    win.resize(1360, 920)
    win.e_host.setText(HOST)            # ← 必须指向 mock，否则会去连真机默认 IP
    win.e_port.setValue(PORT)

    # 白盒：先把"程序清单"填成现场该有的样子 —— 号由人定，名字可以是英文
    win.store.add(JOG_PROG, "JOGSVC", "点动服务程序")
    win.store.add(JOB_A, "PICK_A", "上料")
    win.store.add(JOB_B, "WELD_B", "")
    win.store.set_jog(JOG_PROG)
    win._refresh_prog_combo(keep=JOG_PROG)
    win.w.post("jog_prog", JOG_PROG)
    robot.jog_prog = JOG_PROG

    win.show()

    def log() -> str:
        return win.log_view.toPlainText()

    def pump(seconds: float, predicate=None) -> bool:
        t0 = time.time()
        while time.time() - t0 < seconds:
            app.processEvents()
            time.sleep(0.02)
            if predicate and predicate():
                return True
        return not predicate if predicate else True

    def step_move(axis: int, size: str, direction: float) -> None:
        """点一下 J{axis} 的 ± 按钮：先选步长，再走一步。"""
        win.cb_step.setCurrentText(size)
        win._on_jog(axis, direction)

    def sel(no: int) -> bool:
        """在下拉里选中某个程序号（号存在 item data 里）。"""
        i = win.cb_prog.findData(no)
        if i < 0:
            return False
        win.cb_prog.setCurrentIndex(i)
        return True

    print("=== PyQt6 界面端到端自检（mock 从站，无真机动作）===\n")

    print("[0] 程序清单（用户自己维护，不猜号/名）")
    check("清单里 3 条", len(win.store.items()) == 3, str(win.store.numbers()))
    check("下拉显示「号 · 名字」", win.cb_prog.count() == 3
          and win.cb_prog.itemText(0).startswith("%d · JOGSVC" % JOG_PROG),
          win.cb_prog.itemText(0))
    check("点动服务程序 = %d（用户声明的那个）" % JOG_PROG, win.store.jog == JOG_PROG)
    check("清单文件已落盘", os.path.exists(TMP_STORE))

    print("\n[1] 连接（只读）")
    win._on_connect()
    check("TCP + Modbus 建链成功", pump(6, lambda: "已连接" in log()))
    check("状态胶囊读到 自动=1", win.pills["auto"].property("on") == "info")
    check("伺服初始未上电", not win.pills["servo"].property("on"))
    check("关节角已刷新（6 个坐标显示）", win.jrows[3].v_deg.text() != "--",
          "J4=%s" % win.jrows[3].v_deg.text())
    check("关节行 = 6 轴 × 2 个 ± 按钮 = 12 个",
          sum(len(r.buttons) for r in win.jrows) == 12)
    check("已注册 Esc 快捷键", len(win.findChildren(QShortcut)) >= 1)
    check("默认步长 1°", win.cb_step.currentText() == "1°")

    print("\n[2] 安全闸门")
    check("未勾选确认时 ± 按钮锁定", not win.jrows[0].buttons[0].isEnabled())
    win.c_gate.setChecked(True)
    pump(0.2)
    check("勾选后 ± 按钮解锁", win.jrows[0].buttons[0].isEnabled())

    print("\n[3] 伺服上电（0.55s 延迟）")
    win.b_son.click()
    pump(0.3)
    check("0.3s 时尚未上电", not win.pills["servo"].property("on"),
          "mock servo=%s" % robot.servo)
    check("吸合后变为已上电",
          pump(4, lambda: win.pills["servo"].property("on") == "good"))

    print("\n[3b] 一键就绪 → 自动拉起点动服务 %d（清单里声明的那个）" % JOG_PROG)
    win.w.post("ready", HOST, PORT)
    check("点动服务程序已加载（0.5s 加载位延迟下仍成功）",
          pump(10, lambda: "④ 程序 %d 已加载" % JOG_PROG in log()))
    check("点动服务已挂起等待指令", pump(6, lambda: "已挂起等待指令" in log()))
    check("服务运行位常亮（mock run=True）", robot.run and robot.prog == JOG_PROG)
    check("挂起期间机器人没动", robot.joints == JOINTS0)
    check("就绪按钮恢复可点", pump(3, lambda: win.b_ready.isEnabled()))

    print("\n[4] ± 步进：J4 正向走 5°（步长选 5°）")
    win.sp_olim.setValue(1.5)
    j4_0 = robot.joints[3]
    step_move(4, "5°", 1.0)
    check("已写入目标绝对角 J4×100 = 1746（12.456+5=17.456）",
          pump(3, lambda: robot.p_ang[3] == 1746), "40142=%d" % robot.p_ang[3])
    check("其余轴目标角保持原值",
          robot.p_ang[0] == 7334 and robot.p_ang[4] == 10772,
          "40139=%d 40143=%d" % (robot.p_ang[0], robot.p_ang[4]))
    check("触发位已置起（40135.Bit0=1）", pump(2, lambda: robot.trig == 1))
    check("熔断已武装（UI 显示）", pump(2, lambda: "熔断已武装" in win.l_fuse.text()))
    check("UI 标为【点动】档", "点动：J4" in win.l_fuse.text(), win.l_fuse.text())
    check("熔断期间 ± 按钮被锁住（防误触覆盖）",
          not win.jrows[3].buttons[1].isEnabled())
    check("点动完成并报告位移（读到 40035.Bit0 回执后自动撤触发）",
          pump(8, lambda: "点动完成（程序回执" in log() and robot.trig == 0))
    dj4 = robot.joints[3] - j4_0
    check("J4 实际位移 ≈ +5°", abs(dj4 - 5.0) < 0.6, "ΔJ4=%+.2f°" % dj4)
    check("熔断已解除", pump(3, lambda: win.fuse is None))
    check("熔断解除后 ± 按钮恢复", pump(2, lambda: win.jrows[3].buttons[1].isEnabled()))

    print("\n[5] ± 步进：J1 负向走 1°（步长选 1°）")
    j1_0 = robot.joints[0]
    step_move(1, "1°", -1.0)
    check("已写入目标绝对角 J1×100 = 7234（73.340−1=72.340）",
          pump(3, lambda: robot.p_ang[0] == 7234), "40139=%d" % robot.p_ang[0])
    check("点动完成", pump(8, lambda: log().count("点动完成") >= 2))
    check("J1 实际位移 ≈ −1°", abs((robot.joints[0] - j1_0) + 1.0) < 0.4,
          "ΔJ1=%+.2f°" % (robot.joints[0] - j1_0))

    print("\n[5b] 步长选择器生效（10° 档）")
    j6_0 = robot.joints[5]
    n_done = log().count("点动完成")
    step_move(6, "10°", 1.0)
    check("目标角按 10° 步长写入（−0.927+10=9.073 → 907）",
          pump(3, lambda: robot.p_ang[5] == 907), "40144=%d" % robot.p_ang[5])
    check("该次点动完成", pump(12, lambda: log().count("点动完成") > n_done))
    check("J6 实际位移 ≈ +10°", abs((robot.joints[5] - j6_0) - 10.0) < 1.0,
          "ΔJ6=%+.2f°" % (robot.joints[5] - j6_0))

    print("\n[5c] 熔断重入保护")
    step_move(4, "10°", 1.0)
    pump(0.4)
    n_before = log().count("点动被拒：上一次动作仍在进行中")
    step_move(5, "10°", 1.0)               # 熔断武装期间再点另一轴
    pump(1.0)
    check("熔断期间再次点动被拒",
          log().count("点动被拒：上一次动作仍在进行中") > n_before)
    pump(12, lambda: win.fuse is None)

    print("\n[6] 回归：点动服务程序不存在 → 就绪拉起失败 + 对照诊断 + 点动安全拒绝")
    win.w.post("stop")
    pump(1.0)
    robot.missing = {JOG_PROG}
    robot.prog = 123                       # 白盒：模拟真机"控制器当前加载的是 123"
    win.w.post("jog_prog", JOG_PROG)
    pump(1.5)
    runs_before = log().count("WAIT 放行")
    j4_before = robot.joints[3]
    win.w.post("ready", HOST, PORT)
    pump(10)
    check("就绪时报告点动服务程序加载失败", "④ 点动服务程序 %d 加载失败" % JOG_PROG in log())
    check("对照诊断：用当前程序号 123 验证了加载机制", "对照：已加载程序 123" in log())
    check("对照诊断结论正确（问题在 %d 号本身）" % JOG_PROG,
          "问题就在 %d 号本身" % JOG_PROG in log())
    check("识别出加载失败引发的报警 5005", "已引发报警 5005" in log())
    check("报警被自动清除", robot.alarm1 == 0, "报警码=%d" % robot.alarm1)
    check("40104 恢复为控制器当前程序号", robot.set_prog == robot.prog,
          "40104=%d 当前程序号=%d" % (robot.set_prog, robot.prog))
    check("全程未触发任何程序运行（WAIT 放行次数不变）",
          log().count("WAIT 放行") == runs_before)
    step_move(4, "1°", 1.0)
    pump(3)
    check("点动被锁存拒绝（不再反复触发报警）", "不再重复重试" in log())
    check("关节未动", abs(robot.joints[3] - j4_before) < 1e-6)
    robot.missing = set()
    win.w.post("jog_prog", JOG_PROG)
    pump(1.0)
    n_arm = log().count("已挂起等待指令")          # ← 计数比较：这串字前面已出现过
    win.w.post("ready", HOST, PORT)
    check("重新就绪后点动服务已挂起",
          pump(10, lambda: log().count("已挂起等待指令") > n_arm))
    check("加载成功后失败锁存已自动解除", win.w.jog_prog_bad is None)

    print("\n[6c] 未指定点动服务程序 → 明确跳过（不瞎试号、不白报 5005）")
    runs0, alarms0 = robot.run_cmds, robot.alarm1
    win.w.post("jog_prog", 0)
    pump(0.6)
    win.w.post("ready", HOST, PORT)
    check("就绪日志明确说「未指定点动服务程序」",
          pump(8, lambda: "未指定点动服务程序" in log()))
    check("没有多发任何运行命令", robot.run_cmds == runs0)
    check("没有引发任何报警", robot.alarm1 == alarms0, "报警=%d" % robot.alarm1)
    check("状态标为「未设置」", win.w.jog_status[0] == "unset")
    step_move(4, "1°", 1.0)
    pump(2)
    check("点动被明确拒绝并指路", "还没指定**点动服务程序**" in log())
    n_rej = log().count("还没指定")
    step_move(4, "1°", 1.0)
    pump(1)
    check("再点也不会发触发位", robot.trig == 0 and log().count("还没指定") > n_rej)
    win.w.post("jog_prog", JOG_PROG)
    pump(0.6)
    win.w.post("ready", HOST, PORT)
    check("重新指定后恢复挂起", pump(10, lambda: win.w.jog_status[0] == "armed"),
          "jog_status=%s" % (win.w.jog_status,))

    print("\n[6b] 手动模式：点动服务【不】拉起（手册：T1/T2 下外部控制被禁用）")
    robot.auto = 0
    runs0 = robot.run_cmds
    win.w._set_jog_status("unknown", JOG_PROG)
    win.w.post("ready", HOST, PORT)
    check("拒绝拉起并提示切【自动】", pump(6, lambda: "不在【自动】模式" in log()))
    check("未发出任何程序运行命令（机器人不会动）", robot.run_cmds == runs0,
          "运行命令 %d → %d" % (runs0, robot.run_cmds))
    pump(1.5)
    check("界面弹出【手动】模式告警", "处于【手动】模式" in log())
    robot.auto = 1
    n_arm = log().count("已挂起等待指令")
    win.w.post("ready", HOST, PORT)
    check("切回自动后可正常拉起", pump(10, lambda: log().count("已挂起等待指令") > n_arm))

    print("\n[7] 熔断：让 mock 额外把 J2 溜出去（rogue）")
    robot.rogue = True
    before = log().count("熔断触发")
    step_move(4, "5°", 1.0)
    check("熔断自动触发", pump(12, lambda: log().count("熔断触发") > before))
    check("是【其他轴越界】分支", "其他轴越界 J2" in log())
    check("已下发停止位 0x1005", robot.stop_cmds >= 1, "stop_cmds=%d" % robot.stop_cmds)
    check("触发位已被撤下（防程序重跑残留触发自行运动）", robot.trig == 0)
    check("机器人已停止", not robot.run or robot.run_t0 is None)
    check("熔断后恢复仅保持伺服（0.5s 后异步补发）",
          pump(3, lambda: robot.cmd == 0x1001), "40101=0x%04X" % robot.cmd)
    j4_f = robot.joints[3]
    pump(1.0)
    check("停止后关节冻结", abs(robot.joints[3] - j4_f) < 1e-6)
    robot.rogue = False

    print("\n[8] 急停（Esc）")
    stops0 = robot.stop_cmds
    win._on_estop()
    pump(1.0)
    check("急停撤触发位", robot.trig == 0)
    check("急停下发停止字", "急停" in log() and robot.stop_cmds > stops0,
          "stop_cmds=%d" % robot.stop_cmds)

    print("\n[9] 一键就绪：断开 + 带报警 + 伺服掉电 → 一次点完恢复（含点动服务）")
    win._on_disconnect()
    pump(0.5)
    robot.alarm = True
    robot.alarm1 = 5005
    robot.servo = False
    win.w._set_jog_status("unknown", JOG_PROG)
    win.c_gate.setChecked(False)
    pump(0.2)
    done0 = log().count("就绪完成")        # 上一节已出现过，必须等"新的"一次
    win._on_ready()
    check("一键就绪自动连接", pump(8, lambda: "① 已连接" in log()))
    check("一键就绪自动清报警", pump(8, lambda: robot.alarm1 == 0),
          "报警码=%d" % robot.alarm1)
    check("一键就绪自动伺服上电", pump(8, lambda: robot.servo))
    check("一键就绪自动拉起点动服务", pump(10, lambda: "已挂起等待指令" in log()))
    check("提示「就绪完成」", pump(8, lambda: log().count("就绪完成") > done0))
    check("现场安全确认被自动勾选", win.gate)
    check("就绪按钮已恢复可点", pump(3, lambda: win.b_ready.isEnabled()))
    check("就绪后 ± 按钮可用", win.jrows[3].buttons[1].isEnabled())

    print("\n[10] 日志")
    check("日志有内容", len(log().strip().splitlines()) > 8)

    # ============================ 测试跑 / 执行 分档 ============================
    print("\n[11] 测试跑：强制 5% 低速 + 越界熔断【真的会掐停】")
    robot.auto = 1
    win.sp_speed.setValue(30)
    win.b_speed.click()
    pump(1.5)
    check("速度已按界面设为 30%", robot.set_speed == 30, "40103=%d" % robot.set_speed)
    check("清单里选中作业程序 %d（不是点动服务程序）" % JOB_A, sel(JOB_A))
    win.cb_secs.setCurrentText("5")
    win.w.post("ready", HOST, PORT)
    check("前置：点动服务处于运行中", pump(15, lambda: win.w.jog_status[0] == "armed"),
          "jog_status=%s" % (win.w.jog_status,))
    # 作业程序让 J2 大幅运动 —— 复现 14:52 现场那次 "J2 5.63° > 5°" 的场景
    robot.job_rate = 120.0
    robot.job_secs = 60.0                  # 程序不会自己结束，只能被熔断掐停
    n_trig = log().count("熔断触发")
    n_run = log().count("（运行程序")
    n_rec = log().count("已自动恢复点动服务")
    n_pause = log().count("已暂停点动服务")
    win._on_run("test")
    check("测试跑一键下发（不需要点两次）",
          pump(12, lambda: log().count("（运行程序") > n_run))
    check("测试跑强制 5%（忽略界面设的 30%）", robot.set_speed == 5,
          "40103=%d" % robot.set_speed)
    check("日志声明低速 + 定时", "测试跑：程序 %d @ **5%% 低速**" % JOB_A in log()
          and "跑 5.0 秒自动停" in log())
    check("运行前自动暂停了点动服务", log().count("已暂停点动服务") > n_pause)
    check("熔断档位显示为【测试跑】",
          pump(4, lambda: "测试跑：" in win.l_fuse.text()), win.l_fuse.text())
    check("测试跑越界 → 熔断掐停", pump(15, lambda: log().count("熔断触发") > n_trig))
    check("掐停原因标为【测试跑越界 J2】", "测试跑越界 J2" in log())
    check("程序已停", pump(6, lambda: not robot.run))
    check("测试跑跑完**不**自动恢复点动",
          log().count("已自动恢复点动服务") == n_rec)

    print("\n[11b] 测试跑**定时**：跑 2 秒自动停（不靠越界，机器人几乎不动）")
    win.w.post("ready", HOST, PORT)
    check("前置：点动服务重新挂起", pump(15, lambda: win.w.jog_status[0] == "armed"))
    robot.job_rate = 0.0                   # 关节不动 → 越界熔断永远不会触发
    robot.job_secs = 60.0                  # 程序自身也不会结束
    sel(JOB_A)
    win.cb_secs.setCurrentText("2")
    check("界面时长读数为 2 秒", win._secs() == 2.0, "secs=%g" % win._secs())
    n_run = log().count("（运行程序")
    n_bound = log().count("测试跑越界")     # 上一节已经出现过一次，只看"新增的"
    stops0 = robot.stop_cmds
    j2_b = robot.joints[1]
    t_arm = time.time()
    win._on_run("test")
    check("测试跑已下发（日志写明定时）", pump(10, lambda: "跑 2.0 秒自动停" in log()))
    check("到设定时长自动停（是定时分支，不是越界）",
          pump(20, lambda: "测试跑到设定时长 2.0s" in log()))
    dt = time.time() - t_arm
    check("用时 ≈2~6 秒（含 0.5s 加载延迟）", 1.2 <= dt <= 8.0, "%.1fs" % dt)
    check("下发过停止字", robot.stop_cmds > stops0, "stop_cmds=%d" % robot.stop_cmds)
    check("程序已停", pump(6, lambda: not robot.run))
    check("本次没有触发越界熔断（证明是定时结束的）",
          log().count("测试跑越界") == n_bound)
    check("机器人 J2 没被挪动（job_rate=0）", abs(robot.joints[1] - j2_b) < 1e-6,
          "ΔJ2=%+.3f" % (robot.joints[1] - j2_b))

    print("\n[12] 执行：生产速度 + **不做越界检测**（回归 14:52 误杀）+ 跑完自动恢复")
    win.w.post("ready", HOST, PORT)
    check("前置：点动服务重新挂起", pump(15, lambda: win.w.jog_status[0] == "armed"))
    robot.job_rate = 120.0                 # 同样的大幅运动
    robot.job_secs = 2.0
    n_trig = log().count("熔断触发")
    n_run = log().count("（运行程序")
    n_rec0 = log().count("已自动恢复点动服务")
    sel(JOB_A)
    win._on_run("run")
    check("执行一键下发", pump(12, lambda: log().count("（运行程序") > n_run))
    check("执行使用界面设定速度 30%（不强制低速）", robot.set_speed == 30,
          "40103=%d" % robot.set_speed)
    check("日志明确声明【越界检测已关闭】", "越界检测已**关闭**" in log())
    check("执行档不做越界检测 → 大幅运动不再误杀",
          not pump(6, lambda: log().count("熔断触发") > n_trig))
    check("程序正常结束 → 熔断解除", pump(15, lambda: win.w.fuse is None),
          "fuse=%s" % (win.w.fuse,))
    check("跑完自动恢复点动服务",
          pump(25, lambda: log().count("已自动恢复点动服务") > n_rec0))
    check("恢复后状态为 运行中/挂起",
          pump(8, lambda: win.w.jog_status[0] == "armed"),
          "jog_status=%s" % (win.w.jog_status,))
    check("恢复后 ± 按钮可用", pump(8, lambda: win.jrows[3].buttons[1].isEnabled()))
    check("执行日志写明「直接跑完」、不带定时",
          "执行：程序 %d @ 30%%" % JOB_A in log() and "直接跑完" in log())
    robot.job_rate = 0.0

    print("\n[13] 关掉自动恢复 → 跑完不再自动拉起")
    win.c_restore.setChecked(False)
    pump(0.3)
    win.w.post("ready", HOST, PORT)
    check("前置：再次挂起", pump(15, lambda: win.w.jog_status[0] == "armed"))
    n_rec = log().count("已自动恢复点动服务")
    sel(JOB_A)
    win._on_run("run")
    check("程序结束", pump(15, lambda: "程序运行位回落，程序结束" in log()))
    pump(3)
    check("未开启时不会自动恢复", log().count("已自动恢复点动服务") == n_rec)
    win.c_restore.setChecked(True)
    pump(0.3)

    print("\n[14] 程序扫描（正反双判据）→ 结果**自动并入清单**")
    # 白盒：先清掉 410/411，验证扫描能把它们自动加回清单
    win.store.remove(JOB_A)
    win.store.remove(JOB_B)
    win._refresh_prog_combo()
    check("前置：清单里只剩点动服务程序", win.store.numbers() == [JOG_PROG],
          str(win.store.numbers()))
    robot.existing = {JOG_PROG, JOB_A, JOB_B}   # 控制器上真实存在的就这 3 个
    win.sp_lo.setValue(405)
    win.sp_hi.setValue(415)
    win.w.post("ready", HOST, PORT)
    check("前置：点动服务挂起", pump(15, lambda: win.w.jog_status[0] == "armed"))
    runs_s = robot.run_cmds                     # ← 必须在就绪**之后**取基线
    win._on_scan()
    check("扫描完成并报告命中 2 个",
          pump(90, lambda: "共命中 **2** 个程序" in log()),
          "日志尾部=%s" % log().strip()[-140:])
    check("识别出 410 存在", "✅ 410 存在" in log())
    check("识别出 411 存在", "✅ 411 存在" in log())
    check("扫到的自动并入清单（410 / 411 都在了）",
          win.store.has(JOB_A) and win.store.has(JOB_B), str(win.store.numbers()))
    check("原有点动服务程序没被冲掉", win.store.has(JOG_PROG))
    check("下拉里能选到 410", win.cb_prog.findData(JOB_A) >= 0)
    check("扫描状态里说明了新增条数", "新加进清单 2 条" in win.l_scan.text(), win.l_scan.text())
    check("日志里也落了这条结论", "新加进清单 2 条" in log(), log().strip()[-160:])
    check("扫描全程未触发运行（只加载）", robot.run_cmds == runs_s,
          "运行命令 %d → %d" % (runs_s, robot.run_cmds))
    check("扫描结束未留报警", robot.alarm1 == 0 and robot.alarm2 == 0,
          "报警=%d/%d" % (robot.alarm1, robot.alarm2))
    check("扫描后点动服务已恢复", pump(15, lambda: win.w.jog_status[0] == "armed"),
          "jog_status=%s" % (win.w.jog_status,))
    check("扫描按钮已复位", win.b_scan.isEnabled() and win.b_scan.text() == "扫描")
    robot.existing = None

    print("\n[15] 断线自动重连（放最后：它会重启 mock）")
    robot.manual, robot.auto = 0, 1
    n_down = log().count("进入自动重连")
    stop_mock()
    check("链路断开被识别为重连中",
          pump(6, lambda: log().count("进入自动重连") > n_down or win.link_state == "retry"))
    robot2, stop2 = start(HOST, PORT)
    check("链路恢复后自动重连成功",
          pump(25, lambda: "链路已恢复" in log()), "日志尾部=%s" % log().strip()[-90:])
    check("重连后提示需重新就绪", "请重新点【一键就绪】" in log())
    stop2()

    print("\n[16] 全程异常检查")
    check("全程无未捕获异常", "命令异常" not in log() and "Modbus 错误" not in log())

    print("\n[17] 程序清单：增删 / 英文名 / 落盘 / 重载 / 设为点动服务")
    st = win.store
    ok, msg = st.add(999, "HELLO_WORLD", "英文名")
    check("添加条目成功", ok and st.has(999), msg)
    check("名字允许英文且能显示", st.label(999) == "999 · HELLO_WORLD（英文名）", st.label(999))
    win._refresh_prog_combo(keep=999)
    check("下拉里能看到它", win.cb_prog.currentText().startswith("999 · HELLO_WORLD"),
          win.cb_prog.currentText())
    check("重复添加同号 = 就地改名（不会出现两条）",
          st.add(999, "HELLO_WORLD")[0] and len([n for n in st.numbers() if n == 999]) == 1)
    check("非法号（0）被拒", st.set_jog(0)[0] is False)

    st2 = ProgramStore(TMP_STORE)          # 换一个实例 = 模拟重启
    check("重启后条目还在（已落盘）", st2.has(999) and st2.get(999)["name"] == "HELLO_WORLD")
    check("重启后点动服务程序也记得", st2.jog == JOG_PROG, "jog=%d" % st2.jog)

    win._refresh_prog_combo(keep=JOB_B)
    win._on_set_jog()
    pump(0.5)
    check("通过界面把 %d 设为点动服务" % JOB_B, win.store.jog == JOB_B, "jog=%d" % win.store.jog)
    check("worker 同步收到新号", pump(2, lambda: win.w.jog_prog == JOB_B),
          "worker.jog_prog=%d" % win.w.jog_prog)
    win._refresh_prog_combo(keep=JOG_PROG)
    win._on_set_jog()
    pump(0.5)
    check("再改回 %d" % JOG_PROG, win.store.jog == JOG_PROG)

    ok, _ = st.remove(999)
    check("删除条目成功", ok and not st.has(999))
    check("删除后重新读回也不在了", not ProgramStore(TMP_STORE).has(999))

    st.set_jog(JOB_B)
    st.remove(JOB_B)
    check("删除点动服务程序会顺手取消标记", st.jog == 0, "jog=%d" % st.jog)

    with open(TMP_STORE, "w", encoding="utf-8") as fh:
        fh.write("{这不是合法 JSON")
    broken = ProgramStore(TMP_STORE)
    check("损坏的 JSON 会被留档为 .bad 而不是被覆盖",
          os.path.exists(TMP_STORE + ".bad") and broken.items() == [],
          "留档=%s" % os.path.exists(TMP_STORE + ".bad"))
    print("   （注：这一步故意写坏了临时清单 —— 只影响 tools/_tmp_programs.json）")

    print("\n[18] 点位记录（示教点）：记录 / 校验 / 落盘 / 改名 / 删除 / 去点位")
    # ⚠ [15] 那节最后把 mock 停掉了（它自带 stop2()），到这里链路是死的。
    #   所以先重启一个 mock 并重连，否则下面的"去点位"链路无从验证。
    robot, stop_mock = start(HOST, PORT)
    win.w.post("connect", HOST, PORT)
    pump(10, lambda: win.connected)
    check("前置：mock 已重启且链路已连接", win.connected, "connected=%s" % win.connected)
    ps = win.points
    ok, msg = ps.add("HOME", JOINTS0, "初始姿态")
    check("记录点位成功", ok and ps.items() and ps.items()[0]["name"] == "HOME", msg)
    check("点名重复被拒", ps.add("HOME", JOINTS0)[0] is False)
    check("角度不是 6 个被拒", ps.add("BAD1", [1, 2, 3])[0] is False)
    check("角度含非数字被拒", ps.add("BAD2", [1, 2, 3, 4, 5, "x"])[0] is False)
    check("角度含 NaN 被拒", ps.add("BAD3", [1, 2, 3, 4, 5, float("nan")])[0] is False)
    check("空名字会自动取 P<n>", ps.add("", JOINTS0)[0] and ps.items()[-1]["name"].strip() != "")

    ps2 = PointStore(TMP_POINTS)                 # 换实例 = 模拟重启
    check("重启后点位还在（已落盘）",
          len(ps2.items()) == len(ps.items()) and ps2.items()[0]["joints"] == JOINTS0,
          "n=%d/%d" % (len(ps2.items()), len(ps.items())))
    check("改名成功", ps.update(1, name="HOME_A")[0] and ps.get(1)["name"] == "HOME_A")
    check("改名撞已有名字被拒", ps.update(1, name=ps.items()[1]["name"])[0] is False)

    win._refresh_pt_list()
    check("⑥ 卡列表已按清单填充", win.lw_pt.count() == len(ps.items()),
          "count=%d" % win.lw_pt.count())
    check("能取到选中点位的号", win._pt_no() > 0, "no=%d" % win._pt_no())
    win._last_j = list(JOINTS0)
    win._on_pt_sel()
    check("位移预演显示「已在该点」", "已在该点" in win.l_pt.text(), win.l_pt.text()[:70])

    # --- 拒绝路径 1：没勾现场安全确认 ---
    win.connected, win.gate, win.fuse = True, False, None
    win._pt_no_first = None
    win.lw_pt.setCurrentRow(0)
    n0 = log().count("已被拦截")
    win._on_pt_goto()
    check("未勾安全确认 → 去点位被拦截", log().count("已被拦截") > n0)

    # --- 拒绝路径 2：点位角度非法（worker 侧，不该动） ---
    #     ⚠ 必须**投队列**让 worker 线程自己执行 —— socket 由它独占，
    #       从测试线程直接调 _do_goto 会撞上"快照读失败"（本用例第一版就踩了）。
    win.w._set_jog_status("armed", JOG_PROG)
    robot.auto, robot.servo, robot.alarm = True, True, False
    robot.alarm1 = robot.alarm2 = 0
    robot.run, robot.prog = True, JOG_PROG
    win.w.post("goto", "坏点", [1, 2, 3])
    pump(1.5, lambda: "点位需要 6 个有限角度值" in log())
    check("worker 拒绝 3 个角度的点位", "点位需要 6 个有限角度值" in log())

    # --- 正常路径：勾选 + 已连接 → mock 上真的走 ---
    win.gate = True
    tgt = list(robot.joints)
    tgt[5] = round(tgt[5] + 2.0, 3)
    ps.add("J6PLUS2", tgt, "测试用")
    no = ps.items()[-1]["no"]
    win._refresh_pt_list(keep=no)
    win._last_j = list(robot.joints)
    j6_before = robot.joints[5]
    win._confirm_goto = lambda name, detail: True      # 免弹窗（自检里覆盖确认钩子）
    win._on_pt_goto()
    arrived = pump(15, lambda: "点位到位" in log())
    check("去点位：收到到位回执", arrived, "log 尾部=%r" % log()[-90:])
    check("J6 实际走到目标（≈+2°）",
          abs(robot.joints[5] - tgt[5]) < 0.6,
          "ΔJ6=%+.2f°（目标 %+.2f）" % (robot.joints[5] - j6_before, tgt[5]))
    check("到位后熔断已解除", win.w.fuse is None)

    # --- 已在点上 → 不该再发动作 ---
    pump(0.8)                       # 等一帧快照把 UI 侧缓存的"熔断已解除"刷过来
    win._last_j = list(robot.joints)
    win._on_pt_sel()
    n1 = log().count("不需要动作")
    win._on_pt_goto()
    check("已在点上时拒绝重复动作", log().count("不需要动作") > n1)

    ok, _ = ps.remove(no)
    check("删除点位成功", ok and ps.get(no) is None)
    check("删除后重新读回也不在了", PointStore(TMP_POINTS).get(no) is None)
    win._refresh_pt_list()          # 界面侧的删除会顺手刷新列表（这里直接调 store，手动刷一次）
    check("⑥ 卡列表同步变短", win.lw_pt.count() == len(ps.items()),
          "count=%d / items=%d" % (win.lw_pt.count(), len(ps.items())))

    with open(TMP_POINTS, "w", encoding="utf-8") as fh:
        fh.write("{也不是合法 JSON")
    broken_pt = PointStore(TMP_POINTS)
    check("损坏的点位 JSON 会留档 .bad 而不是被覆盖",
          os.path.exists(TMP_POINTS + ".bad") and broken_pt.items() == [],
          "留档=%s" % os.path.exists(TMP_POINTS + ".bad"))
    print("   （注：这一步故意写坏了临时点位 —— 只影响 tools/_tmp_points.json）")

    # ---- 收尾 ----
    win._closing = True
    win.w.post("disconnect")
    pump(0.3)
    win.w.stop()
    win.close()

    for p in (TMP_STORE, TMP_STORE + ".bad", TMP_POINTS, TMP_POINTS + ".bad"):
        try:
            os.remove(p)
        except OSError:
            pass

    passed = sum(results)
    print("\n通过 %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
