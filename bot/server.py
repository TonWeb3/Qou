"""
QUOTEX1 Dashboard — Flask + SocketIO backend
"""

import asyncio
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

BASE_DIR     = Path(__file__).parent.parent          # QUOTEX1 root
_ASSETS_DIR  = BASE_DIR / "dashboard"               # HTML/CSS/JS live here
sys.path.insert(0, str(BASE_DIR))

app = Flask(
    __name__,
    template_folder=str(_ASSETS_DIR / "templates"),
    static_folder=str(_ASSETS_DIR / "static"),
)
app.config["SECRET_KEY"] = "qx-dashboard-2026"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ─── Shared state ────────────────────────────────────────────

_tg_client_store: dict = {}   # holds live Telethon client during auth

# Live reference to the TradingBot — set by main.py when bot starts in-process.
_bot_instance = None

# Bot thread + event loop — the bot runs in a background thread with its own loop.
_bot_thread: Optional[threading.Thread] = None
_bot_loop:   Optional[asyncio.AbstractEventLoop] = None

# Fallback store when bot is not running — tracks alert and last known Quotex connection
_server_state: dict = {"alert": None, "quotex_connected": False}

# ── OTP / PIN coordination ─────────────────────────────────
# Used when Quotex requires an email PIN during login.
# The connect thread blocks on _otp_event; /api/quotex/pin sets the value and fires it.
_otp_event: threading.Event = threading.Event()
_otp_value: Optional[str] = None


def _make_otp_callback() -> callable:
    """
    Returns a sync callback to pass as on_otp_callback to pyquotex.
    When called, it emits quotex_otp_required to the dashboard and blocks
    up to 5 minutes for the user to submit the PIN via /api/quotex/pin.
    """
    global _otp_value
    _otp_event.clear()
    _otp_value = None

    def callback(message: str) -> str:
        global _otp_value
        socketio.emit("quotex_otp_required", {"message": str(message)})
        got_it = _otp_event.wait(timeout=300)    # blocks thread, not event loop
        if not got_it:
            return ""
        return _otp_value or ""

    return callback


# ─── Helpers ─────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _normalize_phone(raw) -> str:
    """
    Normalize any user-entered phone number to digits only (no +, spaces, dashes
    or parentheses), so every format works — including a US number typed plainly
    as 12342342345, or +1 (234) 234-2345, or 1-234-234-2345. The country code
    MUST be included (e.g. leading 1 for the US). A leading '00' international
    prefix is converted to nothing extra (kept as digits; Telegram accepts the
    country code form). Returns '' if there are no digits.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    # Drop a leading international call prefix like 00 (e.g. 0012342342345 -> 12342342345)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base. Nested dicts are merged; lists and
    scalars in override replace those in base. This lets the dashboard send only
    the fields it edits while everything else already in config.json (e.g.
    session_name) is preserved exactly.
    """
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _pending_signals(th) -> list:
    """Pending (scheduled) signals with a live countdown for the dashboard."""
    out = []
    if not (th and th.executor):
        return out
    for sig in list(th.executor.pending_signals.values()):
        item = {
            "asset":        sig.get("asset"),
            "direction":    sig.get("direction"),
            "expiry":       sig.get("expiry"),
            "entry_time":   sig.get("entry_time"),
            "seconds_left": None,
        }
        if sig.get("entry_time"):
            try:
                left = th.executor._calc_wait(sig["entry_time"], sig.get("timezone"))
                item["seconds_left"] = round(left, 1) if left is not None else None
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda i: (i["seconds_left"] is None, i["seconds_left"]))
    return out


