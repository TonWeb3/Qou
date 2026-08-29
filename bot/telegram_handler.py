"""
Telegram integration for signal reception.

Supported channel format (single message, all fields inline):

    🚀 One Minute Trade (1 MINT) 🚀

    🀄 EURJPY-OTC
    ⚡️ TIME ZONE UTC +5:30
    ⌚ 02:51:00 ENTRY TIME
    🔴 DOWN 🔴

    💎 Premium Signal 💎
    ━━━━━━━━━━━━━━━
    (promo footer — ignored)

The message may use stylised Unicode (bold / sans-serif) letters and digits;
they are normalised to plain ASCII before parsing. Only the block ABOVE the
first heavy divider (━) is parsed, so the promo footer can never be mistaken
for signal data. Fields extracted:
  • Pair       → two 3-letter groups, optional "-OTC"      (EURJPY-OTC)
  • Timeframe  → "(N MIN…)" → expiry, default 1 minute     (1 → 00:01:00)
  • Timezone   → "UTC +5:30"  (required to schedule the entry, no UTC fallback)
  • Entry time → "HH:MM[:SS] ENTRY TIME"  (waits until this moment, tz-adjusted)
  • Direction  → UP/CALL/BUY → call,  DOWN/PUT/SELL → put  (🟢/🔴 also accepted)

Multiple channels can be enabled simultaneously via telegram.channels in config.json.
"""

import asyncio
import logging
import re
import os
import json
import unicodedata
from datetime import datetime
import pytz
from typing import Optional, Dict, Any, List
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.tl.types import Channel, Chat
from .quotex_handler import QuotexHandler
from .config import Config, ChannelConfig


