<?xml version="1.0" encoding="utf-8"?>
<!-- Date(ymd)/time(hms) 2026/09/23 21:20:00 -->
<!-- 210 —— 示教服务（移动 / 吸取 / 放）。与上位机 efort_teach.py 配套。

     编写约定：**只用官方导出文件里已出现过的元素**
     （对照 C:\Users\Administrator\Desktop\200.XPL 与 桌面\机器人\CARRY.XPL）
     已证实可用：Pous/Pou/Name/Type/Body/rem/text/while/cond/pouref/wait/condition/
                 set/dest/expr/dwell/expr/mjoint/target/speed/zone/tool/call/subroutine/args/arg
     **刻意不用 <if>** —— 你手上所有真实程序（200 / CARRY / 205 / 300）里都没有它，
     元素名写错控制器会报 4902（XPL 文件 API 错误）。所以"按触发位分派"用
     <while cond="触发位"> 来实现：它的元素形状与 200 里已验证的
     <while><cond>true</cond><pouref>1</pouref></while> 完全一致，只换了条件表达式。

     与 200 的关系：210 是 200 的**超集** —— 移动入口同样是 40135.Bit0，
     所以把【点动程序号】改成 210 之后，J1~J6 点动照常可用。 -->
<Xpl-source>
<HostEnvironment>
<Config>
<FileId>0x7c1e04a9</FileId>
<Target>Robox Motion Control</Target>
<XplType>RoboticProgramLanguage</XplType>
</Config>
</HostEnvironment>
<XplEnvironment>
<Title LcId="1033">
<![CDATA[XPL program]]>
</Title>
<Description LcId="1033">
<![CDATA[XPL program edited from teachgun]]>
</Description>
<Version>1.0.0</Version>
<Author>Robox SpA</Author>
<Pous>
<Pou>
<Name>Main</Name>
<Type>Program</Type>
<Body>
<rem>
<text>主循环：服务体在 POU1（与 200 的结构一致）</text>
</rem>
<while>
<cond>true</cond>
<pouref>1</pouref>
</while>
</Body>
<Pou>
<Name>1</Name>
<Body>
<rem>
<text>① 吸真空：电平桥接 —— 上位机置 40135.Bit1 即吸、清零即停</text>
</rem>
<set>
<dest>io.DOut[8]</dest>
<expr>fidbus.mtcp_ro_b[1]</expr>
</set>
<rem>
<text>② 真空到位回读给上位机（40035.Bit1）</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[1]</dest>
<expr>io.DIn[8]</expr>
</set>
<rem>
<text>③ 能力标志：40035.Bit7 = 1 表示 210 已就绪、支持吸取/放</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[7]</dest>
<expr>true</expr>
</set>
<rem>
<text>④ 移动服务：40135.Bit0 为真时执行；上位机一撤位，下面 POU2 里的
   WAIT 放行、外层 WHILE 的条件立刻为假 → 退出，等下一次请求。
   只认 Bit0 是有意为之：200 点动程序也是这个约定。</text>
</rem>
<while>
<cond>fidbus.mtcp_ro_b[0]</cond>
<pouref>2</pouref>
</while>
<rem>
<text>⑤ 破真空服务：40135.Bit2 为真时跑完整时序（关吸 → 吹气 → 关吹）</text>
</rem>
<while>
<cond>fidbus.mtcp_ro_b[2]</cond>
<pouref>3</pouref>
</while>
<rem>
<text>⑥ 扫描节流（20ms）—— 保证 IO 桥接刷新延迟可忽略</text>
</rem>
<dwell>
<expr>0.02</expr>
</dwell>
</Body>
<Pou>
<Name>2</Name>
<Body>
<rem>
<text>移动开始：先清完成位</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[0]</dest>
<expr>false</expr>
</set>
<rem>
<text>走到上位机下发的绝对关节角（×100 存整数，/100 还原）。
   v50perc 与 200 点动一致 —— 有效速度 = v50perc × 速度设定(40103)</text>
</rem>
<mjoint>
<target>POINTJ(fidbus.mtcp_ro_i[0]/100.0,fidbus.mtcp_ro_i[1]/100.0,fidbus.mtcp_ro_i[2]/100.0,fidbus.mtcp_ro_i[3]/100.0,fidbus.mtcp_ro_i[4]/100.0,fidbus.mtcp_ro_i[5]/100.0)</target>
<speed>v50perc</speed>
<zone>fine</zone>
<tool>tool0</tool>
</mjoint>
<rem>
<text>到位 → 置完成位</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[0]</dest>
<expr>true</expr>
</set>
<rem>
<text>等上位机撤掉触发位再回去 —— 防止同一触发被重复执行</text>
</rem>
<wait>
<condition>!fidbus.mtcp_ro_b[0]</condition>
</wait>
</Body>
<Pou>
<Name>3</Name>
<Body>
<rem>
<text>破真空开始：清完成位</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[2]</dest>
<expr>false</expr>
</set>
<rem>
<text>先断开吸真空输出</text>
</rem>
<set>
<dest>io.DOut[8]</dest>
<expr>false</expr>
</set>
<dwell>
<expr>0.30</expr>
</dwell>
<rem>
<text>吹气破真空</text>
</rem>
<set>
<dest>io.DOut[9]</dest>
<expr>true</expr>
</set>
<dwell>
<expr>0.40</expr>
</dwell>
<rem>
<text>关吹气</text>
</rem>
<set>
<dest>io.DOut[9]</dest>
<expr>false</expr>
</set>
<dwell>
<expr>0.10</expr>
</dwell>
<rem>
<text>时序结束 → 置完成位</text>
</rem>
<set>
<dest>fidbus.mtcp_wo_b[2]</dest>
<expr>true</expr>
</set>
<rem>
<text>等上位机撤掉请求位 —— 不撤的话外层 WHILE 会认为"还在请求"而重复吹气</text>
</rem>
<wait>
<condition>!fidbus.mtcp_ro_b[2]</condition>
</wait>
</Body>
</Pou>
</Pou>
</Pou>
</Pou>
</Pous>
</XplEnvironment>
</Xpl-source>
