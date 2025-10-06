from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    __package__ = "shark_etcher"

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
