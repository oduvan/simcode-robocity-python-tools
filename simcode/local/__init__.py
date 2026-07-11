"""``simcode.local`` — offline local-test entry point.

Run your controller against the **real** Robot City engine before you push:

    python -m simcode.local main.py --ticks 200

This is a thin package over :mod:`simcode._local`; it re-exports :func:`run_local`
and :func:`main` so ``python -m simcode.local`` (see ``__main__.py``) and
``from simcode.local import run_local`` both work.
"""

from .._local import main, run_local

__all__ = ["run_local", "main"]
