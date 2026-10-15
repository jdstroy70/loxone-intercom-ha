#!/usr/bin/env python3
"""
go2rtc exec-Quelle (Primaerprozess) fuer die Loxone-Tuerklingel.
- Baut den SIP/RTP-Anruf zur Klingel auf (Logik wie in klingel_call.py).
- Gibt das empfangene Audio (PCMA, 8000 Hz) als "streamendes" WAV auf stdout aus,
  damit go2rtc den Codec selbst erkennt.
- Hoert zusaetzlich auf einem lokalen UDP-Port (BACK_PORT) auf Audio-Haeppchen,
  die klingel_rtp_back.py dort ablegt, und schickt sie als RTP zur Klingel
  (Rueckkanal / Sprechen).
- Legt beim Beenden (SIGTERM, wenn go2rtc keinen Zuschauer mehr hat, ODER wenn
  HA die Hangup-Datei anlegt - siehe automations.yaml) sauber auf.

Aufruf durch go2rtc, z.B.:
  exec:python3 /config/scripts/klingel_rtp_in.py 192.168.178.21
"""
import socket, sys, time, uuid, random, struct, signal, select, hashlib, string, os

DST = sys.argv[1] if len(sys.argv) > 1 else "192.168.178.21"
SIP_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5060   # Testzwecke
RTP_PORT = 4000
BACK_PORT = 47990          # lokaler Port fuer klingel_rtp_back.py

# Von HA angelegte Datei, wenn das "Sprechen"-Toggle ausgeschaltet wird
# (shell_command.klingel_hangup in automations.yaml). Wir pollen sie in der
# Hauptschleife, weil wir uns nicht darauf verlassen koennen, dass go2rtc beim
# Verbindungsabbau der Karte zuverlaessig SIGTERM schickt.
HANGUP_FILE = "/config/scripts/klingel_hangup"

# Eine Hangup-Datei, die schon VOR diesem Skriptstart existiert, ist
# zwangslaeufig ein Ueberbleibsel von einem frueheren Lauf (z.B. weil damals
# kein Skript lief, das sie konsumiert/geloescht hat). Wuerde man sie stehen
# lassen, wuerde die allererste Schleifeniteration sie faelschlich als
# "jetzt sofort auflegen" missverstehen - der Anruf stuerbe binnen
# Millisekunden, noch bevor ueberhaupt eine Antwort der Klingel verarbeitet
# werden konnte (genau das Muster aus den Crash-Mitschnitten). Deshalb hier
# vor dem eigentlichen Start einmalig aufraeumen.
try:
    os.remove(HANGUP_FILE)
except OSError:
    pass

# Nach einem sauberen Auflegen startet go2rtc den exec-Prozess manchmal
# SOFORT neu (Millisekunden spaeter), vermutlich weil der WebRTC-Konsument
# im Browser zu dem Zeitpunkt noch nicht ganz getrennt hat und go2rtc daher
# noch "Nachfrage" sieht. Das knallt der Klingel ein neues INVITE vor den
# Latz, waehrend sie gerade noch das letzte Gespraech abraeumt -> "486 Busy".
# Deshalb: kurze Abklingzeit. COOLDOWN_FILE wird beim Auflegen mit einem
# Zeitstempel beschrieben; findet ein frisch gestarteter Prozess sie vor und
# ist sie juenger als COOLDOWN_SECS, gibt er sofort auf (ohne ueberhaupt ein
# INVITE zu schicken) statt die Klingel weiter zu bedraengen.
#
# COOLDOWN_SECS MUSS laenger sein als die eingebaute Trenn-Verzoegerung der
# webrtc-camera-Kartenbibliothek: deren video-rtc.js wartet beim Entfernen aus
# dem DOM (bzw. bei document.hidden, also auch beim Bildschirm sperren) erst
# DISCONNECT_TIMEOUT=5000ms, bevor sie WebSocket/PeerConnection zu go2rtc
# wirklich schliesst (im Quellcode verifiziert, nicht geraten). Bei 2.0s
# Cooldown war unser Fenster also 3s zu kurz: go2rtc konnte in dieser Luecke
# noch glauben, es gaebe einen Zuschauer, und den exec-Prozess (= neues
# INVITE) erneut starten, waehrend die Klingel noch mit dem letzten Gespraech
# beschaeftigt war. Passt auch zur "Play"-Anzeige im iOS-Sperrbildschirm nach
# dem Auflegen - genau dieses Nachlauf-Fenster.
COOLDOWN_FILE = "/config/scripts/klingel_cooldown"
COOLDOWN_SECS = 7.0

