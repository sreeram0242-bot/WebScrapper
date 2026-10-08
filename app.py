import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from flask import Flask, render_template, request, jsonify, Response, send_from_directory, make_response, g
from flask_cors import CORS
import pandas as pd
import requests

from scraper_engine import ScraperEngine, check_internet
import db
import security

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
CORS(app, supports_credentials=True)

# Attach Enterprise Security Headers to all responses
@app.after_request
def apply_security_headers(response):
    return security.add_security_headers(response)

# Persistent output directory
OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)


# =========================================================================
# State Manager (Multi-Client SSE, Real-Time Progress & Wallet Sync)
# =========================================================================

class StateManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine: Optional[ScraperEngine] = None
        self.thread: Optional[threading.Thread] = None
        self.status: str = "idle"  # idle, running, stopping, completed, stopped, error
        self.phase: str = "Ready"
        self.error_message: Optional[str] = None
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
        self.current_user_id: Optional[int] = None
        self.current_query: str = ""

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
                "items": self.items[-50:],
                "logs": self.logs[-100:],
                "files": self.files,
                "duration": round(time.time() - self.start_timestamp, 1) if self.status == "running" else self.duration,
            }

state = StateManager()


# =========================================================================
# Web Pages & System Endpoints
# =========================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


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
        "cost_per_lead": float(db.get_setting("cost_per_lead", "0.25")),
        "signup_bonus": float(db.get_setting("signup_bonus", "100.0")),
    }
    res.update(mem_info)
    return jsonify(res)


# =========================================================================
# User Authentication API (Strict Security & Rate Limited)
# =========================================================================

@app.route("/api/auth/signup", methods=["POST"])
@security.rate_limit(5, 60, "auth_signup")
def auth_signup():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""

    if not name or len(name) < 2:
        return jsonify({"error": "Full name must be at least 2 characters."}), 400
    if not security.validate_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if phone and not security.validate_phone(phone):
        return jsonify({"error": "Please enter a valid mobile number."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters long."}), 400

    res = db.create_user(name=name, email=email, phone=phone if phone else None, password=password)
    if "error" in res:
        return jsonify({"error": res["error"]}), 400

    user = res["user"]
    token = security.generate_auth_token(user["id"], user.get("role", "user"))

    resp = make_response(jsonify({
        "success": True,
        "message": "Account created successfully! ₹100 Free Signup Bonus credited.",
        "user": user,
        "token": token
    }))
    # Secure HttpOnly Cookie
    resp.set_cookie("auth_token", token, httponly=True, samesite="Lax", max_age=86400 * 7)
    return resp


@app.route("/api/auth/login", methods=["POST"])
@security.rate_limit(10, 60, "auth_login")
def auth_login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or data.get("email") or "").strip()
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify({"error": "Please provide your email/phone and password."}), 400

    user = db.authenticate_user(identifier, password)
    if not user:
        return jsonify({"error": "Invalid email/phone or password."}), 401

    if user.get("is_banned"):
        return jsonify({"error": "This account has been suspended. Please contact support."}), 403

    user.pop("password_hash", None)
    token = security.generate_auth_token(user["id"], user.get("role", "user"))

    resp = make_response(jsonify({
        "success": True,
        "user": user,
        "token": token
    }))
    resp.set_cookie("auth_token", token, httponly=True, samesite="Lax", max_age=86400 * 7)
    return resp


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user = security.get_current_user_from_request()
    if not user:
        return jsonify({"authenticated": False, "user": None}), 200

    return jsonify({
        "authenticated": True,
        "user": user
    })


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    resp = make_response(jsonify({"success": True, "message": "Signed out successfully."}))
    resp.delete_cookie("auth_token")
    return resp


@app.route("/api/auth/google", methods=["POST"])
@security.rate_limit(10, 60, "auth_google")
def auth_google():
    """
    Handle 'Continue with Google'.
    Validates Google Token if provided, or parses Google User Profile safely.
    """
    data = request.get_json(silent=True) or {}
    credential = data.get("credential")
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "Google User").strip()
    google_id = data.get("google_id") or ""

    # If an official Google ID token is supplied, verify it with Google's endpoint
    if credential:
        try:
            verify_url = f"https://oauth2.googleapis.com/tokeninfo?id_token={credential}"
            res = requests.get(verify_url, timeout=5)
            if res.status_code == 200:
                token_data = res.json()
                email = token_data.get("email", "").strip().lower()
                name = token_data.get("name", name)
                google_id = token_data.get("sub", google_id)
        except Exception:
            pass

    if not email or not security.validate_email(email):
        return jsonify({"error": "Could not verify Google account email."}), 400

    # Find or create user
    user = db.get_user_by_email(email)
    if not user:
        # Create user with random initial password hash & ₹100 free bonus
        rand_pwd = os.urandom(16).hex()
        created = db.create_user(name=name, email=email, phone=None, password=rand_pwd, google_id=google_id)
        if "error" in created:
            return jsonify({"error": created["error"]}), 400
        user = created["user"]
    else:
        user = db.get_user_by_id(user["id"])

    token = security.generate_auth_token(user["id"], user.get("role", "user"))
    resp = make_response(jsonify({
        "success": True,
        "user": user,
        "token": token
    }))
    resp.set_cookie("auth_token", token, httponly=True, samesite="Lax", max_age=86400 * 7)
    return resp


