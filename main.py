"""
Quotex Telegram Trading Bot

Entry point: python main.py
  → Starts the dashboard only (http://localhost:5000)
  → Click START in the dashboard to launch the bot
  → Click STOP to shut it down
"""

import asyncio
import sys
import logging
import os
import certifi
from datetime import datetime
from bot.config import Config
from bot.telegram_handler import TelegramHandler
from bot.quotex_handler import QuotexHandler

# Windows: fix event loop and console encoding
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

os.environ['SSL_CERT_FILE'] = certifi.where()


class TradingBot:
    """Trading bot — holds all live state as plain attributes on real objects."""

    def __init__(self):
        self.config           = None
        self.logger           = None
        self.telegram_handler = None
        self.quotex_handler   = None
        self.running          = False
        self.alert: str | None = None   # last error message for the dashboard

    # ── Start ────────────────────────────────────────────────

    async def start(self) -> bool:
        try:
            if not os.path.exists('config.json'):
                print("config.json not found — open Settings to configure.")
                return False

            self.config = Config()
            self._setup_logging()
            self.logger = logging.getLogger(__name__)
            self.logger.info("=== Quotex Telegram Trading Bot Starting ===")

            if not self.config.validate():
                self.logger.error("Configuration validation failed.")
                return False

            self.quotex_handler   = QuotexHandler(self.config)
            self.telegram_handler = TelegramHandler(self.config)

            # Register with the dashboard so it can read live state from memory
            try:
                from bot import server as _srv
                _srv._bot_instance = self
            except Exception:
                pass

            if not await self.telegram_handler.initialize(self.quotex_handler):
                self.logger.error("Failed to initialize Telegram client")
                return False

            if not await self.telegram_handler.test_connection():
                self.logger.error("Telegram connection test failed")
                return False

            # Quotex connection is optional at start — credentials may be set later
            if self.quotex_handler._has_credentials():
                if await self.quotex_handler.connect():
                    self.logger.info("Quotex connected.")
                    self.alert = None
                else:
                    self.logger.error("Quotex login failed. Check credentials in Settings.")
                    self.alert = "Quotex login failed — check your credentials."
            else:
                self.logger.warning(
                    "Quotex credentials not set. "
                    "Open Settings → Quotex Account and click Connect."
                )
                self.alert = None  # JS shows dynamic warning from connection state

            self.running = True
            self.logger.info("Bot started — monitoring Telegram for signals.")
            asyncio.ensure_future(self._health_monitor())
            asyncio.ensure_future(self._balance_monitor())
            await self.telegram_handler.start_monitoring(self._handle_signal)
            return True

        except KeyboardInterrupt:
            await self.shutdown()
            return True
        except Exception as e:
            msg = f"Critical error: {e}"
            if self.logger:
                self.logger.error(msg)
            else:
                print(msg)
            return False

    # ── Signal handler ────────────────────────────────────────

    async def _handle_signal(self, signal):
        try:
            self.logger.info(f"Received signal: {signal['type']} for {signal.get('asset')}")
            if not self.quotex_handler.is_connected:
                self.logger.warning(
                    "Signal ignored — Quotex not connected. "
                    "Connect via Settings -> Quotex Account."
                )
                return
            await self.telegram_handler.executor.schedule_signal(signal)
        except Exception as e:
            self.logger.error(f"Error handling signal: {e}")

    # ── Health monitor ────────────────────────────────────────

    async def _health_monitor(self):
        """Check Quotex every 60 s (not connected) or 3600 s (connected)."""
        self.logger.info("Health monitor started")
        while self.running:
            interval = 3600 if self.quotex_handler.is_connected else 60
            await asyncio.sleep(interval)
            if not self.running:
                break
            try:
                if not self.quotex_handler.is_connected:
                    if self.quotex_handler._has_credentials():
                        self.logger.info("Health monitor: attempting Quotex connect...")
                        if await self.quotex_handler.connect():
                            self.logger.info("Health monitor: Quotex connected OK")
                            self.alert = None
                        else:
                            self.logger.error("Health monitor: Quotex connect failed")
                else:
                    self.logger.info("Health monitor: checking Quotex...")
                    if not self.quotex_handler._connection_alive():
                        self.logger.warning("Health monitor: Quotex disconnected — reconnecting...")
                        self.alert = "Quotex disconnected — reconnecting..."
                        if await self.quotex_handler.connect():
                            self.logger.info("Health monitor: reconnected OK")
                            self.alert = None
                        else:
                            self.alert = "Quotex reconnect failed — reconnect via Settings"
                            self.logger.error(f"Health monitor: {self.alert}")
                    else:
                        self.logger.info("Health monitor: Quotex OK")
            except Exception as e:
                self.logger.error(f"Health monitor error: {e}")
                self.alert = f"Health monitor error: {e}"

    # ── Balance monitor ───────────────────────────────────────

    async def _balance_monitor(self):
        """
        Refresh the balance every 10 s so the dashboard shows live figures even
        when no trades are running. pyquotex serves get_balance() from a cache the
        WebSocket keeps updated, so this costs nothing on the wire.
        """
        while self.running:
            await asyncio.sleep(10)
            if not self.running:
                break
            try:
                if self.quotex_handler.is_connected:
                    await self.quotex_handler._refresh_balance_stats()
            except Exception as e:
                self.logger.debug(f"Balance monitor: {e}")

    # ── Shutdown ──────────────────────────────────────────────

    async def shutdown(self):
        if self.running:
            self.running = False
            self.alert   = None
            if self.logger:
                self.logger.info("Shutting down trading bot...")

            if self.quotex_handler:
                await self.quotex_handler.disconnect()

            if self.telegram_handler:
                await self.telegram_handler.disconnect()

            # Deregister from server
            try:
                from bot import server as _srv
                _srv._bot_instance = None
            except Exception:
                pass

            if self.logger:
                self.logger.info("Bot shutdown complete")

    # ── Logging setup ─────────────────────────────────────────

    def _setup_logging(self):
        from logging.handlers import RotatingFileHandler

        os.makedirs('logs', exist_ok=True)
        log_level = getattr(logging, self.config.logging.log_level.upper(), logging.INFO)
        log_file  = f"logs/{self.config.logging.log_file}"

        fmt_console = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s', '%H:%M:%S'
        )
        fmt_file = logging.Formatter(
            '%(asctime)s | %(name)s | %(levelname)s | %(message)s', '%Y-%m-%d %H:%M:%S'
        )

        root = logging.getLogger()
        root.setLevel(log_level)
        root.handlers.clear()

        # Console — respects log_level
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt_console)
        console.setLevel(log_level)
        root.addHandler(console)

        # File — same level as console (not hardcoded DEBUG)
        # Python's logging propagation bypasses the parent logger's level when
        # checking handler eligibility, so the handler level must be set correctly.
        fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
        fh.setFormatter(fmt_file)
        fh.setLevel(log_level)
        root.addHandler(fh)

        # ── Silence noisy third-party libraries ───────────────────────────────
        # websockets streams every single WS frame at DEBUG. Regardless of the
        # configured log_level, limit it to WARNING so frames never appear.
        _silent_at_warning = [
            'websockets', 'websockets.client', 'websockets.server',
            'websockets.legacy', 'websockets.connection',
            'telethon.network', 'telethon.extensions', 'telethon.crypto',
            'werkzeug',
            'asyncio',
            'pyasn1',
            'urllib3', 'charset_normalizer',
        ]
        for name in _silent_at_warning:
            logging.getLogger(name).setLevel(logging.WARNING)

        # telethon connection-level messages are useful at INFO+
        logging.getLogger('telethon').setLevel(max(log_level, logging.INFO))

        # ── Trade and signal loggers — always INFO ────────────────────────────
        trade_log = logging.getLogger('trades')
        th = RotatingFileHandler(
            f"logs/trades_{datetime.now().strftime('%Y-%m-%d')}.log",
            maxBytes=5*1024*1024, backupCount=10, encoding='utf-8'
        )
        th.setFormatter(fmt_file)
        trade_log.addHandler(th)
        trade_log.setLevel(logging.INFO)

        sig_log = logging.getLogger('signals')
        sh = RotatingFileHandler(
            'logs/received_signals.log', maxBytes=5*1024*1024, backupCount=10, encoding='utf-8'
        )
        sh.setFormatter(fmt_file)
        sig_log.addHandler(sh)
        sig_log.setLevel(logging.INFO)

        logging.getLogger(__name__).info(
            "Logging initialized — level: %s  file: %s",
            self.config.logging.log_level, log_file
        )


# ─── Entry point ─────────────────────────────────────────────────────────────
# python main.py → starts ONLY the dashboard.
# The bot starts when the user clicks START in the dashboard.

if __name__ == "__main__":
    from bot.server import start_server
    start_server(host="0.0.0.0", port=5000)
