"""Quality gate: automatic checks so no bad clip gets marked ready."""

from __future__ import annotations

from .checks import (
    QualityReport,
    check_clip,
    loudness_filter,
    run_for_job,
    score_hook,
)

__all__ = ["QualityReport", "check_clip", "loudness_filter", "run_for_job", "score_hook"]
