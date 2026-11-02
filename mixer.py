#!/usr/bin/env python3
"""Mini mixeur web pour PipeWire / PulseAudio (façon Voicemeeter).

Lancer :  python3 mixer.py
Ouvrir :  http://IP_DU_SERVEUR:8095

Pilote pactl (fonctionne avec pipewire-pulse). Aucune dépendance Python.
Les flux VBAN reçus apparaissent comme des applications / sources normales.
"""
import glob
import ipaddress
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8095


def pactl(*args):
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def plist(kind):
    try:
        return json.loads(pactl("-f", "json", "list", kind))
    except Exception:
        return []


def volume_percent(obj):
    vols = obj.get("volume") or {}
    first = next(iter(vols.values()), None)
    if not first:
        return 0
    return int(first["value_percent"].rstrip("%"))


def get_state():
    sinks = [
        {
            "id": s["index"],
            "name": s.get("description") or s["name"],
            "defname": s["name"],
            "vol": volume_percent(s),
            "mute": s.get("mute", False),
        }
        for s in plist("sinks")
    ]
    sources = [
        {
            "id": s["index"],
            "name": s.get("description") or s["name"],
            "vol": volume_percent(s),
            "mute": s.get("mute", False),
        }
        for s in plist("sources")
        if not s["name"].endswith(".monitor")
    ]
    inputs = []
    for i in plist("sink-inputs"):
        props = i.get("properties", {})
        inputs.append(
            {
                "id": i["index"],
                "name": props.get("application.name")
                or props.get("media.name")
                or "Flux %d" % i["index"],
                "vol": volume_percent(i),
                "mute": i.get("mute", False),
                "sink": i.get("sink"),
            }
        )
    return {
        "sinks": sinks,
        "sources": sources,
        "inputs": inputs,
        "default_sink": pactl("get-default-sink"),
    }


def apply(data):
    kind = data.get("kind")
    cmd = {
        "sink": ("set-sink-volume", "set-sink-mute"),
        "input": ("set-sink-input-volume", "set-sink-input-mute"),
        "source": ("set-source-volume", "set-source-mute"),
    }.get(kind)
    if not cmd:
        return
    ident = str(int(data["id"]))
    if "volume" in data:
        v = max(0, min(150, int(data["volume"])))
        pactl(cmd[0], ident, "%d%%" % v)
    if "mute" in data:
        pactl(cmd[1], ident, "1" if data["mute"] else "0")


def move_input(data):
    pactl("move-sink-input", str(int(data["id"])), str(int(data["sink"])))


def set_default(data):
    pactl("set-default-sink", str(int(data["sink"])))


# ---------------------------------------------------------------- VBAN
BASE = os.path.dirname(os.path.abspath(__file__))
VBAN_JSON = os.path.join(BASE, "vban.json")
PW_CONF = os.path.expanduser("~/.config/pipewire/pipewire.conf.d/vban-mixer.conf")
LEGACY_GLOB = "/etc/pipewire/pipewire.conf.d/vban-*.conf"
NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,32}$")

DEFAULT_VBAN = {
    "recv": [{"name": "VBAN-PC", "ip": "0.0.0.0", "port": 6980}],
    "send": [],
}


def load_vban():
    try:
        with open(VBAN_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(DEFAULT_VBAN))


def clean_vban(cfg):
    out = {"recv": [], "send": []}
    for kind in ("recv", "send"):
        for e in cfg.get(kind, []):
            name = str(e.get("name", "")).strip()
            ip = str(e.get("ip", "")).strip()
            port = int(e.get("port", 6980))
            if not NAME_RE.match(name):
                raise ValueError(
                    "Nom invalide : %r (lettres, chiffres, espace, _ . - ; 32 max)" % name
                )
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                raise ValueError("Adresse IP invalide : %r" % ip)
            if not 1 <= port <= 65535:
                raise ValueError("Port invalide : %d" % port)
            out[kind].append({"name": name, "ip": ip, "port": port})
    seen = set()
    for e in out["recv"]:
        key = (e["ip"], e["port"])
        if key in seen:
            raise ValueError("Deux réceptions sur %s:%d" % key)
        seen.add(key)
    return out


def vban_conf(cfg):
    lines = ["context.modules = ["]
    for i, e in enumerate(cfg["recv"]):
        lines += [
            "  { name = libpipewire-module-vban-recv",
            "    args = {",
            '      source.ip = "%s"' % e["ip"],
            "      source.port = %d" % e["port"],
            "      sess.latency.msec = 50",
            "      audio.position = [ FL FR ]",
            "      stream.props = {",
            '        node.name = "vban-recv-%d-%d"' % (i, e["port"]),
            '        node.description = "%s"' % e["name"],
            "      }",
            "    }",
            "  }",
        ]
    for e in cfg["send"]:
        lines += [
            "  { name = libpipewire-module-vban-send",
            "    args = {",
            '      destination.ip = "%s"' % e["ip"],
            "      destination.port = %d" % e["port"],
            '      sess.name = "%s"' % e["name"],
            "      audio.rate = 48000",
            "      audio.channels = 2",
            "    }",
            "  }",
        ]
    lines.append("]")
    return "\n".join(lines) + "\n"