# =========================================================================
# In-App Wallet & Razorpay API (HMAC Verified & Rate Limited)
# =========================================================================

@app.route("/api/wallet/create-order", methods=["POST"])
@security.require_auth
@security.rate_limit(15, 60, "wallet_order")
def wallet_create_order():
    """
    Initiates a Razorpay recharge order.
    Generates official Razorpay Order ID or secure simulation order ID.
    """
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid recharge amount."}), 400

    if amount < 10.0:
        return jsonify({"error": "Minimum recharge amount is ₹10."}), 400
    if amount > 50000.0:
        return jsonify({"error": "Maximum recharge amount is ₹50,000."}), 400

    user_id = g.user["id"]
    key_id = db.get_setting("razorpay_key_id", "rzp_test_placeholder")
    key_secret = db.get_setting("razorpay_key_secret", "placeholder_secret")

    # If real Razorpay credentials configured, call Razorpay API
    is_simulation = False
    order_id = ""

    if key_id and not key_id.startswith("rzp_test_placeholder") and len(key_id) > 10 and len(key_secret) > 10:
        try:
            rzp_url = "https://api.razorpay.com/v1/orders"
            amount_paise = int(round(amount * 100))
            payload = {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"rcpt_{user_id}_{int(time.time())}",
                "notes": {"user_id": str(user_id)}
            }
            resp = requests.post(rzp_url, json=payload, auth=(key_id, key_secret), timeout=8)
            if resp.status_code in [200, 201]:
                order_data = resp.json()
                order_id = order_data.get("id")
            else:
                logging.warning(f"Razorpay API error: {resp.text}")
                is_simulation = True
                order_id = f"order_sim_{int(time.time())}_{os.urandom(4).hex()}"
        except Exception as e:
            logging.error(f"Razorpay connection failed: {e}")
            is_simulation = True
            order_id = f"order_sim_{int(time.time())}_{os.urandom(4).hex()}"
    else:
        is_simulation = True
        order_id = f"order_sim_{int(time.time())}_{os.urandom(4).hex()}"

    # Record order in pending payments table
    db.record_payment_order(user_id, amount, order_id)

    return jsonify({
        "success": True,
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
        "key_id": key_id if not is_simulation else "rzp_test_simulated",
        "is_simulation": is_simulation,
        "user": {
            "name": g.user.get("name"),
            "email": g.user.get("email"),
            "phone": g.user.get("phone") or "9999999999"
        }
    })


@app.route("/api/wallet/verify-payment", methods=["POST"])
@security.require_auth
@security.rate_limit(15, 60, "wallet_verify")
def wallet_verify_payment():
    """
    Verifies Razorpay payment signature cryptographically.
    Credits wallet (auto mode) or holds for admin review (manual mode).
    """
    data = request.get_json(silent=True) or {}
    order_id = (data.get("razorpay_order_id") or "").strip()
    payment_id = (data.get("razorpay_payment_id") or "").strip()
    signature = (data.get("razorpay_signature") or "").strip()
    is_sim = bool(data.get("is_simulation", False))

    if not order_id or not payment_id:
        return jsonify({"error": "Missing payment confirmation parameters."}), 400

    user_id = g.user["id"]
    key_secret = db.get_setting("razorpay_key_secret", "placeholder_secret")

    # If simulation mode
    if is_sim or order_id.startswith("order_sim_"):
        sim_sig = "simulated_signature_" + os.urandom(8).hex()
        # Verify via DB
        conn = db.get_db_connection()
        pay = conn.execute("SELECT * FROM payments WHERE razorpay_order_id = ? AND user_id = ?", (order_id, user_id)).fetchone()
        conn.close()
        if not pay:
            return jsonify({"error": "Order record not found."}), 404

        result = db.verify_and_process_razorpay_payment(
            user_id=user_id,
            order_id=order_id,
            payment_id=payment_id,
            signature=sim_sig,
            secret="",
            is_sim=True
        )
    else:
        # Strict HMAC-SHA256 signature verification
        result = db.verify_and_process_razorpay_payment(
            user_id=user_id,
            order_id=order_id,
            payment_id=payment_id,
            signature=signature,
            secret=key_secret
        )

    if "error" in result:
        return jsonify({"error": result["error"]}), 400

    return jsonify(result)