def get_bot_state() -> dict:
    """Return live bot state — reads directly from the running TradingBot instance."""
    bot = _bot_instance
    cfg = _read_json(BASE_DIR / "config.json")
    trading = cfg.get("trading", {})

    if bot is not None:
        qh = bot.quotex_handler
        th = bot.telegram_handler
        pending = _pending_signals(th)
        return {
            "status":          "running" if bot.running else "stopped",
            "daily_trades":    qh.daily_trades if qh else 0,
            "wins":            qh.wins if qh else 0,
            "losses":          qh.losses if qh else 0,
            "daily_pnl":       qh.daily_pnl if qh else 0.0,
            "daily_loss":      qh.daily_loss if qh else 0.0,
            "balance":         qh.balance if qh else None,
            "account_type":    (qh.config.trading.account_type if qh
                                else trading.get("account_type", "demo")),
            "risk_mode":       qh.config.trading.risk_mode if qh else "fixed",
            "max_daily_trades":       trading.get("max_daily_trades", 0),
            "max_daily_loss":         trading.get("max_daily_loss", 0),
            "max_daily_loss_enabled": trading.get("max_daily_loss_enabled", True),
            "active_trades":   qh.active_trades if qh else 0,
            "max_concurrent_trades":  trading.get("max_concurrent_trades", 1),
            "last_trade":      qh.last_trade if qh else None,
            "pending":         pending,
            "active_signals":  len(pending),
            "alert":           bot.alert,
            "quotex_connected": qh.is_connected if qh else False,
            "bot_running":     bot.running,
        }
    return {
        "status": "stopped", "daily_trades": 0, "wins": 0, "losses": 0,
        "daily_pnl": 0.0, "daily_loss": 0.0,
        "balance": None,
        "account_type": trading.get("account_type", "demo"),
        "risk_mode": (trading.get("risk_mode") or "fixed"),
        "max_daily_trades":       trading.get("max_daily_trades", 0),
        "max_daily_loss":         trading.get("max_daily_loss", 0),
        "max_daily_loss_enabled": trading.get("max_daily_loss_enabled", True),
        "active_trades": 0,
        "max_concurrent_trades":  trading.get("max_concurrent_trades", 1),
        "last_trade": None, "pending": [], "active_signals": 0,
        "alert":            _server_state.get("alert"),
        "quotex_connected": bool(_server_state.get("quotex_connected", False)),
        "bot_running":      _bot_thread is not None and _bot_thread.is_alive(),
    }


def get_connection_status() -> dict:
    """Return live connection status — reads directly from bot instance when running."""
    config = _read_json(BASE_DIR / "config.json")
    session_name = (config.get("telegram", {}).get("session_name") or "quotex_bot_session")
    session_data = _read_json(BASE_DIR / f"{session_name}.json")
    telegram_ok  = bool(session_data.get("session_string"))

    if _bot_instance is not None and _bot_instance.quotex_handler is not None:
        quotex_ok = _bot_instance.quotex_handler.is_connected
    else:
        quotex_ok = bool(_server_state.get("quotex_connected", False))

    return {"telegram": telegram_ok, "quotex": quotex_ok}


# ─── Routes ──────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    return jsonify(state)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    # no-store so the dashboard always reflects the live config.json and never a
    # cached copy — otherwise edits made directly on disk wouldn't show up.
    resp = jsonify(_read_json(BASE_DIR / "config.json"))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/api/settings", methods=["POST"])
def save_settings():
    try:
        data = request.get_json(force=True)
        # Merge over the existing file so fields the dashboard doesn't send are
        # preserved exactly (e.g. session_name). Whatever the form does send
        # overwrites the matching keys — so frontend edits land in config.json.
        existing = _read_json(BASE_DIR / "config.json", {})
        merged = _deep_merge(existing, data)
        (BASE_DIR / "config.json").write_text(json.dumps(merged, indent=2))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# ─── Bot start / stop ─────────────────────────────────────────

def _run_trading_bot():
    """
    Entry point for the bot thread.
    Creates its own asyncio event loop and runs TradingBot.start().
    The loop — and therefore the bot — live until start() returns or shutdown() is called.
    """
    global _bot_loop, _bot_instance

    import sys as _sys
    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    _bot_loop = loop
    asyncio.set_event_loop(loop)

    try:
        from main import TradingBot
        bot = TradingBot()
        loop.run_until_complete(bot.start())
    except Exception as e:
        print(f"[bot] Fatal error: {e}")
    finally:
        _bot_instance = None
        _bot_loop     = None
        _server_state["alert"] = None
        socketio.emit("bot_status", {"running": False})
        try:
            loop.close()
        except Exception:
            pass


@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global _bot_thread
    if _bot_thread and _bot_thread.is_alive():
        return jsonify({"success": False, "message": "Bot already running"})

    _server_state["alert"] = None
    _bot_thread = threading.Thread(target=_run_trading_bot, daemon=True, name="trading-bot")
    _bot_thread.start()
    return jsonify({"success": True})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global _bot_instance, _bot_loop
    bot  = _bot_instance
    loop = _bot_loop
    if bot and loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(bot.shutdown(), loop)
        try:
            future.result(timeout=10)
        except Exception:
            pass
        socketio.emit("bot_status", {"running": False})
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Bot not running"})


