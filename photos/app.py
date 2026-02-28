#!/usr/bin/env python3
"""Photo Import Control Panel -- lightweight Flask web app for the Fujifilm → Immich pipeline."""

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
HOMELAB_DIR = BASE_DIR.parent
IMPORT_DIR = BASE_DIR / "import"
STATUS_FILE = BASE_DIR / "status.json"
LOG_FILE = BASE_DIR / "import.log"
IMPORT_SCRIPT = BASE_DIR / "import-photos.sh"
ENV_FILE = HOMELAB_DIR / ".env"

_import_proc = None


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def get_status():
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"status": "idle", "message": "No imports yet", "downloaded": 0, "total": 0, "percent": 0, "timestamp": ""}


def camera_connected():
    try:
        out = subprocess.check_output(["lsusb"], text=True, timeout=5)
        for line in out.splitlines():
            if "04cb:" in line:
                model = line.split("04cb:")[-1].strip()
                return {"connected": True, "model": model}
    except (subprocess.SubprocessError, OSError):
        pass
    return {"connected": False, "model": None}


def is_import_running():
    global _import_proc
    if _import_proc is not None:
        if _import_proc.poll() is None:
            return True
        _import_proc = None
    try:
        out = subprocess.check_output(["pgrep", "-f", "import-photos.sh"], text=True, timeout=3)
        return bool(out.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def get_import_folders():
    folders = []
    if not IMPORT_DIR.exists():
        return folders
    for d in sorted(IMPORT_DIR.iterdir(), reverse=True):
        if d.is_dir():
            files = list(d.iterdir())
            file_count = len([f for f in files if f.is_file()])
            size_bytes = sum(f.stat().st_size for f in files if f.is_file())
            folders.append({
                "name": d.name,
                "files": file_count,
                "size_mb": round(size_bytes / (1024 * 1024), 1),
            })
    return folders


# --- API Endpoints ---

@app.route("/api/status")
def api_status():
    status = get_status()
    status["importing"] = is_import_running()
    return jsonify(status)


@app.route("/api/camera")
def api_camera():
    return jsonify(camera_connected())


@app.route("/api/log")
def api_log():
    lines = int(request.args.get("lines", 80))
    if not LOG_FILE.exists():
        return jsonify({"lines": []})
    all_lines = LOG_FILE.read_text().splitlines()
    return jsonify({"lines": all_lines[-lines:]})


@app.route("/api/files")
def api_files():
    return jsonify({"folders": get_import_folders()})


@app.route("/api/import/start", methods=["POST"])
def api_import_start():
    global _import_proc
    if is_import_running():
        return jsonify({"ok": False, "error": "Import already running"}), 409

    env = {**os.environ, **load_env()}
    _import_proc = subprocess.Popen(
        ["bash", str(IMPORT_SCRIPT)],
        cwd=str(HOMELAB_DIR),
        env=env,
        start_new_session=True,
        stdout=open(LOG_FILE, "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"ok": True, "pid": _import_proc.pid})


@app.route("/api/import/stop", methods=["POST"])
def api_import_stop():
    global _import_proc
    killed = False

    if _import_proc is not None and _import_proc.poll() is None:
        os.killpg(os.getpgid(_import_proc.pid), signal.SIGTERM)
        _import_proc = None
        killed = True

    for name in ["import-photos.sh", "gphoto2"]:
        try:
            subprocess.run(["pkill", "-f", name], timeout=3, check=False)
            killed = True
        except subprocess.SubprocessError:
            pass

    try:
        subprocess.run(["pkill", "-f", "immich upload"], timeout=3, check=False)
    except subprocess.SubprocessError:
        pass

    if STATUS_FILE.exists():
        status = get_status()
        status["status"] = "idle"
        status["message"] = "Import stopped by user"
        STATUS_FILE.write_text(json.dumps(status, indent=2))

    return jsonify({"ok": True, "killed": killed})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if not IMPORT_DIR.exists() or not any(IMPORT_DIR.iterdir()):
        return jsonify({"ok": False, "error": "No files to sync"}), 404

    env_vars = load_env()
    api_key = env_vars.get("IMMICH_API_KEY", "")
    immich_url = env_vars.get("IMMICH_URL", "http://localhost:2283")
    album = env_vars.get("IMMICH_ALBUM", "Fuji-XT30")

    if not api_key:
        return jsonify({"ok": False, "error": "IMMICH_API_KEY not configured"}), 400

    env = {**os.environ, "IMMICH_INSTANCE_URL": immich_url, "IMMICH_API_KEY": api_key}

    log_fh = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        ["immich", "upload", "--recursive", "--no-progress", "--album-name", album, str(IMPORT_DIR)],
        env=env,
        start_new_session=True,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    status = get_status()
    status["status"] = "running"
    status["message"] = f"Syncing to Immich ({album})..."
    STATUS_FILE.write_text(json.dumps(status, indent=2))

    def _wait_for_sync():
        proc.wait()
        log_fh.close()
        s = get_status()
        s["status"] = "idle"
        s["message"] = f"Sync to Immich complete ({album})"
        STATUS_FILE.write_text(json.dumps(s, indent=2))

    threading.Thread(target=_wait_for_sync, daemon=True).start()

    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/camera/clear", methods=["POST"])
def api_camera_clear():
    if is_import_running():
        return jsonify({"ok": False, "error": "Cannot clear camera while import is running"}), 409

    cam = camera_connected()
    if not cam["connected"]:
        return jsonify({"ok": False, "error": "No camera connected"}), 404

    try:
        result = subprocess.run(
            ["gphoto2", "--delete-all-files", "--recurse"],
            capture_output=True, text=True, timeout=300,
        )
        msg = result.stdout.strip() or result.stderr.strip()
        with open(LOG_FILE, "a") as f:
            f.write(f"[camera-clear] {msg}\n")

        if result.returncode != 0:
            return jsonify({"ok": False, "error": msg or "gphoto2 delete failed"}), 500

        return jsonify({"ok": True, "message": msg or "All files deleted from camera"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Camera clear timed out"}), 504
    except (subprocess.SubprocessError, OSError) as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    if is_import_running():
        return jsonify({"ok": False, "error": "Cannot cleanup while import is running"}), 409

    if not IMPORT_DIR.exists():
        return jsonify({"ok": True, "deleted": 0})

    deleted = 0
    for d in list(IMPORT_DIR.iterdir()):
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
                deleted += 1
            d.rmdir()

    if STATUS_FILE.exists():
        status = get_status()
        status["message"] = f"Cleaned up {deleted} files"
        STATUS_FILE.write_text(json.dumps(status, indent=2))

    return jsonify({"ok": True, "deleted": deleted})


# --- Web UI ---

@app.route("/")
def index():
    return HTML_PAGE


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Import</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6; --green: #22c55e; --red: #ef4444; --orange: #f97316; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 1.5rem; }
  h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 1.5rem; display: flex; align-items: center; gap: .5rem; }
  .camera-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .camera-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .camera-dot.off { background: var(--red); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
  @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1.25rem; }
  .card h2 { font-size: .85rem; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); margin-bottom: .75rem; }
  .status-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: .5rem; font-size: .9rem; }
  .status-val { font-weight: 600; }
  .progress-wrap { background: var(--bg); border-radius: .5rem; height: 1.5rem; overflow: hidden; margin: .75rem 0; position: relative; }
  .progress-bar { height: 100%; background: var(--accent); border-radius: .5rem; transition: width .4s ease; min-width: 0; }
  .progress-text { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: .75rem; font-weight: 600; }
  .actions { display: flex; flex-wrap: wrap; gap: .5rem; }
  .btn { padding: .55rem 1.1rem; border: none; border-radius: .5rem; font-size: .85rem; font-weight: 600; cursor: pointer; transition: opacity .15s; }
  .btn:hover { opacity: .85; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-blue { background: var(--accent); color: #fff; }
  .btn-green { background: var(--green); color: #fff; }
  .btn-red { background: var(--red); color: #fff; }
  .btn-orange { background: var(--orange); color: #fff; }
  .log-box { background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; padding: .75rem; height: 280px; overflow-y: auto; font-family: 'SF Mono', 'Fira Code', monospace; font-size: .72rem; line-height: 1.5; white-space: pre-wrap; word-break: break-all; color: var(--dim); }
  .files-list { font-size: .85rem; }
  .files-list .folder { display: flex; justify-content: space-between; padding: .35rem 0; border-bottom: 1px solid var(--border); }
  .files-list .folder:last-child { border-bottom: none; }
  .msg { font-size: .85rem; color: var(--dim); margin-top: .5rem; word-break: break-word; }
  .badge { display: inline-block; padding: .15rem .5rem; border-radius: .25rem; font-size: .75rem; font-weight: 600; }
  .badge-idle { background: var(--border); color: var(--dim); }
  .badge-running { background: #1d4ed8; color: #dbeafe; }
  .badge-error { background: #991b1b; color: #fecaca; }
</style>
</head>
<body>

<h1>
  <span class="camera-dot off" id="cameraDot"></span>
  Photo Import
  <span style="font-weight:400; font-size:.85rem; color:var(--dim)" id="cameraModel">No camera</span>
</h1>

<div class="grid">
  <div class="card">
    <h2>Status</h2>
    <div class="status-row">
      <span>State</span>
      <span id="badge" class="badge badge-idle">idle</span>
    </div>
    <div class="status-row">
      <span>Files</span>
      <span class="status-val" id="fileCount">0 / 0</span>
    </div>
    <div class="progress-wrap">
      <div class="progress-bar" id="progressBar" style="width:0%"></div>
      <div class="progress-text" id="progressText">0%</div>
    </div>
    <div class="msg" id="statusMsg">--</div>
  </div>

  <div class="card">
    <h2>Actions</h2>
    <div class="actions">
      <button class="btn btn-blue" id="btnStart" onclick="doAction('/api/import/start')">Start Import</button>
      <button class="btn btn-red" id="btnStop" onclick="doAction('/api/import/stop')">Stop Import</button>
      <button class="btn btn-green" id="btnSync" onclick="doAction('/api/sync')">Sync to Immich</button>
      <button class="btn btn-orange" id="btnClean" onclick="doCleanup()">Cleanup Local</button>
      <button class="btn btn-red" id="btnClearCam" onclick="doClearCamera()">Clear Camera</button>
    </div>
    <div style="margin-top:1rem">
      <h2>Local Files</h2>
      <div class="files-list" id="filesList"><span style="color:var(--dim)">Loading...</span></div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Log</h2>
  <div class="log-box" id="logBox">Loading...</div>
</div>

<script>
const $ = id => document.getElementById(id);

async function fetchJSON(url, opts) {
  try { const r = await fetch(url, opts); return await r.json(); }
  catch { return null; }
}

async function refresh() {
  const [status, camera, files, log] = await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/camera'),
    fetchJSON('/api/files'),
    fetchJSON('/api/log?lines=120'),
  ]);

  if (camera) {
    $('cameraDot').className = 'camera-dot ' + (camera.connected ? 'on' : 'off');
    $('cameraModel').textContent = camera.connected ? camera.model || 'Connected' : 'No camera';
  }

  if (status) {
    const s = status.status || 'idle';
    const badge = $('badge');
    badge.textContent = s;
    badge.className = 'badge badge-' + (s === 'running' ? 'running' : s === 'error' ? 'error' : 'idle');
    $('fileCount').textContent = (status.downloaded || 0) + ' / ' + (status.total || 0);
    const pct = status.percent || 0;
    $('progressBar').style.width = pct + '%';
    $('progressText').textContent = pct + '%';
    $('statusMsg').textContent = status.message || '--';
    $('btnStart').disabled = status.importing;
    $('btnStop').disabled = !status.importing;
  }

  if (files && files.folders) {
    if (files.folders.length === 0) {
      $('filesList').innerHTML = '<span style="color:var(--dim)">No imported files</span>';
    } else {
      $('filesList').innerHTML = files.folders.map(f =>
        '<div class="folder"><span>' + f.name + '</span><span>' + f.files + ' files (' + f.size_mb + ' MB)</span></div>'
      ).join('');
    }
  }

  if (log && log.lines) {
    const box = $('logBox');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
    box.textContent = log.lines.join('\\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
}

async function doAction(url) {
  await fetchJSON(url, { method: 'POST' });
  setTimeout(refresh, 500);
}

async function doCleanup() {
  if (!confirm('Delete all local imported files? (Photos already in Immich are safe)')) return;
  const r = await fetchJSON('/api/cleanup', { method: 'POST' });
  if (r && r.ok) alert('Deleted ' + r.deleted + ' files');
  else if (r && r.error) alert('Error: ' + r.error);
  setTimeout(refresh, 500);
}

async function doClearCamera() {
  if (!confirm('DELETE ALL PHOTOS FROM THE CAMERA?\\n\\nMake sure you have already synced to Immich!')) return;
  if (!confirm('Are you sure? This CANNOT be undone.')) return;
  $('btnClearCam').disabled = true;
  $('btnClearCam').textContent = 'Clearing...';
  const r = await fetchJSON('/api/camera/clear', { method: 'POST' });
  $('btnClearCam').disabled = false;
  $('btnClearCam').textContent = 'Clear Camera';
  if (r && r.ok) alert(r.message || 'Camera cleared');
  else if (r && r.error) alert('Error: ' + r.error);
  setTimeout(refresh, 500);
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