# Sicherheitsnetz: Sollte das Auflegen aus irgendeinem Grund nicht ankommen
# (HANGUP_FILE wird nicht angelegt, go2rtc beendet den Prozess nicht, etc.),
# darf ein Gespraech trotzdem nie "fuer immer" offen bleiben - das wuerde die
# Klingel dauerhaft in "im Gespraech" haengen lassen und auch die native
# Loxone-App blockieren (bestaetigt in klingel_crash_6.pcap: Call #2 lief
# bis zum Ende des Mitschnitts weiter, kein BYE). Deshalb: harte Obergrenze
# ab dem Moment, wo der Anruf angenommen wurde (ok == True). Das bestehende
# try/finally sorgt dafuer, dass auch dieser Abbruch garantiert ein BYE
# an die Klingel schickt.
MAX_CALL_SECS = 60

if os.path.exists(COOLDOWN_FILE):
    try:
        age = time.time() - os.path.getmtime(COOLDOWN_FILE)
    except OSError:
        age = COOLDOWN_SECS + 1
    if age < COOLDOWN_SECS:
        print(f"[in] Abklingzeit nach letztem Auflegen aktiv ({age:.2f}s) - "
              "starte noch keinen neuen Anruf.", file=sys.stderr, flush=True)
        sys.exit(0)

# COOLDOWN_FILE wird NICHT erst beim (sauberen) Auflegen geschrieben, sondern
# schon jetzt, bevor ueberhaupt ein INVITE raus geht. Hintergrund: beim
# schnellen Hin-und-Her-Toggeln (mehrfach in Sekundenbruchteilen) kann go2rtc
# einen laufenden exec-Prozess auch hart beenden (SIGKILL - laesst sich in
# Python prinzipiell nicht abfangen, kein try/finally rettet davor), bevor er
# ueberhaupt zum eigenen finally-Block kommt. Dann wird nie ein Cooldown-
# Zeitstempel hinterlegt, und der naechste (ebenfalls hastig gestartete)
# Prozess waehlt sofort wieder an - genau das Muster aus dem Mitschnitt vom
# wilden Toggle-Test: ein Anruf bekam ein 200 OK, aber nie ein ACK (Prozess
# vermutlich mitten drin gekillt), die Klingel wiederholte ihr 200 OK
# viermal unbeantwortet, und die beiden naechsten Anrufversuche liefen
# prompt in "486 Busy". Mit dem Zeitstempel schon HIER ist die Mindest-
# abstand-Garantie unabhaengig davon, wie (oder ob ueberhaupt) der vorherige
# Prozess noch zum Aufraeumen gekommen ist.
try:
    with open(COOLDOWN_FILE, "w") as f:
        f.write(str(time.time()))
except OSError:
    pass

# Admin-Passwort der Klingel (muss mit dem lokalen Admin-PW *und* dem
# sip.antisip.com-PW identisch sein - die Klingel zieht beide synonym heran).
# Bei Aenderung des Passworts hier einfach den neuen Wert eintragen -
# kein neuer Mitschnitt mehr noetig (siehe Formel unten).
KLINGEL_PASSWORT = "by4MHRmxy4HFqta"

def make_sec_headers(password):
    # secHdrA: beliebiger zufaelliger String (von der Loxone-App als
    # Nonce verwendet, aber kein per-Call-Token - ein fester Wert pro
    # Prozesslauf reicht). secHdrB = MD5(secHdrA + Passwort), hex Grossbuchstaben
    # (reverse-engineered aus Vergleich mehrerer Mitschnitte).
    alphabet = string.ascii_letters + string.digits
    a = "".join(random.choice(alphabet) for _ in range(31))
    b = hashlib.md5((a + password).encode()).hexdigest().upper()
    return a, b

SECHDR_A, SECHDR_B = make_sec_headers(KLINGEL_PASSWORT)

def eprint(*a): print(*a, file=sys.stderr, flush=True)

def local_ip(dst):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((dst, 9)); ip = s.getsockname()[0]; s.close(); return ip

def wav_streaming_header_alaw(sample_rate=8000):
    # RIFF/WAVE-Header mit A-law-Formatcode (6), Datengroesse als "unbekannt"
    # (0xFFFFFFFF), damit Player/Demuxer bis zum Prozessende weiterlesen.
    fmt = struct.pack("<HHIIHH", 6, 1, sample_rate, sample_rate, 1, 8)
    riff_size = 0xFFFFFFFF
    data_size = 0xFFFFFFFF
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVEfmt " +
            struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_size))

LIP = local_ip(DST)
sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sip.bind((LIP, 0))
SPORT = sip.getsockname()[1]
rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rtp.bind((LIP, RTP_PORT))
rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rtcp.bind((LIP, RTP_PORT + 1))
back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); back.bind(("127.0.0.1", BACK_PORT))

