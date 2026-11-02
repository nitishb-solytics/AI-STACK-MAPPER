"""Filesystem helpers shared by the stack and risk scanners."""
from __future__ import annotations

import os
from typing import Iterable, List, Set

# A virtual environment is identified by what it contains, not what it is
# called. `IGNORE_DIRS` only catches the conventional names (`venv`, `.venv`,
# `env`), but projects routinely name the environment after the product --
# `eRag/`, `nimbus/`, `doc/` -- and by name alone those are indistinguishable
# from a source package. Left unpruned, the scanner walks the whole of
# site-packages and reports every vendored SDK as part of the codebase.
VENV_MARKERS = ("pyvenv.cfg", "conda-meta")


def is_virtualenv_dir(path: str) -> bool:
    """True when ``path`` is the root of a Python virtual environment."""
    return any(os.path.exists(os.path.join(path, marker)) for marker in VENV_MARKERS)


def prune_dirs(dirpath: str, dirnames: Iterable[str], ignore: Set[str]) -> List[str]:
    """Subdirectories worth descending into, sorted for reproducible walks."""
    return sorted(
        name
        for name in dirnames
        if name not in ignore
        and not name.endswith(".egg-info")
        and not is_virtualenv_dir(os.path.join(dirpath, name))
    )
