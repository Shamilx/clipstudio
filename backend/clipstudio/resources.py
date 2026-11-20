"""CPU politeness: keep the desktop responsive while jobs render.

* ffmpeg/ffprobe children run at NORMAL priority with no console window. They must
  not be started BELOW_NORMAL: on the target Windows machine a below-normal ffmpeg
  was starved to ~0 CPU for tens of minutes (a 52 s clip "rendered" for 45+ min and
  produced 2 MB), and children inherit the parent's class, so the server itself
  must stay at normal priority too;
* encoder threads: all logical cores by default; set ``RENDER_THREADS=N`` in
  ``.env`` to cap;
* OpenCV is limited to 4 threads, numpy/OpenMP to 2;
* only one render runs at a time (``render_lock``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

#: Serialises renders (job worker and API re-exports share it).
render_lock = threading.Lock()

ANALYSIS_THREADS = 2
CV2_THREADS = 4


def limit_thread_env() -> None:
    """Set BLAS/OpenMP thread limits. Must run before numpy/cv2 are imported."""
    for var in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ.setdefault(var, str(ANALYSIS_THREADS))


def render_threads() -> int:
    """Encoder threads: ``RENDER_THREADS`` if set (>0), else all logical cores."""
    raw = os.environ.get("RENDER_THREADS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, os.cpu_count() or 4)


def popen_kwargs() -> dict:
    """Extra ``subprocess`` arguments for ffmpeg/ffprobe children."""
    if sys.platform == "win32":
        # Explicit NORMAL class so a child never inherits a lowered priority.
        return {"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000020}
    return {"preexec_fn": lambda: os.nice(5)}


def lower_own_priority() -> None:
    """Deliberately a no-op.

    Lowering the server's priority made every ffmpeg child inherit it and stall
    (see module docstring). Responsiveness is handled by leaving a core free.
    """


def limit_cv2() -> None:
    try:
        import cv2

        cv2.setNumThreads(CV2_THREADS)
    except Exception:
        pass
