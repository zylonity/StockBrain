"""Verified upstream imports. The submodule remains unmodified."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from stockbrain.intelligence.research import UPSTREAM_COMMIT, ResearchError


@lru_cache(maxsize=1)
def upstream_root() -> Path:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[3] / "third_party/TradingAgents",
        here.parents[2] / "third_party/TradingAgents",
    )
    root = next((path for path in candidates if path.is_dir()), None)
    if root is None:
        raise ResearchError("TradingAgents submodule missing; initialize recursive submodules")
    pin = json.loads(here.with_name("tradingagents-pin.json").read_text())
    if pin["commit"] != UPSTREAM_COMMIT:
        raise ResearchError("TradingAgents pin mismatch")
    actual = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tradingagents").rglob("*.py")
    }
    if actual != pin["files"]:
        raise ResearchError("TradingAgents source differs from the pinned release")
    sys.path.insert(0, str(root))
    return root


def upstream_module(name: str) -> Any:
    root = upstream_root()
    module = importlib.import_module("tradingagents." + name)
    if not module.__file__ or not Path(module.__file__).resolve().is_relative_to(root):
        raise ResearchError("TradingAgents imported from an unpinned installation")
    return module