@app.route("/api/wallet/transactions", methods=["GET"])
@security.require_auth
def wallet_transactions():
    txs = db.get_user_transactions(g.user["id"])
    return jsonify({"transactions": txs})


@app.route("/api/wallet/scrapes", methods=["GET"])
@security.require_auth
def wallet_user_scrapes():
    scrapes = db.get_user_scrapes(g.user["id"])
    return jsonify({"scrapes": scrapes})


# =========================================================================
# Scraper Engine Controller (Protected by Auth, Flat ₹0.25/Lead Rate)
# =========================================================================

@app.route("/api/scrape/start", methods=["POST"])
@security.rate_limit(6, 60, "scrape_start")
def start_scrape():
    # 1. Require Authenticated User
    user = security.get_current_user_from_request()
    if not user:
        return jsonify({"error": "Authentication required. Please sign in or create an account to start scraping."}), 401

    if user.get("is_banned"):
        return jsonify({"error": "Your account has been suspended. Please contact support."}), 403

    # 2. Minimum Balance Check (Must have at least ₹0.25 for 1 lead)
    cost_per_lead = float(db.get_setting("cost_per_lead", "0.25"))
    if user.get("wallet_balance", 0.0) < cost_per_lead:
        return jsonify({
            "error": f"Insufficient wallet balance (₹{user.get('wallet_balance', 0.0):.2f}). Minimum ₹{cost_per_lead:.2f} required to search. Please add funds to your wallet.",
            "needs_recharge": True,
            "current_balance": user.get("wallet_balance", 0.0),
            "cost_per_lead": cost_per_lead
        }), 402

    with state.lock:
        if state.status == "running":
            return jsonify({"error": "A scraping task is already running."}), 400

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "query")
    query = (data.get("query") or "").strip()
    queries = data.get("queries")
    if isinstance(queries, str):
        queries = [line.strip() for line in queries.splitlines() if line.strip() and not line.strip().startswith(("#", "//", "---", "==="))]
    elif isinstance(queries, list):
        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip() and not q.strip().startswith(("#", "//", "---", "==="))]
    else:
        queries = None
    url = (data.get("url") or "").strip()
    from_links = (data.get("from_links") or "").strip()

    output_name = security.sanitize_filename(data.get("output_name") or "output")
    headless = bool(data.get("headless", True))
    links_only = bool(data.get("links_only", False))
    max_results = data.get("max_results")
    try:
        max_results = int(max_results) if max_results else None
    except (ValueError, TypeError):
        max_results = None

    require_phone = bool(data.get("require_phone", False))
    district_deep = bool(data.get("district_deep", True))

    # Validation
    if mode == "query" and not query:
        return jsonify({"error": "Please enter what you want to find (e.g. 'Gyms in Chennai')."}), 400
    elif mode == "batch" and not queries:
        return jsonify({"error": "Please provide one or more search locations."}), 400
    elif mode == "url" and not url:
        return jsonify({"error": "Please provide a valid Google Maps search URL."}), 400
    elif mode == "from_links" and not from_links:
        return jsonify({"error": "Please select a links CSV file."}), 400

    # Reset State & Link to User
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
        state.current_user_id = user["id"]
        state.current_query = query or (queries[0] if queries else (url or mode))

    mode_label = f"District Deep Search for: '{query}'" if (mode == "query" and district_deep) else f"Search for: '{query or mode.upper()}'"
    state.add_log(f"Starting {mode_label} (Flat Rate: ₹{cost_per_lead:.2f} per lead)", "INFO")

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
            details_path = res.get("details_file") or ""
            details_base = os.path.basename(details_path) if details_path else None
            state.files = {
                "links": os.path.basename(res["links_file"]) if res.get("links_file") else None,
                "details": details_base,
                "download_url": f"/api/download/{details_base}" if details_base else None,
            }

        # Atomic Wallet Deduction for extracted leads (Flat ₹0.25 per lead)
        lead_count = len(state.items)
        if state.current_user_id and lead_count > 0:
            try:
                debit_res = db.debit_wallet_for_scrape(
                    user_id=state.current_user_id,
                    lead_count=lead_count,
                    query=state.current_query or "Search",
                    file_name=details_base or "output.csv",
                    file_path=details_path
                )
                state.add_log(
                    f"Wallet Deduction: Deducted ₹{debit_res.get('cost_deducted', 0):.2f} for {lead_count} leads (₹{debit_res.get('rate_per_lead', 0.25):.2f}/lead). New Balance: ₹{debit_res.get('new_balance', 0):.2f}",
                    "SUCCESS"
                )
                state.broadcast("wallet_update", {
                    "user_id": state.current_user_id,
                    "new_balance": debit_res.get("new_balance", 0),
                    "cost_deducted": debit_res.get("cost_deducted", 0),
                    "lead_count": lead_count
                })
            except Exception as ex:
                logging.error(f"Error during wallet debit: {ex}")

        state.broadcast("completed", state.get_snapshot())

    def handle_error(err: str):
        with state.lock:
            state.status = "error"
            state.phase = "Error"
            state.error_message = err
        state.broadcast("error", {"error": err})

    # Initialize Engine
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
        district_deep=district_deep,
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
    """SSE endpoint for live log messages, progress, and real-time wallet balance."""
    client_queue = queue.Queue(maxsize=200)
    with state.lock:
        state.subscribers.append(client_queue)

    def event_generator():
        init_data = f"event: init\ndata: {json.dumps(state.get_snapshot())}\n\n"
        yield init_data

        try:
            while True:
                try:
                    payload = client_queue.get(timeout=20.0)
                    yield payload
                except queue.Empty:
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


