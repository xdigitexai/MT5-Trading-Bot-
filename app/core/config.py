import json
from enum import StrEnum
from functools import lru_cache
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class Settings(BaseSettings):
    # CSV values such as SYMBOLS and ALLOWED_ORIGINS are normalized by csv()
    # below; automatic JSON decoding would reject ordinary .env CSV strings.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", enable_decoding=False)
    trading_mode: TradingMode = TradingMode.DEMO
    live_trading_enabled: bool = False
    mt5_login: int | None = None
    mt5_password: SecretStr | None = None
    mt5_server: str | None = None
    mt5_terminal_path: str | None = None
    database_url: str = "postgresql+psycopg://forex:forex@localhost:5432/forexbot"
    redis_url: str = "redis://localhost:6379/0"
    api_secret_key: SecretStr = SecretStr("change-me")
    api_token: SecretStr = SecretStr("change-me")
    allowed_origins: list[str] = ["http://localhost:3000"]
    symbols: list[str] = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD", "EURJPY", "GBPJPY", "EURGBP"]
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=2)
    max_daily_loss_pct: float = Field(default=2, gt=0, le=10)
    max_drawdown_pct: float = Field(default=10, gt=0, le=50)
    # Hard server-side limits. The dollar limits are absolute ceilings on a small real-money
    # account, not percentages of equity: MAX_BOT_CAPITAL_USD is the allocation ceiling for this
    # bot (never the account's equity), and the loss limits are what the derived volume must
    # respect. They are persisted in risk_state, so a restart cannot reset them. Every one of them
    # is a ceiling and never a target: the technical setup may always risk less, and a setup that
    # cannot fit inside them is refused rather than traded with a wider stop or a looser limit.
    max_open_positions: int = Field(default=1, ge=1, le=50)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=5)
    max_bot_capital_usd: float = Field(default=3.0, gt=0, le=1_000_000)
    max_loss_per_trade_usd: float = Field(default=0.70, gt=0, le=100_000)
    max_session_loss_usd: float = Field(default=1.40, gt=0, le=1_000_000)
    max_daily_trades: int = Field(default=3, ge=1, le=10_000)
    # Hard volume ceiling for the whole position, independent of the risk budget: a setup whose
    # risk-derived volume would exceed it is rejected, never clamped into a bigger-than-intended
    # position and never size-reduced by tightening the strategy's stop.
    max_lots_per_position: float = Field(default=0.01, gt=0, le=1_000.0)
    min_signal_score: int = Field(default=75, ge=0, le=100)
    max_spread_points_default: int = Field(default=25, ge=1)
    order_deviation_points: int = Field(default=10, ge=0)
    magic_number: int = 260825
    news_fail_closed: bool = True
    emergency_close_positions: bool = False
    # Strategy layer: only names in app.strategies.STRATEGY_NAMES are accepted, and the safe
    # default keeps the ensemble combiner off until an operator opts in.
    enabled_strategies: list[str] = ["trend_following"]
    ensemble_min_votes: int = Field(default=2, ge=1, le=6)
    ensemble_min_confidence: float = Field(default=0.6, ge=0, le=1)
    strategy_timeframes: dict[str, str] = {}
    # Data/execution/risk layer guards. These are additive: they never relax the limits above and
    # they never touch the trading_mode / live_trading_enabled dual gate.
    max_total_exposure_pct: float = Field(default=200, gt=0, le=1000)
    min_margin_level_pct: float = Field(default=150, gt=0)
    max_risk_per_trade_pct: float = Field(default=2, gt=0, le=10)
    min_risk_reward_ratio: float = Field(default=2, gt=0, le=10)
    # Runtime knobs for the market loop, the reconciler and the news policy. Every default keeps
    # the loop conservative: it never relaxes a limit above, and it never touches the
    # trading_mode / live_trading_enabled dual gate.
    scheduler_interval_seconds: int = Field(default=60, ge=1, le=3600)
    scheduler_candle_count: int = Field(default=300, ge=210, le=5000)
    scheduler_lock_ttl_seconds: int = Field(default=300, ge=5)
    market_data_max_age_seconds: int = Field(default=7200, ge=1)
    reconciliation_interval_seconds: int = Field(default=300, ge=1)
    reconciliation_lookback_hours: int = Field(default=168, ge=1)
    reconciliation_match_window_seconds: int = Field(default=300, ge=0)
    stale_guard_seconds: int = Field(default=900, ge=1)
    news_window_minutes: int = Field(default=30, ge=0, le=1440)
    news_events_file: str | None = None
    # Calendar provider. `trading_economics` reads the official Trading Economics calendar API; the
    # credential is read from the environment only and is never logged. A provider that cannot be
    # reached, cannot be authorized, cannot be parsed or is older than NEWS_MAX_AGE_SECONDS is
    # reported as unusable, and NEWS_FAIL_CLOSED then refuses every new entry.
    news_provider: str = "static"
    trading_economics_api_key: SecretStr | None = None
    trading_economics_base_url: str = "https://api.tradingeconomics.com"
    news_refresh_seconds: int = Field(default=300, ge=5, le=86_400)
    news_max_age_seconds: int = Field(default=900, ge=30, le=604_800)
    news_provider_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    news_lookback_hours: int = Field(default=12, ge=0, le=720)
    news_horizon_hours: int = Field(default=168, ge=1, le=8760)
    # Read only by runtime/engine_service.py, the unattended Windows runner: it decides whether the
    # runner asks the API to resume the market loop after a reboot. It never bypasses a gate - the
    # runner calls the same validated /api/bot/start an operator would.
    auto_start_trading: bool = False

    @field_validator("allowed_origins", "symbols", "enabled_strategies", mode="before")
    @classmethod
    def csv(cls, value):
        return [x.strip() for x in value.split(",") if x.strip()] if isinstance(value, str) else value

    @field_validator("strategy_timeframes", mode="before")
    @classmethod
    def csv_pairs(cls, value):
        # Accepts either a mapping or "breakout:H1,momentum:M30" from the environment.
        if not isinstance(value, str): return value
        text = value.strip()
        if text.startswith("{"): return json.loads(text)
        pairs = {}
        for item in text.split(","):
            name, _, timeframe = item.partition(":")
            if name.strip(): pairs[name.strip()] = timeframe.strip().upper()
        return pairs

    @field_validator("enabled_strategies")
    @classmethod
    def known_strategies(cls, value: list[str]) -> list[str]:
        from app.strategies import STRATEGY_NAMES  # imported here to avoid a circular import
        unknown = [name for name in value if name not in STRATEGY_NAMES]
        if unknown: raise ValueError(f"unknown strategy names {unknown}; known strategies are {list(STRATEGY_NAMES)}")
        return value

    @field_validator("strategy_timeframes")
    @classmethod
    def known_timeframe_strategies(cls, value: dict[str, str]) -> dict[str, str]:
        from app.strategies import STRATEGY_NAMES  # imported here to avoid a circular import
        unknown = [name for name in value if name not in STRATEGY_NAMES]
        if unknown: raise ValueError(f"unknown strategy names {unknown}; known strategies are {list(STRATEGY_NAMES)}")
        if any(not timeframe for timeframe in value.values()): raise ValueError("strategy_timeframes values must be non-empty timeframes")
        return value

    @property
    def live_orders_permitted(self) -> bool:
        return self.trading_mode is TradingMode.LIVE and self.live_trading_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
