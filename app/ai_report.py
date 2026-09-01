"""CLI: python -m app.ai_report"""
from __future__ import annotations

from pathlib import Path
from app.ai.analyst import analyze
from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    stats: dict = {"source": "reports", "files": []}
    reports = Path("reports")
    if reports.exists():
        for p in sorted(reports.glob("*.txt")):
            stats["files"].append(p.name)
            if p.name == "all_pairs_report.txt":
                stats["all_pairs_summary"] = p.read_text(encoding="utf-8")[:4000]
    print(analyze(stats, settings))


if __name__ == "__main__":
    main()