def _patch_state(patch: dict):
    """
    Update mutable state fields.
    - alert: stored on bot instance (if running) or _server_state fallback
    - quotex_connected: stored in _server_state for badge display when bot not running
    """
    if "alert" in patch:
        if _bot_instance is not None:
            _bot_instance.alert = patch["alert"]
        else:
            _server_state["alert"] = patch["alert"]
    if "quotex_connected" in patch:
        _server_state["quotex_connected"] = bool(patch["quotex_connected"])


# ─── Log file tailing ─────────────────────────────────────────

def _tail_log_file():
    """When bot is not a subprocess (e.g. restarted externally), tail its log."""
    log_path = BASE_DIR / "logs" / "quotex_bot.log"
    last_size = 0
    while True:
        try:
            if log_path.exists():
                size = log_path.stat().st_size
                if size > last_size:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        for line in f:
                            line = line.strip()
                            if line:
                                level = ("ERROR" if "ERROR" in line
                                         else "WARNING" if "WARNING" in line else "INFO")
                                socketio.emit("log", {
                                    "message": line, "level": level,
                                    "time": datetime.now().strftime("%H:%M:%S"),
                                })
                    last_size = size
        except Exception:
            pass
        time.sleep(0.5)


# ─── State broadcasting ───────────────────────────────────────

def _broadcast_state():
    """Push state updates to all connected clients every 3 seconds."""
    while True:
        time.sleep(3)
        try:
            state = get_bot_state()
            state["connections"] = get_connection_status()
            socketio.emit("state_update", state)
        except Exception:
            pass


# ─── Telegram auth ────────────────────────────────────────────

@app.route("/api/telegram/connect", methods=["POST"])
def telegram_connect():
    data = request.get_json(force=True)
    phone = _normalize_phone(data.get("phone"))
    if len(phone) < 7:
        return jsonify({
            "success": False,
            "message": "Enter a valid phone number WITH country code "
                       "(e.g. 12342342345 for the US).",
        }), 400

    config = _read_json(BASE_DIR / "config.json")
    api_id   = config.get("telegram", {}).get("api_id")
    api_hash = config.get("telegram", {}).get("api_hash", "")

    result: dict = {"success": False, "message": ""}

    def _run():
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = TelegramClient(StringSession(), api_id, api_hash)

        async def _send():
            await client.connect()
            await client.send_code_request(phone)

        try:
            loop.run_until_complete(_send())
            _tg_client_store["client"] = client
            _tg_client_store["loop"]   = loop
            _tg_client_store["phone"]  = phone
            result["success"] = True
        except Exception as e:
            result["message"] = str(e)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:
                pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=20)

    return jsonify(result)


@app.route("/api/telegram/verify", methods=["POST"])
def telegram_verify():
    data   = request.get_json(force=True)
    code   = (data.get("code") or "").strip()
    client = _tg_client_store.get("client")
    loop   = _tg_client_store.get("loop")
    phone  = _tg_client_store.get("phone")

    if not (client and loop and phone):
        return jsonify({"success": False, "message": "No auth session active — send code first"}), 400

    result: dict = {"success": False, "message": ""}

    def _run():
        async def _verify():
            try:
                await client.sign_in(phone, code)
            except Exception as e:
                # 2FA / password required
                if "password" in str(e).lower():
                    result["needs_password"] = True
                    result["message"] = "2FA password required"
                    return
                result["message"] = str(e)
                return

            session_str  = client.session.save()
            config       = _read_json(BASE_DIR / "config.json")
            session_name = config.get("telegram", {}).get("session_name", "quotex_bot_session")
            (BASE_DIR / f"{session_name}.json").write_text(json.dumps({
                "session_string": session_str,
                "phone_number":   phone,
                "saved_at":       datetime.now().isoformat(),
            }))
            result["success"] = True
            await client.disconnect()
            _tg_client_store.clear()

        loop.run_until_complete(_verify())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=20)

    if result.get("success"):
        socketio.emit("connection_update", {"telegram": True})
    return jsonify(result)