class SignalParser:
    """Parses the single-message premium signal format (see module docstring)."""

    def __init__(self, config: Config):
        self.logger = logging.getLogger(__name__)
        self.config = config

    @staticmethod
    def _normalize(text: str) -> str:
        """
        Convert stylised Unicode (mathematical bold / sans-serif) letters and
        digits back to plain ASCII so the field regexes match.
        NFKC maps e.g. 𝗢→O, 𝐓→T, 𝟭→1, 𝟓→5.
        """
        return unicodedata.normalize('NFKC', text)

    def parse_signal(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Parse one premium-format signal message.
        Returns a signal dict, or None if the message is not a valid signal
        (it must contain both a currency pair and a direction).
        """
        norm = self._normalize(text)
        # Everything we need is in the block above the first heavy divider (━).
        # The promo footer below it can contain stray uppercase words
        # (OFFICIAL, REGISTRATION…) that must not be mistaken for a pair.
        head = norm.split('━')[0]

        # ── Pair: two 3-letter groups (optionally slash-separated) + optional OTC ──
        pair_m = re.search(r'\b([A-Z]{3})\s*/?\s*([A-Z]{3})\b\s*(-?\s*OTC\b)?', head)
        if not pair_m:
            return None
        asset = (pair_m.group(1) + pair_m.group(2)).upper()
        if pair_m.group(3):
            asset += '-OTC'

        # ── Direction ──
        if re.search(r'\b(UP|CALL|BUY)\b', head, re.IGNORECASE) or '🟢' in head:
            direction = 'call'
        elif re.search(r'\b(DOWN|PUT|SELL)\b', head, re.IGNORECASE) or '🔴' in head:
            direction = 'put'
        else:
            return None

        # ── Expiry from "(N MIN…)" — default 1 minute ──
        expiry = '00:01:00'
        tf_m = re.search(r'(\d+)\s*MIN', head, re.IGNORECASE)
        if tf_m:
            expiry = f'00:{int(tf_m.group(1)):02d}:00'

        # ── Entry time — "HH:MM[:SS] ENTRY TIME" (or "ENTRY TIME: HH:MM") ──
        entry_time = None
        ent_m = (
            re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)\s*ENTRY', head, re.IGNORECASE)
            or re.search(r'ENTRY\s*TIME\s*[:\-]?\s*(\d{1,2}:\d{2}(?::\d{2})?)', head, re.IGNORECASE)
        )
        if ent_m:
            t = ent_m.group(1)
            entry_time = t if t.count(':') == 2 else t + ':00'

        # ── Timezone — "UTC +5:30" (required by the scheduler; no UTC fallback) ──
        timezone_str = None
        tz_m = re.search(r'(UTC\s*[+-]\s*\d{1,2}:\d{2}|[+-]\d{1,2}:\d{2})', head, re.IGNORECASE)
        if tz_m:
            timezone_str = tz_m.group(1).replace(' ', '').upper()

        signal = {
            'type':       'trade',
            'asset':      asset,
            'direction':  direction,
            'expiry':     expiry,
            'entry_time': entry_time,
            'timezone':   timezone_str,
            'parsed_at':  datetime.now().isoformat(),
        }
        self.logger.info(
            f"[signal] {asset} {direction.upper()}  "
            f"entry: {entry_time} ({timezone_str})  expiry: {expiry}"
        )
        return signal


class SignalExecutor:
    """Executes trade signals — time-scheduled when the signal carries an entry time."""

    def __init__(self, config: Config, quotex_handler: QuotexHandler):
        self.config          = config
        self.quotex_handler  = quotex_handler
        self.logger          = logging.getLogger(__name__)
        self.pending_signals: Dict[str, Any] = {}

    async def schedule_signal(self, signal: Dict[str, Any]) -> bool:
        try:
            if signal['type'] == 'trade':
                sid = f"trade_{signal['asset']}_{signal['direction']}_{signal['parsed_at']}"
                self.pending_signals[sid] = {**signal, 'executed': False}
                await self._execute(sid)
                return True
            return False
        except Exception as e:
            self.logger.error(f"Error scheduling signal: {e}")
            return False

    async def _execute(self, sid: str):
        try:
            signal = self.pending_signals.get(sid)
            if not signal or signal.get('executed'):
                return

            asset     = signal['asset']
            direction = signal['direction']
            expiry    = signal.get('expiry')

            # ── Step 1: Select the pair immediately ───────────────
            try:
                await self.quotex_handler.select_asset(asset)
            except Exception as e:
                self.logger.error(f"Could not select asset {asset}: {e}")

            # ── Step 1b: For timed signals, work out the wait first ──
            # The timezone MUST come from the signal — there is no UTC fallback,
            # so a trade is never fired at the wrong moment on an assumed zone.
            wait_secs = None
            if signal.get('entry_time'):
                wait_secs = self._calc_wait(signal['entry_time'], signal.get('timezone'))
                if wait_secs is None:
                    self.logger.error(
                        f"Skipping {asset} {direction.upper()} — entry time "
                        f"'{signal['entry_time']}' cannot be scheduled "
                        f"(missing or invalid timezone in signal)."
                    )
                    return
                if wait_secs < -30:
                    self.logger.warning(
                        f"Entry time already passed by {abs(wait_secs):.0f}s — skipping."
                    )
                    return

            # ── Step 1c: Pre-set amount + expiry for timed signals ─
            # Everything is configured upfront; at entry time only the button is clicked.
            pre_configured_amount = None
            if signal.get('entry_time'):
                try:
                    pre_configured_amount = await self.quotex_handler.pre_configure_trade(expiry)
                except Exception as e:
                    self.logger.error(f"Pre-configure failed — will retry at entry time: {e}")

            # ── Step 2: Wait until entry time (minus the latency lead) ──────
            # The option now runs for a fixed duration from the fill, so the window
            # is whatever the broker accepts, NOT a candle boundary — the trade must
            # therefore land ON the entry second, not before it. early_entry_seconds
            # is pure latency compensation: it should equal the observed order
            # round trip (logged as "fill latency"), typically a fraction of a second.
            if signal.get('entry_time'):
                early = max(0.0, float(self.config.quotex.early_entry_seconds))
                self.logger.info(
                    f"Pair {asset} armed. Waiting {max(0.0, wait_secs - early):.1f}s "
                    f"for entry {signal['entry_time']} (lead {early:.2f}s)..."
                )
                # Sleep against the wall clock in chunks and re-measure each time:
                # one long sleep can drift (or the machine can suspend), which would
                # push the fill past the entry second.
                while True:
                    remaining = self._calc_wait(signal['entry_time'], signal.get('timezone'))
                    if remaining is None:
                        return
                    delay = remaining - early
                    if delay <= 0.0:
                        break
                    await asyncio.sleep(delay if delay < 1.0 else min(delay - 0.5, 30.0))
                late = -(self._calc_wait(signal['entry_time'], signal.get('timezone')) or 0.0)
                self.logger.info(
                    f"Firing now — {abs(late):.2f}s "
                    f"{'after' if late > 0 else 'before'} entry {signal['entry_time']}."
                )

            # ── Step 4: Place trade ───────────────────────────────
            self.logger.info(f"Executing: {asset} {direction.upper()}")
            success = await self.quotex_handler.perform_trade(
                asset, direction, expiry=expiry,
                pre_configured_amount=pre_configured_amount,
            )

            if success:
                signal['executed'] = True
                self.logger.info(f"Trade executed: {asset} {direction.upper()}")
                logging.getLogger('trades').info(
                    f"QUOTEX_TRADE_EXECUTED | {json.dumps(signal)}"
                )
            else:
                self.logger.error("Trade execution failed.")

        except Exception as e:
            self.logger.error(f"Error executing trade {sid}: {e}")
        finally:
            self.pending_signals.pop(sid, None)

    def _calc_wait(self, entry_time_str: str, timezone_str: Optional[str]) -> Optional[float]:
        """
        Returns seconds until entry time. Negative means it already passed.

        The timezone MUST be supplied by the signal — there is NO UTC fallback.
        Returns None when the timezone is missing/unparseable or the entry time
        cannot be parsed; the caller then skips the trade so it is never fired at
        the wrong moment based on an assumed timezone.
        """
        if not timezone_str:
            self.logger.error(
                f"No timezone in signal for entry '{entry_time_str}' — "
                f"cannot schedule (UTC fallback removed). Skipping."
            )
            return None

        try:
            tz_clean = timezone_str.replace(' ', '')
            if tz_clean.upper().startswith('UTC'):
                tz_clean = tz_clean[3:]
            m = re.match(r'^([+-])(\d{1,2}):(\d{2})$', tz_clean)
            if not m:
                self.logger.error(
                    f"Unparseable timezone '{timezone_str}' for entry "
                    f"'{entry_time_str}' — skipping."
                )
                return None
            sign       = 1 if m.group(1) == '+' else -1
            offset_min = sign * (int(m.group(2)) * 60 + int(m.group(3)))
            signal_tz  = pytz.FixedOffset(offset_min)

            # Build the entry instant on TODAY'S DATE IN THE SIGNAL'S TIMEZONE.
            # Using UTC's date is wrong: when the UTC date differs from the
            # signal-tz date (e.g. early-morning IST is still the previous UTC
            # day), the entry lands ~24h off — the "passed by 86248s" bug.
            now_tz = datetime.now(signal_tz)
            t      = datetime.strptime(entry_time_str, '%H:%M:%S')
            entry  = now_tz.replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            )

            delta = (entry - now_tz).total_seconds()
            # Near-midnight wrap: an entry that looks more than 12h in the past is
            # really the next day's occurrence (e.g. 00:05 entry sent at 23:58).
            if delta < -43200:
                delta += 86400
            return delta

        except Exception as e:
            self.logger.error(f"Could not calculate wait time for '{entry_time_str}': {e}")
            return None


class TelegramHandler:
    """Complete Telegram integration — monitors multiple channels simultaneously."""

    def __init__(self, config):
        self.config          = config
        self.logger          = logging.getLogger(__name__)
        self.client          = None
        self.parser          = SignalParser(self.config)
        self.executor        = None
        self.signal_callback = None
        self.session_string  = None
        self.api_id          = self.config.telegram.api_id
        self.api_hash        = self.config.telegram.api_hash
        self.phone_number    = None

    async def initialize(self, quotex_handler: QuotexHandler):
        try:
            self.logger.info("Initializing Telegram client...")
            self._load_session()

            if not self.phone_number:
                print("\n" + "=" * 60)
                print("    TELEGRAM LOGIN REQUIRED")
                print("=" * 60)
                print("Get API credentials from: https://my.telegram.org/apps\n")
                self.phone_number = input("Enter your phone number (with country code): ").strip()

            session = StringSession(self.session_string) if self.session_string else StringSession()
            self.client = TelegramClient(session, self.api_id, self.api_hash)
            await self.client.start(phone=self.phone_number)

            self.session_string = self.client.session.save()
            self._save_session()
            await self._list_chats()

            self.executor = SignalExecutor(self.config, quotex_handler)
            self.logger.info("Telegram client initialized successfully")
            return True

        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram client: {e}")
            return False

    def _load_session(self):
        try:
            session_file = f"{self.config.telegram.session_name}.json"
            if os.path.exists(session_file):
                with open(session_file, 'r') as f:
                    data = json.load(f)
                self.session_string = data.get('session_string')
                self.phone_number   = data.get('phone_number')
                self.logger.info("Telegram session loaded")
        except Exception as e:
            self.logger.debug(f"Could not load session: {e}")

    def _save_session(self):
        try:
            session_file = f"{self.config.telegram.session_name}.json"
            with open(session_file, 'w') as f:
                json.dump({
                    'session_string': self.session_string,
                    'phone_number':   self.phone_number,
                    'saved_at':       datetime.now().isoformat(),
                }, f)
            self.logger.info("Telegram session saved")
        except Exception as e:
            self.logger.error(f"Could not save session: {e}")

    async def _list_chats(self):
        try:
            dialogs = await self.client.get_dialogs(limit=None)
            groups  = [d for d in dialogs if isinstance(d.entity, (Channel, Chat))]
            print("\n" + "=" * 65)
            print("  AVAILABLE GROUPS & CHANNELS")
            print("=" * 65)
            print(f"  {'TYPE':<12} {'ID (use in config.json)':<25} NAME")
            print("  " + "-" * 62)
            for d in groups:
                pid   = get_peer_id(d.entity)
                etype = "Channel" if isinstance(d.entity, Channel) else "Group"
                title = getattr(d.entity, 'title', 'Unknown')
                print(f"  {etype:<12} {str(pid):<25} {title}")
                self.logger.info(f"Chat: {etype} | {pid} | {title}")
            print("=" * 65)
            enabled = [c for c in self.config.telegram.channels if c.enabled]
            print(f"  Enabled channels: {[c.identifier for c in enabled]}")
            print("=" * 65 + "\n")
        except Exception as e:
            self.logger.error(f"Error listing chats: {e}")

    async def _resolve_channel(self, identifier):
        """Resolve a channel identifier (name, @username, or numeric ID) to a Telethon entity."""
        is_plain = isinstance(identifier, str) and ' ' in identifier.strip()

        if not is_plain:
            try:
                return await self.client.get_entity(identifier)
            except Exception:
                pass
            if isinstance(identifier, str) and identifier.strip().lstrip('-').isdigit():
                try:
                    return await self.client.get_entity(int(identifier.strip()))
                except Exception:
                    pass

        dialogs  = await self.client.get_dialogs(limit=None)
        id_lower = str(identifier).strip().lower()
        for d in dialogs:
            title = getattr(d.entity, 'title', '') or getattr(d.entity, 'first_name', '') or ''
            if title.strip().lower() == id_lower:
                self.logger.info(f"Found '{identifier}' (ID: {get_peer_id(d.entity)})")
                return d.entity

        raise ValueError(f"Could not find channel/group '{identifier}'.")

    async def start_monitoring(self, signal_callback):
        try:
            if not self.client:
                raise Exception("Telegram client not initialized")

            self.signal_callback = signal_callback

            enabled: List[ChannelConfig] = [
                c for c in self.config.telegram.channels if c.enabled
            ]
            if not enabled:
                self.logger.error("No channels enabled. Set enabled=true in telegram.channels.")
                return

            watch_ids: List[int] = []

            for ch in enabled:
                try:
                    entity = await self._resolve_channel(ch.identifier)
                    watch_ids.append(entity.id)
                    self.logger.info(
                        f"Watching: {getattr(entity, 'title', ch.identifier)} (ID: {entity.id})"
                    )
                    async for msg in self.client.iter_messages(entity, limit=1):
                        preview = msg.text[:80] if msg.text else '[media]'
                        self.logger.info(f"  Last message (ID: {msg.id}): '{preview}'")
                except Exception as e:
                    self.logger.error(f"Could not resolve channel '{ch.identifier}': {e}")

            if not watch_ids:
                self.logger.error("No channels could be resolved. Check config.")
                return

            @self.client.on(events.NewMessage(chats=watch_ids))
            async def on_message(event):
                await self._handle_message(event)

            await self.client.run_until_disconnected()

        except Exception as e:
            self.logger.error(f"Error monitoring channels: {e}")

    async def _handle_message(self, event):
        """Parse every incoming text message; non-signals parse to None and are dropped."""
        msg = event.message
        self.logger.info(f"New message ID: {msg.id} from chat {event.chat_id}")
        try:
            if not msg.text:
                return

            text = msg.text
            self.logger.info(f"Text: '{text[:120]}'")

            signal = self.parser.parse_signal(text)
            if signal and self.signal_callback:
                await self.signal_callback(signal)

        except Exception as e:
            self.logger.error(f"Error handling message {msg.id}: {e}")

    async def disconnect(self):
        try:
            if self.client:
                await self.client.disconnect()
                self.logger.info("Telegram client disconnected")
        except Exception as e:
            self.logger.error(f"Error disconnecting: {e}")

    async def test_connection(self) -> bool:
        try:
            if not self.client:
                return False
            me = await self.client.get_me()
            self.logger.info(f"Connected as: {me.first_name}")
            return True
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            return False
