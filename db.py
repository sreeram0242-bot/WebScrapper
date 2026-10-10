import os
import re
import sqlite3
import hashlib
import hmac
import time
from datetime import datetime
from typing import Optional, Dict, Any, List
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

# Store database in the persistent output directory
OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "output"))
os.makedirs(OUTPUT_DIR, exist_ok=True)
DB_PATH = os.path.join(OUTPUT_DIR, "scrapper.db")


def get_db_connection():
    """Create a thread-safe database connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=20.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Initialize database tables, default settings, and admin account."""
    conn = get_db_connection()
    with conn:
        # 1. Users Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                phone TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                google_id TEXT,
                wallet_balance REAL DEFAULT 100.0,
                role TEXT DEFAULT 'user', -- 'user' or 'admin'
                is_banned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Ensure schema migrations for existing databases
        user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "google_id" not in user_cols:
            try:
                conn.execute("ALTER TABLE users ADD COLUMN google_id TEXT")
            except Exception:
                pass
        if "phone" not in user_cols:
            try:
                conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            except Exception:
                pass

        # 2. Wallet Transactions Table (Credit & Debit audit log)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL, -- 'credit' or 'debit'
                description TEXT NOT NULL,
                lead_count INTEGER DEFAULT 0,
                balance_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # 3. Payments Table (Razorpay & manual top-ups)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                razorpay_order_id TEXT,
                razorpay_payment_id TEXT,
                razorpay_signature TEXT,
                status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
                approval_type TEXT DEFAULT 'auto', -- 'auto' or 'manual'
                created_at TEXT NOT NULL,
                approved_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # 4. Scrapes Table (Audit history of all scraping jobs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scrapes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                lead_count INTEGER NOT NULL,
                cost_deducted REAL NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT DEFAULT 'completed',
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # 5. System Settings Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # 6. Support Tickets Table (Help & Support Tickets with Problem Info & Attachments)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                user_email TEXT,
                user_phone TEXT,
                category TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                attachment_path TEXT,
                attachment_filename TEXT,
                status TEXT DEFAULT 'open', -- 'open', 'in_progress', 'resolved', 'closed'
                admin_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # Default Settings
        defaults = {
            "cost_per_lead": "0.25",              # 0.25 paise per lead (User requested)
            "signup_bonus": "100.0",              # 100 Rs free signup bonus
            "payment_approval_mode": "auto",      # 'auto' or 'manual'
            "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID", "rzp_test_placeholder"),
            "razorpay_key_secret": os.environ.get("RAZORPAY_KEY_SECRET", "placeholder_secret"),
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID", "536868490079-8n0ms1d6p9turlbr9p6c39rhktpcaern.apps.googleusercontent.com"),
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        # Create Default Admin User if none exists
        cur = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1")
        admin_row = cur.fetchone()
        if not admin_row:
            admin_pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
            admin_hash = generate_password_hash(admin_pwd)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT INTO users (name, email, phone, password_hash, wallet_balance, role, is_banned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ("Administrator", "admin@leadscrapper.com", "9999999999", admin_hash, 99999.0, "admin", 0, now, now))

    conn.close()


# =========================================================================
# User & Auth Helper Functions
# =========================================================================

def get_setting(key: str, default: str = "") -> str:
    env_val = os.environ.get(key.upper()) or os.environ.get(key)
    if env_val:
        return env_val
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def update_setting(key: str, value: str):
    conn = get_db_connection()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.close()


