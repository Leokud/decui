"""Tracebacks for errors caught by the application during Studio preview."""

from __future__ import annotations

import os
import traceback


def report_preview_exception() -> None:
    """Send the currently handled exception to the Studio process log."""
    if os.environ.get("DECUI_STUDIO_PREVIEW") == "1":
        traceback.print_exc()