callid = str(uuid.uuid4()).upper(); ftag = str(uuid.uuid4()).upper()
branch = "z9hG4bKPj" + str(uuid.uuid4()).upper(); cseq = random.randint(1000, 30000)
sdp = ("v=0\r\no=- {n} {n} IN IP4 {ip}\r\ns=pjmedia\r\nt=0 0\r\na=X-nat:0\r\n"
       "m=audio {rp} RTP/AVP 8 121\r\nc=IN IP4 {ip}\r\na=rtcp:{rc} IN IP4 {ip}\r\n"
       "a=sendrecv\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:121 telephone-event/8000\r\n"
       "a=fmtp:121 0-16\r\n").format(n=int(time.time()), ip=LIP, rp=RTP_PORT, rc=RTP_PORT + 1)
inv = ("INVITE sip:admin@{dst} SIP/2.0\r\n"
       "Via: SIP/2.0/UDP {ip}:{sp};rport;branch={br}\r\nMax-Forwards: 70\r\n"
       "From: sip:smarthome@loxone.com;tag={ft}\r\nTo: sip:admin@{dst}\r\n"
       "Contact: <sip:smarthome@{ip}:{sp};ob>\r\nCall-ID: {cid}\r\nCSeq: {cs} INVITE\r\n"
       "Allow: PRACK, INVITE, ACK, BYE, CANCEL, UPDATE, INFO, SUBSCRIBE, NOTIFY, REFER, MESSAGE, OPTIONS\r\n"
       "Supported: replaces, 100rel, timer, norefersub\r\nSession-Expires: 1800\r\nMin-SE: 90\r\n"
       "User-Agent: Loxone Pjsua2 Wrapper\r\nsecHdrA: {a}\r\nsecHdrB: {b}\r\n"
       "Priority: urgent\r\nanswer-mode: auto\r\nContent-Type: application/sdp\r\n"
       "Content-Length: {cl}\r\n\r\n").format(dst=DST, ip=LIP, sp=SPORT, br=branch, ft=ftag,
       cid=callid, cs=cseq, a=SECHDR_A, b=SECHDR_B, cl=len(sdp)) + sdp

stop = False
def handle_term(signum, frame):
    global stop; stop = True
signal.signal(signal.SIGTERM, handle_term); signal.signal(signal.SIGINT, handle_term)

eprint(f"[in] {LIP}:{SPORT} -> {DST}:{SIP_PORT}, Rueckkanal auf 127.0.0.1:{BACK_PORT}")
sip.sendto(inv.encode(), (DST, SIP_PORT))
t0 = time.time(); last = t0; rexmit = 0.5
to_tag = None; rem_rtp = None; ok = False; seq_out = 0; ts_out = 0; t_call_start = None
ssrc_out = random.getrandbits(32)

header_written = False

