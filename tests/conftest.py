"""Shared test setup.

The pure core (passes, scope, ratelimit, netcheck, apk) has no Home Assistant imports. To
test it without Home Assistant, the integration directory is registered as a bare package
named `hearth_guests`, so its __init__.py (which imports Home Assistant) never runs.

Tests in tests/ha need Home Assistant and pytest-homeassistant-custom-component; they are
skipped when those are not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "hearth_guests"

if "hearth_guests" not in sys.modules:
    _package = types.ModuleType("hearth_guests")
    _package.__path__ = [str(COMPONENT_DIR)]
    sys.modules["hearth_guests"] = _package

collect_ignore: list[str] = []
if (
    importlib.util.find_spec("homeassistant") is None
    or importlib.util.find_spec("pytest_homeassistant_custom_component") is None
):
    collect_ignore.append("ha")