def create_user(name: str, email: str, phone: Optional[str], password: str, google_id: Optional[str] = None) -> Dict[str, Any]:
    """Register a new user with password hashing and free signup bonus."""
    email_clean = (email or "").strip().lower()
    name_clean = (name or "").strip()
    
    phone_clean = None
    if phone:
        digits = re.sub(r'\D', '', str(phone))
        if digits:
            phone_clean = digits[-10:] if len(digits) >= 10 else digits

    conn = get_db_connection()
    try:
        # Check duplicate by email
        existing = conn.execute("SELECT id FROM users WHERE lower(email) = ?", (email_clean,)).fetchone()
        if existing:
            return {"error": "An account with this email already exists."}
        if phone_clean:
            existing_phone = conn.execute(
                "SELECT id FROM users WHERE phone = ? OR (length(phone) >= 10 AND substr(phone, -10) = ?)",
                (phone_clean, phone_clean)
            ).fetchone()
            if existing_phone:
                return {"error": "An account with this phone number already exists."}

        pwd_hash = generate_password_hash(password)
        bonus = float(get_setting("signup_bonus", "100.0"))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with conn:
            cur = conn.execute("""
                INSERT INTO users (name, email, phone, password_hash, google_id, wallet_balance, role, is_banned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'user', 0, ?, ?)
            """, (name_clean, email_clean, phone_clean, pwd_hash, google_id, bonus, now, now))
            user_id = cur.lastrowid

            # Record bonus in transaction history
            conn.execute("""
                INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                VALUES (?, ?, 'credit', 'Welcome Bonus Credit', ?, ?)
            """, (user_id, bonus, bonus, now))

        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

        user = get_user_by_id(user_id)
        return {"success": True, "user": user}
    except sqlite3.IntegrityError as ie:
        err_msg = str(ie).lower()
        if "users.email" in err_msg or "unique constraint failed: users.email" in err_msg:
            return {"error": "An account with this email already exists."}
        elif "users.phone" in err_msg or "unique constraint failed: users.phone" in err_msg:
            return {"error": "An account with this phone number already exists."}
        return {"error": f"Database integrity constraint failed: {ie}"}
    except Exception as e:
        return {"error": f"Account creation failed: {str(e)}"}
    finally:
        conn.close()


