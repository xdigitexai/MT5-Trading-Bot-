from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.schemas import EmergencyRequest
from app.database.base import TradeRecord
from app.database.session import get_db
from app.mt5.gateway import MT5Gateway
from app.services.bot import BotService

settings = get_settings(); gateway = MT5Gateway(settings); bot = BotService(settings, gateway); bearer = HTTPBearer()
def auth(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if credentials.credentials != settings.api_token.get_secret_value(): raise HTTPException(401, "invalid token")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    gateway.shutdown()
app = FastAPI(title="MT5 Forex Bot", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["Authorization","Content-Type"])

@app.get("/api/health")
def health():
    h = gateway.health(); return {"status":"ok" if h.connected else "degraded", "mt5":h.detail, "mode":settings.trading_mode, "live_orders_permitted":settings.live_orders_permitted}
@app.get("/api/account", dependencies=[Depends(auth)])
def account(): return {"account": str(gateway.account_info()) if gateway.health().connected else None}
@app.get("/api/positions", dependencies=[Depends(auth)])
def positions(): return {"positions":[str(x) for x in gateway.positions()]}
@app.get("/api/orders", dependencies=[Depends(auth)])
def orders(): return {"orders":[str(x) for x in gateway.orders()]}
@app.get("/api/trades", dependencies=[Depends(auth)])
def trades(db: Session = Depends(get_db)): return [{"trade_id":x.trade_id,"symbol":x.symbol,"status":x.status} for x in db.scalars(select(TradeRecord).order_by(TradeRecord.id.desc()).limit(200))]
@app.get("/api/performance", dependencies=[Depends(auth)])
def performance(db: Session = Depends(get_db)):
    trades = list(db.scalars(select(TradeRecord)))
    return {"recorded_trades":len(trades), "note":"Realized P/L analytics are populated only after MT5 history reconciliation."}
@app.get("/api/statistics", dependencies=[Depends(auth)])
def statistics(): return {"win_rate":None,"profit_factor":None,"maximum_drawdown":None,"status":"awaiting reconciled closed trades"}
@app.get("/api/signals", dependencies=[Depends(auth)])
def signals(): return {"signals":[],"note":"Signal persistence is wired after the scheduler is configured."}
@app.get("/api/strategies", dependencies=[Depends(auth)])
def strategies(): return {"strategies":["trend_following","breakout","momentum","mean_reversion","volatility","ensemble"],"enabled":["trend_following"]}
@app.get("/api/risk", dependencies=[Depends(auth)])
def risk(): return {"risk_per_trade_pct":settings.risk_per_trade_pct,"max_daily_loss_pct":settings.max_daily_loss_pct,"max_drawdown_pct":settings.max_drawdown_pct,"emergency_locked":bot.state.emergency_locked}
@app.get("/api/bot/status", dependencies=[Depends(auth)])
def status(): return {"running":bot.state.running,"emergency_locked":bot.state.emergency_locked,"mode":settings.trading_mode}
@app.post("/api/bot/start", dependencies=[Depends(auth)])
def start():
    ok, detail = bot.start()
    if not ok: raise HTTPException(409, detail)
    return {"status":detail}
@app.post("/api/bot/stop", dependencies=[Depends(auth)])
def stop(): bot.stop(); return {"status":"stopped"}
@app.post("/api/trading/emergency-stop", dependencies=[Depends(auth)])
def emergency(request: EmergencyRequest, db: Session = Depends(get_db)):
    bot.emergency_stop(); return {"status":"emergency locked", "close_positions_requested":request.close_positions}
@app.post("/api/trading/emergency-reset", dependencies=[Depends(auth)])
def reset(): bot.emergency_reset(); return {"status":"manual reset complete"}
