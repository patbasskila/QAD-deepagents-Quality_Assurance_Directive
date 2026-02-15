import logging
import sys
from typing import Optional


def setup_logging(level: str = "INFO") -> None:
    """
    Simple structured-ish logging that works well locally and in containers.
    """
    root = logging.getLogger()
    root.handlers.clear()

    lvl = getattr(logging, level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root.addHandler(handler)
    root.setLevel(lvl)

    # Reduce noise from common libs
    logging.getLogger("uvicorn").setLevel(lvl)
    logging.getLogger("uvicorn.error").setLevel(lvl)
    logging.getLogger("uvicorn.access").setLevel(lvl)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pypdf").setLevel(logging.WARNING)
