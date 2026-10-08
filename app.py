import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Any, List

from flask import Flask, render_template, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import pandas as pd

from scraper_engine import ScraperEngine, check_internet

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
CORS(app)

# Global State
OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)

class StateManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine: ScraperEngine = None
        self.thread: threading.Thread = None
        self.status: str = "idle"  # idle, running, stopping, completed, stopped, error
        self.phase: str = "Ready"
        self.error_message: str = None
        self.progress: Dict[str, Any] = {
            "current": 0,
            "total": 0,
            "percent": 0.0,
            "links_count": 0,
            "scraped_count": 0,
        }
        self.logs: List[Dict[str, Any]] = []
        self.items: List[Dict[str, Any]] = []
        self.files: Dict[str, str] = {}
        self.subscribers: List[queue.Queue] = []
        self.start_timestamp: float = 0
        self.duration: float = 0

    def broadcast(self, event_type: str, data: Any):
        """Push an event to all connected SSE clients."""
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            dead_subs = []
            for q in self.subscribers:
                try:
                    q.put_nowait(payload)
                except Exception:
                    dead_subs.append(q)
            for d in dead_subs:
                if d in self.subscribers:
                    self.subscribers.remove(d)

    def add_log(self, text: str, level: str = "INFO"):
        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "text": text,
            "level": level.upper(),
        }
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 1000:
                self.logs.pop(0)
        self.broadcast("log", entry)

    def add_item(self, item: Dict[str, Any]):
        with self.lock:
            self.items.append(item)
            self.progress["scraped_count"] = len(self.items)
        self.broadcast("item", item)

    def update_progress(self, prog_data: Dict[str, Any]):
        with self.lock:
            self.progress.update(prog_data)
        self.broadcast("progress", self.progress)

    def set_phase(self, phase_name: str):
        with self.lock:
            self.phase = phase_name
        self.broadcast("phase", {"phase": phase_name})

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "phase": self.phase,
                "error": self.error_message,
                "progress": dict(self.progress),
                "items_count": len(self.items),
                "items": self.items[-50:],  # return most recent 50
                "logs": self.logs[-100:],   # return most recent 100
                "files": self.files,
                "duration": round(time.time() - self.start_timestamp, 1) if self.status == "running" else self.duration,
            }

state = StateManager()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/system", methods=["GET"])
def get_system():
    online = check_internet()
    mem_info = {}
    try:
        import psutil
        vm = psutil.virtual_memory()
        mem_info = {
            "total_ram_gb": round(vm.total / (1024**3), 1),
            "free_ram_gb": round(vm.available / (1024**3), 1),
            "ram_percent": vm.percent,
            "cpu_count": os.cpu_count(),
        }
    except Exception:
        pass
    res = {
        "status": "online",
        "internet": online,
        "python_version": sys.version.split(" ")[0],
        "output_dir": OUTPUT_DIR,
    }
    res.update(mem_info)
    return jsonify(res)


@app.route("/api/scrape/start", methods=["POST"])
def start_scrape():
    with state.lock:
        if state.status == "running":
            return jsonify({"error": "A scraping task is already running."}), 400

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "query")
    query = data.get("query", "").strip()
    queries = data.get("queries")
    if isinstance(queries, str):
        queries = [line.strip() for line in queries.splitlines() if line.strip() and not line.strip().startswith(("#", "//", "---", "==="))]
    elif isinstance(queries, list):
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip() and not q.strip().startswith(("#", "//", "---", "==="))]
    else:
        queries = None
    url = data.get("url", "").strip()
    from_links = data.get("from_links", "").strip()

    output_name = data.get("output_name", "output").strip() or "output"
    headless = bool(data.get("headless", True))
    links_only = bool(data.get("links_only", False))
    max_results = data.get("max_results")
    try:
        max_results = int(max_results) if max_results else None
    except (ValueError, TypeError):
        max_results = None

    require_phone = bool(data.get("require_phone", True))

    # Validation
    if mode == "query" and not query:
        return jsonify({"error": "Please enter what you want to find (e.g. 'Gyms in Chennai')."}), 400
    elif mode == "batch" and not queries:
        return jsonify({"error": "Please provide one or more search locations."}), 400
    elif mode == "url" and not url:
        return jsonify({"error": "Please provide a valid Google Maps search URL."}), 400
    elif mode == "from_links" and not from_links:
        return jsonify({"error": "Please select a links CSV file."}), 400

    # Reset state
    with state.lock:
        state.status = "running"
        state.phase = "Starting..."
        state.error_message = None
        state.logs = []
        state.items = []
        state.files = {}
        state.progress = {
            "current": 0,
            "total": 0,
            "percent": 0.0,
            "links_count": 0,
            "scraped_count": 0,
            "target_limit": max_results,
        }
        state.start_timestamp = time.time()
        state.duration = 0

    state.add_log(f"⚡ Starting Ultra-Fast Search for: '{query or mode.upper()}' (Limit: {max_results or 'Unlimited'})", "INFO")

    # Callbacks
    def handle_log(msg: str, lvl: str = "INFO"):
        state.add_log(msg, lvl)

    def handle_phase(ph: str):
        state.set_phase(ph)

    def handle_progress(p: Dict[str, Any]):
        state.update_progress(p)

    def handle_item(item: Dict[str, Any]):
        state.add_item(item)

    def handle_complete(res: Dict[str, Any]):
        with state.lock:
            state.status = res.get("status", "completed")
            state.phase = "Finished" if state.status == "completed" else "Stopped"
            state.duration = res.get("duration", 0)
            state.files = {
                "links": os.path.basename(res["links_file"]) if res.get("links_file") else None,
                "details": os.path.basename(res["details_file"]) if res.get("details_file") else None,
            }
        state.broadcast("completed", state.get_snapshot())

    def handle_error(err: str):
        with state.lock:
            state.status = "error"
            state.phase = "Error"
            state.error_message = err
        state.broadcast("error", {"error": err})

    # Initialize Engine (Single Ultra-Fast Engine)
    engine = ScraperEngine(
        query=query if mode == "query" else None,
        queries=queries if mode == "batch" else None,
        url=url if mode == "url" else None,
        from_links=from_links if mode == "from_links" else None,
        output_name=output_name,
        output_dir=OUTPUT_DIR,
        headless=headless,
        links_only=links_only,
        max_results=max_results,
        require_phone=require_phone,
        on_log=handle_log,
        on_phase=handle_phase,
        on_progress=handle_progress,
        on_item_scraped=handle_item,
        on_complete=handle_complete,
        on_error=handle_error,
    )

    state.engine = engine

    def worker():
        try:
            engine.run()
        except Exception as e:
            handle_error(str(e))

    thread = threading.Thread(target=worker, daemon=True)
    state.thread = thread
    thread.start()

    return jsonify({"status": "started", "snapshot": state.get_snapshot()})


