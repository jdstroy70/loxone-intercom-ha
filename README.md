# Loxone Intercom Gen 1 (vor 10/2020) in Home Assistant

## Was das ist

Dieses Setup gibt einer SIP-faehigen Loxone Intercom Gen1 (vor 10/2020) echtes bidirektionales Gegensprechen direkt in Home Assistant - ohne die Loxone-App. Home Assistant ruft die Klingel per SIP selbst an, empfaengt das Audio und schickt das Mikrofon des Browsers/Handys als Rueckkanal zurueck. Video laeuft weiterhin ueber die normale MJPEG-Kamera-Entity, nicht ueber diesen Pfad.

Technisch: ein go2rtc-Stream mit zwei kleinen Python-Skripten als Quelle (SIP/RTP-Bruecke), eine `input_boolean`-Helper-Entity als Schalter, zwei Home-Assistant-Automationen (Auflegen + Timeout) und eine Dashboard-Karte mit WebRTC-Wiedergabe.

## Voraussetzungen

- Der Kamera Stream ist bereits per MJPEG vom separaten Kameramodul abgegriffen und lässt sich als Karte darstellen
- Klingelsignal wird per pyloxone bereits in HA abgefragt und kann per Automation signalisieren (z.B. Push und Dashboards wechseln)
- Home Assistant mit dem **go2rtc**-Add-on installiert (liefert die SIP/RTP-Bruecke und den WebRTC-Player)
- Die **WebRTC Camera**-Karte von AlexxIT (`custom:webrtc-camera`), per HACS installiert
- Dateizugriff auf `/config` (z. B. File-Editor- oder SSH/Terminal-Add-on)
- Die IP-Adresse der Klingel im LAN (hier beispielhaft `192.168.178.21`)
- Das **Admin-Passwort der Klingel** (dasselbe, das auch fuer sip.antisip.com gilt)
- Etwas Geduld: die Klingel reagiert auf SIP-Grobheiten (zu schnelles, wiederholtes Anwaehlen) empfindlich - das Setup ist bewusst defensiv/langsam gebaut, siehe Abschnitt *Technischer Hintergrund*
- Firmware Update auf Loxone_TuermodulPlus_GN2MK_DSP_de_V8g7: https://www.loxone.com/dede/wp-content/uploads/sites/2/2019/10/20191001_LoxoneSipUpdate.zip

## Anmerkungen zur Firmware
- Ab dieser Firmware wird offensichtlich das Passwort des admin Accounts der Intercom im SIP Invite geschickt
- In der Loxone Config ist nur ein Feld für Passwort (intern und extern)
- Daraus folgt: der sip.antisip.com User und Admin müssen dasselbe Passwort haben.

## Datei-Checkliste

| Datei | Zielort | Zweck |
| --- | --- | --- |
| `klingel_rtp_in.py` | `/config/scripts/` | Haupt-Skript: baut den SIP/RTP-Anruf auf, liefert empfangenes Audio an go2rtc, legt bei Bedarf auf |
| `klingel_rtp_back.py` | `/config/scripts/` | Rueckkanal-Skript: nimmt Mikrofon-Audio von go2rtc entgegen und reicht es an `klingel_rtp_in.py` weiter |
| `go2rtc.yaml` | go2rtc-Add-on-Konfiguration | definiert den Stream `klingel_audio` mit den beiden Skripten als Quelle |
| `configuration.yaml` (Ausschnitt) | `/config/` | registriert den `shell_command.klingel_hangup` |
| Automation "Auflegen" | `automations.yaml` | loest `shell_command.klingel_hangup` aus, wenn der Schalter auf *aus* geht |
| Automation "Auto-Timeout" | `automations.yaml` | schaltet den Schalter nach 2 Minuten automatisch aus, falls niemand manuell auflegt |
| Dashboard-Abschnitt (Türklingel-View) | Lovelace-Dashboard | Sprechen-/Auflegen-Buttons, Video-/Audio-Karte mit Auflegen-Shortcut |
| `input_boolean.klingel_gegensprechen` | Helfer (UI oder YAML) | der eigentliche Schalter, den alles andere beobachtet |


## Installation Schritt fuer Schritt

**1. Helfer anlegen** Einstellungen -> Geraete & Dienste -> Helfer -> "Schalter" (`input_boolean`) mit dem Entity-Namen `input_boolean.klingel_gegensprechen` anlegen.

**2. Skripte kopieren** `klingel_rtp_in.py` und `klingel_rtp_back.py` nach `/config/scripts/` legen und ausfuehrbar machen (`chmod +x`). Im Kopf von `klingel_rtp_in.py` das Admin-Passwort der Klingel eintragen (`KLINGEL_PASSWORT`).