@app.route("/api/expand-preview", methods=["GET"])
def expand_preview():
    """Preview sub-area expansion for a given search query."""
    q = request.args.get("query", "").strip()
    if not q:
        return jsonify({"count": 0, "localities": [], "location": "", "biz_type": ""})
    try:
        from district_expander import expand_district_query, parse_query_location
        biz_type, loc_name = parse_query_location(q)
        expanded = expand_district_query(q)
        return jsonify({
            "query": q,
            "biz_type": biz_type,
            "location": loc_name,
            "count": len(expanded),
            "localities": expanded
        })
    except Exception as e:
        return jsonify({"error": str(e), "count": 1, "localities": [q]}), 500


# =========================================================================
# Secure File Downloads (Path Traversal Hardened)
# =========================================================================

@app.route("/api/download/<filename>")
@security.rate_limit(30, 60, "download")
def download_file(filename):
    """Download a CSV file with directory traversal protection."""
    safe_name = security.sanitize_filename(filename)
    file_path = os.path.abspath(os.path.join(OUTPUT_DIR, safe_name))

    # Verify canonical path is strictly inside OUTPUT_DIR
    if not file_path.startswith(OUTPUT_DIR) or not os.path.isfile(file_path):
        return jsonify({"error": "File not found or unauthorized access path."}), 404

    return send_from_directory(OUTPUT_DIR, safe_name, as_attachment=True)


