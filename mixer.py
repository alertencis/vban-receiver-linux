#!/usr/bin/env python3
"""Mini mixeur web pour PipeWire / PulseAudio (façon Voicemeeter).

Lancer :  python3 mixer.py
Ouvrir :  http://IP_DU_SERVEUR:8095

Pilote pactl (fonctionne avec pipewire-pulse). Aucune dépendance Python.
Les flux VBAN reçus apparaissent comme des applications / sources normales.
"""
import json
import subprocess
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
</style>
</head>
<body>
<h1>Mixeur audio</h1>
<h2>Applications / flux (dont VBAN)</h2><div class="row" id="inputs"></div>
<h2>Sorties</h2><div class="row" id="sinks"></div>
<h2>Entrées (micros, sources)</h2><div class="row" id="sources"></div>

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
            else:
                return self._send(404, "{}")
            self._send(200, '{"ok":true}')
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))


if __name__ == "__main__":
    print("Mixeur sur http://%s:%d" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()