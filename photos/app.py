#!/usr/bin/env python3
"""Photo Import Control Panel -- lightweight Flask web app for the Fujifilm → Immich pipeline."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import urllib.error
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
RECIPES_FILE = BASE_DIR / "recipes.json"

RECIPE_FIELDS = [
    "FilmMode", "DynamicRangeSetting", "HighlightTone", "ShadowTone",
    "Saturation", "Sharpness", "NoiseReduction", "GrainEffectRoughness",
    "GrainEffectSize", "ColorChromeEffect", "ColorChromeFXBlue", "Clarity",
    "WhiteBalance", "WhiteBalanceFineTune",
]

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


def write_status(status, message, **extra):
    data = get_status()
    data["status"] = status
    data["message"] = message
    data.update(extra)
    STATUS_FILE.write_text(json.dumps(data, indent=2))


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


def cleanup_import_dir():
    """Remove all files and subdirectories from the import directory."""
    deleted = 0
    if not IMPORT_DIR.exists():
        return deleted
    for d in list(IMPORT_DIR.iterdir()):
        if d.is_dir():
            for f in d.iterdir():
                f.unlink()
                deleted += 1
            d.rmdir()
    return deleted


def immich_api(path):
    """Call the Immich REST API. Returns parsed JSON or None on error."""
    env_vars = load_env()
    api_key = env_vars.get("IMMICH_API_KEY", "")
    base_url = env_vars.get("IMMICH_URL", "http://localhost:2283")
    if not api_key:
        return None
    url = f"{base_url}/api/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"x-api-key": api_key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def get_immich_album_files():
    """Get the set of original filenames in the configured Immich album."""
    env_vars = load_env()
    album_name = env_vars.get("IMMICH_ALBUM", "Fuji-XT30")
    albums = immich_api("albums")
    if not albums:
        return None, album_name
    target = None
    for a in albums:
        if a.get("albumName") == album_name:
            target = a
            break
    if not target:
        return set(), album_name
    album_detail = immich_api(f"albums/{target['id']}")
    if not album_detail:
        return None, album_name
    return {a["originalFileName"] for a in album_detail.get("assets", [])}, album_name


def get_camera_files(with_sizes=False):
    """Get the list of filenames on the camera via gphoto2.

    If *with_sizes* is True, return a list of (filename, size_bytes) tuples.
    """
    try:
        result = subprocess.run(
            ["gphoto2", "--list-files"],
            capture_output=True, text=True, timeout=30,
        )
        files = []
        for line in result.stdout.splitlines():
            if line.startswith("#"):
                match = re.search(r"#\d+\s+(\S+)\s+\S+\s+(\d+)\s+KB", line)
                if match:
                    if with_sizes:
                        files.append((match.group(1), int(match.group(2)) * 1024))
                    else:
                        files.append(match.group(1))
        return files
    except (subprocess.SubprocessError, OSError):
        return None


def get_immich_album_stats():
    """Return stats for the configured Immich album: count, total size, album name."""
    env_vars = load_env()
    album_name = env_vars.get("IMMICH_ALBUM", "Fuji-XT30")
    albums = immich_api("albums")
    if not albums:
        return None

    target = None
    for a in albums:
        if a.get("albumName") == album_name:
            target = a
            break
    if not target:
        return {"album": album_name, "count": 0, "size_bytes": 0}

    album_detail = immich_api(f"albums/{target['id']}")
    if not album_detail:
        return None

    assets = album_detail.get("assets", [])
    total_size = 0
    for asset in assets:
        exif = asset.get("exifInfo") or {}
        total_size += exif.get("fileSizeInByte", 0)

    return {
        "album": album_name,
        "count": len(assets),
        "size_bytes": total_size,
    }


def get_local_stats():
    """Return total file count and size across all import subdirectories."""
    total_files = 0
    total_bytes = 0
    if IMPORT_DIR.exists():
        for d in IMPORT_DIR.iterdir():
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file():
                        total_files += 1
                        total_bytes += f.stat().st_size
    return {"count": total_files, "size_bytes": total_bytes}


# --- Recipe helpers ---

def _exiftool_available():
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def extract_recipes_from_dir(directory: Path) -> list[dict]:
    """Run exiftool once over a directory and return per-file recipe dicts."""
    if not directory.exists():
        return []

    exiftool_tags = [f"-{f}" for f in RECIPE_FIELDS] + [
        "-FileName", "-DateTimeOriginal", "-ISO", "-ExposureTime",
        "-FNumber", "-FocalLength", "-LensID",
    ]

    try:
        result = subprocess.run(
            ["exiftool", "-j", "-r", "-ext", "JPG", "-ext", "RAF"] + exiftool_tags + [str(directory)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 and not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        return []


def recipe_fingerprint(photo_data: dict) -> str:
    """Produce a stable hash from the recipe-defining fields of a photo."""
    parts = [str(photo_data.get(f, "")) for f in RECIPE_FIELDS]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def recipe_from_photo(photo_data: dict) -> dict:
    """Extract the recipe portion from an exiftool JSON entry."""
    recipe = {}
    for f in RECIPE_FIELDS:
        val = photo_data.get(f)
        if val is not None:
            recipe[f] = str(val)

    if "FilmMode" not in recipe:
        sat = recipe.get("Saturation", "")
        if "B&W" in sat or "Sepia" in sat:
            recipe["FilmMode"] = "Monochrome (Sepia)" if "Sepia" in sat else "Monochrome"
        elif "Acros" in sat:
            recipe["FilmMode"] = "ACROS"

    return recipe


def aggregate_recipes(photos: list[dict]) -> list[dict]:
    """Group photos by recipe fingerprint and return a list of unique recipes with metadata."""
    buckets: dict[str, dict] = {}
    for p in photos:
        fp = recipe_fingerprint(p)
        if fp not in buckets:
            rec = recipe_from_photo(p)
            buckets[fp] = {
                "id": fp,
                "settings": rec,
                "count": 0,
                "sample_files": [],
                "dates": [],
            }
        buckets[fp]["count"] += 1
        fname = p.get("FileName", "")
        if fname and len(buckets[fp]["sample_files"]) < 5:
            buckets[fp]["sample_files"].append(fname)
        dt = p.get("DateTimeOriginal", "")
        if dt:
            buckets[fp]["dates"].append(dt)

    result = sorted(buckets.values(), key=lambda r: r["count"], reverse=True)
    for r in result:
        dates = sorted(r.pop("dates"))
        r["first_used"] = dates[0] if dates else ""
        r["last_used"] = dates[-1] if dates else ""
    return result


def load_recipe_library() -> dict:
    if RECIPES_FILE.exists():
        try:
            return json.loads(RECIPES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"recipes": []}


def save_recipe_library(lib: dict):
    RECIPES_FILE.write_text(json.dumps(lib, indent=2))


def recipe_to_display_text(settings: dict, name: str = "") -> str:
    """Format a recipe as human-readable text (like FujiXWeekly style)."""
    label_map = {
        "FilmMode": "Film Simulation",
        "DynamicRangeSetting": "Dynamic Range",
        "HighlightTone": "Highlight",
        "ShadowTone": "Shadow",
        "Saturation": "Color",
        "Sharpness": "Sharpness",
        "NoiseReduction": "Noise Reduction",
        "GrainEffectRoughness": "Grain Effect",
        "GrainEffectSize": "Grain Size",
        "ColorChromeEffect": "Color Chrome Effect",
        "ColorChromeFXBlue": "Color Chrome FX Blue",
        "Clarity": "Clarity",
        "WhiteBalance": "White Balance",
        "WhiteBalanceFineTune": "WB Shift",
    }
    lines = []
    if name:
        lines.append(f"  {name}")
        lines.append("  " + "─" * len(name))
    for field in RECIPE_FIELDS:
        val = settings.get(field, "—")
        label = label_map.get(field, field)
        lines.append(f"  {label:24s}  {val}")
    return "\n".join(lines)


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

    write_status("idle", "Import stopped by user")
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

    write_status("running", f"Syncing to Immich ({album})...")

    def _wait_for_sync():
        proc.wait()
        log_fh.close()
        deleted = cleanup_import_dir()
        write_status("idle", f"Synced to {album}, cleaned up {deleted} local files")

    threading.Thread(target=_wait_for_sync, daemon=True).start()

    return jsonify({"ok": True, "pid": proc.pid})


_camera_stats_cache = {"count": "?", "size_bytes": 0}
_immich_stats_cache = {"data": None, "ts": 0}
_IMMICH_STATS_TTL = 30

@app.route("/api/stats")
def api_stats():
    """Combined stats for local, Immich album, and camera.

    Local is always fresh, Immich is cached for 30s, camera uses its own
    cache that is only refreshed explicitly via /api/camera/stats.
    """
    local = get_local_stats()

    now = time.time()
    if _immich_stats_cache["data"] and (now - _immich_stats_cache["ts"]) < _IMMICH_STATS_TTL:
        immich = _immich_stats_cache["data"]
    else:
        immich = get_immich_album_stats()
        if immich is None:
            immich = {"album": "?", "count": "?", "size_bytes": 0}
        _immich_stats_cache["data"] = immich
        _immich_stats_cache["ts"] = now

    cam = camera_connected()
    cam_result = {
        "connected": cam["connected"],
        "model": cam["model"],
        **_camera_stats_cache,
    }

    return jsonify({"local": local, "immich": immich, "camera": cam_result})


@app.route("/api/camera/stats")
def api_camera_stats():
    """Fetch fresh camera file count and size via gphoto2 (slow, call sparingly)."""
    global _camera_stats_cache
    cam = camera_connected()
    if not cam["connected"]:
        _camera_stats_cache = {"count": "?", "size_bytes": 0}
        return jsonify({"connected": False, **_camera_stats_cache})

    cam_files = get_camera_files(with_sizes=True)
    if cam_files is not None:
        _camera_stats_cache = {"count": len(cam_files), "size_bytes": sum(s for _, s in cam_files)}
    else:
        _camera_stats_cache = {"count": "?", "size_bytes": 0}

    return jsonify({"connected": True, "model": cam["model"], **_camera_stats_cache})


@app.route("/api/camera/check", methods=["GET"])
def api_camera_check():
    """Compare camera files against Immich album to find what's synced and what's missing."""
    cam = camera_connected()
    if not cam["connected"]:
        return jsonify({"ok": False, "error": "No camera connected"}), 404

    write_status("running", "Checking camera vs Immich...")

    camera_files = get_camera_files()
    if camera_files is None:
        write_status("idle", "Camera check failed")
        return jsonify({"ok": False, "error": "Could not list camera files"}), 500

    immich_files, album_name = get_immich_album_files()
    if immich_files is None:
        write_status("idle", "Camera check failed -- Immich API error")
        return jsonify({"ok": False, "error": "Could not query Immich album"}), 500

    camera_set = set(camera_files)
    in_both = camera_set & immich_files
    only_camera = sorted(camera_set - immich_files)

    result = {
        "ok": True,
        "camera_total": len(camera_set),
        "in_immich": len(in_both),
        "missing_from_immich": len(only_camera),
        "missing_files": only_camera[:50],
        "album": album_name,
    }

    write_status("idle", f"Check done: {len(in_both)}/{len(camera_set)} in Immich")
    return jsonify(result)


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


# --- Recipe API Endpoints ---

_scan_result = {"state": "idle", "recipes": [], "total_photos": 0, "error": ""}

@app.route("/api/recipes/status")
def api_recipes_status():
    return jsonify({"exiftool": _exiftool_available(), "scan": _scan_result})


@app.route("/api/recipes/scan", methods=["POST"])
def api_recipes_scan():
    global _scan_result
    if not _exiftool_available():
        return jsonify({"ok": False, "error": "exiftool not installed (sudo apt install libimage-exiftool-perl)"}), 500

    if _scan_result.get("state") == "scanning":
        return jsonify({"ok": False, "error": "Scan already in progress"}), 409

    _scan_result = {"state": "scanning", "recipes": [], "total_photos": 0, "error": ""}

    def _bg_scan():
        global _scan_result
        try:
            photos = extract_recipes_from_dir(IMPORT_DIR)
            recipes = aggregate_recipes(photos) if photos else []
            _scan_result = {"state": "done", "recipes": recipes, "total_photos": len(photos), "error": ""}
        except Exception as e:
            _scan_result = {"state": "error", "recipes": [], "total_photos": 0, "error": str(e)}

    threading.Thread(target=_bg_scan, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/recipes/scan/result")
def api_recipes_scan_result():
    return jsonify(_scan_result)


@app.route("/api/recipes")
def api_recipes_list():
    lib = load_recipe_library()
    return jsonify(lib)


@app.route("/api/recipes/save", methods=["POST"])
def api_recipes_save():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    settings = data.get("settings")
    recipe_id = data.get("id", "")

    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 400
    if not settings:
        return jsonify({"ok": False, "error": "Settings are required"}), 400

    lib = load_recipe_library()
    for r in lib["recipes"]:
        if r["id"] == recipe_id:
            r["name"] = name
            r["settings"] = settings
            save_recipe_library(lib)
            return jsonify({"ok": True, "action": "updated"})

    lib["recipes"].append({
        "id": recipe_id or recipe_fingerprint(settings),
        "name": name,
        "settings": settings,
        "saved_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    save_recipe_library(lib)
    return jsonify({"ok": True, "action": "saved"})


@app.route("/api/recipes/delete", methods=["POST"])
def api_recipes_delete():
    data = request.get_json(force=True)
    recipe_id = data.get("id", "")
    lib = load_recipe_library()
    before = len(lib["recipes"])
    lib["recipes"] = [r for r in lib["recipes"] if r["id"] != recipe_id]
    save_recipe_library(lib)
    return jsonify({"ok": True, "removed": before - len(lib["recipes"])})


@app.route("/api/recipes/export")
def api_recipes_export():
    recipe_id = request.args.get("id", "")
    lib = load_recipe_library()
    for r in lib["recipes"]:
        if r["id"] == recipe_id:
            text = recipe_to_display_text(r["settings"], r["name"])
            return text, 200, {"Content-Type": "text/plain; charset=utf-8"}
    return "Recipe not found", 404


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    if is_import_running():
        return jsonify({"ok": False, "error": "Cannot cleanup while import is running"}), 409

    deleted = cleanup_import_dir()
    write_status("idle", f"Cleaned up {deleted} files")
    return jsonify({"ok": True, "deleted": deleted})


# --- Web UI ---

@app.route("/")
def index():
    return HTML_PAGE


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Import</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #e2e8f0; --dim: #94a3b8; --accent: #3b82f6; --green: #22c55e; --red: #ef4444; --orange: #f97316; --yellow: #eab308; --purple: #a855f7; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 1.5rem; }
  h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 1rem; display: flex; align-items: center; gap: .5rem; }
  .camera-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .camera-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .camera-dot.off { background: var(--red); }

  .tabs { display: flex; gap: 0; margin-bottom: 1rem; border-bottom: 2px solid var(--border); }
  .tab { padding: .6rem 1.2rem; font-size: .9rem; font-weight: 600; cursor: pointer; border: none; background: none; color: var(--dim); border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all .15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

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
  .btn-purple { background: var(--purple); color: #fff; }
  .btn-sm { padding: .35rem .7rem; font-size: .75rem; }
  .log-box { background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; padding: .75rem; height: 280px; overflow-y: auto; font-family: 'SF Mono', 'Fira Code', monospace; font-size: .72rem; line-height: 1.5; white-space: pre-wrap; word-break: break-all; color: var(--dim); }
  .files-list { font-size: .85rem; }
  .files-list .folder { display: flex; justify-content: space-between; padding: .35rem 0; border-bottom: 1px solid var(--border); }
  .files-list .folder:last-child { border-bottom: none; }
  .msg { font-size: .85rem; color: var(--dim); margin-top: .5rem; word-break: break-word; }
  .badge { display: inline-block; padding: .15rem .5rem; border-radius: .25rem; font-size: .75rem; font-weight: 600; }
  .badge-idle { background: var(--border); color: var(--dim); }
  .badge-running { background: #1d4ed8; color: #dbeafe; }
  .badge-error { background: #991b1b; color: #fecaca; }

  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal { background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1.5rem; max-width: 520px; width: 90%; max-height: 85vh; overflow-y: auto; }
  .modal h3 { font-size: 1.1rem; margin-bottom: 1rem; }
  .modal p { font-size: .9rem; color: var(--dim); margin-bottom: .75rem; line-height: 1.5; }
  .modal .stat { display: flex; justify-content: space-between; padding: .4rem 0; font-size: .9rem; border-bottom: 1px solid var(--border); }
  .modal .stat:last-of-type { border-bottom: none; }
  .modal .stat .num { font-weight: 700; }
  .modal .warn { color: var(--yellow); font-weight: 600; font-size: .85rem; margin: .75rem 0; }
  .modal .modal-actions { display: flex; gap: .5rem; margin-top: 1.25rem; flex-wrap: wrap; }

  .recipe-card { background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; padding: 1rem; margin-bottom: .75rem; }
  .recipe-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: .5rem; }
  .recipe-title { font-weight: 700; font-size: .95rem; }
  .recipe-title .saved-name { color: var(--accent); }
  .recipe-count { font-size: .75rem; color: var(--dim); background: var(--border); padding: .15rem .5rem; border-radius: .25rem; white-space: nowrap; }
  .recipe-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .15rem .75rem; font-size: .8rem; margin: .5rem 0; }
  .recipe-grid .rk { color: var(--dim); }
  .recipe-grid .rv { font-weight: 600; }
  .recipe-meta { font-size: .72rem; color: var(--dim); margin-top: .5rem; }
  .recipe-actions { display: flex; gap: .35rem; margin-top: .5rem; }
  .recipe-samples { font-size: .72rem; color: var(--dim); font-style: italic; }
  .recipe-empty { text-align: center; color: var(--dim); padding: 2rem; }
  .recipe-search { width: 100%; padding: .5rem .75rem; background: var(--bg); border: 1px solid var(--border); border-radius: .5rem; color: var(--text); font-size: .85rem; margin-bottom: .75rem; outline: none; }
  .recipe-search:focus { border-color: var(--accent); }
  .recipe-search::placeholder { color: var(--dim); }

  .stats-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
  @media (max-width: 700px) { .stats-row { grid-template-columns: 1fr; } }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: .75rem; padding: 1rem 1.25rem; }
  .stat-card h3 { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); margin-bottom: .5rem; display: flex; align-items: center; gap: .4rem; }
  .stat-card .stat-icon { font-size: 1rem; }
  .stat-card .stat-big { font-size: 1.6rem; font-weight: 700; line-height: 1.2; }
  .stat-card .stat-detail { font-size: .8rem; color: var(--dim); margin-top: .25rem; }
  .stat-card .stat-na { color: var(--dim); font-size: .9rem; }
  .stat-border-green { border-left: 3px solid var(--green); }
  .stat-border-blue { border-left: 3px solid var(--accent); }
  .stat-border-orange { border-left: 3px solid var(--orange); }
</style>
</head>
<body>

<h1>
  <span class="camera-dot off" id="cameraDot"></span>
  Photo Import
  <span style="font-weight:400; font-size:.85rem; color:var(--dim)" id="cameraModel">No camera</span>
</h1>

<div class="tabs">
  <button class="tab active" onclick="switchTab('import')">Import</button>
  <button class="tab" onclick="switchTab('recipes')">Recipes</button>
</div>

<!-- ==================== IMPORT TAB ==================== -->
<div class="tab-content active" id="tab-import">

<div class="stats-row">
  <div class="stat-card stat-border-orange" id="statCamera">
    <h3>Camera</h3>
    <div class="stat-na" id="camStatBody">Disconnected</div>
  </div>
  <div class="stat-card stat-border-green">
    <h3>Local (Import)</h3>
    <div class="stat-big" id="localCount">--</div>
    <div class="stat-detail" id="localSize">--</div>
  </div>
  <div class="stat-card stat-border-blue">
    <h3>Immich <span style="font-weight:400;text-transform:none" id="immichAlbumName"></span></h3>
    <div class="stat-big" id="immichCount">--</div>
    <div class="stat-detail" id="immichSize">--</div>
  </div>
</div>

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
      <button class="btn btn-green" id="btnSync" onclick="doSync()">Sync to Immich</button>
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

</div>

<!-- ==================== RECIPES TAB ==================== -->
<div class="tab-content" id="tab-recipes">

<div class="grid">
  <div class="card">
    <h2>Discover Recipes</h2>
    <p style="font-size:.85rem;color:var(--dim);margin-bottom:.75rem">Scan imported photos to discover film simulation recipes from EXIF data.</p>
    <div class="actions" style="margin-bottom:.75rem">
      <button class="btn btn-purple" id="btnScan" onclick="doScanRecipes()">Scan Photos</button>
    </div>
    <div id="scanStatus" style="font-size:.85rem;color:var(--dim)"></div>
    <div id="scanResults"></div>
  </div>

  <div class="card">
    <h2>Saved Recipes</h2>
    <div id="savedRecipes"><span style="color:var(--dim);font-size:.85rem">Loading...</span></div>
  </div>
</div>

</div>

<div class="modal-overlay" id="modalOverlay">
  <div class="modal" id="modalContent"></div>
</div>

<script>
const $ = id => document.getElementById(id);

async function fetchJSON(url, opts) {
  try { const r = await fetch(url, opts); return await r.json(); }
  catch { return null; }
}

function showModal(html) {
  $('modalContent').innerHTML = html;
  $('modalOverlay').classList.add('active');
}

function hideModal() {
  $('modalOverlay').classList.remove('active');
}

$('modalOverlay').addEventListener('click', e => {
  if (e.target === $('modalOverlay')) hideModal();
});

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
  $('tab-' + name).classList.add('active');
  if (name === 'recipes') loadSavedRecipes();
}

// ---------- Import Tab ----------

function fmtSize(bytes) {
  if (typeof bytes !== 'number' || bytes === 0) return '0 B';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

let _statsTimer = 0;
let _prevCamConnected = null;

function renderCamStats(cam) {
  if (cam && cam.connected) {
    $('cameraDot').className = 'camera-dot on';
    $('cameraModel').textContent = cam.model || 'Connected';
    let h = '<div class="stat-big">' + (cam.count === '?' ? '?' : cam.count) + '</div>';
    h += '<div class="stat-detail">' + fmtSize(cam.size_bytes) + '</div>';
    $('camStatBody').innerHTML = h;
  } else {
    $('cameraDot').className = 'camera-dot off';
    $('cameraModel').textContent = 'No camera';
    $('camStatBody').innerHTML = '<span class="stat-na">Disconnected</span>';
  }
}

async function fetchCameraStats() {
  $('camStatBody').innerHTML = '<span class="stat-na">Reading camera\u2026</span>';
  const cam = await fetchJSON('/api/camera/stats');
  renderCamStats(cam);
}

async function refreshStats() {
  const stats = await fetchJSON('/api/stats');
  if (!stats) return;

  const cam = stats.camera;
  const nowConnected = cam && cam.connected;
  if (_prevCamConnected === false && nowConnected) {
    fetchCameraStats();
  } else {
    renderCamStats(cam);
  }
  _prevCamConnected = !!nowConnected;

  const loc = stats.local;
  $('localCount').textContent = loc.count;
  $('localSize').textContent = fmtSize(loc.size_bytes);

  const im = stats.immich;
  $('immichAlbumName').textContent = im.album ? '(' + im.album + ')' : '';
  $('immichCount').textContent = im.count === '?' ? '?' : im.count;
  $('immichSize').textContent = im.count === '?' ? 'API error' : fmtSize(im.size_bytes);
}

async function refresh() {
  const [status, files, log] = await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/files'),
    fetchJSON('/api/log?lines=120'),
  ]);

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
    box.textContent = log.lines.join('\n');
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  _statsTimer++;
  if (_statsTimer >= 5 || _statsTimer === 1) {
    _statsTimer = 0;
    refreshStats();
  }
}

async function doAction(url) {
  await fetchJSON(url, { method: 'POST' });
  setTimeout(refresh, 500);
}

async function doSync() {
  if (!confirm('Sync all local files to Immich? Local copies will be deleted after upload.')) return;
  await doAction('/api/sync');
}

async function doCleanup() {
  if (!confirm('Delete all local imported files?')) return;
  const r = await fetchJSON('/api/cleanup', { method: 'POST' });
  if (r && r.ok) showModal('<h3>Cleanup Done</h3><p>Deleted ' + r.deleted + ' local files.</p><div class="modal-actions"><button class="btn btn-blue" onclick="hideModal()">OK</button></div>');
  else if (r && r.error) alert('Error: ' + r.error);
  setTimeout(refresh, 500);
}

async function doClearCamera() {
  $('btnClearCam').disabled = true;
  $('btnClearCam').textContent = 'Checking...';

  const check = await fetchJSON('/api/camera/check');

  $('btnClearCam').disabled = false;
  $('btnClearCam').textContent = 'Clear Camera';

  if (!check || !check.ok) {
    alert('Error: ' + (check ? check.error : 'Could not reach server'));
    return;
  }

  const total = check.camera_total;
  const inImmich = check.in_immich;
  const missing = check.missing_from_immich;
  const album = check.album;

  let html = '<h3>Clear Camera</h3>';
  html += '<div class="stat"><span>Files on camera</span><span class="num">' + total + '</span></div>';
  html += '<div class="stat"><span>Found in Immich (' + album + ')</span><span class="num">' + inImmich + '</span></div>';
  html += '<div class="stat"><span>Missing from Immich</span><span class="num">' + missing + '</span></div>';

  if (missing > 0) {
    html += '<p class="warn">' + missing + ' file(s) on the camera are NOT in Immich and will be permanently lost!</p>';
    if (check.missing_files && check.missing_files.length > 0) {
      html += '<p style="font-size:.75rem;color:var(--dim)">e.g. ' + check.missing_files.slice(0, 5).join(', ') + (missing > 5 ? ', ...' : '') + '</p>';
    }
    html += '<div class="modal-actions">';
    html += '<button class="btn btn-green" onclick="doSyncThenClear()">Sync Missing First</button>';
    html += '<button class="btn btn-red" onclick="doConfirmedClear()">Delete Anyway</button>';
    html += '<button class="btn btn-blue" onclick="hideModal()">Cancel</button>';
    html += '</div>';
  } else {
    html += '<p style="color:var(--green)">All camera files are safely in Immich.</p>';
    html += '<p>This action <strong>cannot be undone</strong>. All ' + total + ' files will be deleted from the camera.</p>';
    html += '<div class="modal-actions">';
    html += '<button class="btn btn-red" onclick="doConfirmedClear()">Delete All From Camera</button>';
    html += '<button class="btn btn-blue" onclick="hideModal()">Cancel</button>';
    html += '</div>';
  }

  showModal(html);
}

async function doSyncThenClear() {
  hideModal();
  showModal('<h3>Syncing...</h3><p>Importing missing files to Immich first. Check the log for progress.<br>You can clear the camera after the sync finishes.</p><div class="modal-actions"><button class="btn btn-blue" onclick="hideModal()">OK</button></div>');
  await doAction('/api/import/start');
}

async function doConfirmedClear() {
  hideModal();
  $('btnClearCam').disabled = true;
  $('btnClearCam').textContent = 'Clearing...';
  const r = await fetchJSON('/api/camera/clear', { method: 'POST' });
  $('btnClearCam').disabled = false;
  $('btnClearCam').textContent = 'Clear Camera';
  if (r && r.ok) {
    showModal('<h3>Camera Cleared</h3><p>' + (r.message || 'All files deleted.') + '</p><div class="modal-actions"><button class="btn btn-blue" onclick="hideModal()">OK</button></div>');
    fetchCameraStats();
  } else if (r && r.error) {
    alert('Error: ' + r.error);
  }
  setTimeout(refresh, 500);
}

// ---------- Recipes Tab ----------

const RECIPE_LABELS = {
  FilmMode: 'Film Simulation', DynamicRangeSetting: 'Dynamic Range',
  HighlightTone: 'Highlight', ShadowTone: 'Shadow',
  Saturation: 'Color', Sharpness: 'Sharpness',
  NoiseReduction: 'Noise Reduction', GrainEffectRoughness: 'Grain Effect',
  GrainEffectSize: 'Grain Size', ColorChromeEffect: 'Color Chrome',
  ColorChromeFXBlue: 'CC FX Blue', Clarity: 'Clarity',
  WhiteBalance: 'White Balance', WhiteBalanceFineTune: 'WB Shift',
};

function renderRecipeSettings(settings) {
  let html = '<div class="recipe-grid">';
  for (const [key, label] of Object.entries(RECIPE_LABELS)) {
    const val = settings[key] || '\u2014';
    html += '<span class="rk">' + label + '</span><span class="rv">' + val + '</span>';
  }
  html += '</div>';
  return html;
}

function renderRecipeCard(r, isSaved) {
  const name = r.name || r.settings?.FilmMode || 'Unknown';
  let html = '<div class="recipe-card">';
  html += '<div class="recipe-header">';
  html += '<span class="recipe-title">' + (r.name ? '<span class="saved-name">' + r.name + '</span>' : name) + '</span>';
  if (r.count) html += '<span class="recipe-count">' + r.count + ' photos</span>';
  html += '</div>';
  html += renderRecipeSettings(r.settings || r);

  if (r.first_used && r.last_used) {
    html += '<div class="recipe-meta">Used: ' + r.first_used.split(' ')[0] + ' \u2013 ' + r.last_used.split(' ')[0] + '</div>';
  }
  if (r.sample_files && r.sample_files.length > 0) {
    html += '<div class="recipe-samples">e.g. ' + r.sample_files.slice(0, 3).join(', ') + '</div>';
  }
  if (r.saved_at) {
    html += '<div class="recipe-meta">Saved: ' + r.saved_at + '</div>';
  }

  html += '<div class="recipe-actions">';
  if (!isSaved) {
    html += '<button class="btn btn-green btn-sm" onclick="saveRecipe(\'' + r.id + '\')">Save</button>';
  } else {
    html += '<button class="btn btn-blue btn-sm" onclick="exportRecipe(\'' + r.id + '\')">Export</button>';
    html += '<button class="btn btn-orange btn-sm" onclick="renameRecipe(\'' + r.id + '\')">Rename</button>';
    html += '<button class="btn btn-red btn-sm" onclick="deleteRecipe(\'' + r.id + '\')">Delete</button>';
  }
  html += '</div></div>';
  return html;
}

let _scannedRecipes = [];

async function doScanRecipes() {
  $('btnScan').disabled = true;
  $('btnScan').textContent = 'Scanning...';
  $('scanStatus').textContent = 'Starting exiftool scan on imported photos\u2026';
  $('scanResults').innerHTML = '';

  const r = await fetchJSON('/api/recipes/scan', { method: 'POST' });
  if (!r || !r.ok) {
    $('btnScan').disabled = false;
    $('btnScan').textContent = 'Scan Photos';
    $('scanStatus').innerHTML = '<span style="color:var(--red)">' + (r ? r.error : 'Failed to reach server') + '</span>';
    return;
  }

  pollScanResult();
}

async function pollScanResult() {
  const r = await fetchJSON('/api/recipes/scan/result');
  if (!r) { $('scanStatus').textContent = 'Lost connection...'; return; }

  if (r.state === 'scanning') {
    $('scanStatus').textContent = 'Scanning photos with exiftool\u2026 this may take up to a minute.';
    setTimeout(pollScanResult, 2000);
    return;
  }

  $('btnScan').disabled = false;
  $('btnScan').textContent = 'Scan Photos';

  if (r.state === 'error') {
    $('scanStatus').innerHTML = '<span style="color:var(--red)">' + r.error + '</span>';
    return;
  }

  _scannedRecipes = r.recipes || [];
  $('scanStatus').textContent = 'Found ' + _scannedRecipes.length + ' unique recipes across ' + r.total_photos + ' photos.';

  if (_scannedRecipes.length === 0) {
    $('scanResults').innerHTML = '<div class="recipe-empty">No photos found. Import some first!</div>';
    return;
  }

  let html = '<input type="text" class="recipe-search" placeholder="Filter recipes\u2026" oninput="filterScanned(this.value)">';
  html += '<div id="scannedList">';
  for (const rec of _scannedRecipes) {
    html += renderRecipeCard(rec, false);
  }
  html += '</div>';
  $('scanResults').innerHTML = html;
}

function filterScanned(query) {
  const q = query.toLowerCase();
  const filtered = _scannedRecipes.filter(r => {
    const vals = Object.values(r.settings || {}).join(' ').toLowerCase();
    return vals.includes(q);
  });
  let html = '';
  for (const rec of filtered) {
    html += renderRecipeCard(rec, false);
  }
  $('scannedList').innerHTML = html || '<div class="recipe-empty">No matches</div>';
}

async function saveRecipe(id) {
  const rec = _scannedRecipes.find(r => r.id === id);
  if (!rec) return;
  const defaultName = rec.settings.FilmMode || 'Recipe';
  const name = prompt('Name this recipe:', defaultName);
  if (!name) return;

  await fetchJSON('/api/recipes/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: rec.id, name, settings: rec.settings }),
  });
  loadSavedRecipes();
}

async function loadSavedRecipes() {
  const lib = await fetchJSON('/api/recipes');
  if (!lib || !lib.recipes || lib.recipes.length === 0) {
    $('savedRecipes').innerHTML = '<div class="recipe-empty">No saved recipes yet.<br>Scan photos and save the ones you like.</div>';
    return;
  }
  let html = '';
  for (const r of lib.recipes) {
    html += renderRecipeCard(r, true);
  }
  $('savedRecipes').innerHTML = html;
}

async function exportRecipe(id) {
  try {
    const r = await fetch('/api/recipes/export?id=' + id);
    const text = await r.text();
    showModal('<h3>Recipe Export</h3><pre style="background:var(--bg);padding:.75rem;border-radius:.5rem;font-size:.8rem;white-space:pre-wrap;color:var(--dim);overflow-x:auto;max-height:50vh">' + text + '</pre><div class="modal-actions"><button class="btn btn-blue btn-sm" onclick="navigator.clipboard.writeText(document.querySelector(\'.modal pre\').textContent);this.textContent=\'Copied!\'">Copy</button><button class="btn btn-blue btn-sm" onclick="hideModal()">Close</button></div>');
  } catch { alert('Failed to export'); }
}

async function renameRecipe(id) {
  const lib = await fetchJSON('/api/recipes');
  const rec = lib?.recipes?.find(r => r.id === id);
  if (!rec) return;
  const name = prompt('Rename recipe:', rec.name);
  if (!name) return;
  await fetchJSON('/api/recipes/save', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id, name, settings: rec.settings }),
  });
  loadSavedRecipes();
}

async function deleteRecipe(id) {
  if (!confirm('Delete this saved recipe?')) return;
  await fetchJSON('/api/recipes/delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id }),
  });
  loadSavedRecipes();
}

// ---------- Init ----------
async function init() {
  await refresh();
  const cam = await fetchJSON('/api/camera');
  if (cam && cam.connected) {
    _prevCamConnected = true;
    fetchCameraStats();
  } else {
    _prevCamConnected = false;
  }
}
init();
setInterval(refresh, 2000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
