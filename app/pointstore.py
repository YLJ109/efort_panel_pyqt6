# -*- coding: utf-8 -*-
"""点位清单（示教点持久化）—— 「摇到位 → 记录当前点 → 以后一键去这个点」。

为什么要这个文件（2026-09-23 用户要求，补齐 `docs/方案_点选式改造.md` 第 3/6.1 节留下的钩子）：
  · Modbus **没有任何"读取示教点"的接口**，控制器上的点位文件 PC 也拿不到
    （通道穷举过：只有 80/502/8000/8008，无 FTP/SMB/SSH/VNC）。
  · ⇒ 唯一可行的是**上位机自己记**（方案里的 A 方案）：把机器人摇到位，
    读 `40011~40022`（J1~J6 当前角，FLOAT/CDAB）存下来，以后按这个绝对角走回去。
  · 一条点位 = **名称 + 6 个关节角 + 记录时间**；名称是给你认的，角度才是下发给机器人的。

落盘策略与 `programs.json` 一致：每次改动**立即**原子写（临时文件 + os.replace）；
JSON 损坏时改名留档 `.bad`，**绝不静默覆盖**你手记的点位。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time

#: 默认文件名（项目根目录，肉眼可见、可用记事本直接改）
DEFAULT_FILE = "points.json"

#: 关节角合法范围（只是防手改文件写出天文数字；真机软限位由控制器把关）
ANGLE_LIMIT = 1000.0

README_TEXT = (
    "【点位清单】把机器人摇到位后点【记录当前点】就会存到这里。"
    "points[].no = 序号（本地用，不下发）；points[].name = 点名（界面辨认用，不参与下发）；"
    "points[].joints = 记录当时的 J1~J6 **绝对关节角**（度，真正下发给机器人的就是这 6 个数）；"
    "points[].ts = 记录时间；points[].note = 备注。"
    "点【去这个点】时，上位机把这 6 个角写进 40139~44 再置触发位，让控制器常驻的点动服务程序走过去。"
    "可以直接手改本文件，也可以在界面⑥卡里【记录】/【改名】/【删除】。"
)


class PointStore:
    VERSION = 1

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._list: list[dict] = []
        self.load()

    # ============================================================ 磁盘
    def load(self) -> None:
        self._list = []
        if not os.path.exists(self.path):
            self.save()                     # 首次运行：落地一份带说明的空清单
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("根节点不是对象")
        except (OSError, ValueError):
            self._quarantine()
            return
        seen: set[int] = set()
        for it in (raw.get("points") or []):
            if not isinstance(it, dict):
                continue
            try:
                no = int(it.get("no") or 0)
            except (TypeError, ValueError):
                continue
            joints = self.sanitize(it.get("joints"))
            if no <= 0 or joints is None or no in seen:
                continue
            seen.add(no)
            self._list.append({
                "no": no,
                "name": str(it.get("name") or "").strip() or ("P%d" % no),
                "joints": joints,
                "ts": str(it.get("ts") or "").strip(),
                "note": str(it.get("note") or "").strip(),
            })
        self._list.sort(key=lambda x: x["no"])

    def _quarantine(self) -> None:
        """JSON 损坏 → 留 `.bad` 备份，绝不静默抹掉用户手记的点位。"""
        try:
            shutil.move(self.path, self.path + ".bad")
        except OSError:
            pass

    def save(self) -> bool:
        data = {"_readme": README_TEXT, "version": self.VERSION, "points": self._list}
        d = os.path.dirname(self.path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".pts-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)          # 原子替换
            return True
        except OSError:
            return False

    # ============================================================ 校验
    @staticmethod
    def sanitize(joints) -> list[float] | None:
        """收一份 6 轴角度：必须是 6 个有限数且在 ±ANGLE_LIMIT 内，否则返回 None。"""
        if not isinstance(joints, (list, tuple)) or len(joints) != 6:
            return None
        out: list[float] = []
        for v in joints:
            try:
                x = float(v)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(x) or abs(x) > ANGLE_LIMIT:
                return None
            out.append(round(x, 3))
        return out

    # ============================================================ 查询
    def items(self) -> list[dict]:
        return [dict(it) for it in self._list]

    def get(self, no: int) -> dict | None:
        for it in self._list:
            if it["no"] == int(no):
                return dict(it)
        return None

    def has(self, no: int) -> bool:
        return self.get(no) is not None

    def name_taken(self, name: str, exclude: int | None = None) -> bool:
        n = (name or "").strip().lower()
        return any(it["name"].lower() == n and it["no"] != exclude for it in self._list)

    def next_no(self) -> int:
        return (max((it["no"] for it in self._list), default=0) + 1)

    def label(self, no: int) -> str:
        it = self.get(no)
        if it is None:
            return str(no)
        return it["name"] + ("（%s）" % it["note"] if it["note"] else "")

    # ============================================================ 修改（即时落盘）
    def add(self, name: str, joints, note: str = "") -> tuple[bool, str]:
        js = self.sanitize(joints)
        if js is None:
            return False, "点位角度无效：需要 6 个有限数（J1~J6），且 |角| ≤ %g°" % ANGLE_LIMIT
        no = self.next_no()
        nm = (name or "").strip() or ("P%d" % no)
        if len(nm) > 24:
            return False, "点名太长（≤24 字符）"
        if self.name_taken(nm):
            return False, "点名「%s」已存在 —— 换个名字（点名用于辨认，必须唯一）" % nm
        self._list.append({"no": no, "name": nm, "joints": js,
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "note": (note or "").strip()})
        self._list.sort(key=lambda x: x["no"])
        return (True, "已记录点位 %s：%s" % (nm, " ".join("%+.3f" % v for v in js))) \
            if self.save() else (False, "已记到内存，但**写文件失败**：%s" % self.path)

    def update(self, no: int, name: str | None = None, note: str | None = None,
               joints=None, restamp: bool = False) -> tuple[bool, str]:
        it = None
        for x in self._list:
            if x["no"] == int(no):
                it = x
                break
        if it is None:
            return False, "点位清单里没有 %s 号" % no
        if name is not None:
            nm = name.strip()
            if not nm:
                return False, "点名不能为空"
            if len(nm) > 24:
                return False, "点名太长（≤24 字符）"
            if self.name_taken(nm, exclude=it["no"]):
                return False, "点名「%s」已存在" % nm
            it["name"] = nm
        if note is not None:
            it["note"] = note.strip()
        if joints is not None:
            js = self.sanitize(joints)
            if js is None:
                return False, "点位角度无效"
            it["joints"] = js
            if restamp:
                it["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return (True, "已更新点位 %s" % self.label(no)) if self.save() \
            else (False, "已改到内存，但**写文件失败**：%s" % self.path)

    def remove(self, no: int) -> tuple[bool, str]:
        no = int(no)
        before = len(self._list)
        self._list = [it for it in self._list if it["no"] != no]
        if len(self._list) == before:
            return False, "点位清单里没有 %s 号" % no
        return (True, "已删除点位 %s" % no) if self.save() \
            else (False, "已从内存删除，但**写文件失败**：%s" % self.path)