@app.route("/api/scrape/stop", methods=["POST"])
def stop_scrape():
    with state.lock:
        if state.status != "running" or not state.engine:
            state.status = "idle"
            state.phase = "Ready"
            state.broadcast("completed", state.get_snapshot())
            return jsonify({"status": "idle", "message": "State reset to ready."}), 200
        state.status = "stopping"
        state.phase = "Stopping gracefully..."

    state.add_log("Stop requested. Waiting for driver to exit cleanly...", "WARN")
    state.broadcast("phase", {"phase": "Stopping gracefully..."})

    def async_stop():
        if state.engine:
            try:
                state.engine.stop()
            except Exception:
                pass
        with state.lock:
            state.status = "stopped"
            state.phase = "Stopped"
        state.broadcast("completed", state.get_snapshot())

    threading.Thread(target=async_stop, daemon=True).start()
    return jsonify({"status": "stopping"})


@app.route("/api/scrape/status", methods=["GET"])
def scrape_status():
    return jsonify(state.get_snapshot())


@app.route("/api/scrape/items", methods=["GET"])
def get_all_items():
    with state.lock:
        return jsonify({
            "count": len(state.items),
            "items": state.items,
        })


@app.route("/api/scrape/stream")
def stream_events():
    """SSE endpoint for live log messages and real-time updates."""
    client_queue = queue.Queue(maxsize=200)
    with state.lock:
        state.subscribers.append(client_queue)

    def event_generator():
        # Send initial snapshot immediately
        init_data = f"event: init\ndata: {json.dumps(state.get_snapshot())}\n\n"
        yield init_data

        try:
            while True:
                try:
                    payload = client_queue.get(timeout=20.0)
                    yield payload
                except queue.Empty:
                    # Ping keep-alive
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with state.lock:
                if client_queue in state.subscribers:
                    state.subscribers.remove(client_queue)

    return Response(event_generator(), mimetype="text/event-stream")


@app.route("/api/history", methods=["GET"])
def get_history():
    """List all CSV files in the output directory."""
    files = []
    if os.path.exists(OUTPUT_DIR):
        for f in sorted(os.listdir(OUTPUT_DIR), reverse=True):
            if f.endswith(".csv"):
                path = os.path.join(OUTPUT_DIR, f)
                stats = os.stat(path)
                # Count lines/rows
                row_count = 0
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fl:
                        row_count = max(0, sum(1 for _ in fl) - 1)
                except Exception:
                    row_count = "?"

                files.append({
                    "name": f,
                    "size_kb": round(stats.st_size / 1024, 1),
                    "modified": datetime.fromtimestamp(stats.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "rows": row_count,
                    "is_details": "_details.csv" in f,
                    "is_links": "_links.csv" in f,
                })
    return jsonify({"files": files})


@app.route("/api/download/<filename>")
def download_file(filename):
    """Download a file from the output directory."""
    safe_name = os.path.basename(filename)
    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=True)


@app.route("/api/preview/<filename>")
def preview_file(filename):
    """Preview first 50 rows of a CSV file."""
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.isfile(path):
        return jsonify({"error": "File not found"}), 404
    try:
        df = pd.read_csv(path, nrows=50)
        return jsonify({
            "columns": list(df.columns),
            "rows": df.fillna("").to_dict(orient="records"),
            "total_previewed": len(df),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-folder", methods=["POST"])
def open_output_folder():
    """Open output folder in Windows File Explorer."""
    try:
        if sys.platform == "win32":
            os.startfile(OUTPUT_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", OUTPUT_DIR])
        else:
            subprocess.Popen(["xdg-open", OUTPUT_DIR])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/shutdown", methods=["POST", "GET"])
def shutdown_service():
    """Cleanly stop the scraper engine and shut down Flask server."""
    if state.engine:
        try:
            state.engine.stop()
        except Exception:
            pass
    def _kill():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return jsonify({"status": "shutting_down", "message": "Scraper server has been stopped."})


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    env_port = int(os.environ.get("PORT", 5000))
    ports_to_listen = sorted(list({5000, 3000, env_port}))

    print(f"============================================================")
    print(f" Fast Map Leads Web Service")
    print(f" Listening on ports: {ports_to_listen} (host {host})")
    print(f" Output directory: {OUTPUT_DIR}")
    print(f"============================================================")

    # Start secondary ports in background threads
    for p in ports_to_listen[:-1]:
        threading.Thread(
            target=lambda port=p: app.run(host=host, port=port, debug=False, threaded=True),
            daemon=True
        ).start()

    # Run the primary port in the main thread
    app.run(host=host, port=ports_to_listen[-1], debug=False, threaded=True)


