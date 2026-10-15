#!/usr/bin/env python3
"""
go2rtc Rueckkanal-Prozess (backchannel=1).
Liest rohes PCMA (A-law, 8000 Hz) von stdin, wie go2rtc es ueber
#audio=alaw/8000 anliefert, und reicht es in kleinen Haeppchen per UDP
an klingel_rtp_in.py weiter, das daraus RTP-Pakete zur Klingel baut.

Aufruf durch go2rtc, z.B.:
  exec:python3 /config/scripts/klingel_rtp_back.py#backchannel=1#audio=alaw/8000
"""
import socket, sys

BACK_PORT = 47990
CHUNK = 160   # 20 ms bei 8000 Hz / 8 bit = 160 Byte, passend zu ptime=20

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
buf = b""
while True:
    data = sys.stdin.buffer.read(CHUNK)
    if not data:
        break
    buf += data
    while len(buf) >= CHUNK:
        s.sendto(buf[:CHUNK], ("127.0.0.1", BACK_PORT))
        buf = buf[CHUNK:]
