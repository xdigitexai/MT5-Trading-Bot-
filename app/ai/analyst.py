"""OpenAI AI Analyst: reads aggregated stats only. Never returns executable trade orders."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import Settings, get_settings

log = logging.getLogger("ai_analyst")

SYSTEM = """You are a quantitative trading research analyst for a DEMO forex bot.
You receive aggregated backtest/journal statistics only.
You must NOT output BUY/SELL/OPEN/CLOSE/INCREASE LOT SIZE as executable instructions.
You may recommend offline experiments only. RiskEngine remains the authority.
Never request or invent credentials.
"""


def analyze(stats: dict[str, Any], settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not settings.openai_analyst_enabled:
        return (
            "AI Analyst disabled.\n"
            "Enable OPENAI_ANALYST_ENABLED=true and set OPENAI_API_KEY to generate AI analysis.\n"
            "The bot continues to operate without OpenAI."
        )
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not key:
        return "AI Analyst enabled but OPENAI_API_KEY is missing."

    safe = {k: v for k, v in stats.items() if k.lower() not in ("password", "token", "api_key", "login")}
    user = (
        "Analyze these DEMO trading statistics and produce:\n"
        "AI PERFORMANCE SUMMARY\nSTRATEGY OBSERVATIONS\nSESSION OBSERVATIONS\n"
        "REGIME OBSERVATIONS\nPOSSIBLE PROBLEMS\nAREAS TO BACKTEST\nRECOMMENDED EXPERIMENTS\n\n"
        "Data:\n" + json.dumps(safe, default=str)[:12000]
    )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content or "(empty AI response)"
    except Exception as exc:
        log.exception("OpenAI analyst failed")
        return f"AI Analyst error: {exc}"
