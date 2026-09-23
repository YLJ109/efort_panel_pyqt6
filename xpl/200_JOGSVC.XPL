<?xml version="1.0" encoding="utf-8"?>
<!-- Date(ymd)/time(hms) 2026/09/23 18:52:49 -->
<Xpl-source>
<HostEnvironment>
<Config>
<FileId>0x25d78854</FileId>
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
<while>
<cond>true</cond>
<pouref>1</pouref>
</while>
<rem>
<text />
</rem>
<rem>
<text />
</rem>
<rem>
<text />
</rem>
<rem>
<text />
</rem>
</Body>
<Pou>
<Name>1</Name>
<Body>
<rem>
<text />
</rem>
<set>
<dest>fidbus.mtcp_wo_b[0]</dest>
<expr>false</expr>
</set>
<wait>
<condition>fidbus.mtcp_ro_b[0]</condition>
</wait>
<mjoint>
<target>POINTJ(fidbus.mtcp_ro_i[0]/100.0,fidbus.mtcp_ro_i[1]/100.0,fidbus.mtcp_ro_i[2]/100.0,fidbus.mtcp_ro_i[3]/100.0,fidbus.mtcp_ro_i[4]/100.0,fidbus.mtcp_ro_i[5]/100.0)</target>
<speed>v50perc</speed>
<zone>fine</zone>
<tool>tool0</tool>
</mjoint>
<set>
<dest>fidbus.mtcp_wo_b[0]</dest>
<expr>true</expr>
</set>
<wait>
<condition>!fidbus.mtcp_ro_b[0]</condition>
</wait>
<rem>
<text>
</text>
</rem>
</Body>
</Pou>
</Pou>
</Pous>
</XplEnvironment>
</Xpl-source>
