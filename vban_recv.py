#!/usr/bin/env python3
"""Récepteur VBAN autonome pour Ubuntu / Xubuntu (PulseAudio ou PipeWire).

Lancer :  python3 vban_recv.py
Ouvrir :  http://IP_DU_PC:8096   (réglage des récepteurs : nom, port, IP, flux, sortie)

Aucune dépendance Python. Le son est joué par `pacat` (paquet pulseaudio-utils,
présent par défaut) ou, à défaut, par `aplay` (alsa-utils).
Chaque récepteur apparaît comme une application dans le mixeur (mixer.py / pavucontrol).
Réglages enregistrés dans vban_recv.json, à côté du script.
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "vban_recv.json")
BACKEND = "pacat"

HEADER = struct.Struct("<4sBBBB16sI")  # 28 octets

SAMPLE_RATES = [
    6000, 12000, 24000, 48000, 96000, 192000, 384000,
    8000, 16000, 32000, 64000, 128000, 256000, 512000,
    11025, 22050, 44100, 88200, 176400, 352800, 705600,
]

# type VBAN -> (taille octets, format pacat, format aplay)
DATATYPES = {
    0: (1, "u8", "U8"),
    1: (2, "s16le", "S16_LE"),
    2: (3, "s24le", "S24_3LE"),
    3: (4, "s32le", "S32_LE"),
    4: (4, "float32le", "FLOAT_LE"),
}

NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,32}$")
STREAM_RE = re.compile(r"^[\x20-\x7e]{0,16}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_.:,=\-]{0,128}$")

DEFAULT_CFG = {
    "receivers": [
        {"name": "VBAN-PC", "enabled": True, "port": 6980, "src": "",
         "stream": "", "device": "", "latency": 50},
    ]
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fmt_text(fmt):
    rate, channels, dtype = fmt
    return "%d Hz · %d can · %s" % (rate, channels, DATATYPES[dtype][1])


# ---------------------------------------------------------------- config
def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return clean_config(json.load(f))
    except Exception:
        return json.loads(json.dumps(DEFAULT_CFG))


def clean_config(data):
    out = []
    for e in data.get("receivers", []):
        name = str(e.get("name", "")).strip()
        if not NAME_RE.match(name):
            raise ValueError(
                "Nom invalide : %r (lettres, chiffres, espace, _ . - ; 32 max)" % name)
        port = int(e.get("port", 6980))
        if not 1 <= port <= 65535:
            raise ValueError("Port invalide : %d" % port)
        src = str(e.get("src", "")).strip()
        if src:
            try:
                ipaddress.ip_address(src)
            except ValueError:
                raise ValueError("IP émettrice invalide : %r" % src)
        stream = str(e.get("stream", "")).strip()
        if not STREAM_RE.match(stream):
            raise ValueError("Nom de flux invalide : %r (16 caractères max)" % stream)
        device = str(e.get("device", "")).strip()
        if not DEVICE_RE.match(device):
            raise ValueError("Sortie invalide : %r" % device)
        latency = int(e.get("latency", 50))
        if not 10 <= latency <= 2000:
            raise ValueError("Latence invalide : %d ms (10 à 2000)" % latency)
        out.append({
            "name": name, "enabled": bool(e.get("enabled", True)), "port": port,
            "src": src, "stream": stream, "device": device, "latency": latency,
        })
    return {"receivers": out}


def list_devices():
    try:
        if BACKEND == "pacat":
            out = subprocess.run(["pactl", "list", "short", "sinks"],
                                 capture_output=True, text=True, timeout=5).stdout
            return [l.split("\t")[1] for l in out.splitlines() if "\t" in l]
        out = subprocess.run(["aplay", "-L"],
                             capture_output=True, text=True, timeout=5).stdout
        return [l for l in out.splitlines() if l and not l[0].isspace()]
    except Exception:
        return []


# ---------------------------------------------------------------- VBAN
def parse(packet):
    """Retourne (stream, (rate, canaux, type), frame, pcm) ou None."""
    if len(packet) <= HEADER.size:
        return None
    magic, sr, nbs, nbc, bit, name, frame = HEADER.unpack_from(packet)
    if magic != b"VBAN":
        return None
    if sr & 0xE0:          # sous-protocole non audio (texte, série…)
        return None
    if bit & 0xF0:         # codec compressé (non PCM)
        return None
    sr &= 0x1F
    dtype = bit & 0x07
    if sr >= len(SAMPLE_RATES) or dtype not in DATATYPES:
        return None
    samples, channels = nbs + 1, nbc + 1
    size = DATATYPES[dtype][0] * samples * channels
    pcm = packet[HEADER.size:HEADER.size + size]
    if len(pcm) != size:
        return None
    stream = name.split(b"\0", 1)[0].decode("ascii", "replace")
    return stream, (SAMPLE_RATES[sr], channels, dtype), frame, pcm


class Player:
    """Processus pacat/aplay alimenté en PCM brut sur stdin."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.fmt = None

    def start(self, fmt):
        rate, channels, dtype = fmt
        _, pa_fmt, alsa_fmt = DATATYPES[dtype]
        c = self.cfg
        if BACKEND == "pacat":
            cmd = [
                "pacat", "--playback", "--raw",
                "--format=%s" % pa_fmt,
                "--rate=%d" % rate,
                "--channels=%d" % channels,
                "--latency-msec=%d" % c["latency"],
                "--client-name=VBAN %s" % c["name"],
                "--stream-name=%s" % c["name"],
            ]
            if c["device"]:
                cmd.append("--device=%s" % c["device"])
        else:
            cmd = [
                "aplay", "-q", "-t", "raw",
                "-f", alsa_fmt, "-r", str(rate), "-c", str(channels),
                "-B", str(c["latency"] * 1000),
            ]
            if c["device"]:
                cmd += ["-D", c["device"]]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        self.fmt = fmt
        log("[%s] Lecture : %s" % (c["name"], fmt_text(fmt)))

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.fmt = None

    def write(self, fmt, pcm):
        if self.proc and (self.fmt != fmt or self.proc.poll() is not None):
            self.stop()
        if not self.proc:
            self.start(fmt)
        try:
            self.proc.stdin.write(pcm)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.stop()