**3. go2rtc konfigurieren** In der go2rtc-Add-on-Konfiguration den Stream eintragen:

```yaml
streams:
  klingel_audio:
    - exec:python3 /config/scripts/klingel_rtp_in.py 192.168.178.21
    - exec:python3 /config/scripts/klingel_rtp_back.py#backchannel=1#audio=alaw/8000
```

IP-Adresse an die eigene Klingel anpassen.

**4. shell\_command registrieren** In `configuration.yaml`:

```yaml
shell_command:
  klingel_hangup: "touch /config/scripts/klingel_hangup"
```

**5. Automation "Auflegen" anlegen** (in `automations.yaml`):

```yaml
- alias: Klingel Gegensprechen - Auflegen
  trigger:
    - platform: state
      entity_id: input_boolean.klingel_gegensprechen
      to: "off"
  action:
    - service: shell_command.klingel_hangup
```

**6. Automation "Auto-Timeout" anlegen** (zweiter Eintrag in `automations.yaml`, eigene `id`):

```yaml
- id: "1791975955200"
  alias: Klingel Gegensprechen - Auto-Timeout nach 2min
  trigger:
    - trigger: state
      entity_id: input_boolean.klingel_gegensprechen
      to: "on"
      for:
        minutes: 2
  condition: []
  action:
    - action: input_boolean.turn_off
      target:
        entity_id: input_boolean.klingel_gegensprechen
  mode: single
```

**7. Home Assistant neu laden** Einstellungen -> System -> Neu laden -> Automationen (und ggf. YAML neu laden), damit Helfer, `shell_command` und Automationen aktiv werden. Das go2rtc-Add-on braucht nur beim allerersten Einrichten einen Neustart; spaetere Aenderungen an den `.py`-Dateien wirken automatisch beim naechsten Anruf, ohne Add-on-Neustart.

## Dashboard-Karte

Die Türklingel-Seite ist eine eigene Lovelace-Unterseite (`type: sections`, `subview: true`). Der relevante Ausschnitt:

1. **Kamerabild** (`picture-glance`) zeigt den nativen MJPEG/Still-Stream der Klingelkamera (`camera.hausture`) – bewusst NICHT über go2rtc geroutet, da dafür kein Umweg nötig ist.
2. **Conditional Card** mit der eigentlichen `custom:webrtc-camera`-Karte für das Gegensprechen, sichtbar nur wenn `input_boolean.klingel_gegensprechen` = `on` ist. Darin:
   - `streams: klingel_audio` im `mode: webrtc`, `media: audio,microphone` – das ist der go2rtc-Stream aus Schritt 3.
   - `shortcuts:` fügt einen eigenen "Auflegen"-Button (Icon `mdi:phone-hangup`) direkt im Video-Overlay hinzu, der `input_boolean.turn_off` aufruft.
   - `style:` verkleinert die Karte auf 60px Höhe (gerade genug für Lautsprecher/Unmute-Icon), blendet Header und ungenutzte Icons (Vollbild, Screenshot, Picture-in-Picture, Stream-Auswahl) aus und färbt den Auflegen-Button rot.
3. Zwei **Buttons** "Sprechen" (grün, schaltet den input\_boolean ein) und "Auflegen" (rot, schaltet ihn aus), die sich über `visibility:`-Bedingungen gegenseitig ausblenden – nie beide gleichzeitig sichtbar.
4. Weitere Buttons (Türöffner, Garage, Licht) gehören nicht zur Gegensprech-Funktion, sondern sind installationsspezifisch und können weggelassen oder ersetzt werden.

Alle Entity-IDs wie `camera.hausture`, `button.turoffner_homekit`, `cover.gargentor` und `light.licht_hausture_hausture_strahler` sind Beispiele aus dieser Installation – beim Nachbauen durch die eigenen Entities ersetzen. Nur der Teil mit `input_boolean.klingel_gegensprechen` und der `custom:webrtc-camera`-Karte ist für die Gegensprechfunktion selbst notwendig.

## Testen

1. **Klingeln simulieren / Sprechen starten**: Auf der Türklingel-Seite "Sprechen" drücken. Der `input_boolean` geht auf `on`, die Gegensprechen-Karte erscheint, `klingel_rtp_in.py` schickt das SIP-INVITE an die Klingel.
2. **Prüfen, ob Audio ankommt**: Ton der Klingel sollte im Browser/der App zu hören sein (ggf. auf das Lautsprecher-Icon tippen, falls die Karte mit `muted: false` trotzdem stumm startet – Browser-Autoplay-Policy).
3. **Zurücksprechen**: Mikrofon am Gerät/Browser erlauben, sprechen – sollte an der Klingel hörbar sein.
4. **Auflegen testen** – auf drei Arten, alle sollten funktionieren:
   - über den roten "Auflegen"-Button auf der Dashboard-Seite,
   - über den "Auflegen"-Shortcut direkt im Video-Overlay,
   - automatisch nach 2 Minuten, falls niemand manuell auflegt (Auto-Timeout-Automation).