def listening_ports():
    try:
        out = subprocess.run(
            ["ss", "-uln"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    ports = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            ports.add(parts[3].rsplit(":", 1)[-1])
    return sorted(ports)


def restart_pipewire():
    uid = os.getuid()
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % uid)
    r = subprocess.run(
        ["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0, r.stderr.strip()


def save_vban(data):
    cfg = clean_vban(data)
    os.makedirs(os.path.dirname(PW_CONF), exist_ok=True)
    with open(VBAN_JSON, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    with open(PW_CONF, "w", encoding="utf-8") as f:
        f.write(vban_conf(cfg))
    ok, err = restart_pipewire()
    time.sleep(2)
    return ok, err


def vban_state():
    return {
        "cfg": load_vban(),
        "listening": listening_ports(),
        "legacy": glob.glob(LEGACY_GLOB),
    }


PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mixeur</title>
<style>
  :root { --bg:#14161a; --card:#1f232a; --fg:#e8eaed; --dim:#8b93a1; --acc:#4da3ff; --red:#e5484d; }
  * { box-sizing:border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px system-ui,sans-serif; }
  h1 { font-size:18px; margin:0 0 12px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin:22px 0 8px; }
  .row { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }
  .strip { background:var(--card); border-radius:10px; padding:12px; }
  .strip.def { outline:2px solid var(--acc); }
  .name { font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:8px; }
  .ctl { display:flex; align-items:center; gap:8px; }
  input[type=range] { flex:1; accent-color:var(--acc); }
  .pct { width:44px; text-align:right; color:var(--dim); font-variant-numeric:tabular-nums; }
  button { background:#2b3039; color:var(--fg); border:0; border-radius:6px; padding:6px 10px; cursor:pointer; }
  button.on { background:var(--red); }
  select { width:100%; margin-top:8px; background:#2b3039; color:var(--fg); border:0; border-radius:6px; padding:6px; }
  .star { margin-top:8px; width:100%; }
  .empty { color:var(--dim); }
  #vban { max-width:640px; }
  .vrow { display:flex; gap:6px; margin-bottom:6px; flex-wrap:wrap; align-items:center; }
  .vrow input { background:#2b3039; color:var(--fg); border:0; border-radius:6px; padding:6px; min-width:0; }
  .dot { width:10px; height:10px; border-radius:50%; background:#555; }
  .dot.ok { background:#3fb950; }
  .msg { color:var(--dim); margin-left:8px; }
  .warn { color:#f0b429; margin-top:8px; }
</style>
</head>
<body>
<h1>Mixeur audio</h1>
<h2>Applications / flux (dont VBAN)</h2><div class="row" id="inputs"></div>
<h2>Sorties</h2><div class="row" id="sinks"></div>
<h2>Entrées (micros, sources, flux VBAN reçus)</h2><div class="row" id="sources"></div>

<h2>VBAN</h2>
<div class="strip" id="vban">
  <div class="name">Réception : cet Ubuntu écoute</div>
  <div id="vrecv"></div>
  <button onclick="addV('recv')">+ Réception</button>
  <div class="name" style="margin-top:16px">Envoi : cet Ubuntu envoie vers</div>
  <div id="vsend"></div>
  <button onclick="addV('send')">+ Envoi</button>
  <div style="margin-top:16px"><button id="vapply" onclick="applyVban()">Appliquer</button><span class="msg" id="vmsg"></span></div>
  <div class="warn" id="vwarn"></div>
</div>

<script>
let dragging = false;
let lastSink = {};

async function post(url, body) {
  await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
}

function strip(kind, o, extra) {
  const el = document.createElement('div');
  el.className = 'strip';
  const name = document.createElement('div');
  name.className = 'name'; name.textContent = o.name; name.title = o.name;
  const ctl = document.createElement('div'); ctl.className = 'ctl';
  const mute = document.createElement('button');
  mute.textContent = 'Mute'; if (o.mute) mute.className = 'on';
  mute.onclick = async () => { await post('/api/set', {kind, id:o.id, mute:!o.mute}); refresh(true); };
  const range = document.createElement('input');
  range.type = 'range'; range.min = 0; range.max = 150; range.value = o.vol;
  const pct = document.createElement('span'); pct.className = 'pct'; pct.textContent = o.vol + '%';
  range.oninput = () => { pct.textContent = range.value + '%'; post('/api/set', {kind, id:o.id, volume:+range.value}); };
  range.onpointerdown = () => dragging = true;
  range.onpointerup = () => { dragging = false; };
  ctl.append(mute, range, pct);
  el.append(name, ctl);
  if (extra) extra(el);
  return el;
}

function fill(id, list, build) {
  const box = document.getElementById(id);
  box.replaceChildren();
  if (!list.length) { const e = document.createElement('div'); e.className='empty'; e.textContent='Rien pour le moment'; box.append(e); return; }
  list.forEach(o => box.append(build(o)));
}

async function refresh(force) {
  if (dragging && !force) return;
  const st = await (await fetch('/api/state')).json();
  fill('inputs', st.inputs, o => strip('input', o, el => {
    const sel = document.createElement('select');
    st.sinks.forEach(s => {
      const op = document.createElement('option');
      op.value = s.id; op.textContent = '→ ' + s.name; if (s.id === o.sink) op.selected = true;
      sel.append(op);
    });
    sel.onchange = async () => { await post('/api/move', {id:o.id, sink:+sel.value}); refresh(true); };
    el.append(sel);
  }));
  fill('sinks', st.sinks, o => {
    const el = strip('sink', o, el => {
      const b = document.createElement('button');
      b.className = 'star';
      b.textContent = (o.defname === st.default_sink) ? 'Sortie par défaut' : 'Définir par défaut';
      b.onclick = async () => { await post('/api/default', {sink:o.id}); refresh(true); };
      el.append(b);
    });
    return el;
  });
  fill('sources', st.sources, o => strip('source', o));
}

let vban = {recv: [], send: []};
let vlisten = [];

function vrow(kind, i) {
  const e = vban[kind][i];
  const row = document.createElement('div'); row.className = 'vrow';
  const mk = (key, ph, w) => {
    const inp = document.createElement('input');
    inp.value = e[key]; inp.placeholder = ph; inp.style.width = w;
    inp.oninput = () => { e[key] = (key === 'port') ? (+inp.value || 0) : inp.value; };
    return inp;
  };
  row.append(
    mk('name', 'Nom', '9em'),
    mk('ip', kind === 'recv' ? "IP d'écoute" : 'IP destination', '10em'),
    mk('port', 'Port', '5em')
  );
  if (kind === 'recv') {
    const dot = document.createElement('span');
    const on = vlisten.includes(String(e.port));
    dot.className = 'dot' + (on ? ' ok' : '');
    dot.title = on ? 'Port ouvert' : 'Port non ouvert (pas encore appliqué ?)';
    row.append(dot);
  }
  const del = document.createElement('button'); del.textContent = '✕';
  del.onclick = () => { vban[kind].splice(i, 1); renderVban(); };
  row.append(del);
  return row;
}

function renderVban() {
  ['recv', 'send'].forEach(k => {
    const box = document.getElementById('v' + k);
    box.replaceChildren();
    vban[k].forEach((_, i) => box.append(vrow(k, i)));
  });
}

function addV(kind) {
  vban[kind].push(kind === 'recv'
    ? {name: 'VBAN-' + (vban.recv.length + 1), ip: '0.0.0.0', port: 6980 + vban.recv.length}
    : {name: 'Ubuntu', ip: '', port: 6980});
  renderVban();
}

async function loadVban() {
  const r = await (await fetch('/api/vban')).json();
  vban = r.cfg; vlisten = r.listening.map(String);
  document.getElementById('vwarn').textContent = r.legacy.length
    ? 'À supprimer (doublon avec cette page) : ' + r.legacy.join(', ') : '';
  renderVban();
}

async function applyVban() {
  const msg = document.getElementById('vmsg');
  msg.textContent = 'Application… (coupure audio ~3 s)';
  try {
    const res = await fetch('/api/vban', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(vban)});
    const r = await res.json();
    msg.textContent = r.ok ? 'Appliqué.' : 'Erreur : ' + (r.error || 'inconnue');
  } catch (e) { msg.textContent = 'Erreur : ' + e; }
  setTimeout(() => { loadVban(); refresh(true); }, 500);
}

loadVban();
refresh(true);
setInterval(() => refresh(false), 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
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
        elif self.path == "/api/state":
            self._send(200, json.dumps(get_state()))
        elif self.path == "/api/vban":
            self._send(200, json.dumps(vban_state()))
        else:
            self._send(404, "{}")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/set":
                apply(data)
            elif self.path == "/api/move":
                move_input(data)
            elif self.path == "/api/default":
                set_default(data)
            elif self.path == "/api/vban":
                ok, err = save_vban(data)
                return self._send(200, json.dumps({"ok": ok, "error": err}))
            else:
                return self._send(404, "{}")
            self._send(200, '{"ok":true}')
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))


if __name__ == "__main__":
    print("Mixeur sur http://%s:%d" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()