# -*- coding: utf-8 -*-
"""EFORT 远程控制台（PyQt6）—— 界面与工作线程。

分层：
    efort_client.py   协议层（纯 stdlib，Modbus TCP / 寄存器语义 / 命令字）
    app/tokens.py     主题令牌（唯一色源）
    app/widgets.py    通用控件（卡片 / 胶囊 / 关节行 / 日志）
    app/worker.py     机器人工作线程（唯一持有 socket 的地方）
    app/window.py     主窗口（只做展示与交互）
"""
