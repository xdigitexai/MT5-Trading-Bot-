from dataclasses import dataclass
from app.core.config import Settings
from app.mt5.gateway import MT5Gateway

@dataclass
class BotState:
    running: bool = False
    emergency_locked: bool = False

class BotService:
    def __init__(self, settings: Settings, gateway: MT5Gateway): self.settings, self.gateway, self.state = settings, gateway, BotState()
    def start(self) -> tuple[bool, str]:
        if self.state.emergency_locked: return False, "emergency reset required"
        health = self.gateway.initialize()
        if not health.connected: return False, health.detail
        login = self.gateway.login()
        if not login.connected: self.gateway.shutdown(); return False, login.detail
        self.state.running = True; return True, "started after MT5 validation"
    def stop(self): self.state.running = False
    def emergency_stop(self): self.state.running = False; self.state.emergency_locked = True
    def emergency_reset(self): self.state.emergency_locked = False
