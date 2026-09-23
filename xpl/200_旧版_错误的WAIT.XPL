WHILE true DO
    fidbus.mtcp_wo_b[0] := false;
    WAIT(fidbus.mtcp_ro_b[0]);
    J1 := fidbus.mtcp_ro_i[0]/100.0;
    J2 := fidbus.mtcp_ro_i[1]/100.0;
    J3 := fidbus.mtcp_ro_i[2]/100.0;
    J4 := fidbus.mtcp_ro_i[3]/100.0;
    J5 := fidbus.mtcp_ro_i[4]/100.0;
    J6 := fidbus.mtcp_ro_i[5]/100.0;
    MJOINT(POINTJ(J1,J2,J3,J4,J5,J6), v100perc, fine, tool0);
    fidbus.mtcp_wo_b[0] := true;
END_WHILE;
