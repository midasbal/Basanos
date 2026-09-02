"""Basanos measurement layer: read-only analyses over already-collected data.

Everything under this package only reads files the collector has already
written (under a `--data-dir`); nothing here writes to or modifies a
collector data file. Analysis output (reports, JSON summaries) is written
to its own separate location, never back into the collector's own tree.
"""
