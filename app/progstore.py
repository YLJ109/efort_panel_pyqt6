# -*- coding: utf-8 -*-
"""程序清单（持久化）—— **绝不猜程序号 / 程序名**，全部由用户自己维护。

为什么要有这个文件（2026-09-23 用户要求）：
  · Modbus 侧只认**数字**（40104 = 目标程序号），控制器不会把程序名吐给我们；
    而程序文件本身可能叫 `PICK_A.XPL` 这种英文名 —— 名字只存在于示教器里。
  · 所以「号」和「名」必须分开存：**号**用于下发，**名**只用于你自己在界面上认。
  · 谁都不知道现场有哪些程序 → 由用户**自己添加 / 删除 / 改名**，而不是让程序去猜。
  · 其中某一条可以被标记为「**点动服务程序**」—— 点动功能依赖它在控制器上常驻。

落盘策略：每次改动**立即**原子写（临时文件 + os.replace），中途断电不会留半个文件；
文件损坏时改名留档（`.bad`）而不是静默覆盖，避免丢掉用户手写的清单。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

#: 默认文件名（放在项目根目录，肉眼可见、方便手改）
DEFAULT_FILE = "programs.json"

#: 写进文件里的自解释说明 —— 你直接用记事本打开这个 json 也能看懂怎么改
README_TEXT = (
    "【程序清单】由你自己维护，程序绝不替你猜号或猜名。"
    "list[].no = 程序号（真正下发给控制器的数字）；"
    "list[].name = 程序名（可中文可英文，仅供界面辨认，不参与下发）；"
    "list[].note = 备注；"
    "jog = 点动服务程序号（0 表示还没设置，在界面上点【设为点动服务】即可）。"
    "可以直接手改本文件，也可以在界面⑤卡里【添加】/【改名】/【删除】，或先跑一次【扫描】自动发现。"
)


class ProgramStore:
    VERSION = 1

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._list: list[dict] = []
        self._jog = 0
        self.load()

    # ============================================================ 磁盘
    def load(self) -> None:
        self._list, self._jog = [], 0
        if not os.path.exists(self.path):
            # 首次运行：落地一份**带说明的空清单**，让你打开就能照着改
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("根节点不是对象")
        except (OSError, ValueError):
            self._quarantine()
            return
        for it in (raw.get("list") or []):
            if not isinstance(it, dict):
                continue
            try:
                no = int(it.get("no") or 0)
            except (TypeError, ValueError):
                continue
            if no <= 0:
                continue
            self._list.append({
                "no": no,
                "name": str(it.get("name") or "").strip(),
                "note": str(it.get("note") or "").strip(),
            })
        self._dedup()
        try:
            self._jog = int(raw.get("jog") or 0)
        except (TypeError, ValueError):
            self._jog = 0
        if not self.has(self._jog):
            self._jog = 0

    def _quarantine(self) -> None:
        """JSON 损坏 → 留一份 .bad 备份，绝不静默抹掉用户数据。"""
        try:
            shutil.move(self.path, self.path + ".bad")
        except OSError:
            pass

    def save(self) -> bool:
        data = {"_readme": README_TEXT, "version": self.VERSION,
                "jog": self._jog, "list": self._list}
        d = os.path.dirname(self.path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".prog-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)              # 原子替换
            return True
        except OSError:
            return False

    def _dedup(self) -> None:
        seen: set[int] = set()
        out: list[dict] = []
        for it in self._list:
            if it["no"] in seen:
                continue
            seen.add(it["no"])
            out.append(it)
        self._list = out

    # ============================================================ 查询
    def items(self) -> list[dict]:
        return [dict(it) for it in self._list]

    def numbers(self) -> list[int]:
        return [it["no"] for it in self._list]

    def has(self, no: int) -> bool:
        return any(it["no"] == no for it in self._list)

    def get(self, no: int) -> dict | None:
        for it in self._list:
            if it["no"] == no:
                return dict(it)
        return None

    def label(self, no: int) -> str:
        """下拉里显示用：「410 · PICK_A（备注）」。没名字就只显示号。"""
        it = self.get(no)
        if it is None:
            return str(no)
        txt = str(no)
        if it["name"]:
            txt += " · %s" % it["name"]
        if it["note"]:
            txt += "（%s）" % it["note"]
        return txt

    @property
    def jog(self) -> int:
        return self._jog

    def jog_item(self) -> dict | None:
        return self.get(self._jog) if self._jog else None

    # ============================================================ 修改（全部即时落盘）
    def add(self, no: int, name: str = "", note: str = "") -> tuple[bool, str]:
        no = int(no)
        if no <= 0:
            return False, "程序号必须是正整数（Modbus 40104 只认数字）"
        if self.has(no):
            return self.update(no, name or None, note or None)
        self._list.append({"no": no, "name": (name or "").strip(),
                           "note": (note or "").strip()})
        self._list.sort(key=lambda x: x["no"])
        return (True, "已添加 %s" % self.label(no)) if self.save() \
            else (False, "已添加到内存，但**写文件失败**：%s" % self.path)

    def update(self, no: int, name: str | None = None, note: str | None = None) -> tuple[bool, str]:
        it = None
        for x in self._list:
            if x["no"] == int(no):
                it = x
                break
        if it is None:
            return False, "清单里没有 %s 号" % no
        if name is not None:
            it["name"] = name.strip()
        if note is not None:
            it["note"] = note.strip()
        return (True, "已更新 %s" % self.label(no)) if self.save() \
            else (False, "已改到内存，但**写文件失败**：%s" % self.path)

    def remove(self, no: int) -> tuple[bool, str]:
        no = int(no)
        before = len(self._list)
        self._list = [it for it in self._list if it["no"] != no]
        if len(self._list) == before:
            return False, "清单里没有 %s 号" % no
        cleared = self._jog == no
        if cleared:
            self._jog = 0
        ok = self.save()
        msg = "已从清单删除 %s 号" % no
        if cleared:
            msg += "（它原本是点动服务程序，已一并取消标记）"
        return (ok and True, msg if ok else msg + "；但**写文件失败**：%s" % self.path)

    def set_jog(self, no: int) -> tuple[bool, str]:
        no = int(no)
        if not self.has(no):
            return False, "清单里没有 %s 号 —— 先【添加】它，再设为点动服务" % no
        self._jog = no
        return (True, "点动服务程序已设为 %s" % self.label(no)) if self.save() \
            else (False, "已改到内存，但**写文件失败**：%s" % self.path)

    def clear_jog(self) -> tuple[bool, str]:
        self._jog = 0
        return (True, "已取消点动服务程序标记") if self.save() \
            else (False, "已改到内存，但**写文件失败**：%s" % self.path)

    def merge_scan(self, found: list[int]) -> tuple[list[int], int]:
        """把扫描到的程序号**并入**清单（只加没有的，绝不动已有名字）。

        返回 (新增的号列表, 写盘是否成功=0/1)。
        """
        added: list[int] = []
        for n in found:
            n = int(n)
            if n > 0 and not self.has(n):
                self._list.append({"no": n, "name": "", "note": ""})
                added.append(n)
        if added:
            self._list.sort(key=lambda x: x["no"])
        ok = self.save() if added else True
        return added, (1 if ok else 0)
