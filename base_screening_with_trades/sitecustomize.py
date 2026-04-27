"""Make the repository root importable when notebooks start from this folder."""

from __future__ import annotations

import sys
from pathlib import Path


def _add_repo_root() -> None:
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if (candidate / "item_lists").is_dir():
            root = str(candidate)
            if root not in sys.path:
                sys.path.insert(0, root)
            return


_add_repo_root()
