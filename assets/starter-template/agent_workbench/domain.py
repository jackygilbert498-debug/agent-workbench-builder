"""Scenario-specific planning boundary.

Replace ReferenceProvider with the project's real provider or domain rules. Keep
the same deterministic result contract so approval and idempotency stay outside
the provider boundary.
"""

from __future__ import annotations

import re
from typing import Any


PROJECT_SLUG = __PROJECT_SLUG_JSON__
PROJECT_TITLE = __PROJECT_TITLE_JSON__
SCENARIO = __PROJECT_SCENARIO_JSON__
OBSERVABLE_OUTPUT = __PROJECT_OBSERVABLE_OUTPUT_JSON__


class ReferenceProvider:
    """Deterministic offline provider used only for local acceptance."""

    name = "reference-deterministic"
    external = False

    def build_plan(self, request_id: str, content: str) -> dict[str, Any]:
        normalized = re.sub(r"\s+", " ", content).strip()
        lowered = normalized.casefold()
        urgent_words = ("紧急", "立即", "今天", "urgent", "asap")
        priority = "high" if any(word in lowered for word in urgent_words) else "normal"
        if any(word in lowered for word in ("发票", "预算", "invoice", "budget")):
            category = "finance"
        elif any(word in lowered for word in ("会议", "日程", "meeting", "calendar")):
            category = "schedule"
        else:
            category = "general"
        return {
            "schema": "agent-workbench-plan/v1",
            "projectSlug": PROJECT_SLUG,
            "requestId": request_id,
            "scenario": SCENARIO,
            "summary": normalized[:160],
            "category": category,
            "priority": priority,
            "observableOutput": OBSERVABLE_OUTPUT,
            "provider": self.name,
        }