# WICHTIG: Alles, was nach einem etablierten Call passieren kann (Pipe von
# go2rtc geschlossen -> BrokenPipeError beim Schreiben auf stdout, SIGTERM,
# oder irgendein anderer unerwarteter Fehler), darf niemals dazu fuehren,
# dass der Prozess einfach stirbt, OHNE der Klingel ein BYE zu schicken.
# Genau das ist vermutlich der Grund fuer die minutenlangen Aussetzer: die
# Klingel bleibt "im Gespraech" haengen und bekommt nur noch unzustellbares
# RTP (ICMP port unreachable), ohne je zu erfahren, dass wir laengst weg
# sind. Deshalb hier try/finally: das BYE im finally-Block wird in JEDEM
# Fall versucht, auch bei einer Exception mitten in der Schleife.
try:
    while not stop:
        now = time.time()
        if not ok and now - t0 > 10:
            eprint("[in] Keine Antwort der Klingel - verwerfe Anruf."); sys.exit(1)
        if not ok and now - last >= rexmit:
            sip.sendto(inv.encode(), (DST, SIP_PORT)); last = now; rexmit = min(rexmit * 2, 4)
        if os.path.exists(HANGUP_FILE):
            eprint("[in] Hangup-Signal von HA erhalten - lege auf.")
            try: os.remove(HANGUP_FILE)
            except OSError: pass
            stop = True; break
        if ok and t_call_start is not None and now - t_call_start > MAX_CALL_SECS:
            eprint(f"[in] MAX_CALL_SECS ({MAX_CALL_SECS}s) erreicht - lege sicherheitshalber auf.")
            stop = True; break

        r, _, _ = select.select([sip, rtp, back], [], [], 0.05)
        for s in r:
            data, addr = s.recvfrom(4096)
            if s is sip:
                txt = data.decode("latin1"); first = txt.split("\r\n")[0]
                if first.startswith("SIP/2.0 2") and "INVITE" in txt.split("CSeq:")[1].split("\r\n")[0]:
                    if not ok:
                        for l in txt.split("\r\n"):
                            if l.lower().startswith("to:") and "tag=" in l:
                                to_tag = l.split("tag=")[1].strip()
                        rport = RTP_PORT
                        for l in txt.split("\r\n"):
                            if l.startswith("m=audio"): rport = int(l.split()[1])
                        rem_rtp = (DST, rport); ok = True; t_call_start = now
                        eprint("[in] Anruf angenommen, Audio laeuft.")
                    ack = ("ACK sip:jstreu@{dst}:5060 SIP/2.0\r\nVia: SIP/2.0/UDP {ip}:{sp};rport;branch=z9hG4bKPj{u}\r\n"
                           "Max-Forwards: 70\r\nFrom: sip:smarthome@loxone.com;tag={ft}\r\nTo: sip:admin@{dst};tag={tt}\r\n"
                           "Call-ID: {cid}\r\nCSeq: {cs} ACK\r\nContent-Length: 0\r\n\r\n").format(
                           dst=DST, ip=LIP, sp=SPORT, u=uuid.uuid4().hex.upper(), ft=ftag, tt=to_tag, cid=callid, cs=cseq)
                    sip.sendto(ack.encode(), (DST, SIP_PORT))
                elif first.startswith("SIP/2.0 ") and int(first.split()[1]) >= 300:
                    eprint("[in] Klingel lehnt ab:", first)
                    # WICHTIG: Jede finale Antwort auf ein INVITE - auch eine
                    # Fehlerantwort wie "486 Busy Here" - MUSS per SIP-Spec mit
                    # einem ACK bestaetigt werden (RFC 3261, 17.1.1.3). Fehlt das,
                    # wiederholt die Klingel ihre Antwort minutenlang (Retransmit-
                    # Timer), weil sie denkt, die Antwort sei verlorengegangen -
                    # das kann den (schwachbruestigen) SIP/Web-Stack des Geraets
                    # ordentlich ausbremsen. Dieses ACK gehoert zur selben
                    # Transaktion wie das INVITE: gleicher Branch, gleiche CSeq-
                    # Nummer (nur Methode ACK statt INVITE), To-Tag aus der
                    # Fehlerantwort uebernehmen, falls vorhanden.
                    err_to_tag = None
                    for l in txt.split("\r\n"):
                        if l.lower().startswith("to:") and "tag=" in l:
                            err_to_tag = l.split("tag=")[1].strip()
                    err_ack = ("ACK sip:jstreu@{dst}:5060 SIP/2.0\r\nVia: SIP/2.0/UDP {ip}:{sp};rport;branch={br}\r\n"
                               "Max-Forwards: 70\r\nFrom: sip:smarthome@loxone.com;tag={ft}\r\nTo: sip:admin@{dst}{tt}\r\n"
                               "Call-ID: {cid}\r\nCSeq: {cs} ACK\r\nContent-Length: 0\r\n\r\n").format(
                               dst=DST, ip=LIP, sp=SPORT, br=branch, ft=ftag,
                               tt=(";tag=" + err_to_tag if err_to_tag else ""),
                               cid=callid, cs=cseq)
                    sip.sendto(err_ack.encode(), (DST, SIP_PORT))
                    sys.exit(2)
                elif first.startswith("INVITE ") and ok:
                    # Re-INVITE der Klingel MITTEN im laufenden Gespraech -
                    # das ist ihr eigener SIP-Session-Timer-Refresh (wir haben
                    # "Session-Expires: 1800" angeboten, die Klingel fordert
                    # trotzdem alle paar Sekunden ein Refresh ein). Wird das
                    # nicht beantwortet, wiederholt sie es endlos UND setzt
                    # dabei offenbar zeitweise das Audio aus (im Mitschnitt
                    # bestaetigt: 11+ Sekunden RTP-Stille genau in diesem
                    # Fenster) - also unbedingt mit 200 OK + derselben SDP
                    # bestaetigen, damit sie weiss, dass wir noch da sind.
                    via_lines = "\r\n".join(l for l in txt.split("\r\n") if l.lower().startswith("via:"))
                    from_line = next((l for l in txt.split("\r\n") if l.lower().startswith("from:")), f"From: sip:admin@{DST}")
                    to_line = next((l for l in txt.split("\r\n") if l.lower().startswith("to:")), f"To: sip:smarthome@loxone.com;tag={ftag}")
                    callid_line = next((l for l in txt.split("\r\n") if l.lower().startswith("call-id:")), f"Call-ID: {callid}")
                    cseq_line = next((l for l in txt.split("\r\n") if l.lower().startswith("cseq:")), "CSeq: 1 INVITE")
                    reinv_ok = ("SIP/2.0 200 OK\r\n{via}\r\n{frm}\r\n{to}\r\n{cid}\r\n{cs}\r\n"
                                "Contact: <sip:smarthome@{ip}:{sp};ob>\r\nContent-Type: application/sdp\r\n"
                                "Content-Length: {cl}\r\n\r\n").format(
                                via=via_lines, frm=from_line, to=to_line, cid=callid_line, cs=cseq_line,
                                ip=LIP, sp=SPORT, cl=len(sdp)) + sdp
                    sip.sendto(reinv_ok.encode(), addr)
                    eprint("[in] Re-INVITE (Session-Refresh) der Klingel beantwortet.")
                elif first.startswith("BYE "):
                    # Klingel legt selbst auf (z.B. weil ihr eigener Timer
                    # abgelaufen ist). Kurz quittieren und sauber beenden -
                    # kein eigenes BYE mehr hinterherschicken, der Dialog ist
                    # schon vorbei.
                    via_lines = "\r\n".join(l for l in txt.split("\r\n") if l.lower().startswith("via:"))
                    from_line = next((l for l in txt.split("\r\n") if l.lower().startswith("from:")), "")
                    to_line = next((l for l in txt.split("\r\n") if l.lower().startswith("to:")), "")
                    callid_line = next((l for l in txt.split("\r\n") if l.lower().startswith("call-id:")), f"Call-ID: {callid}")
                    cseq_line = next((l for l in txt.split("\r\n") if l.lower().startswith("cseq:")), "CSeq: 1 BYE")
                    bye_ok = ("SIP/2.0 200 OK\r\n{via}\r\n{frm}\r\n{to}\r\n{cid}\r\n{cs}\r\n"
                              "Content-Length: 0\r\n\r\n").format(
                              via=via_lines, frm=from_line, to=to_line, cid=callid_line, cs=cseq_line)
                    sip.sendto(bye_ok.encode(), addr)
                    eprint("[in] Klingel hat selbst aufgelegt (BYE erhalten).")
                    to_tag = None  # verhindert, dass wir im finally-Block nochmal ein BYE schicken
                    stop = True; break
            elif s is rtp and ok:
                if len(data) <= 12: continue
                pt = data[1] & 0x7f
                if pt != 8: continue  # nur PCMA, DTMF-Events (121) ignorieren
                if not header_written:
                    sys.stdout.buffer.write(wav_streaming_header_alaw()); header_written = True
                sys.stdout.buffer.write(data[12:]); sys.stdout.buffer.flush()
            elif s is back and ok and rem_rtp:
                # Rueckkanal-Haeppchen von klingel_rtp_back.py -> als RTP zur Klingel
                pkt = struct.pack("!BBHII", 0x80, 8, seq_out & 0xffff, ts_out, ssrc_out) + data
                rtp.sendto(pkt, rem_rtp)
                seq_out += 1; ts_out += len(data)