def create_user_by_admin(name: str, email: str, phone: Optional[str], password: str, wallet_balance: float = 100.0, role: str = "user") -> Dict[str, Any]:
    """Admin-specific user creation function allowing custom role and initial wallet balance."""
    email_clean = (email or "").strip().lower()
    name_clean = (name or "").strip()
    
    phone_clean = None
    if phone:
        digits = re.sub(r'\D', '', str(phone))
        if digits:
            phone_clean = digits[-10:] if len(digits) >= 10 else digits

    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT id FROM users WHERE lower(email) = ?", (email_clean,)).fetchone()
        if existing:
            return {"error": "An account with this email already exists."}
        if phone_clean:
            existing_phone = conn.execute(
                "SELECT id FROM users WHERE phone = ? OR (length(phone) >= 10 AND substr(phone, -10) = ?)",
                (phone_clean, phone_clean)
            ).fetchone()
            if existing_phone:
                return {"error": "An account with this phone number already exists."}

        pwd_hash = generate_password_hash(password)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with conn:
            cur = conn.execute("""
                INSERT INTO users (name, email, phone, password_hash, wallet_balance, role, is_banned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (name_clean, email_clean, phone_clean, pwd_hash, float(wallet_balance), role, now, now))
            user_id = cur.lastrowid

            conn.execute("""
                INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                VALUES (?, ?, 'credit', 'Admin Account Provisioning', ?, ?)
            """, (user_id, float(wallet_balance), float(wallet_balance), now))

        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

        user = get_user_by_id(user_id)
        return {"success": True, "user": user}
    except sqlite3.IntegrityError as ie:
        err_msg = str(ie).lower()
        if "users.email" in err_msg or "unique constraint failed: users.email" in err_msg:
            return {"error": "An account with this email already exists."}
        elif "users.phone" in err_msg or "unique constraint failed: users.phone" in err_msg:
            return {"error": "An account with this phone number already exists."}
        return {"error": f"Database integrity constraint failed: {ie}"}
    except Exception as e:
        return {"error": f"Account creation failed: {str(e)}"}
    finally:
        conn.close()


def authenticate_user(identifier: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate user by email, phone, name, or username and password, supporting flexible formatting."""
    conn = get_db_connection()
    try:
        id_clean = (identifier or "").strip().lower()
        id_no_spaces = re.sub(r'\s+', '', id_clean)
        digits = re.sub(r'\D', '', identifier or '')
        ten_digit = digits[-10:] if len(digits) >= 10 else digits

        row = conn.execute(
            """SELECT * FROM users 
               WHERE (lower(email) = ? 
                      OR lower(name) = ?
                      OR lower(replace(name, ' ', '')) = ?
                      OR lower(email) = ? || '@gmail.com'
                      OR phone = ? 
                      OR phone = ? 
                      OR (length(phone) >= 10 AND substr(phone, -10) = ?))
                 AND is_banned = 0""",
            (id_clean, id_clean, id_no_spaces, id_clean, id_clean, digits if digits else None, ten_digit if ten_digit else None)
        ).fetchone()

        if row:
            if check_password_hash(row["password_hash"], password) or check_password_hash(row["password_hash"], (password or "").strip()):
                return dict(row)
        return None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d.pop("password_hash", None)
        return d
    return None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    if not email:
        return None
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_google_id(google_id: str) -> Optional[Dict[str, Any]]:
    if not google_id:
        return None
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM users WHERE google_id = ?", (str(google_id).strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    if not phone:
        return None
    digits = re.sub(r'\D', '', str(phone))
    ten = digits[-10:] if len(digits) >= 10 else digits
    conn = get_db_connection()
    row = conn.execute(
        """SELECT * FROM users 
           WHERE phone = ? OR phone = ? OR (length(phone) >= 10 AND substr(phone, -10) = ?)""",
        (str(phone).strip(), digits, ten)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def link_google_account(user_id: int, google_id: str, name: Optional[str] = None):
    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        if name:
            conn.execute("UPDATE users SET google_id = ?, name = COALESCE(NULLIF(name, ''), ?), updated_at = ? WHERE id = ?", (google_id, name, now, user_id))
        else:
            conn.execute("UPDATE users SET google_id = ?, updated_at = ? WHERE id = ?", (google_id, now, user_id))
    conn.close()


# =========================================================================
# Wallet & Payment Helpers (Atomic Operations)
# =========================================================================

def credit_wallet(user_id: int, amount: float, description: str) -> float:
    """Atomically credit money to user wallet."""
    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        conn.execute("UPDATE users SET wallet_balance = wallet_balance + ?, updated_at = ? WHERE id = ?", (amount, now, user_id))
        row = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
        new_bal = row["wallet_balance"]
        conn.execute("""
            INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
            VALUES (?, ?, 'credit', ?, ?, ?)
        """, (user_id, amount, description, new_bal, now))
    conn.close()
    return new_bal


def adjust_wallet_admin(user_id: int, action: str = "add", amount: float = 0.0, reason: str = "") -> Dict[str, Any]:
    """
    Atomically adjust user wallet from Admin Panel.
    Supported actions:
    - 'add': Credit positive funds
    - 'reduce': Deduct/reduce funds (prevents negative balance)
    - 'set': Explicitly set exact wallet balance
    """
    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn:
            user = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                return {"error": "User not found."}

            current_bal = float(user["wallet_balance"])
            amount = float(amount)
            action = (action or "add").lower().strip()

            if action == "reduce" or amount < 0:
                deduct_amt = abs(amount)
                new_bal = round(max(0.0, current_bal - deduct_amt), 2)
                actual_deducted = round(current_bal - new_bal, 2)
                desc = reason or f"Admin Wallet Reduction (-₹{actual_deducted:.2f})"
                conn.execute("UPDATE users SET wallet_balance = ?, updated_at = ? WHERE id = ?", (new_bal, now, user_id))
                conn.execute("""
                    INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                    VALUES (?, ?, 'debit', ?, ?, ?)
                """, (user_id, actual_deducted, desc, new_bal, now))
            elif action == "set":
                target_bal = round(max(0.0, amount), 2)
                diff = round(target_bal - current_bal, 2)
                t_type = "credit" if diff >= 0 else "debit"
                desc = reason or f"Admin Set Balance to ₹{target_bal:.2f}"
                conn.execute("UPDATE users SET wallet_balance = ?, updated_at = ? WHERE id = ?", (target_bal, now, user_id))
                conn.execute("""
                    INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, abs(diff), t_type, desc, target_bal, now))
                new_bal = target_bal
            else:  # default "add"
                add_amt = abs(amount)
                new_bal = round(current_bal + add_amt, 2)
                desc = reason or f"Admin Wallet Credit (+₹{add_amt:.2f})"
                conn.execute("UPDATE users SET wallet_balance = ?, updated_at = ? WHERE id = ?", (new_bal, now, user_id))
                conn.execute("""
                    INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                    VALUES (?, ?, 'credit', ?, ?, ?)
                """, (user_id, add_amt, desc, new_bal, now))

            return {
                "success": True,
                "user_id": user_id,
                "previous_balance": current_bal,
                "new_balance": new_bal,
                "action": action
            }
    finally:
        conn.close()


def create_user_by_admin(name: str, email: str, phone: Optional[str], password: str, wallet_balance: float = 100.0, role: str = "user") -> Dict[str, Any]:
    """Admin function to create user with custom starting balance and role."""
    res = create_user(name=name, email=email, phone=phone, password=password)
    if "error" in res:
        return res
    user = res["user"]
    user_id = user["id"]

    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        if wallet_balance != 100.0:
            diff = round(wallet_balance - 100.0, 2)
            conn.execute("UPDATE users SET wallet_balance = ?, role = ?, updated_at = ? WHERE id = ?", (wallet_balance, role, now, user_id))
            if diff != 0:
                t_type = "credit" if diff > 0 else "debit"
                conn.execute("""
                    INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                    VALUES (?, ?, ?, 'Admin Initial Balance Adjustment', ?, ?)
                """, (user_id, abs(diff), t_type, wallet_balance, now))
        else:
            conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, now, user_id))
    conn.close()
    return {"success": True, "user": get_user_by_id(user_id)}


def debit_wallet_for_scrape(user_id: int, lead_count: int, query: str, file_name: str, file_path: str) -> Dict[str, Any]:
    """
    Atomically debit user wallet for leads extracted at 0.25 paise per lead.
    Returns transaction summary and remaining balance.
    """
    conn = get_db_connection()
    rate = float(get_setting("cost_per_lead", "0.25"))
    total_cost = round(lead_count * rate, 2)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn:
            user = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                return {"error": "User not found."}

            current_balance = user["wallet_balance"]
            deduction = min(current_balance, total_cost)
            new_balance = round(max(0.0, current_balance - deduction), 2)

            conn.execute("UPDATE users SET wallet_balance = ?, updated_at = ? WHERE id = ?", (new_balance, now, user_id))

            # Record debit transaction
            conn.execute("""
                INSERT INTO wallet_transactions (user_id, amount, type, description, lead_count, balance_after, created_at)
                VALUES (?, ?, 'debit', ?, ?, ?, ?)
            """, (user_id, deduction, f"Scraped {lead_count} leads for '{query}' (₹{rate}/lead)", lead_count, new_balance, now))

            # Record in scrapes audit log
            conn.execute("""
                INSERT INTO scrapes (user_id, query, lead_count, cost_deducted, file_name, file_path, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)
            """, (user_id, query, lead_count, deduction, file_name, file_path, now))

        return {
            "success": True,
            "lead_count": lead_count,
            "cost_deducted": deduction,
            "new_balance": new_balance,
            "rate_per_lead": rate
        }
    finally:
        conn.close()
    return {
        "success": True,
        "lead_count": lead_count,
        "cost_deducted": deduction,
        "new_balance": new_balance,
        "rate_per_lead": rate
    }


def record_payment_order(user_id: int, amount: float, rzp_order_id: str) -> int:
    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    approval_mode = get_setting("payment_approval_mode", "auto")
    with conn:
        cur = conn.execute("""
            INSERT INTO payments (user_id, amount, razorpay_order_id, status, approval_type, created_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (user_id, amount, rzp_order_id, approval_mode, now))
        pay_id = cur.lastrowid
    conn.close()
    return pay_id


def verify_and_process_razorpay_payment(
    user_id: int,
    order_id: str,
    payment_id: str,
    signature: str,
    secret: str,
    is_sim: bool = False
) -> Dict[str, Any]:
    """
    Cryptographic HMAC-SHA256 signature verification for Razorpay payments.
    Handles both auto-approval and manual-approval modes.
    """
    # 1. Verify HMAC-SHA256 signature unless running in simulation test mode
    if not is_sim and not order_id.startswith("order_sim_"):
        if not secret:
            return {"error": "Razorpay secret key not configured."}
        msg = f"{order_id}|{payment_id}".encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected_sig, signature):
            return {"error": "Invalid cryptographic signature. Payment verification failed."}

    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = get_setting("payment_approval_mode", "auto")

    try:
        with conn:
            # High-Level Cyber Security Check 1: Anti-Replay Duplicate Payment ID Prevention
            if not is_sim and not payment_id.startswith("pay_sim_"):
                dup = conn.execute(
                    "SELECT id, razorpay_order_id, user_id FROM payments WHERE razorpay_payment_id = ? AND status = 'approved'",
                    (payment_id,)
                ).fetchone()
                if dup:
                    return {"error": "Security Alert: This payment transaction has already been processed and credited.", "already_processed": True}

            # High-Level Cyber Security Check 2: Order Lookup & Status Validation
            pay = conn.execute("SELECT * FROM payments WHERE razorpay_order_id = ?", (order_id,)).fetchone()
            if not pay:
                return {"error": "Payment order record not found in system."}

            # If order is already approved, return current state without double-crediting
            if pay["status"] == "approved":
                user = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
                bal = user["wallet_balance"] if user else 0.0
                return {"success": True, "status": "approved", "already_processed": True, "new_balance": bal, "amount": pay["amount"]}

            # High-Level Cyber Security Check 3: User Ownership Binding
            if int(pay["user_id"]) != int(user_id):
                return {"error": "Security Alert: Access denied. Order does not belong to this account."}

            amount = float(pay["amount"])

            if mode == "auto":
                # Instant atomic wallet credit
                conn.execute("""
                    UPDATE payments 
                    SET razorpay_payment_id = ?, razorpay_signature = ?, status = 'approved', approved_at = ?
                    WHERE razorpay_order_id = ?
                """, (payment_id, signature, now, order_id))

                conn.execute("UPDATE users SET wallet_balance = wallet_balance + ?, updated_at = ? WHERE id = ?", (amount, now, user_id))
                user = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
                new_bal = user["wallet_balance"]

                conn.execute("""
                    INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                    VALUES (?, ?, 'credit', ?, ?, ?)
                """, (user_id, amount, f"Wallet Recharge via Razorpay ({payment_id})", new_bal, now))

                result = {"success": True, "status": "approved", "new_balance": new_bal, "amount": amount}
            else:
                # Mark for manual admin approval
                conn.execute("""
                    UPDATE payments 
                    SET razorpay_payment_id = ?, razorpay_signature = ?, status = 'pending'
                    WHERE razorpay_order_id = ?
                """, (payment_id, signature, order_id))
                result = {"success": True, "status": "pending", "amount": amount, "message": "Payment recorded. Awaiting Admin Approval."}
        return result
    finally:
        conn.close()


def approve_payment_by_admin(payment_id: int) -> Dict[str, Any]:
    """Admin manually approves a pending payment."""
    conn = get_db_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with conn:
            pay = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            if not pay:
                return {"error": "Payment record not found."}
            if pay["status"] == "approved":
                return {"error": "Payment is already approved."}

            user_id = pay["user_id"]
            amount = pay["amount"]

            conn.execute("UPDATE payments SET status = 'approved', approved_at = ? WHERE id = ?", (now, payment_id))
            conn.execute("UPDATE users SET wallet_balance = wallet_balance + ?, updated_at = ? WHERE id = ?", (amount, now, user_id))
            user = conn.execute("SELECT wallet_balance FROM users WHERE id = ?", (user_id,)).fetchone()
            new_bal = user["wallet_balance"]

            conn.execute("""
                INSERT INTO wallet_transactions (user_id, amount, type, description, balance_after, created_at)
                VALUES (?, ?, 'credit', ?, ?, ?)
            """, (user_id, amount, f"Admin Approved Recharge (Payment #{payment_id})", new_bal, now))

        return {"success": True, "user_id": user_id, "amount": amount, "new_balance": new_bal}
    finally:
        conn.close()


def reject_payment_by_admin(payment_id: int) -> Dict[str, Any]:
    conn = get_db_connection()
    with conn:
        conn.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (payment_id,))
    conn.close()
    return {"success": True}


# =========================================================================
# Admin Queries
# =========================================================================

def get_admin_dashboard_stats() -> Dict[str, Any]:
    conn = get_db_connection()
    users_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    total_rev = conn.execute("SELECT COALESCE(SUM(amount), 0) as s FROM payments WHERE status = 'approved'").fetchone()["s"]
    pending_count = conn.execute("SELECT COUNT(*) as c FROM payments WHERE status = 'pending'").fetchone()["c"]
    pending_tickets = conn.execute("SELECT COUNT(*) as c FROM support_tickets WHERE status = 'open'").fetchone()["c"]
    total_leads = conn.execute("SELECT COALESCE(SUM(lead_count), 0) as s FROM scrapes").fetchone()["s"]
    conn.close()
    return {
        "users_count": users_count,
        "total_revenue": round(total_rev, 2),
        "pending_payments": pending_count,
        "pending_tickets": pending_tickets,
        "total_leads_scraped": total_leads,
        "cost_per_lead": get_setting("cost_per_lead", "0.25"),
        "signup_bonus": get_setting("signup_bonus", "100.0"),
        "payment_mode": get_setting("payment_approval_mode", "auto"),
    }


def get_all_users() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT u.id, u.name, u.email, u.phone, u.wallet_balance, u.role, u.is_banned, u.created_at,
               COALESCE((SELECT SUM(s.lead_count) FROM scrapes s WHERE s.user_id = u.id), 0) as total_leads,
               COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.user_id = u.id AND p.status = 'approved'), 0) as total_paid
        FROM users u
        ORDER BY u.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_payments() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT p.*, u.name as user_name, u.email as user_email, u.phone as user_phone
        FROM payments p
        JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_scrapes() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT s.*, u.name as user_name, u.email as user_email
        FROM scrapes s
        JOIN users u ON s.user_id = u.id
        ORDER BY s.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_transactions(user_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM wallet_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_scrapes(user_id: int) -> List[Dict[str, Any]]:
    """Get all scrape audit records for a specific user."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM scrapes WHERE user_id = ? ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def update_user_password(user_id: int, new_password: str) -> bool:
    pw_hash = generate_password_hash(new_password)
    conn = get_db_connection()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
    conn.commit()
    conn.close()
    return True


def update_user_profile(user_id: int, name: str, phone: Optional[str] = None) -> bool:
    conn = get_db_connection()
    conn.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (name.strip(), phone.strip() if phone else None, user_id))
    conn.commit()
    conn.close()
    return True


# =========================================================================
# Support & Problem Reports Database Helpers
# =========================================================================

def create_support_ticket(user_id: int, category: str, subject: str, message: str,
                          attachment_path: Optional[str] = None,
                          attachment_filename: Optional[str] = None) -> Dict[str, Any]:
    """Create a new support ticket with problem info and optional attachment."""
    conn = get_db_connection()
    try:
        user = conn.execute("SELECT name, email, phone FROM users WHERE id = ?", (user_id,)).fetchone()
        user_name = user["name"] if user else ""
        user_email = user["email"] if user else ""
        user_phone = user["phone"] if user else ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with conn:
            cur = conn.execute("""
                INSERT INTO support_tickets 
                (user_id, user_name, user_email, user_phone, category, subject, message, attachment_path, attachment_filename, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """, (user_id, user_name, user_email, user_phone, category, subject, message, attachment_path, attachment_filename, now, now))
            ticket_id = cur.lastrowid
        return {"success": True, "ticket_id": ticket_id, "created_at": now}
    finally:
        conn.close()


def get_user_support_tickets(user_id: int) -> List[Dict[str, Any]]:
    """Get all tickets submitted by a specific user."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM support_tickets WHERE user_id = ? ORDER BY id DESC LIMIT 50",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_support_tickets(status: Optional[str] = None, limit: int = 150) -> List[Dict[str, Any]]:
    """Get all support tickets for admin inspection."""
    conn = get_db_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM support_tickets WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM support_tickets ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_support_ticket_status(ticket_id: int, status: str, admin_notes: Optional[str] = None) -> bool:
    """Update status of a support ticket (e.g. 'open', 'in_progress', 'resolved')."""
    conn = get_db_connection()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with conn:
            if admin_notes is not None:
                conn.execute(
                    "UPDATE support_tickets SET status = ?, admin_notes = ?, updated_at = ? WHERE id = ?",
                    (status, admin_notes, now, ticket_id)
                )
            else:
                conn.execute(
                    "UPDATE support_tickets SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, ticket_id)
                )
        return True
    finally:
        conn.close()


# Auto-initialize database on import
init_db()
