"""
Quotex trading handler using pyquotex — no browser required.

Credentials resolved in this priority order:
  1. Environment variables  QUOTEX_EMAIL  /  QUOTEX_PASSWORD
  2. config.json → quotex.email  /  quotex.password  (set via Settings dashboard)

Install:
  pip install git+https://github.com/cleitonleonel/pyquotex.git
"""

import asyncio
import os
import time
import logging
from datetime import datetime
from typing import Optional, Any
from .config import Config

try:
    from pyquotex.stable_api import Quotex  # type: ignore[import]
except ImportError:
    Quotex = None


class QuotexHandler:
    def __init__(self, config: Config):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.client: Optional[Any] = None
        self.is_connected = False
        self.last_place_latency: Optional[float] = None  # measured seconds from buy() call to confirmation
        self.balance: Optional[float] = None              # last known account balance (dashboard)
        self.last_trade: Optional[dict] = None            # last placed trade + its result (dashboard)
        self.daily_trades = 0
        self.daily_loss = 0.0          # realized drawdown today (opening balance − current)
        self.wins = 0                  # settled winning trades today
        self.losses = 0                # settled losing trades today
        self.daily_pnl = 0.0           # realized profit/loss today (from check_win)
        self.active_trades = 0
        self._trade_date = None
        self._day_start_balance: Optional[float] = None   # balance snapshot at 00:00

    # ─────────────────────────────────────────────────────────
    #  CONNECTION
    # ─────────────────────────────────────────────────────────

    async def connect(self, otp_callback=None) -> bool:
        if Quotex is None:
            self.logger.error(
                "pyquotex is not installed. "
                "Run: pip install git+https://github.com/cleitonleonel/pyquotex.git"
            )
            return False

        email    = os.getenv("QUOTEX_EMAIL")    or self.config.quotex.email
        password = os.getenv("QUOTEX_PASSWORD") or self.config.quotex.password

        if not email or not password:
            self.logger.error(
                "Quotex credentials missing. "
                "Enter them in Settings → Quotex Account, or set "
                "QUOTEX_EMAIL / QUOTEX_PASSWORD environment variables."
            )
            return False

        try:
            self.logger.info(f"Connecting to Quotex as {email}...")
            self.client = Quotex(
                email=email,
                password=password,
                on_otp_callback=otp_callback,
            )
            check, reason = await self.client.connect()

            if not check:
                # pyquotex's _check_connect has a short (2 s) timeout — WS auth may arrive later.
                self.logger.info("Waiting up to 8 s for WebSocket auth to settle...")
                for _ in range(8):
                    await asyncio.sleep(1)
                    if await self.client.check_connect():
                        check = True
                        break

            if not check:
                # Still failing — likely a stale/rejected WebSocket token.
                # Clear just the token from session.json so pyquotex fetches a fresh one.
                # Cookies are preserved so HTTP re-login is silent (no PIN needed).
                self.logger.info(
                    "Clearing stale WebSocket token and retrying in 5 s "
                    "(rate-limit cooldown)..."
                )
                self._clear_session_token(email)
                await asyncio.sleep(5)

                # Re-create the client so it reads the updated session file
                self.client = Quotex(
                    email=email,
                    password=password,
                    on_otp_callback=otp_callback,
                )
                check, reason = await self.client.connect()

                if not check:
                    for _ in range(8):
                        await asyncio.sleep(1)
                        if await self.client.check_connect():
                            check = True
                            break

            if not check:
                self.logger.error(f"Quotex login failed: {reason}")
                return False

            # Switch demo / live account
            account_map = {"demo": "PRACTICE", "live": "REAL"}
            target = account_map.get(self.config.trading.account_type.lower(), "PRACTICE")
            await self.client.change_account(target)
            self.logger.info(f"Account type: {target}")

            balance = await self.client.get_balance()
            self.logger.info(f"Connected. Balance: ${balance:.2f}")
            self.is_connected = True
            await self._refresh_balance_stats()   # snapshot the day's opening balance
            return True

        except Exception as e:
            self.logger.error(f"Error connecting to Quotex: {e}")
            return False

    def _clear_session_token(self, email: str):
        """
        Remove the stale WebSocket authorization token from pyquotex's session.json.
        Leaves HTTP cookies intact so the next connect can re-login silently.
        """
        import json
        from pathlib import Path
        session_file = Path("session.json")
        if not session_file.exists():
            return
        try:
            sessions = json.loads(session_file.read_text())
            if email in sessions:
                sessions[email]["token"] = None
                session_file.write_text(json.dumps(sessions, indent=4))
                self.logger.info(f"Cleared stale WebSocket token for {email}")
        except Exception as e:
            self.logger.warning(f"Could not clear session token: {e}")

    def _has_credentials(self) -> bool:
        """Return True if email + password are available (env var or config)."""
        email    = os.getenv("QUOTEX_EMAIL")    or self.config.quotex.email
        password = os.getenv("QUOTEX_PASSWORD") or self.config.quotex.password
        return bool(email and password)

    def _connection_alive(self) -> bool:
        """Return True if the connection is marked as live."""
        return self.is_connected and self.client is not None

    async def disconnect(self):
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.is_connected = False
        self.logger.info("Quotex disconnected.")

    # ─────────────────────────────────────────────────────────
    #  ASSET SELECTION  (no-op — asset is specified in buy() at trade time)
    # ─────────────────────────────────────────────────────────

    async def select_asset(self, asset: str):
        """
        Pre-warm the price stream for the asset.
        pyquotex's buy() waits for realtime_price[asset] to be populated.
        Starting the candle stream here gives the server time to begin
        delivering ticks before the trade button is pressed.
        """
        formatted = self._format_asset(asset)
        self.logger.info(f"Asset queued: {formatted} — pre-subscribing price stream")
        try:
            await self.client.start_candles_stream(formatted, 60)
        except Exception as e:
            self.logger.warning(f"Pre-subscribe failed for {formatted}: {e}")

    async def _is_asset_open(self, asset_name: str) -> bool:
        """
        Return True if the asset is tradable right now (market open).
        check_asset_open() returns (raw, (id, name, is_open)); is_open is the flag.
        On a check failure, return True so a transient error doesn't block a real
        trade — the buy() timeout in perform_trade() is the backstop for hangs.
        """
        try:
            _, status_info = await self.client.check_asset_open(asset_name)
            return bool(status_info and status_info[2])
        except Exception as e:
            self.logger.warning(f"Could not verify if {asset_name} is open: {e}")
            return True

    # ─────────────────────────────────────────────────────────
    #  PRE-CONFIGURE  (timed signals)
    # ─────────────────────────────────────────────────────────

    async def pre_configure_trade(self, expiry: Optional[str] = None) -> float:
        """
        Called immediately after asset selection for timed signals.
        With the API there is no UI to pre-fill; just calculate and return the
        amount so it is ready the moment the entry time arrives.
        """
        amount = await self._calc_amount()

        # Warm the HTTPS path buy() uses. pyquotex's buy() calls get_server_time()
        # -> get_profile() -> GET /api/v1/cabinets/digest on EVERY order; doing one
        # here, while there is still time to spare, leaves a live keep-alive TLS
        # connection so the call at entry time is a warm round trip, not a
        # cold handshake. Failures are ignored — this is purely an optimisation.
        try:
            t0 = time.monotonic()
            await self.client.get_server_time()
            self.logger.info(f"Order path pre-warmed in {time.monotonic() - t0:.2f}s")
        except Exception as e:
            self.logger.debug(f"Pre-warm skipped: {e}")

        self.logger.info(
            f"Pre-configured: ${amount:.2f} | duration: {expiry or '00:01:00'} "
            f"— ready, waiting for entry time."
        )
        return amount

    # ─────────────────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────────────────

    async def _calc_amount(self) -> float:
        """
        Calculate the BASE trade amount: percent of balance, or fixed dollars.
        Martingale step sizing (doubling) is handled by the recovery loop in
        perform_trade(), not here — this always returns the step-1 base.
        """
        if self.config.trading.risk_mode == "percent":
            try:
                balance = await self.client.get_balance()
                if balance:
                    amount = round(balance * (self.config.trading.risk_amount / 100), 2)
                    self.logger.info(
                        f"Balance: ${balance:.2f} | "
                        f"{self.config.trading.risk_amount}% = ${amount:.2f}"
                    )
                    return amount
            except Exception:
                pass
            self.logger.warning("Could not read balance — using fixed risk_amount.")

        return self.config.trading.risk_amount

    @staticmethod
    def _format_asset(asset: str) -> str:
        """
        Convert signal asset name to pyquotex format.
          EURUSD     → EURUSD
          EURUSD-OTC → EURUSD_otc
        """
        if asset.upper().endswith("-OTC"):
            return asset[:-4].upper() + "_otc"
        return asset.upper()

    @staticmethod
    def _expiry_to_seconds(expiry: str) -> int:
        """'00:05:00' → 300"""
        try:
            h, m, s = expiry.split(":")
            return int(h) * 3600 + int(m) * 60 + int(s)
        except Exception:
            return 60

    def _reset_daily_counters_if_new_day(self):
        today = datetime.now().date()
        if self._trade_date != today:
            self.daily_trades       = 0
            self.daily_loss         = 0.0
            self.wins               = 0
            self.losses             = 0
            self.daily_pnl          = 0.0
            self._day_start_balance = None   # re-snapshot opening balance on the new day
            self._trade_date        = today

    async def _refresh_balance_stats(self):
        """
        Snapshot the day's opening balance at 00:00, then derive realized daily
        P&L and drawdown from the live account balance:
            daily_pnl  = current_balance − opening_balance
            daily_loss = max(0, −daily_pnl)     ← what max_daily_loss is checked against
        Both are sourced from the balance so they reflect actual money, not stakes.
        """
        self._reset_daily_counters_if_new_day()
        try:
            balance = await self.client.get_balance()
        except Exception as e:
            self.logger.warning(f"Could not read balance for daily P&L/loss: {e}")
            return
        if balance is None:
            return
        self.balance = balance
        if self._day_start_balance is None:
            self._day_start_balance = balance
            self.logger.info(f"Opening balance for {self._trade_date}: ${balance:.2f}")

        # In percent risk mode, express P&L / loss as a % of the opening balance
        # so the max_daily_loss limit is also read as a percentage. Otherwise dollars.
        gross = balance - self._day_start_balance
        if self.config.trading.risk_mode == "percent" and self._day_start_balance:
            self.daily_pnl = round(gross / self._day_start_balance * 100, 2)
        else:
            self.daily_pnl = round(gross, 2)
        self.daily_loss = max(0.0, -self.daily_pnl)

    # ─────────────────────────────────────────────────────────
    #  MARTINGALE
    # ─────────────────────────────────────────────────────────

    async def _get_trade_result(self, order_id: Any, duration_secs: int):
        """
        Uses check_win() which waits event-driven until the trade closes
        (up to 300s).
        Returns a (won, profit) tuple where won is True = win, False = loss,
        None = unknown, and profit is the realized P&L for the trade
        (positive on a win, negative on a loss, 0.0 if unknown).
        """
        try:
            win_status, profit = await self.client.check_win(str(order_id), duration_secs)
            profit = float(profit or 0.0)
            won = True if win_status == "win" else False if win_status else None
            self.logger.info(f"Trade result: {win_status}  profit={profit:.2f}")
            return won, profit
        except Exception as e:
            self.logger.error(f"Could not read trade result: {e}")
            return None, 0.0

    def _record_result(self, won: Optional[bool], profit: float):
        """
        Update win/loss counters from a settled trade.
        Daily P&L / loss are NOT accumulated here — they are derived from the live
        balance in _refresh_balance_stats() (dollars, or % in percent risk mode).
        """
        self._reset_daily_counters_if_new_day()
        if won is True:
            self.wins += 1
        elif won is False:
            self.losses += 1
        if self.last_trade is not None:
            self.last_trade["result"] = "win" if won is True else "loss" if won is False else "unknown"
            self.last_trade["profit"] = round(profit, 2)
        self.logger.info(
            f"Stats: {self.wins}W / {self.losses}L  |  last trade profit: ${profit:.2f}"
        )

    async def _settle_and_record(self, order_id: Any, duration_secs: int):
        """Background settle: wait for the trade to close, record win/loss + P&L."""
        won, profit = await self._get_trade_result(order_id, duration_secs)
        self._record_result(won, profit)
        await self._refresh_balance_stats()

    async def _place_one(
        self,
        asset_name: str,
        direction: str,
        period_secs: int,
        amount: float,
        expiry: Optional[str],
        wait_result: bool,
    ):
        """
        Place a single trade and optionally wait for its result.
        Returns (placed, won):
          placed = False        → the order never went on (timeout / rejected)
          won    = True/False/None when wait_result is True; always None otherwise
        """
        self.logger.info(
            f"Placing trade: {asset_name} {direction.upper()} "
            f"${amount:.2f}  duration: {period_secs}s  (runs {period_secs}s from entry)"
        )

        # Signals come from Telegram, not from price data, so the realtime price
        # stream is NOT needed to decide a trade. buy() would otherwise block in
        # start_realtime_price() waiting for live ticks and time out valid trades.
        # Seed one price entry so that check passes instantly and the order is sent
        # immediately on the already-selected pair. (buy() ignores the value — the
        # broker fills at its own market price; this only skips the pointless wait.)
        try:
            self.client.api.realtime_price.setdefault(asset_name, []).append(
                {"time": 0, "price": 0}
            )
        except Exception as _e:
            self.logger.debug(f"Could not seed price stream: {_e}")

        # time_mode="TIMER": duration option (optionType 100, expiry sent as a
        # RELATIVE number of seconds). The trade runs for exactly `period_secs`
        # from the moment the broker accepts it — it is NOT snapped to the next
        # candle boundary, so a 1-minute signal is always a full 1 minute and a
        # late placement can never land on the wrong candle. Bounded timeout so a
        # stuck buy() can't hold the concurrency slot forever.
        fired_at = time.monotonic()
        try:
            status, buy_info = await asyncio.wait_for(
                self.client.buy(amount, asset_name, direction, period_secs, time_mode="TIMER"),
                timeout=20,
            )
        except asyncio.TimeoutError:
            self.logger.error(
                f"Trade NOT placed for {asset_name} — no order confirmation from "
                f"Quotex within 20s."
            )
            return False, None

        if not status:
            self.logger.error(f"Trade rejected by Quotex — NOT placed: {buy_info}")
            return False, None

        self.last_place_latency = time.monotonic() - fired_at
        self.daily_trades += 1
        order_id = buy_info.get("id") if isinstance(buy_info, dict) else None
        self.last_trade = {
            "asset":     asset_name,
            "direction": direction,
            "amount":    amount,
            "duration":  period_secs,
            "latency":   round(self.last_place_latency, 2),
            "at":        datetime.now().strftime("%H:%M:%S"),
            "result":    None,     # filled in by _record_result when it settles
            "profit":    None,
        }
        self.logger.info(
            f"Trade #{self.daily_trades} placed — {asset_name} {direction.upper()} "
            f"${amount:.2f} | id={order_id} | fill latency {self.last_place_latency:.2f}s "
            f"| Daily: {self.daily_trades}/{self.config.trading.max_daily_trades}"
        )
        import json as _json, logging as _logging
        _logging.getLogger('trades').info(
            f"QUOTEX_TRADE_EXECUTED | {_json.dumps({'asset': asset_name, 'direction': direction, 'amount': amount, 'expiry': expiry, 'order_id': str(order_id)})}"
        )

        if order_id is None:
            self.logger.warning(
                "Trade placed but broker returned no order id — win/loss not trackable."
            )
            return True, None

        # check_win timeout: the option closes exactly period_secs after the fill.
        result_timeout = period_secs + 30
        if wait_result:
            won, profit = await self._get_trade_result(order_id, result_timeout)
            self._record_result(won, profit)
            await self._refresh_balance_stats()
            return True, won
        # Non-martingale: record in the background so the next signal isn't delayed.
        asyncio.ensure_future(self._settle_and_record(order_id, result_timeout))
        return True, None

    # ─────────────────────────────────────────────────────────
    #  MAIN TRADE FLOW
    # ─────────────────────────────────────────────────────────

    async def perform_trade(
        self,
        symbol: str,
        direction: str,
        expiry: Optional[str] = None,
        pre_configured_amount: Optional[float] = None,
    ) -> bool:
        if not self.is_connected or not self.client:
            self.logger.error("QuotexHandler not connected.")
            return False

        # Refresh realized daily P&L / drawdown from the live balance before gating.
        await self._refresh_balance_stats()
        loss_unit = "%" if self.config.trading.risk_mode == "percent" else "$"

        if self.daily_trades >= self.config.trading.max_daily_trades:
            self.logger.warning(
                f"Daily trade limit reached ({self.config.trading.max_daily_trades}). Skipping."
            )
            return False

        if (self.config.trading.max_daily_loss_enabled
                and self.daily_loss >= self.config.trading.max_daily_loss):
            self.logger.warning(
                f"Daily loss limit reached ({self.daily_loss:.2f}{loss_unit} "
                f">= {self.config.trading.max_daily_loss:.2f}{loss_unit}). Skipping."
            )
            return False

        if self.active_trades >= self.config.trading.max_concurrent_trades:
            self.logger.warning(
                f"Max concurrent trades reached ({self.config.trading.max_concurrent_trades}). Skipping."
            )
            return False

        asset_name = self._format_asset(symbol)

        # Verify the asset is actually tradable RIGHT NOW before claiming a slot.
        # Otherwise buy() blocks waiting for a realtime price that never arrives
        # (e.g. a non-OTC pair when its market is closed), holding the concurrency
        # slot and silently blocking every later trade. Fail fast and loudly here.
        if not await self._is_asset_open(asset_name):
            self.logger.error(
                f"Asset {asset_name} is CLOSED / unavailable right now — "
                f"trade NOT placed, skipping (slot not held)."
            )
            return False

        # Claim slot before any await so concurrent callers see the correct count
        self.active_trades += 1

        try:
            base_amount = (
                pre_configured_amount
                if pre_configured_amount is not None
                else await self._calc_amount()
            )
            period_secs = self._expiry_to_seconds(expiry or "00:01:00")   # M1=60, M5=300

            # ── No martingale: place once, settle in the background ───────────
            if not self.config.trading.martingale_enabled:
                placed, _ = await self._place_one(
                    asset_name, direction, period_secs, base_amount, expiry,
                    wait_result=False,
                )
                return placed

            # ── Auto-Martingale: one sequence per signal, confined to THIS pair ──
            # On a loss, immediately re-enter the SAME pair in the SAME direction at
            # x multiplier, up to `steps` trades. A win ends the sequence. NOTHING is
            # carried over: however the sequence ends — won, lost, or cut short by a
            # limit — the next Telegram signal starts at the base amount on its own
            # pair, so a losing streak on one symbol never follows you to another.
            #   e.g. steps=2, mult=2, base=$10 -> $10, then $20 if it loses. Done.
            mult   = self.config.trading.martingale_multiplier
            steps  = max(1, int(self.config.trading.martingale_steps))
            amount = base_amount

            for step in range(1, steps + 1):
                placed, won = await self._place_one(
                    asset_name, direction, period_secs, amount, expiry,
                    wait_result=True,
                )
                if not placed:
                    # Couldn't place — nothing to carry, the sequence just ends.
                    return step > 1

                if won is True:
                    self.logger.info(
                        f"Martingale: WIN at ${amount:.2f} on step {step}/{steps} — "
                        f"sequence complete, back to base ${base_amount:.2f}."
                    )
                    return True
                if won is None:
                    self.logger.warning(
                        "Martingale: result unknown — ending this sequence "
                        "(no recovery trade on an unread result)."
                    )
                    return True

                # ── Loss ──
                if step >= steps:
                    self.logger.warning(
                        f"Martingale: all {steps} step(s) lost on {asset_name} — "
                        f"sequence over. The next signal starts at base "
                        f"${base_amount:.2f} on its own pair."
                    )
                    return True

                amount = round(amount * mult, 2)

                # Daily caps are checked before every recovery trade. Nothing is
                # carried forward — the sequence simply stops here.
                if self.daily_trades >= self.config.trading.max_daily_trades:
                    self.logger.warning(
                        f"Martingale: daily trade limit reached — recovery at "
                        f"${amount:.2f} abandoned."
                    )
                    return True
                if (self.config.trading.max_daily_loss_enabled
                        and self.daily_loss >= self.config.trading.max_daily_loss):
                    self.logger.warning(
                        f"Martingale: daily loss limit reached — recovery at "
                        f"${amount:.2f} abandoned."
                    )
                    return True

                self.logger.info(
                    f"Martingale: LOSS — re-entering {asset_name} {direction.upper()} "
                    f"at ${amount:.2f} (step {step + 1}/{steps})."
                )

        except Exception as e:
            self.logger.error(f"Error placing trade on {symbol}: {e}")
            return False
        finally:
            self.active_trades = max(0, self.active_trades - 1)
