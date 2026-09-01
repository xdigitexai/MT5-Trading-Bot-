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
    max_open_positions: int = Field(default=5, ge=1, le=50)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=5)
    min_signal_score: int = Field(default=75, ge=0, le=100)
    max_spread_points_default: int = Field(default=25, ge=1)
    order_deviation_points: int = Field(default=10, ge=0)
    magic_number: int = 260825
    news_fail_closed: bool = True
    emergency_close_positions: bool = False
    max_demo_volume: float = Field(default=0.10, gt=0, le=50)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4o-mini"
    openai_analyst_enabled: bool = False
    backtest_spread_points: float = Field(default=10.0, ge=0)
    backtest_commission_per_lot: float = Field(default=7.0, ge=0)
    backtest_slippage_points: float = Field(default=2.0, ge=0)

    @field_validator("allowed_origins", "symbols", mode="before")
    @classmethod
    def csv(cls, value):
        return [x.strip() for x in value.split(",") if x.strip()] if isinstance(value, str) else value

    @property
    def live_orders_permitted(self) -> bool:
        return self.trading_mode is TradingMode.LIVE and self.live_trading_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
