"""Shared fixtures: SQLite in-memory database and a fake MT5 gateway (no terminal required)."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.schemas import Signal, SignalAction, TradeIntent
from app.database.base import Base
from app.mt5.gateway import MT5Health
from app.mt5.timeframes import timeframe_minutes, timeframe_name
from app.risk.state import RiskStateStore

TRADE_RETCODE_DONE = 10009

@dataclass
class FakeSymbolInfo:
    point: float = 0.00001
    digits: int = 5
    trade_tick_size: float = 0.00001
    trade_tick_value: float = 1.0
    trade_tick_value_profit: float = 0.0
    trade_contract_size: float = 100_000.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    trade_stops_level: int = 0
    freeze_level: int = 0
    filling_mode: int = 1
    margin_initial: float = 0.0


@dataclass
class FakeAccount:
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin_free: float = 9_000.0
    margin_level: float = 1_000.0
    leverage: float = 100.0
    # 0 = DEMO, 1 = CONTEST, 2 = REAL, as MT5 reports it.
    trade_mode: int = 0
    login: int = 134693538
    server: str = "FakeServer-Demo"
    company: str = "Fake Broker"
    currency: str = "USD"


@dataclass
class FakeTerminalInfo:
    trade_allowed: bool = True
    connected: bool = True


@dataclass
class FakeOrderResult:
    retcode: int = TRADE_RETCODE_DONE
    order: int = 55501
    deal: int = 55502
    price: float = 1.1
    comment: str = "Done"


@dataclass
class FakeTick:
    """MT5 symbol_info_tick stand-in; `time` is seconds since the epoch, as MT5 reports it."""
    bid: float = 1.09995
    ask: float = 1.10005
    time: float = 0.0


@dataclass
class FakePosition:
    ticket: int = 55501
    symbol: str = "EURUSD"
    volume: float = 0.05
    price_open: float = 1.1
    price_current: float = 1.1
    sl: float = 1.09
    tp: float = 1.12
    type: int = 0
    magic: int = 260825


@dataclass
class FakeOrder:
    ticket: int = 55511
    symbol: str = "EURUSD"
    volume: float = 0.05
    price_open: float = 1.1
    type: int = 2
    magic: int = 260825


@dataclass
class FakeDeal:
    """MT5 history deal stand-in: entry 0 opens a position, entry 1 closes it."""
    ticket: int = 9001
    order: int = 8001
    position_id: int = 7001
    symbol: str = "EURUSD"
    type: int = 0
    entry: int = 0
    volume: float = 0.05
    price: float = 1.1
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    magic: int = 260825
    time: float = 0.0
    comment: str = "bot-trade"
    reason: int = 0


@dataclass
class FakeGateway:
    """Gateway stand-in: MT5 is never imported and every call is observable."""
    symbols: dict = field(default_factory=lambda: {"EURUSD": FakeSymbolInfo(), "GBPUSD": FakeSymbolInfo()})
    account: FakeAccount | None = field(default_factory=FakeAccount)
    terminal: object = field(default_factory=FakeTerminalInfo)
    result: object = field(default_factory=FakeOrderResult)
    error: Exception | None = None
    connected: bool = True
    requests: list = field(default_factory=list)
    tick_value: object = None
    # Candle frames keyed by timeframe name ("M15", "H1", "H4"); shared by every symbol.
    bar_frames: dict = field(default_factory=dict)
    deals: tuple = ()
    position_list: tuple = ()
    order_list: tuple = ()
    closed: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    rates_calls: list = field(default_factory=list)
    history_fails: bool = False

    def health(self): return MT5Health(self.connected, "fake gateway")
    def symbol_info(self, symbol): return self.symbols.get(symbol)
    def account_info(self): return self.account
    def terminal_info(self): return self.terminal
    def tick(self, symbol): return self.tick_value
    def rates(self, symbol, timeframe, count):
        self.rates_calls.append((symbol, timeframe, count))
        return self.bar_frames.get(timeframe_name(timeframe))
    def order_send(self, request):
        self.requests.append(request)
        if self.error: raise self.error
        return self.result
    def positions(self): return tuple(self.position_list)
    def orders(self): return tuple(self.order_list)
    def history(self, start, end): return None if self.history_fails else tuple(self.deals)
    def discover_symbol(self, canonical): return canonical if canonical in self.symbols else None
    def modify_position(self, ticket, symbol, stop_loss, take_profit):
        self.modified.append((ticket, symbol, stop_loss, take_profit))
    def close_position(self, position):
        self.closed.append(position)
        if self.error: raise self.error
        return self.result
    def cancel_order(self, order):
        self.cancelled.append(order)
        if self.error: raise self.error
        return self.result
    def initialize(self): return self.health()
    def login(self): return self.health()
    def shutdown(self): self.connected = False


def candle(close, spread=0.0002):
    """OHLC frame where each candle opens at the previous close."""
    close = np.asarray(close, dtype=float)
    spread = np.asarray(spread, dtype=float)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + spread, "low": np.minimum(open_, close) - spread, "close": close})


def bar_times(now: datetime, timeframe: str, count: int) -> pd.Series:
    """Bar open times ending with the still-forming bar of the current period."""
    minutes = timeframe_minutes(timeframe)
    interval = timedelta(minutes=minutes)
    latest = pd.Timestamp(now).floor(f"{minutes}min")
    return pd.Series([(latest - interval * (count - 1 - index)).to_pydatetime() for index in range(count)], dtype="datetime64[ns, UTC]")


def market_frames(now: datetime | None = None, count: int = 260) -> dict:
    """Deterministic H4/H1/M15 frames that make trend_following emit a BUY with a valid bracket.

    The breakout is forced onto the last *completed* M15 candle: the newest bar is still forming,
    so a test that expects a trade also proves the loop ignores it.
    """
    moment = now or datetime.now(timezone.utc)
    frames = {}
    for timeframe, timeframe_close in (("H4", 1.10 + 0.0002 * np.arange(count)), ("H1", 1.10 + 0.0003 * np.arange(count))):
        frame = candle(timeframe_close)
        frame.insert(0, "time", bar_times(moment, timeframe, count))
        frames[timeframe] = frame
    base = candle(1.10 + 0.0008 * np.sin(np.arange(count) / 3.0))
    open_, high, low, closes = (base[column].to_numpy().copy() for column in ("open", "high", "low", "close"))
    open_[-2], closes[-2] = closes[-3], closes[-3] + 0.0030
    high[-2], low[-2] = closes[-2] + 0.0002, open_[-2] - 0.0006
    open_[-1], closes[-1] = closes[-2], closes[-2] + 0.0001
    high[-1], low[-1] = closes[-1] + 0.0001, open_[-1] - 0.0001
    frames["M15"] = pd.DataFrame({"time": bar_times(moment, "M15", count), "open": open_, "high": high, "low": low, "close": closes})
    return frames


def market_gateway(now: datetime | None = None, **kwargs) -> FakeGateway:
    """FakeGateway wired for a full scheduler cycle on EURUSD/GBPUSD."""
    moment = now or datetime.now(timezone.utc)
    return FakeGateway(bar_frames=market_frames(moment), tick_value=FakeTick(time=moment.timestamp()), **kwargs)


@pytest.fixture
def settings() -> Settings:
    # _env_file=None keeps the suite independent of any developer .env file.
    return Settings(_env_file=None)


def fresh_store() -> RiskStateStore:
    """A brand-new, empty risk state on its own private in-memory database.

    The gate refuses to trade without a readable risk state, so a unit test that wants to prove
    something *other* than that must supply one; each call here is a pristine session with no
    realized P/L, no trades and no kill switch.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return RiskStateStore(sessionmaker(bind=engine)())


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


def make_signal(
    action=SignalAction.BUY,
    symbol="EURUSD",
    entry=1.1,
    stop_loss=1.09,
    take_profit=1.12,
    score=80,
    strategy="trend_following",
    timeframe="M15",
    confidence=0.8,
    reasons=None,
) -> Signal:
    return Signal(
        action=action, confidence=confidence, score=score, symbol=symbol, entry=entry,
        stop_loss=stop_loss, take_profit=take_profit, strategy=strategy, timeframe=timeframe,
        reasons=list(reasons or ["fixture signal"]), timestamp=datetime.now(timezone.utc),
    )


def make_intent(signal=None, volume=0.05, price=1.1, trade_id="trade-1", key="key-1", signal_id=None) -> TradeIntent:
    return TradeIntent(
        trade_id=trade_id, signal=signal if signal is not None else make_signal(), volume=volume,
        requested_price=price, idempotency_key=key, signal_id=signal_id,
    )


@pytest.fixture
def signal_factory(): return make_signal


@pytest.fixture
def intent_factory(): return make_intent