@app.route("/api/telegram/password", methods=["POST"])
def telegram_password():
    """Handle Telegram 2FA password."""
    data     = request.get_json(force=True)
    password = data.get("password", "")
    client   = _tg_client_store.get("client")
    loop     = _tg_client_store.get("loop")
    phone    = _tg_client_store.get("phone")

    if not (client and loop):
        return jsonify({"success": False, "message": "No session active"}), 400

    result: dict = {"success": False, "message": ""}

    def _run():
        async def _pw():
            try:
                await client.sign_in(password=password)
                session_str  = client.session.save()
                config       = _read_json(BASE_DIR / "config.json")
                session_name = config.get("telegram", {}).get("session_name", "quotex_bot_session")
                (BASE_DIR / f"{session_name}.json").write_text(json.dumps({
                    "session_string": session_str,
                    "phone_number":   phone,
                    "saved_at":       datetime.now().isoformat(),
                }))
                result["success"] = True
                await client.disconnect()
                _tg_client_store.clear()
            except Exception as e:
                result["message"] = str(e)

        loop.run_until_complete(_pw())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=20)

    if result.get("success"):
        socketio.emit("connection_update", {"telegram": True})
    return jsonify(result)


@app.route("/api/telegram/disconnect", methods=["POST"])
def telegram_disconnect():
    config       = _read_json(BASE_DIR / "config.json")
    session_name = config.get("telegram", {}).get("session_name", "quotex_bot_session")
    session_file = BASE_DIR / f"{session_name}.json"
    if session_file.exists():
        session_file.unlink()
    socketio.emit("connection_update", {"telegram": False})
    return jsonify({"success": True})


# ─── Quotex connection ────────────────────────────────────────

@app.route("/api/quotex/connect", methods=["POST"])
def quotex_connect():
    """
    Save Quotex credentials from the dashboard form, then verify the connection.
    If Quotex sends a PIN to the user's email, emits quotex_otp_required via
    SocketIO and waits for the user to submit it via /api/quotex/pin.
    """
    data     = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required."}), 400

    # Persist credentials to config.json immediately so the bot can read them
    cfg = _read_json(BASE_DIR / "config.json")
    cfg.setdefault("quotex", {})["email"]    = email
    cfg.setdefault("quotex", {})["password"] = password
    try:
        (BASE_DIR / "config.json").write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not save config: {e}"}), 500

    # Build the OTP callback before starting the thread so the event is ready
    otp_callback = _make_otp_callback()
    result: dict = {"success": False, "message": ""}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _test():
            from bot.config import Config
            from bot.quotex_handler import QuotexHandler
            config  = Config(str(BASE_DIR / "config.json"))
            handler = QuotexHandler(config)
            connected = await handler.connect(otp_callback=otp_callback)
            if connected:
                result["success"] = True
                _patch_state({"quotex_connected": True, "alert": None})
                socketio.emit("connection_update", {"quotex": True})
            else:
                result["message"] = "Login failed — check your email/password."
                _patch_state({"quotex_connected": False})
            await handler.disconnect()

        loop.run_until_complete(_test())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Wait longer (up to 10 min) to allow time for OTP entry
    t.join(timeout=600)

    return jsonify(result)


@app.route("/api/quotex/pin", methods=["POST"])
def quotex_pin():
    """Receive the PIN the user typed and unblock the waiting connect thread."""
    global _otp_value
    data = request.get_json(force=True)
    pin  = (data.get("pin") or "").strip()
    if not pin:
        return jsonify({"success": False, "message": "PIN is required."}), 400
    _otp_value = pin
    _otp_event.set()
    return jsonify({"success": True})


@app.route("/api/quotex/disconnect", methods=["POST"])
def quotex_disconnect():
    """Clear saved Quotex credentials and mark as disconnected."""
    cfg = _read_json(BASE_DIR / "config.json")
    cfg.setdefault("quotex", {})["email"]    = ""
    cfg.setdefault("quotex", {})["password"] = ""
    try:
        (BASE_DIR / "config.json").write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass
    _patch_state({"quotex_connected": False})
    socketio.emit("connection_update", {"quotex": False})
    return jsonify({"success": True})


# ─── SocketIO events ──────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    state = get_bot_state()
    state["connections"] = get_connection_status()
    emit("state_update", state)


# ─── Entry point ─────────────────────────────────────────────

def start_server(host="0.0.0.0", port=5000):
    threading.Thread(target=_tail_log_file,    daemon=True).start()
    threading.Thread(target=_broadcast_state,  daemon=True).start()
    print(f"\n  QUOTEX1 Dashboard → http://localhost:{port}\n")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
