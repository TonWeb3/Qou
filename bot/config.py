"""
Configuration management for the Quotex Telegram Trading Bot
"""

import json
import os
from dataclasses import dataclass, field
import logging
from typing import Optional, Union, List


@dataclass
class ChannelConfig:
    """Configuration for a single monitored Telegram channel."""
    enabled: bool = True
    identifier: Union[str, int] = ""   # group name, @username, or numeric ID


@dataclass
class QuotexConfig:
    """Quotex platform configuration"""
    email: str = ""
    password: str = ""
    early_entry_seconds: float = 0.5   # latency lead: fire this many seconds before the entry second
    # Grace period: if the order cannot be sent within this many seconds of the
    # intended firing moment, the signal is dropped instead of entered late.
    late_entry_grace_seconds: float = 5.0
    # Order style. "TIMER" = fixed duration from the fill (optionType 100).
    # "TURBO" = candle-aligned expiry (optionType 1) — fall back to it if Quotex
    # stops acknowledging duration orders on an asset.
    time_mode: str = "TIMER"


@dataclass
class TelegramConfig:
    """Telegram configuration"""
    api_id: Optional[int] = None
    api_hash: str = ""
    session_name: str = "quotex_bot_session"
    channels: List[ChannelConfig] = field(default_factory=list)


@dataclass
class TradingConfig:
    """Trading configuration"""
    account_type: str = "demo"          # "demo" or "live"
    risk_mode: str = "fixed"            # "fixed" (dollar amount) or "percent" (% of balance)
    risk_amount: float = 1.0            # Dollar amount OR percentage value (e.g. 5 = 5%)
    max_daily_trades: int = 10
    max_daily_loss: float = 50.0
    max_daily_loss_enabled: bool = True   # when False, the daily-loss limit is not enforced
    max_concurrent_trades: int = 1
    martingale_enabled: bool = False
    martingale_multiplier: float = 2.0
    martingale_steps: int = 2      # trades per signal on the SAME pair (initial + auto recoveries)
    # expiry is intentionally absent — each signal format provides its own duration


@dataclass
class LoggingConfig:
    """Logging configuration"""
    log_level: str = "INFO"
    log_file: str = "quotex_bot.log"


class Config:
    """Main configuration class"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.logger = logging.getLogger(__name__)
        self._load_config()

    def _load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config_data = json.load(f)

                quotex_data = config_data.get('quotex', {})
                self.quotex = QuotexConfig(
                    email=quotex_data.get('email', ''),
                    password=quotex_data.get('password', ''),
                    early_entry_seconds=float(quotex_data.get('early_entry_seconds', 0.5)),
                    late_entry_grace_seconds=float(
                        quotex_data.get('late_entry_grace_seconds', 5.0)
                    ),
                    time_mode=quotex_data.get('time_mode', 'TIMER'),
                )

                tg_data = config_data.get('telegram', {})
                channels = [
                    ChannelConfig(
                        enabled=c.get('enabled', True),
                        identifier=c.get('identifier', ''),
                    )
                    for c in tg_data.get('channels', [])
                ]
                self.telegram = TelegramConfig(
                    api_id=tg_data.get('api_id'),
                    api_hash=tg_data.get('api_hash', ''),
                    session_name=tg_data.get('session_name', 'quotex_bot_session'),
                    channels=channels,
                )

                tr_data = config_data.get('trading', {})
                self.trading = TradingConfig(
                    account_type=tr_data.get('account_type', 'demo'),
                    risk_mode=tr_data.get('risk_mode', 'fixed'),
                    risk_amount=tr_data.get('risk_amount', 1.0),
                    max_daily_trades=tr_data.get('max_daily_trades', 10),
                    max_daily_loss=tr_data.get('max_daily_loss', 50.0),
                    max_daily_loss_enabled=tr_data.get('max_daily_loss_enabled', True),
                    max_concurrent_trades=tr_data.get('max_concurrent_trades', 1),
                    martingale_enabled=tr_data.get('martingale_enabled', False),
                    martingale_multiplier=tr_data.get('martingale_multiplier', 2.0),
                    martingale_steps=tr_data.get('martingale_steps', 2),
                )

                self.logging = LoggingConfig(**config_data.get('logging', {}))
                self.logger.info(f"Configuration loaded from {self.config_file}")
            else:
                self._create_default_config()
                self.logger.warning(f"Created default config file: {self.config_file}")

        except Exception as e:
            self.logger.error(f"Error loading configuration: {e}")
            self._create_default_config()

    def _create_default_config(self):
        self.quotex  = QuotexConfig()
        self.telegram = TelegramConfig()
        self.trading  = TradingConfig()
        self.logging  = LoggingConfig()
        self.save_config()

    def save_config(self):
        try:
            config_data = {
                'quotex': {
                    'email':              self.quotex.email,
                    'password':           self.quotex.password,
                    'early_entry_seconds': self.quotex.early_entry_seconds,
                    'late_entry_grace_seconds': self.quotex.late_entry_grace_seconds,
                    'time_mode':           self.quotex.time_mode,
                },
                'telegram': {
                    'api_id':          self.telegram.api_id,
                    'api_hash':        self.telegram.api_hash,
                    'session_name':    self.telegram.session_name,
                    'channels': [
                        {
                            'enabled':    c.enabled,
                            'identifier': c.identifier,
                        }
                        for c in self.telegram.channels
                    ],
                },
                'trading': {
                    'account_type':          self.trading.account_type,
                    'risk_mode':             self.trading.risk_mode,
                    'risk_amount':           self.trading.risk_amount,
                    'max_daily_trades':         self.trading.max_daily_trades,
                    'max_daily_loss':           self.trading.max_daily_loss,
                    'max_daily_loss_enabled':   self.trading.max_daily_loss_enabled,
                    'max_concurrent_trades':    self.trading.max_concurrent_trades,
                    'martingale_enabled':    self.trading.martingale_enabled,
                    'martingale_multiplier': self.trading.martingale_multiplier,
                    'martingale_steps':      self.trading.martingale_steps,
                },
                'logging': self.logging.__dict__,
            }
            with open(self.config_file, 'w') as f:
                json.dump(config_data, f, indent=2)
            self.logger.info(f"Configuration saved to {self.config_file}")
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")

    def validate(self) -> bool:
        errors = []
        if self.telegram.api_id is None:
            errors.append("Telegram API ID is required")
        if not self.telegram.api_hash:
            errors.append("Telegram API Hash is required")
        enabled = [c for c in self.telegram.channels if c.enabled]
        if not enabled:
            errors.append("At least one channel must be enabled in telegram.channels")
        for error in errors:
            self.logger.error(f"Configuration error: {error}")
        return len(errors) == 0