class Receiver:
    """Un flux VBAN joué sur une sortie, selon les filtres IP / nom de flux."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.player = Player(cfg)
        self.error = ""
        self.lost = 0
        self.reset()

    def reset(self):
        self.locked = None     # (ip, flux) suivi
        self.last_frame = None
        self.last_rx = 0.0
        self.fmt = None

    def accepts(self, ip, stream):
        c = self.cfg
        if c["src"] and ip != c["src"]:
            return False
        if c["stream"] and stream != c["stream"]:
            return False
        return self.locked is None or self.locked == (ip, stream)

    def feed(self, ip, stream, fmt, frame, pcm, now):
        if self.locked is None:
            self.locked = (ip, stream)
            log("[%s] Flux reçu : %r depuis %s" % (self.cfg["name"], stream, ip))
        if self.last_frame is not None:
            gap = (frame - self.last_frame - 1) & 0xFFFFFFFF
            if 0 < gap < 1000:
                self.lost += gap
        self.last_frame = frame
        self.last_rx = now
        self.fmt = fmt
        self.player.write(fmt, pcm)

    def idle(self, now):
        if self.locked and now - self.last_rx > 2:
            log("[%s] Plus de signal, en attente…" % self.cfg["name"])
            self.player.stop()
            self.reset()

    def status(self):
        if self.error:
            return {"state": "error", "text": self.error}
        if not self.locked:
            return {"state": "wait", "text": "En attente de flux…"}
        ip, stream = self.locked
        return {
            "state": "ok",
            "text": "%s depuis %s · %s · %d perdus"
                    % (stream, ip, fmt_text(self.fmt), self.lost),
        }


class Listener(threading.Thread):
    """Un socket UDP par port, partagé par les récepteurs de ce port."""

    def __init__(self, port, receivers, manager):
        super().__init__(daemon=True)
        self.port = port
        self.receivers = receivers
        self.manager = manager
        self.running = True

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            sock.close()
            msg = "Port %d indisponible : %s" % (self.port, e.strerror)
            log(msg)
            for r in self.receivers:
                r.error = msg
            return
        sock.settimeout(0.5)
        log("Écoute VBAN sur le port %d" % self.port)
        with sock:
            while self.running:
                try:
                    packet, (ip, _) = sock.recvfrom(2048)
                except socket.timeout:
                    packet = None
                now = time.time()
                res = parse(packet) if packet else None
                if res:
                    stream, fmt, frame, pcm = res
                    self.manager.saw(self.port, ip, stream, fmt, now)
                    for r in self.receivers:
                        if r.accepts(ip, stream):
                            r.feed(ip, stream, fmt, frame, pcm, now)
                for r in self.receivers:
                    r.idle(now)
        for r in self.receivers:
            r.player.stop()


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.listeners = []
        self.receivers = {}    # index config -> Receiver
        self.seen = {}         # (port, ip, flux) -> (fmt, dernier paquet)

    def start(self):
        with self.lock:
            self._stop()
            self.receivers = {
                i: Receiver(c) for i, c in enumerate(self.cfg["receivers"]) if c["enabled"]
            }
            ports = sorted({r.cfg["port"] for r in self.receivers.values()})
            self.listeners = [
                Listener(p, [r for r in self.receivers.values() if r.cfg["port"] == p], self)
                for p in ports
            ]
            for l in self.listeners:
                l.start()

    def _stop(self):
        for l in self.listeners:
            l.running = False
        for l in self.listeners:
            l.join()
        self.listeners = []

    def stop(self):
        with self.lock:
            self._stop()

    def apply(self, data):
        cfg = clean_config(data)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        self.cfg = cfg
        self.start()

    def saw(self, port, ip, stream, fmt, now):
        self.seen[(port, ip, stream)] = (fmt, now)

    def status(self):
        now = time.time()
        rx = []
        for i in range(len(self.cfg["receivers"])):
            r = self.receivers.get(i)
            rx.append(r.status() if r else {"state": "off", "text": "Désactivé"})
        seen = [
            {"port": port, "ip": ip, "stream": stream, "fmt": fmt_text(fmt)}
            for (port, ip, stream), (fmt, t) in sorted(list(self.seen.items()))
            if now - t < 5
        ]
        return {"receivers": rx, "seen": seen}


# ---------------------------------------------------------------- web
PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Récepteur VBAN</title>
<style>
  :root { --bg:#14161a; --card:#1f232a; --fg:#e8eaed; --dim:#8b93a1; --acc:#4da3ff; --red:#e5484d; }
  * { box-sizing:border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px system-ui,sans-serif; }
  h1 { font-size:18px; margin:0 0 12px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin:22px 0 8px; }
  .card { background:var(--card); border-radius:10px; padding:12px; overflow-x:auto; }
  table { border-collapse:collapse; width:100%; }
  th { text-align:left; font-weight:500; color:var(--dim); font-size:12px; padding:4px 6px; white-space:nowrap; }
  td { padding:4px 6px; vertical-align:middle; }
  input, select { background:#2b3039; color:var(--fg); border:0; border-radius:6px; padding:6px; min-width:0; font:inherit; }
  input[type=checkbox] { accent-color:var(--acc); width:18px; height:18px; }
  button { background:#2b3039; color:var(--fg); border:0; border-radius:6px; padding:6px 10px; cursor:pointer; font:inherit; }
  button.main { background:var(--acc); color:#000; }
  .st { display:flex; align-items:center; gap:6px; color:var(--dim); font-size:13px; min-width:16em; }
  .dot { flex:none; width:10px; height:10px; border-radius:50%; background:#555; }
  .dot.ok { background:#3fb950; } .dot.wait { background:#f0b429; } .dot.error { background:var(--red); }
  .msg { color:var(--dim); margin-left:8px; }
  .bar { margin-top:12px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .seen { display:flex; gap:10px; align-items:center; padding:6px 0; border-bottom:1px solid #2b3039; }
  .seen:last-child { border:0; }
  .empty { color:var(--dim); }
  .hint { color:var(--dim); font-size:13px; margin-top:8px; }
</style>
</head>
<body>
<h1>Récepteur VBAN</h1>

<h2>Récepteurs</h2>
<div class="card">
  <table>
    <thead><tr>
      <th>Actif</th><th>Nom</th><th>Port</th><th>IP émettrice</th><th>Nom du flux</th>
      <th>Sortie</th><th>Latence ms</th><th>État</th><th></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="bar">
    <button onclick="addRx({})">+ Récepteur</button>
    <button class="main" onclick="apply()">Appliquer</button>
    <span class="msg" id="msg"></span>
  </div>
  <div class="hint">IP émettrice vide = toutes les machines. Nom du flux vide = le premier flux reçu.</div>
</div>

<h2>Flux détectés sur les ports écoutés</h2>
<div class="card" id="seen"></div>

<script>
const $ = id => document.getElementById(id);
let cfg = {receivers: []}, devices = [];

function field(obj, key, opts) {
  const i = document.createElement('input');
  if (opts.check) {
    i.type = 'checkbox'; i.checked = obj[key];
    i.onchange = () => obj[key] = i.checked;
  } else {
    i.value = obj[key]; i.placeholder = opts.ph || ''; i.style.width = opts.w;
    if (opts.max) i.maxLength = opts.max;
    i.oninput = () => obj[key] = opts.num ? (+i.value || 0) : i.value;
  }
  const td = document.createElement('td'); td.append(i); return td;
}

function render() {
  const box = $('rows'); box.replaceChildren();
  cfg.receivers.forEach((r, i) => {
    const tr = document.createElement('tr');
    const sel = document.createElement('select');
    const opts = [['', 'Sortie par défaut'], ...devices.map(d => [d, d])];
    if (r.device && !devices.includes(r.device)) opts.push([r.device, r.device + ' (absente)']);
    opts.forEach(([v, t]) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (v === r.device) o.selected = true; sel.append(o);
    });
    sel.onchange = () => r.device = sel.value;
    const tdSel = document.createElement('td'); tdSel.append(sel);
    const tdSt = document.createElement('td');
    tdSt.innerHTML = '<div class="st"><span class="dot"></span><span></span></div>';
    tdSt.id = 'st' + i;
    const del = document.createElement('button'); del.textContent = '✕';
    del.onclick = () => { cfg.receivers.splice(i, 1); render(); };
    const tdDel = document.createElement('td'); tdDel.append(del);
    tr.append(
      field(r, 'enabled', {check: true}),
      field(r, 'name', {ph: 'Nom', w: '9em', max: 32}),
      field(r, 'port', {ph: '6980', w: '5em', num: true}),
      field(r, 'src', {ph: 'toutes', w: '9em'}),
      field(r, 'stream', {ph: 'premier reçu', w: '9em', max: 16}),
      tdSel,
      field(r, 'latency', {w: '4em', num: true}),
      tdSt, tdDel
    );
    box.append(tr);
  });
}

function addRx(o) {
  cfg.receivers.push(Object.assign(
    {name: 'VBAN-' + (cfg.receivers.length + 1), enabled: true, port: 6980,
     src: '', stream: '', device: '', latency: 50}, o));
  render();
  $('msg').textContent = 'Cliquer sur Appliquer pour activer.';
}

async function load() {
  const r = await (await fetch('/api/config')).json();
  cfg = r.cfg; devices = r.devices;
  render(); status();
}

async function apply() {
  $('msg').textContent = 'Application…';
  try {
    const res = await fetch('/api/config', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cfg)});
    const r = await res.json();
    if (!r.ok) { $('msg').textContent = 'Erreur : ' + (r.error || 'inconnue'); return; }
    $('msg').textContent = 'Appliqué.';
    load();
  } catch (e) { $('msg').textContent = 'Erreur : ' + e; }
}

async function status() {
  const st = await (await fetch('/api/status')).json();
  st.receivers.forEach((s, i) => {
    const td = $('st' + i); if (!td) return;
    td.querySelector('.dot').className = 'dot ' + s.state;
    td.querySelector('span:last-child').textContent = s.text;
  });
  const box = $('seen'); box.replaceChildren();
  if (!st.seen.length) {
    const e = document.createElement('div'); e.className = 'empty';
    e.textContent = 'Aucun flux VBAN reçu pour le moment.'; box.append(e); return;
  }
  st.seen.forEach(s => {
    const row = document.createElement('div'); row.className = 'seen';
    const t = document.createElement('span');
    t.textContent = s.stream + ' — ' + s.ip + ' — port ' + s.port + ' — ' + s.fmt;
    const b = document.createElement('button'); b.textContent = 'Recevoir';
    b.onclick = () => addRx({
      name: (s.stream.replace(/[^A-Za-z0-9 _.\-]/g, '_').slice(0, 32) || 'VBAN'),
      port: s.port, src: s.ip, stream: s.stream});
    row.append(t, b); box.append(row);
  });
}

load();
setInterval(status, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    manager = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html")
        elif self.path == "/api/config":
            self._send(200, json.dumps({"cfg": self.manager.cfg, "devices": list_devices()}))
        elif self.path == "/api/status":
            self._send(200, json.dumps(self.manager.status()))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/config":
            return self._send(404, "{}")
        try:
            n = int(self.headers.get("Content-Length", 0))
            self.manager.apply(json.loads(self.rfile.read(n) or b"{}"))
            self._send(200, '{"ok":true}')
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "error": str(e)}))


def main():
    global BACKEND
    p = argparse.ArgumentParser(description="Récepteur VBAN -> haut-parleurs")
    p.add_argument("--web-port", type=int, default=8096, help="port de l'interface (8096)")
    p.add_argument("--web-bind", default="0.0.0.0", help="IP de l'interface (0.0.0.0)")
    p.add_argument("--backend", choices=["pacat", "aplay"], help="forcer le lecteur")
    args = p.parse_args()

    BACKEND = args.backend or ("pacat" if shutil.which("pacat") else "aplay")
    if not shutil.which(BACKEND):
        sys.exit("Installer : sudo apt install pulseaudio-utils  (ou alsa-utils)")

    manager = Manager()
    manager.start()
    Handler.manager = manager
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("Interface sur http://%s:%d (%s)" % (args.web_bind, args.web_port, BACKEND))
    try:
        ThreadingHTTPServer((args.web_bind, args.web_port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
