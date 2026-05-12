"""Singleton EngineState shared between the running engine and the API server.

When the engine boots, it wires its RiskManager/Broker/Scorer into this object.
The API reads from it. This is intentionally simple — both live in the same
process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EngineState:
    running: bool = False
    risk: Optional[object] = None       # RiskManager
    broker: Optional[object] = None     # Broker
    scorer: Optional[object] = None     # Scorer


engine_state = EngineState()
