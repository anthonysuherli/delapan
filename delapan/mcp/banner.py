"""Delapan wordmark — the banner that leads the resume/tap surface.

Keeping it here (not inside `select_preamble`) keeps the public preamble XML clean
— the banner is a conversation-surface concern only. The mark is the figure-8
(``delapan`` = eight) wrapped by the dotted context ring ``◌``.
"""

from __future__ import annotations

DELAPAN_BANNER = """\
        ╱──
──────◌
        ╲──
   d i v e r g e n c e
   knowledge, tapped"""
