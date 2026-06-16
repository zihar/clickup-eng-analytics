"""Tipe data bersama antar-sumber (DB scorecard maupun GitLab API langsung)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommitStats:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    active_days: int = 0
    repos: int = 0
