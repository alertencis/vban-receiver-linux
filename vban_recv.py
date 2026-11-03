#!/usr/bin/env python3
"""Récepteur VBAN autonome pour Ubuntu / Xubuntu (PulseAudio ou PipeWire).

Lancer :  python3 vban_recv.py
Ouvrir :  http://IP_DU_PC:8096   (réglage des récepteurs : nom, port, IP, flux, sortie)

Documentation : README.md. Licence MIT.

Aucune dépendance Python. Le son est joué par `pacat` (paquet pulseaudio-utils,
présent par défaut) ou, à défaut, par `aplay` (alsa-utils).
Chaque récepteur apparaît comme une application dans le mixeur du système (pavucontrol).
Réglages enregistrés dans vban_recv.json, à côté du script.
"""
import argparse
from array import array
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


def peak_level(dtype, pcm):
    """Crête 0..1 d'un paquet (un échantillon sur 4, pour le vumètre)."""
    if dtype == 0:
        a, zero, full = array("B", pcm), 128, 128.0
    elif dtype == 1:
        a, zero, full = array("h", pcm), 0, 32768.0
    elif dtype == 2:       # 24 bits : l'octet de poids fort suffit
        a, zero, full = array("b", pcm[2::3]), 0, 128.0
    elif dtype == 3:
        a, zero, full = array("i", pcm), 0, 2147483648.0
    else:
        a, zero, full = array("f", pcm), 0, 1.0
    a = a[::4]
    if not a:
        return 0.0
    peak = max(abs(x - zero) for x in a) / full
    return peak if peak < 1.0 else 1.0


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
        self.peak = 0.0
        self.peak_t = 0.0

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
        p = peak_level(fmt[2], pcm)
        if now - self.peak_t > 0.3:
            self.peak, self.peak_t = p, now
        elif p > self.peak:
            self.peak = p
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
            "level": self.peak if time.time() - self.peak_t < 0.6 else 0.0,
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%2338bdf8'/%3E%3Cpath d='M5 12h2l2-5 3 10 3-8 2 3h2' fill='none' stroke='%23062030' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
  :root {
    --bg:#0d1117; --bg2:#121821; --card:#161d27; --card2:#1c2531; --line:#263142;
    --fg:#e6edf3; --dim:#8b98a9; --acc:#38bdf8; --acc2:#818cf8;
    --ok:#3fb950; --wait:#f0b429; --err:#f85149; --r:14px;
  }
  * { box-sizing:border-box; }
  html { color-scheme:dark; }
  body {
    margin:0; min-height:100vh; color:var(--fg); font:15px/1.45 system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif;
    background:radial-gradient(1200px 500px at 10% -10%, #17324a 0%, transparent 60%),
               radial-gradient(900px 400px at 100% 0%, #241f4a 0%, transparent 55%), var(--bg);
  }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 20px 120px; }

  header { display:flex; align-items:center; gap:14px; margin-bottom:26px; flex-wrap:wrap; }
  .logo { width:44px; height:44px; border-radius:12px; display:grid; place-items:center;
          background:linear-gradient(135deg,var(--acc),var(--acc2)); box-shadow:0 6px 24px #38bdf833; }
  .logo svg { width:26px; height:26px; }
  h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
  .sub { color:var(--dim); font-size:13px; }
  .pills { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; }
  .pill { display:flex; align-items:center; gap:7px; background:var(--card); border:1px solid var(--line);
          border-radius:999px; padding:6px 12px; font-size:13px; color:var(--dim); }
  .pill b { color:var(--fg); font-weight:600; }

  .section { display:flex; align-items:center; justify-content:space-between; margin:30px 0 12px; gap:12px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.12em; color:var(--dim); margin:0; font-weight:600; }

  button { font:inherit; color:var(--fg); background:var(--card2); border:1px solid var(--line);
           border-radius:10px; padding:8px 14px; cursor:pointer; transition:.15s; }
  button:hover { border-color:#3a4a60; background:#222d3b; }
  button.primary { background:linear-gradient(135deg,var(--acc),var(--acc2)); color:#061423; border:0; font-weight:600; }
  button.primary:hover { filter:brightness(1.08); }
  button.ghost { background:transparent; }
  button.icon { padding:6px 9px; background:transparent; border-color:transparent; color:var(--dim); }
  button.icon:hover { color:var(--err); background:#f8514914; }
  button:disabled { opacity:.45; cursor:default; }

  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; }
  .card { background:linear-gradient(180deg,var(--card),var(--bg2)); border:1px solid var(--line);
          border-radius:var(--r); padding:16px; box-shadow:0 8px 30px #0006; transition:.2s; }
  .card.live { border-color:#3fb95055; }
  .card.off { opacity:.55; }

  .head { display:flex; align-items:center; gap:10px; }
  input, select { font:inherit; color:var(--fg); background:var(--bg); border:1px solid var(--line);
                  border-radius:9px; padding:8px 10px; width:100%; min-width:0; transition:.15s; }
  input:focus, select:focus { outline:0; border-color:var(--acc); box-shadow:0 0 0 3px #38bdf826; }
  input::placeholder { color:#5b6878; }
  input.title { font-size:17px; font-weight:600; background:transparent; border-color:transparent; padding:4px 6px; }
  input.title:hover { border-color:var(--line); }

  .switch { position:relative; flex:none; width:40px; height:22px; cursor:pointer; }
  .switch input { opacity:0; width:0; height:0; position:absolute; }
  .switch span { position:absolute; inset:0; background:#2a3443; border-radius:99px; transition:.2s; }
  .switch span::after { content:""; position:absolute; left:3px; top:3px; width:16px; height:16px;
                        border-radius:50%; background:#c9d1d9; transition:.2s; }
  .switch input:checked + span { background:var(--ok); }
  .switch input:checked + span::after { transform:translateX(18px); background:#fff; }

  .state { display:flex; align-items:center; gap:8px; margin:12px 0 8px; font-size:13px; color:var(--dim); min-height:20px; }
  .dot { flex:none; width:9px; height:9px; border-radius:50%; background:#4b5563; }
  .dot.ok { background:var(--ok); box-shadow:0 0 0 4px #3fb95026; animation:pulse 1.6s infinite; }
  .dot.wait { background:var(--wait); }
  .dot.error { background:var(--err); }
  @keyframes pulse { 50% { box-shadow:0 0 0 7px #3fb95000; } }

  .meter { position:relative; height:8px; border-radius:99px; overflow:hidden;
           background:linear-gradient(90deg,#2ea043 0%,#3fb950 62%,#f0b429 82%,#f85149 100%); }
  .meter .cover { position:absolute; top:0; right:0; bottom:0; width:100%; background:#0b1018; transition:width .12s linear; }

  .fields { display:grid; grid-template-columns:1fr 1fr; gap:10px 12px; margin-top:14px; }
  .fld { display:flex; flex-direction:column; gap:5px; font-size:12px; color:var(--dim); }
  .fld.wide { grid-column:1 / -1; }

  .empty { text-align:center; padding:34px 16px; color:var(--dim); border:1px dashed var(--line); border-radius:var(--r); }
  .empty button { margin-top:12px; }

  .seen { display:flex; flex-direction:column; gap:8px; }
  .srow { display:flex; align-items:center; gap:14px; background:var(--card); border:1px solid var(--line);
          border-radius:12px; padding:10px 14px; flex-wrap:wrap; }
  .srow .nm { font-weight:600; }
  .srow .ad { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; color:var(--acc); }
  .srow .fm { color:var(--dim); font-size:13px; }
  .srow button { margin-left:auto; }
  .tag { font-size:12px; color:var(--ok); margin-left:auto; }

  .savebar { position:fixed; left:50%; bottom:20px; transform:translate(-50%,140%); transition:.25s;
             display:flex; align-items:center; gap:12px; background:#1b2430ee; backdrop-filter:blur(8px);
             border:1px solid var(--line); border-radius:14px; padding:10px 12px 10px 18px; box-shadow:0 12px 40px #000a; }
  .savebar.show { transform:translate(-50%,0); }
  .savebar span { color:var(--dim); font-size:14px; white-space:nowrap; }

  .toast { position:fixed; top:18px; right:18px; padding:10px 16px; border-radius:10px; background:var(--card2);
           border:1px solid var(--line); opacity:0; transform:translateY(-8px); transition:.25s; pointer-events:none; max-width:420px; }
  .toast.show { opacity:1; transform:none; }
  .toast.err { border-color:var(--err); color:#ffb4ae; }
  .toast.ok { border-color:var(--ok); }

  footer { margin-top:36px; color:var(--dim); font-size:13px; }
  footer code { background:var(--card); padding:2px 6px; border-radius:6px; }
  @media (max-width:520px) { .grid { grid-template-columns:1fr; } .fields { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#061423" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h2.5l2.5-6 3.5 12 3.5-9 2 3H21"/></svg></div>
    <div>
      <h1>Récepteur VBAN</h1>
      <div class="sub" id="host">—</div>
    </div>
    <div class="pills">
      <div class="pill"><span class="dot" id="gdot"></span><b id="nlive">0</b> en réception</div>
      <div class="pill"><b id="nrx">0</b> récepteur(s)</div>
    </div>
  </header>

  <div class="section">
    <h2>Récepteurs</h2>
    <button onclick="addRx({})">+ Ajouter un récepteur</button>
  </div>
  <div class="grid" id="rx"></div>

  <div class="section"><h2>Flux détectés sur le réseau</h2></div>
  <div class="seen" id="seen"></div>

  <footer>
    Dans Voicemeeter : <b>Menu → VBAN</b>, ajoute un flux sortant vers l'IP de ce PC, port <code>6980</code>, puis active-le.
    IP émettrice vide = toutes les machines · Nom du flux vide = le premier flux reçu.
  </footer>
</div>

<div class="savebar" id="savebar">
  <span>Modifications non appliquées</span>
  <button class="ghost" onclick="load()">Annuler</button>
  <button class="primary" id="applyBtn" onclick="apply()">Appliquer</button>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let cfg = {receivers: []}, devices = [], dirty = false, seenKey = '', lastStatus = null;

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  e.append(...kids.filter(k => k != null));
  return e;
}

function toast(text, kind) {
  const t = $('toast');
  t.textContent = text; t.className = 'toast show ' + (kind || '');
  clearTimeout(toast.tm); toast.tm = setTimeout(() => t.className = 'toast', 3500);
}

function touch() { dirty = true; $('savebar').classList.add('show'); }

function card(r, i) {
  const bind = (inp, key, num) => {
    inp.oninput = () => { r[key] = num ? (+inp.value || 0) : inp.value; touch(); };
    return inp;
  };
  const fld = (label, input, wide) =>
    h('label', {className: 'fld' + (wide ? ' wide' : '')}, h('span', {textContent: label}), input);

  const sel = h('select', {onchange: () => { r.device = sel.value; touch(); }});
  const opts = [['', 'Sortie par défaut du système'], ...devices.map(d => [d, d])];
  if (r.device && !devices.includes(r.device)) opts.push([r.device, r.device + ' (absente)']);
  opts.forEach(([v, t]) => sel.append(h('option', {value: v, textContent: t, selected: v === r.device})));

  return h('div', {className: 'card' + (r.enabled ? '' : ' off'), id: 'card' + i},
    h('div', {className: 'head'},
      h('label', {className: 'switch', title: 'Activer / désactiver'},
        h('input', {type: 'checkbox', checked: r.enabled,
                    onchange: e => { r.enabled = e.target.checked; touch(); render(); }}),
        h('span')),
      bind(h('input', {className: 'title', value: r.name, maxLength: 32, placeholder: 'Nom du récepteur'}), 'name'),
      h('button', {className: 'icon', title: 'Supprimer', textContent: '✕',
                   onclick: () => { cfg.receivers.splice(i, 1); touch(); render(); }})),
    h('div', {className: 'state', id: 'st' + i},
      h('span', {className: 'dot'}),
      h('span', {className: 'txt', textContent: dirty ? 'Non appliqué' : '…'})),
    h('div', {className: 'meter', title: 'Niveau du signal'}, h('div', {className: 'cover', id: 'lv' + i})),
    h('div', {className: 'fields'},
      fld('Port UDP', bind(h('input', {value: r.port, inputMode: 'numeric', placeholder: '6980'}), 'port', true)),
      fld('Latence (ms)', bind(h('input', {value: r.latency, inputMode: 'numeric', placeholder: '50'}), 'latency', true)),
      fld('IP émettrice', bind(h('input', {value: r.src, placeholder: 'Toutes'}), 'src')),
      fld('Nom du flux', bind(h('input', {value: r.stream, maxLength: 16, placeholder: 'Premier reçu'}), 'stream')),
      fld('Sortie audio', sel, true)));
}

function render() {
  const box = $('rx'); box.replaceChildren();
  $('nrx').textContent = cfg.receivers.length;
  if (!cfg.receivers.length) {
    box.append(h('div', {className: 'empty', style: 'grid-column:1/-1'},
      h('div', {textContent: 'Aucun récepteur configuré.'}),
      h('button', {className: 'primary', textContent: '+ Ajouter un récepteur', onclick: () => addRx({})})));
  }
  cfg.receivers.forEach((r, i) => box.append(card(r, i)));
  if (lastStatus) paint(lastStatus);
}

function addRx(o) {
  cfg.receivers.push(Object.assign(
    {name: 'VBAN-' + (cfg.receivers.length + 1), enabled: true, port: 6980,
     src: '', stream: '', device: '', latency: 50}, o));
  touch(); render();
}

async function load() {
  const r = await (await fetch('/api/config')).json();
  cfg = r.cfg; devices = r.devices; dirty = false;
  $('host').textContent = r.host + ' · lecteur ' + r.backend + ' · interface port ' + location.port;
  $('savebar').classList.remove('show');
  render(); status();
}

async function apply() {
  const btn = $('applyBtn'); btn.disabled = true; btn.textContent = 'Application…';
  try {
    const res = await fetch('/api/config', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cfg)});
    const r = await res.json();
    if (r.ok) { toast('Réglages appliqués', 'ok'); await load(); }
    else toast('Erreur : ' + (r.error || 'inconnue'), 'err');
  } catch (e) { toast('Erreur : ' + e, 'err'); }
  btn.disabled = false; btn.textContent = 'Appliquer';
}

function paint(st) {
  let live = 0;
  st.receivers.forEach((s, i) => {
    if (s.state === 'ok') live++;
    const box = $('st' + i); if (!box || dirty) return;
    box.querySelector('.dot').className = 'dot ' + s.state;
    box.querySelector('.txt').textContent = s.text;
    $('card' + i).classList.toggle('live', s.state === 'ok');
    const lv = s.level || 0;
    const pct = lv > 0 ? Math.max(0, Math.min(100, (20 * Math.log10(lv) + 60) / 60 * 100)) : 0;
    $('lv' + i).style.width = (100 - pct) + '%';
  });
  $('nlive').textContent = live;
  $('gdot').className = 'dot' + (live ? ' ok' : '');

  const key = JSON.stringify(st.seen) + JSON.stringify(cfg.receivers);
  if (key === seenKey) return;
  seenKey = key;
  const box = $('seen'); box.replaceChildren();
  if (!st.seen.length) {
    box.append(h('div', {className: 'empty',
      textContent: 'Aucun flux VBAN reçu pour le moment sur les ports écoutés.'}));
    return;
  }
  st.seen.forEach(s => {
    const used = cfg.receivers.some(r => r.port === s.port && r.stream === s.stream && (!r.src || r.src === s.ip));
    box.append(h('div', {className: 'srow'},
      h('span', {className: 'dot ok'}),
      h('span', {className: 'nm', textContent: s.stream || '(sans nom)'}),
      h('span', {className: 'ad', textContent: s.ip + ':' + s.port}),
      h('span', {className: 'fm', textContent: s.fmt}),
      used ? h('span', {className: 'tag', textContent: '✓ Configuré'})
           : h('button', {textContent: 'Recevoir ce flux', onclick: () => addRx({
               name: s.stream.replace(/[^A-Za-z0-9 _.\-]/g, '_').slice(0, 32) || 'VBAN',
               port: s.port, src: s.ip, stream: s.stream})})));
  });
}

async function status() {
  try {
    lastStatus = await (await fetch('/api/status')).json();
    paint(lastStatus);
  } catch (e) {}
}

window.onbeforeunload = () => dirty ? true : undefined;
load();
setInterval(status, 400);
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
            self._send(200, json.dumps({
                "cfg": self.manager.cfg, "devices": list_devices(),
                "host": socket.gethostname(), "backend": BACKEND,
            }))
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
