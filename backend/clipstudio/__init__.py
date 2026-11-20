"""ClipStudio — open-source, local-first AI video clipper."""

__version__ = "0.1.0"

__all__ = ["__version__"]

# ClipStudio: pick up NVIDIA_API_KEY / NVIDIA_MODEL / GROQ_API_KEY from .env.
from .envfile import load_env as _load_env  # noqa: E402
from .resources import limit_thread_env as _limit_threads  # noqa: E402

_load_env()
_limit_threads()
