# -*- coding: utf-8 -*-
"""单轴点动 —— 带**密集轨迹采样**的受控点动工具（做真实运动用）。

比 `diag_jog.py` 多两件事：
  1. **真的会动**：目标角 = 当前实际角 + delta（`diag_jog.py` 故意设成当前角，不动）。
  2. **逐点采样**：运动中每 ~30ms 读一次 6 轴角位，落 `logs/jog_axis_*.csv`，
     并在终端打印"角度-时间"轨迹 ⇒ 用数据回答"到底动了没有、多快"。

安全设计
  · 默认 **只读预演**（不触发）。要真动必须显式加 `--go`。
  · **棘轮式**：`--steps` 步，每步都重新读**实际角**再加 delta（不累计理论值）
    ⇒ 即使某步落后，也不会越滚越大。
  · 每步 **防冲预置**：非目标轴一律写成其当前实际角。
  · 每步之间检查 报警/伺服/运行位，一有异常**立刻停手**并撤触发。
  · 硬上限：单步 ≤45°、累计 ≤120°；超了直接拒绝。
  · `--speeds 5,100`：逐步交替设定 `40103` 速度（**实测确实影响点动快慢**）；
    结束时恢复原值。

★ 2026-09-23 现场踩坑（已在本工具里修掉）
  · **写角度→立刻触发会"空转"**：程序停在 WAIT 上时，`40139~44` 刚写进去
    还没同步到程序变量空间，触发已被放行 → MJOINT 用的是**上一轮旧目标角**
    → 实测位移 0.000°、完成位却秒回。现场 6 步里中招 1 次。
    ⇒ 修法：写完**回读校验**目标寄存器 + **静置 `--settle`（默认 0.15s）** 再触发。
  · **40103 确实控制点动速度**：实测 5% → 1.27°/s、100% → 16.7°/s（同一程序、同一 5°）。

用法：
    # 只预演（安全，随时可跑）：看当前姿态、打印将要执行的动作计划
    python tools/jog_axis.py --axis 6 --delta 5 --steps 6

    # 真动（现场确认安全后）：J6 每步 +5°，共 6 步 = +30°
    python tools/jog_axis.py --axis 6 --delta 5 --steps 6 --go

    # 100% 速度下一个约 1 秒的动作（~16°/s）
    python tools/jog_axis.py --axis 6 --delta 16 --steps 1 --speeds 100 --go

    # 对比速度（交替 5% / 100%）
    python tools/jog_axis.py --axis 6 --delta 5 --steps 4 --speeds 5,100 --go
"""
from __future__ import annotations

import csv
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from efort_client import EfortClient, ModbusError  # noqa: E402

MAX_STEP_DEG = 45.0     # 单步上限
MAX_TOTAL_DEG = 120.0   # 累计上限
STEP_TIMEOUT = 25.0     # 单步最长等待（超时视为卡住，撤触发）
MOVE_EPS = 0.05         # 判定"开始动"的角度阈值


def fmt_angles(vals) -> str:
    return "  ".join("J%d=%+.2f" % (i + 1, v) for i, v in enumerate(vals))


def decode_ro(words) -> list:
    """`snapshot()["jog_ro"]` 已是 40139~44 的 6 个字（客户端内部切好了 ro[4:10]）
    → 这里只做"负数还原 + 除以 100"。"""
    return [((v - 65536 if v > 32767 else v) / 100.0) for v in (words or [])]


def preflight(c: EfortClient, s: dict) -> dict | None:
    """伺服/报警/运行位检查，必要时自动上伺服。返回最新 snapshot。"""
    if not s["bits"]["servo"]:
        # 实机经验：进过示教器程序编辑态 → 伺服被下使能。自动走一次重吸合。
        print("  伺服未上电 → 自动重吸合（0x0000 → 等 0.6s → 0x1001）…")
        try:
            c.servo_reengage()
        except ModbusError:
            pass
        t0 = time.time()
        while time.time() - t0 < 3.5:
            s = c.snapshot()
            if s is None or s["bits"]["servo"]:
                break
            time.sleep(0.15)
        print("  伺服=%s（%.2fs）" % (s["bits"]["servo"] if s else "读失败", time.time() - t0))
    if s is None:
        return None
    if not s["bits"]["servo"]:
        print("  ✗ 伺服仍未上电 —— 请在示教器使能/检查安全回路，再点面板【一键就绪】。")
        return None
    if s["alarm1"] or s["alarm2"] or s["bits"]["alarm"]:
        print("  ✗ 有报警（%d/%d）—— 先清报警。" % (s["alarm1"], s["alarm2"]))
        return None
    if not s["bits"]["run"]:
        print("  ✗ 运行位=0 —— 点动服务程序没在运行，先点面板【一键就绪】。")
        return None
    return s


