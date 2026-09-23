"""Unattended runner for the MT5 engine on Windows.

This is what the Windows Task Scheduler starts. It is deliberately small, dependency-free and
fail-closed:

- **Rotating logs.** Every record — including uvicorn's own — goes to ``runtime/logs/engine.log``,
  which rotates at 5 MB with five kept files. No credential is ever written here: the application
  logs symbols, statuses and reasons only, and this file adds nothing to them.
- **One engine.** The task is registered with ``MultipleInstancesPolicy=IgnoreNew`` so a second
  launch is discarded, and the market loop's database lease makes a second *scheduler* incapable of
  ordering even if two processes ever did run.
- **Restart after an unexpected exit.** The loop below restarts the API after a delay, and the task
  itself is registered with a restart-on-failure policy so a harness-level failure is retried too.
  A deliberate stop is expressed by creating ``runtime/stop_engine.flag``, which makes the process
  exit without restarting.
- **Resume only through the validated path.** Once the API answers, this runner asks the *same*
  ``POST /api/bot/start`` an operator would call, so trading resumes only after the bot has loaded
  the persistent risk state, verified PostgreSQL, MT5, the pinned account, the emergency lock, the
  session loss, the trade count and the news provider. Anything that does not verify leaves the
  engine running, serving status, and refusing to trade.

    python runtime/engine_service.py            # run under the task scheduler
    python runtime/engine_service.py --once     # foreground, no restart loop
"""
import argparse
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOG_DIR = REPO_ROOT / "runtime" / "logs"
LOG_FILE = LOG_DIR / "engine.log"
ERROR_FILE = LOG_DIR / "engine-service-error.log"
STOP_FILE = REPO_ROOT / "runtime" / "stop_engine.flag"
API_BASE = "http://127.0.0.1:8000"
HOST, PORT = "127.0.0.1", 8000
LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 5
RESTART_DELAY_SECONDS = 15
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    if sys.stdout is not None:  # pythonw.exe has no console and must not be written to
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(console)
    return logging.getLogger("app.engine_service")


def api_ready(timeout: float = 180.0) -> bool:
    """Wait for the local API to answer; the engine is not resumed before it does."""
    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{API_BASE}/api/health", timeout=5.0).status_code == 200:
                return True
        except Exception:  # not up yet: keep waiting, never guess
            pass
        time.sleep(2.0)
    return False


def resume_trading(log: logging.Logger) -> bool:
    """Ask the validated start path to resume the market loop; never bypasses a gate."""
    import httpx

    from app.core.config import get_settings

    settings = get_settings()
    if not settings.auto_start_trading:
        log.info("trading_resume_skipped reason=auto_start_trading_disabled")
        return False
    if not api_ready():
        log.error("trading_resume_failed reason=api_not_ready")
        return False
    token = settings.api_token.get_secret_value()
    try:
        response = httpx.post(f"{API_BASE}/api/bot/start", headers={"Authorization": f"Bearer {token}"}, timeout=180.0)
    except Exception as error:  # an unreachable API is not a reason to trade
        log.error("trading_resume_failed reason=request_error error=%s", type(error).__name__)
        return False
    if response.status_code == 200:
        log.info("trading_resumed live_orders_permitted=%s", settings.live_orders_permitted)
        return True
    detail = ""
    try:
        detail = str(response.json().get("detail", ""))[:400]
    except Exception:
        detail = response.text[:200]
    log.warning("trading_not_resumed http_status=%s detail=%s", response.status_code, detail)
    return False


def serve() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info", log_config=None)


def stop_requested() -> bool:
    return STOP_FILE.exists()


def single_run(log: logging.Logger) -> int:
    """One engine process: resume trading through the validated path, then serve until it exits."""
    if stop_requested():
        log.info("engine_service_stopped reason=stop_flag_present")
        return 0
    threading.Thread(target=resume_trading, args=(log,), name="resume-trading", daemon=True).start()
    try:
        serve()
    except KeyboardInterrupt:
        log.info("engine_service_stopped reason=keyboard_interrupt")
        return 0
    except Exception as error:  # a failed engine is retried by the supervisor, never by guessing
        log.exception("engine_service_crashed error=%s", type(error).__name__)
        return 1
    log.info("engine_service_exited")
    return 0


def run_forever(log: logging.Logger) -> int:
    """Restart the API after an unexpected exit; a stop flag ends the supervision."""
    while True:
        code = single_run(log)
        if stop_requested():
            log.info("engine_service_stopped reason=stop_flag_present after_exit=%s", code)
            return 0
        if code == 0:
            return 0
        log.warning("engine_service_restarting delay_s=%s exit_code=%s", RESTART_DELAY_SECONDS, code)
        time.sleep(RESTART_DELAY_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the MT5 engine unattended.")
    parser.add_argument("--once", action="store_true", help="run in the foreground and do not restart after an exit")
    args = parser.parse_args(argv)
    log = configure_logging()
    if args.once:
        return single_run(log)
    return run_forever(log)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fatal:  # pythonw.exe has no console, so the reason must reach a file
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(ERROR_FILE), level=logging.ERROR, format=LOG_FORMAT)
        logging.getLogger("app.engine_service").exception("engine_service_fatal error=%s", type(fatal).__name__)
        raise