5. **Logs prüfen**: `klingel_rtp_in.py` schreibt Statusmeldungen nach stderr – bei Problemen die go2rtc-Logs (Einstellungen → Add-ons → go2rtc → Log) ansehen, dort tauchen die stdout/stderr-Ausgaben der `exec:`-Quelle auf.
6. **Mehrfach hintereinander testen**: Mehrmals toggeln (Sprechen → Auflegen → Sprechen …) um die Cooldown-Logik zu prüfen. Ein zu schnelles erneutes Sprechen-Drücken direkt nach dem Auflegen wird für `COOLDOWN_SECS` (7 Sekunden) ignoriert – das ist Absicht, siehe Abschnitt "Grenzen".

## Grenzen und bekannte Eigenheiten

Diese Lösung basiert auf ausgiebigem Testen mit einer konkreten Loxone-Türsprechanlage und reverse-engineertem SIP-Verhalten (keine offizielle API). Ein paar Dinge, die man kennen sollte, bevor man sich wundert:

- **Feste RTP-Ports (4000/4001)**: Bewusst nicht dynamisch vergeben. Frühere Versuche mit wechselnden Ports führten zu Instabilität am Gerät. Nicht ohne Grund ändern.
- **Cooldown von 7 Sekunden**: Nach jedem Gesprächsende (Auflegen) ignoriert das Skript für 7 Sekunden ein neues INVITE. Grund: Die `webrtc-camera`-Karte selbst wartet intern 5 Sekunden, bevor sie ihre WebRTC-Verbindung tatsächlich schließt (hartcodiertes `DISCONNECT_TIMEOUT` im Karten-Code) – der Cooldown muss länger sein als das, sonst überschneiden sich altes Schließen und neues Öffnen.
- **Schnelles Hin-und-Her-Toggeln**: Wer sehr schnell mehrfach hintereinander Sprechen/Auflegen drückt, kann go2rtc dazu bringen, den laufenden `exec:`-Prozess hart zu beenden (SIGKILL statt eines sauberen Stopps), bevor das Python-Skript sein BYE senden oder aufräumen kann. Das lässt sich in Python grundsätzlich nicht abfangen. Der Cooldown-beim-Start-Fix (COOLDOWN\_FILE wird schon beim Verbindungsaufbau geschrieben, nicht erst beim Auflegen) reduziert das Risiko deutlich, schließt es aber nicht zu 100% aus.
- **Gelegentliches ca. 3-minütiges "Einfrieren" des Klingel-Geräts**: Wurde ab und zu beobachtet, ließ sich aber nicht zweifelsfrei auf das eigene Skript zurückführen – vermutlich ein interner Watchdog/Timeout der Klingel-Firmware selbst, unabhängig von unserem Code. Nicht abschließend geklärt. Tritt die Klingel in diesen Zustand, hilft nur Warten (ca. 3 Minuten), danach nimmt sie wieder INVITEs an.
- **MAX\_CALL\_SECS im Skript ist 60, nicht 120**: Die Beschreibung der Auto-Timeout-Automation erwähnt ein Zusammenspiel mit "MAX\_CALL\_SECS=120" im Skript – im tatsächlich verwendeten, finalen Skript steht aber `MAX_CALL_SECS = 60`. Das ist eine kleine Inkonsistenz zwischen Kommentar und Code, die beim Testen nicht aufgefallen ist, weil die 2-Minuten-Automation ohnehin früher greift. Funktional ohne Belang, aber falls man die Timeouts anpassen will: beide Stellen prüfen.
- **HA Companion App (iOS) – Lockscreen-Artefakt**: Einmalig beobachtet, dass nach Auflegen und Sperren des iPhones noch ein "Play"-Button auf dem Sperrbildschirm erschien, als liefe noch ein Media-Stream. Nicht reproduzierbar gewesen – vermutlich ein Nebeneffekt der App selbst beim Beenden der Audio-Session, kein Fehler in unserem Code.
- **Kein offizieller SIP-Stack**: `klingel_rtp_in.py` baut SIP-Nachrichten manuell als Strings zusammen (inkl. der proprietären `secHdrA`/`secHdrB`-Authentifizierung von Loxone). Das funktioniert zuverlässig mit der getesteten Firmware-Version, ist aber kein Ersatz für eine echte SIP-Bibliothek und reagiert entsprechend empfindlich auf unerwartetes Serververhalten.
- **WebRTC Karte ist leider sichtbar und trotz Schalter muted: false in der Companion App immer auf mute. Muss jedes mal unmuted geschaltet werden.