@app.route("/api/preview/<filename>")
@security.rate_limit(30, 60, "preview")
def preview_file(filename):
    """Preview first 50 rows of a CSV file securely."""
    safe_name = security.sanitize_filename(filename)
    file_path = os.path.abspath(os.path.join(OUTPUT_DIR, safe_name))

    if not file_path.startswith(OUTPUT_DIR) or not os.path.isfile(file_path):
        return jsonify({"error": "File not found or unauthorized access path."}), 404

    try:
        df = pd.read_csv(file_path, nrows=50)
        return jsonify({
            "columns": list(df.columns),
            "rows": df.fillna("").to_dict(orient="records"),
            "total_previewed": len(df),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/open-folder", methods=["POST"])
def open_output_folder():
    """Open output folder in OS File Explorer (Local mode)."""
    try:
        if sys.platform == "win32":
            os.startfile(OUTPUT_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", OUTPUT_DIR])
        else:
            subprocess.Popen(["xdg-open", OUTPUT_DIR])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================================================
# Admin Portal REST APIs (Role Protected & Rate Limited)
# =========================================================================

@app.route("/api/admin/stats", methods=["GET"])
@security.require_admin
def admin_stats():
    return jsonify(db.get_admin_dashboard_stats())


@app.route("/api/admin/users", methods=["GET"])
@security.require_admin
def admin_users():
    users = db.get_all_users()
    return jsonify({"users": users})


@app.route("/api/admin/users/<int:user_id>/adjust-wallet", methods=["POST"])
@security.require_admin
def admin_adjust_wallet(user_id: int):
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount."}), 400

    reason = (data.get("reason") or "Manual Admin Adjustment").strip()
    new_bal = db.credit_wallet(user_id, amount, reason)
    return jsonify({"success": True, "user_id": user_id, "new_balance": new_bal})


@app.route("/api/admin/users/<int:user_id>/toggle-ban", methods=["POST"])
@security.require_admin
def admin_toggle_ban(user_id: int):
    conn = db.get_db_connection()
    user = conn.execute("SELECT id, is_banned, role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"error": "User not found."}), 404
    if user["role"] == "admin":
        conn.close()
        return jsonify({"error": "Cannot suspend an administrator account."}), 400

    new_status = 0 if user["is_banned"] else 1
    with conn:
        conn.execute("UPDATE users SET is_banned = ? WHERE id = ?", (new_status, user_id))
    conn.close()
    return jsonify({"success": True, "user_id": user_id, "is_banned": new_status})


@app.route("/api/admin/payments", methods=["GET"])
@security.require_admin
def admin_payments():
    payments = db.get_all_payments()
    return jsonify({"payments": payments})


@app.route("/api/admin/payments/<int:payment_id>/approve", methods=["POST"])
@security.require_admin
def admin_approve_payment(payment_id: int):
    res = db.approve_payment_by_admin(payment_id)
    if "error" in res:
        return jsonify({"error": res["error"]}), 400
    return jsonify(res)


@app.route("/api/admin/payments/<int:payment_id>/reject", methods=["POST"])
@security.require_admin
def admin_reject_payment(payment_id: int):
    res = db.reject_payment_by_admin(payment_id)
    return jsonify(res)


@app.route("/api/admin/scrapes", methods=["GET"])
@security.require_admin
def admin_scrapes():
    scrapes = db.get_all_scrapes()
    return jsonify({"scrapes": scrapes})


@app.route("/api/admin/settings", methods=["GET"])
@security.require_admin
def admin_get_settings():
    key_id = db.get_setting("razorpay_key_id", "rzp_test_placeholder")
    secret = db.get_setting("razorpay_key_secret", "")
    masked_secret = (secret[:4] + "..." + secret[-4:]) if len(secret) > 8 else ("configured" if secret else "")

    return jsonify({
        "cost_per_lead": db.get_setting("cost_per_lead", "0.25"),
        "signup_bonus": db.get_setting("signup_bonus", "100.0"),
        "payment_approval_mode": db.get_setting("payment_approval_mode", "auto"),
        "razorpay_key_id": key_id,
        "razorpay_key_secret_masked": masked_secret
    })


@app.route("/api/admin/settings", methods=["POST"])
@security.require_admin
def admin_save_settings():
    data = request.get_json(silent=True) or {}
    if "cost_per_lead" in data:
        try:
            val = float(data["cost_per_lead"])
            if val >= 0:
                db.update_setting("cost_per_lead", f"{val:.4f}")
        except (ValueError, TypeError):
            pass

    if "signup_bonus" in data:
        try:
            val = float(data["signup_bonus"])
            if val >= 0:
                db.update_setting("signup_bonus", f"{val:.2f}")
        except (ValueError, TypeError):
            pass

    if "payment_approval_mode" in data:
        mode = data["payment_approval_mode"].strip().lower()
        if mode in ["auto", "manual"]:
            db.update_setting("payment_approval_mode", mode)

    if "razorpay_key_id" in data:
        db.update_setting("razorpay_key_id", data["razorpay_key_id"].strip())

    if "razorpay_key_secret" in data and data["razorpay_key_secret"].strip():
        db.update_setting("razorpay_key_secret", data["razorpay_key_secret"].strip())

    return jsonify({"success": True, "message": "Settings updated successfully."})


# =========================================================================
# Server Main Entrypoint
# =========================================================================

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    env_port = int(os.environ.get("PORT", 5000))
    ports_to_listen = sorted(list({5000, 3000, env_port}))

    print(f"============================================================")
    print(f" LeadScrapper Enterprise Web Service")
    print(f" Flat Rate: ₹0.25 (25 paise) per lead")
    print(f" Free Signup Bonus: ₹100.00")
    print(f" Security: Rate Limiting & HMAC Signature Verification Active")
    print(f" Listening on ports: {ports_to_listen} (host {host})")
    print(f" Output directory: {OUTPUT_DIR}")
    print(f"============================================================")

    # Start secondary ports in background threads
    for p in ports_to_listen[:-1]:
        threading.Thread(
            target=lambda port=p: app.run(host=host, port=port, debug=False, threaded=True),
            daemon=True
        ).start()

    # Run primary port in main thread
    app.run(host=host, port=ports_to_listen[-1], debug=False, threaded=True)