def wait_done_clear(c: EfortClient, limit: float = 2.0) -> bool:
    """等上一次的完成位归零（程序回到 WAIT 后会在循环头把它清掉）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < limit:
        last = c.read_jog_done()
        if last is False:
            return True
        time.sleep(0.04)
    return last is False


def write_and_verify(c: EfortClient, tgt: list, settle: float) -> bool:
    """写目标角 → **回读校验** → 静置 settle 秒（消除"程序读旧目标角"的竞态）。"""
    c.write_jog_angles(tgt)
    ok = False
    t0 = time.time()
    while time.time() - t0 < 1.0:
        s = c.snapshot()
        rb = decode_ro(s.get("jog_ro")) if s else []
        if len(rb) == 6 and all(abs(rb[i] - tgt[i]) < 0.005 for i in range(6)):
            ok = True
            break
        time.sleep(0.03)
    if not ok:
        print("  ⚠ 目标角回读未一致（可能写失败）。仍继续，但结果请谨慎采信。")
    if settle > 0:
        time.sleep(settle)   # 等控制器把读区抄进程序变量空间
    return ok


def run_step(c: EfortClient, axis: int, delta: float, tag: str,
             rows: list, log, settle: float) -> dict | None:
    """执行一步：重新读实际角 → 加 delta → 写+校验+静置 → 触发 → 密集采样 → 撤触发。"""
    s = c.snapshot()
    if s is None:
        print("  读状态失败，停手。")
        return None
    if s["alarm1"] or s["alarm2"] or s["bits"]["alarm"]:
        print("  ⚠ 出现报警（%d/%d），停手。" % (s["alarm1"], s["alarm2"]))
        return None
    if not s["bits"]["servo"] or not s["bits"]["run"]:
        print("  ⚠ 伺服=%s 运行=%s —— 停手，先点【一键就绪】。"
              % (s["bits"]["servo"], s["bits"]["run"]))
        return None

    cur = list(s["joints"])
    tgt = list(cur)
    tgt[axis - 1] = round(cur[axis - 1] + delta, 2)
    if not wait_done_clear(c):
        print("  ⚠ 完成位没归零（上一步没收尾干净？），仍继续，但请留意。")

    write_and_verify(c, tgt, settle)     # ★ 防冲 + 回读校验 + 静置
    print("  预置+校验完成 → 目标 " + fmt_angles(tgt))

    t0 = time.time()
    try:
        c.trigger_jog()
    except ModbusError as e:
        print("  触发写失败:", e)
        return None

    samples = []
    done_at = None
    timed_out = False
    while True:
        el = time.time() - t0
        if el > STEP_TIMEOUT:
            timed_out = True
            break
        st = c.snapshot()                 # 一次读齐：角位 + 完成位（40135/40035 都在里面）
        if st is not None:
            samples.append((el, list(st["joints"])))
            if st.get("jog_done"):
                done_at = el
                break

    try:
        c.clear_trigger()
    except ModbusError as e:
        print("  !! 撤触发失败:", e, "—— 请手动确认 40135 归零")

    st = c.snapshot()
    if st is None:
        print("  收尾读状态失败。")
        return None

    actual = st["joints"][axis - 1] - cur[axis - 1]
    # 启动延迟：第一个明显偏离起点的采样时刻
    t_first = None
    for el, j in samples:
        if abs(j[axis - 1] - cur[axis - 1]) > MOVE_EPS:
            t_first = el
            break
    span_move = (done_at - t_first) if (done_at and t_first is not None) else None
    rate_move = (abs(actual) / span_move) if span_move and span_move > 0.02 else float("nan")
    rate_all = (abs(actual) / done_at) if done_at and done_at > 0.02 else float("nan")
    other_max = max(abs(st["joints"][i] - cur[i]) for i in range(6) if i != axis - 1)

    res = {
        "tag": tag, "axis": axis, "commanded": delta,
        "from": cur[axis - 1], "to": st["joints"][axis - 1],
        "actual": actual, "rate_move": rate_move, "rate_all": rate_all,
        "done_at": done_at, "t_first": t_first, "timed_out": timed_out,
        "nsamples": len(samples), "other_max": other_max,
        "alarm": (st["alarm1"], st["alarm2"]),
        "servo": st["bits"]["servo"], "run": st["bits"]["run"],
        "speed": st["speed"], "set_speed": st.get("set_speed"),
    }
    for el, j in samples:
        log.writerow([tag, "%.3f" % el] + ["%.3f" % v for v in j])
        rows.append({"t": el, "j": j, "tag": tag})

    print("  采样 %d 点（%.0f Hz 量级）| 完成位 %s | 启动延迟 %s"
          % (len(samples), len(samples) / max(done_at or 1e-6, 1e-6),
             ("%.2fs" % done_at) if done_at else ("超时(>%.0fs)" % STEP_TIMEOUT),
             ("%.2fs" % t_first) if t_first is not None else "未动"))
    print("  J%d: %+.2f → %+.2f  （命令 %+.2f，实测 %+.3f）"
          % (axis, cur[axis - 1], st["joints"][axis - 1], delta, actual))
    print("  运动段 %.2f°/s | 全程均速 %.2f°/s | 其他 5 轴 %.3f° | 报警 %d/%d"
          % (rate_move, rate_all, other_max, st["alarm1"], st["alarm2"]))
    if abs(actual) < MOVE_EPS and delta:
        print("  ⚠ **本步没动**（完成位秒回 + 零位移）→ 典型的"
              "「程序读到旧目标角」竞态，加大 --settle 或重跑本步。")
    return res


def print_trajectory(rows: list, axis: int, limit: int = 26) -> None:
    """打印"角度-时间"轨迹（等间隔抽稀，便于肉眼看出是否连续运动）。"""
    if not rows:
        print("  （无采样数据）")
        return
    step = max(1, len(rows) // limit)
    picked = rows[::step]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    print("  轨迹（每 ~%d 点取 1）:" % step)
    j0 = picked[0]["j"][axis - 1]
    prev = None
    for r in picked:
        v = r["j"][axis - 1]
        d = "" if prev is None else "%+.2f" % (v - prev)
        bar = int(abs(v - j0) * 2)
        print("    t=%5.2fs  J%d=%+8.2f  Δ%-7s %s"
              % (r["t"], axis, v, d, "#" * min(bar, 40)))
        prev = v


def main() -> int:
    argv = sys.argv[1:]
    host = argv[argv.index("--host") + 1] if "--host" in argv else "192.168.1.12"
    axis = int(argv[argv.index("--axis") + 1]) if "--axis" in argv else 6
    delta = float(argv[argv.index("--delta") + 1]) if "--delta" in argv else 5.0
    steps = int(argv[argv.index("--steps") + 1]) if "--steps" in argv else 1
    settle = float(argv[argv.index("--settle") + 1]) if "--settle" in argv else 0.15
    pause = float(argv[argv.index("--pause") + 1]) if "--pause" in argv else 0.30
    go = "--go" in argv
    speeds = None
    if "--speeds" in argv:
        speeds = [int(x) for x in argv[argv.index("--speeds") + 1].split(",")]

    if not 1 <= axis <= 6:
        print("轴号必须是 1~6")
        return 2
    if abs(delta) > MAX_STEP_DEG:
        print("单步超过 %.0f° 上限，拒绝执行（改小 --delta）。" % MAX_STEP_DEG)
        return 2
    if abs(delta * steps) > MAX_TOTAL_DEG:
        print("累计 %.1f° 超过 %.0f° 上限，拒绝执行。" % (abs(delta * steps), MAX_TOTAL_DEG))
        return 2

    c = EfortClient(host)
    print("连接:", c.connect())
    s = c.snapshot()
    if s is None:
        print("读状态失败")
        c.close()
        return 1

    print("\n── 预检 ──")
    print("  自动=%d 伺服=%d 运行=%d 报警=%d/%d 当前程序=%d 速度reg=%d"
          % (s["bits"]["auto"], s["bits"]["servo"], s["bits"]["run"],
             s["alarm1"], s["alarm2"], s["prog"], s["speed"]))
    print("  当前姿态 " + fmt_angles(s["joints"]))

    print("\n── 计划 ──")
    print("  轴 J%d，每步 %+.2f°，共 %d 步（累计 %+.1f°）"
          % (axis, delta, steps, delta * steps))
    print("  预计终点 J%d ≈ %+.2f°" % (axis, s["joints"][axis - 1] + delta * steps))
    if speeds:
        print("  速度档位交替: %s（每次触发前写 40103）" % speeds)
    if abs(s["joints"][axis - 1] + delta * steps) > 180:
        print("  ⚠ 终点超过 ±180°，请自行确认软限位。")

    if not go:
        print("\n（预演模式，未做任何运动。确认现场安全后加 --go 才会真动）")
        c.close()
        return 0

    ok = preflight(c, s)
    if ok is None:
        c.close()
        return 1

    orig_set_speed = s.get("set_speed")
    logdir = os.path.join(ROOT, "logs")
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, "jog_axis_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    results = []
    rows: list = []
    with open(path, "w", newline="", encoding="utf-8") as f:
        log = csv.writer(f)
        log.writerow(["tag", "t_s"] + ["J%d" % i for i in range(1, 7)])
        try:
            for k in range(steps):
                tag = "step%02d" % (k + 1)
                print("\n── %s / 共 %d 步 ──" % (tag, steps))
                if speeds:
                    sp = speeds[k % len(speeds)]
                    try:
                        c.set_speed(sp)
                        time.sleep(0.12)
                    except ModbusError as e:
                        print("  写 40103 失败:", e)
                    print("  已设 40103 = %d%%" % sp)
                    tag = "%s_spd%d" % (tag, sp)
                r = run_step(c, axis, delta, tag, rows, log, settle)
                if r is None:
                    print("\n⚠ 第 %d 步异常，已停手。剩余步数取消。" % (k + 1))
                    break
                results.append(r)
                f.flush()
                if k + 1 < steps and pause > 0:
                    time.sleep(pause)
        finally:
            try:
                c.clear_trigger()
            except ModbusError:
                pass
            if speeds and orig_set_speed is not None:
                try:
                    c.set_speed(int(orig_set_speed))
                    print("\n已恢复 40103 = %s" % orig_set_speed)
                except ModbusError:
                    pass

    print("\n════ 结果汇总 ════")
    if not results:
        print("  没有任何一步完成。")
    for r in results:
        print("  %-16s 命令 %+6.2f°  实测 %+7.3f°  J%d: %+7.2f→%+7.2f  "
              "耗时 %-7s 启动 %-7s 运动段 %5.2f°/s  其他轴 %.3f°"
              % (r["tag"], r["commanded"], r["actual"], r["axis"], r["from"], r["to"],
                 ("%.2fs" % r["done_at"]) if r["done_at"] else "超时",
                 ("%.2fs" % r["t_first"]) if r["t_first"] is not None else "未动",
                 r["rate_move"], r["other_max"]))

    tot_cmd = sum(abs(r["commanded"]) for r in results)
    tot_act = sum(abs(r["actual"]) for r in results)
    noop = [r["tag"] for r in results if abs(r["actual"]) < MOVE_EPS]
    if results:
        print("\n  累计：命令 %.2f° → 实测 %.3f°（误差 %+.3f°）"
              % (tot_cmd, tot_act, tot_act - tot_cmd))
        print("  全程报警：%s" % ("无" if all(r["alarm"] == (0, 0) for r in results) else "有！"))
        if noop:
            print("  ⚠ 空转步（完成位回了但零位移）：%s → 竞态，加大 --settle 重跑" % ", ".join(noop))
        print("  其他 5 轴最大位移：%.3f°（应≈0，用于确认只动了目标轴）"
              % max(r["other_max"] for r in results))

    if len(results) >= 2:
        if speeds and any("_spd" in r["tag"] for r in results):
            groups: dict = {}
            for r in results:
                if "_spd" not in r["tag"]:
                    continue
                groups.setdefault(r["tag"].split("_spd")[-1], []).append(r)
            if len(groups) > 1:
                print("\n  ── 40103 对点动速度的影响（同程序、同角度）──")
                for sp, rs in sorted(groups.items(), key=lambda kv: int(kv[0])):
                    ad = [r["done_at"] for r in rs if r["done_at"]]
                    ar = [r["rate_move"] for r in rs if r["rate_move"] == r["rate_move"]]
                    print("    40103=%-4s%% n=%d  平均耗时 %.2fs  运动段 %.2f°/s"
                          % (sp, len(rs), (sum(ad) / len(ad)) if ad else float("nan"),
                             (sum(ar) / len(ar)) if ar else float("nan")))
                print("    ⇒ 两组差异明显 = **40103 就是点动速度闸门**（现场实测 5%%≈1.3°/s、100%%≈16°/s）")
        print("\n  ── 最后一步（%s）轨迹 ──" % results[-1]["tag"])
        last_tag = results[-1]["tag"]
        tail = [r for r in rows if r.get("tag") == last_tag]
        print_trajectory(tail or rows, axis)

    print("\n  轨迹原始数据: %s" % path)
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