## Technischer Hintergrund

Für wer tiefer einsteigen oder das Skript anpassen will:

**Ablauf eines Gesprächs:**

1. `input_boolean.klingel_gegensprechen` wird auf `on` gesetzt (Button oder Automation).
2. go2rtc startet die `exec:`-Quelle des Streams `klingel_audio`, also `klingel_rtp_in.py`.
3. Das Skript baut ein SIP-INVITE mit Loxones proprietärer Auth (`secHdrA`/`secHdrB`, `secHdrB = MD5(secHdrA + Passwort).hexdigest().upper()`) und fester SDP für Audio auf Port 4000 (RTP) / 4001 (RTCP).
4. Nach `200 OK` von der Klingel wird mit ACK bestätigt, danach läuft der eigentliche RTP-Audiostrom bidirektional: eingehendes Audio wird über go2rtc an den Browser/die App weitergegeben, das Mikrofon des Nutzers über die zweite `exec:`-Quelle (`klingel_rtp_back.py`) zurück an die Klingel geschickt.
5. Auflegen (manuell, Timeout oder Fehlerfall) setzt eine `HANGUP_FILE`-Markerdatei; das Skript erkennt das im Hauptloop, sendet BYE und beendet sich. Ein `finally`-Block garantiert, dass BYE auch bei Exceptions verschickt wird, solange das Gespräch bereits stand (`ok and to_tag`).
6. Fehlerhafte SIP-Antworten (Status ≥ 300) werden immer per ACK bestätigt, auch wenn das Gespräch damit abgebrochen wird – das ist laut RFC 3261 §17.1.1.3 für jede finale INVITE-Antwort Pflicht, nicht nur für 2xx. Ohne dieses ACK sendet die Gegenseite die Fehlerantwort in Schleife erneut.
7. Ein erneutes INVITE während eines laufenden Gesprächs (Re-INVITE, z. B. Session-Timer-Refresh) wird mit `200 OK` und der gleichen SDP beantwortet, statt es als neues Gespräch zu behandeln.

**Warum zwei separate Python-Skripte statt eines go2rtc-Standard-Treibers**: go2rtc hat keinen eingebauten SIP-Client. Die `exec:`-Quelle erlaubt es, beliebige Programme als Medienquelle/-senke einzubinden – hier wird das für eine komplette, von Hand gebaute SIP/RTP-Session genutzt, einmal für die eingehende Richtung (`klingel_rtp_in.py`, sendet INVITE, empfängt und leitet RTP-Audio an go2rtc weiter) und einmal für die Rückrichtung (`klingel_rtp_back.py`, nimmt PCMA-Häppchen von go2rtc entgegen und reicht sie als UDP an den laufenden `klingel_rtp_in.py`-Prozess weiter, der sie dann als RTP zur Klingel sendet).

**Warum das Video nicht über go2rtc läuft**: Die native Kamera-Entity (`camera.hausture`) liefert das Bild bereits zuverlässig über den normalen Home-Assistant-Kamerapfad (MJPEG/Standbild). Es gab keinen Grund, das zusätzlich durch go2rtc zu schleusen – nur die Audiospur (SIP/RTP) brauchte die Sonderbehandlung.

**Kleine bekannte Inkonsistenz**: Die Automation "Auto-Timeout" beschreibt ihr Zusammenspiel mit einem Sicherheitsnetz `MAX_CALL_SECS=120` im Skript – tatsächlich steht im Skript `MAX_CALL_SECS = 60`. Beide Mechanismen greifen ohnehin unabhängig voneinander (2-Minuten-Automation vs. 60-Sekunden-Skript-Limit als zusätzliches Netz), das Skript-Limit greift also immer zuerst. Rein kosmetischer Kommentar-Unterschied, aber gut zu wissen, falls man eines der beiden Timeouts bewusst verlängern möchte.

**Warum keine fertige SIP-Bibliothek verwendet wird**: Loxones Türsprechanlagen-Firmware nutzt eine nicht standardkonforme, proprietäre Authentifizierung (`secHdrA`/`secHdrB`) obendrauf auf sonst recht klassisches SIP. Eine generische SIP-Bibliothek hätte das nicht ohne Weiteres unterstützt; der Handbau erlaubte es, exakt auf das beobachtete Verhalten der Klingel einzugehen (inklusive der Eigenheiten aus dem Abschnitt "Grenzen").