except BrokenPipeError:
    eprint("[in] stdout-Pipe von go2rtc geschlossen (kein Zuschauer mehr) - lege auf.")
except SystemExit:
    raise
except Exception as e:
    eprint(f"[in] Unerwarteter Fehler in der Hauptschleife ({e!r}) - lege trotzdem auf.")
finally:
    if ok and to_tag:
        bye = ("BYE sip:jstreu@{dst}:5060 SIP/2.0\r\nVia: SIP/2.0/UDP {ip}:{sp};rport;branch=z9hG4bKPj{u}\r\n"
               "Max-Forwards: 70\r\nFrom: sip:smarthome@loxone.com;tag={ft}\r\nTo: sip:admin@{dst};tag={tt}\r\n"
               "Call-ID: {cid}\r\nCSeq: {cs} BYE\r\nUser-Agent: Loxone Pjsua2 Wrapper\r\nContent-Length: 0\r\n\r\n").format(
               dst=DST, ip=LIP, sp=SPORT, u=uuid.uuid4().hex.upper(), ft=ftag, tt=to_tag, cid=callid, cs=cseq + 1)
        try:
            sip.sendto(bye.encode(), (DST, SIP_PORT))
            eprint("[in] Aufgelegt.")
        except OSError as e:
            eprint(f"[in] Konnte BYE nicht senden: {e!r}")
        try:
            with open(COOLDOWN_FILE, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
