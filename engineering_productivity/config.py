"""Pemuatan & validasi konfigurasi."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Engineer:
    """Satu engineer yang mau dianalisis. Punya email atau id (atau dua-duanya)."""

    name: str
    email: str | None = None
    id: int | None = None


@dataclass
class GitlabConfig:
    url: str
    token: str
    projects: list[str]
    aliases: dict[str, str] = field(default_factory=dict)  # alias_email -> canonical_email
    noise_patterns: list[str] = field(default_factory=list)  # tambahan pola file noise


@dataclass
class Config:
    token: str
    engineers: list[Engineer]
    team_id: str | None = None
    db_dsn: str | None = None
    gitlab: GitlabConfig | None = None
    extra: dict = field(default_factory=dict)

    @property
    def emails(self) -> list[str]:
        return [e.email.lower() for e in self.engineers if e.email]

    @property
    def explicit_ids(self) -> list[int]:
        return [e.id for e in self.engineers if e.id]


class ConfigError(Exception):
    """Konfigurasi tidak valid / tidak lengkap."""


def load_config(path: str | Path) -> Config:
    """Muat config.yaml. Token boleh dari env CLICKUP_TOKEN agar tidak ditulis ke file."""

    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"File konfigurasi '{path}' tidak ditemukan. "
            "Salin config.example.yaml menjadi config.yaml lalu isi."
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    token = os.environ.get("CLICKUP_TOKEN") or raw.get("token") or ""
    token = token.strip()
    if not token:
        raise ConfigError(
            "Token ClickUp kosong. Set environment variable CLICKUP_TOKEN "
            "atau isi field 'token' di config.yaml."
        )

    engineers_raw = raw.get("engineers") or []
    if not engineers_raw:
        raise ConfigError("Daftar 'engineers' di config.yaml kosong.")

    engineers: list[Engineer] = []
    for item in engineers_raw:
        if not isinstance(item, dict):
            raise ConfigError(f"Entri engineer tidak valid: {item!r}")
        name = item.get("name") or item.get("email") or str(item.get("id"))
        eng = Engineer(name=name, email=item.get("email"), id=item.get("id"))
        if not eng.email and not eng.id:
            raise ConfigError(f"Engineer '{name}' harus punya 'email' atau 'id'.")
        engineers.append(eng)

    db_section = raw.get("db") or {}
    db_dsn = os.environ.get("SCORECARD_DSN") or db_section.get("dsn") or None
    if db_dsn:
        db_dsn = db_dsn.strip() or None

    gitlab = _parse_gitlab(raw.get("gitlab") or {})

    return Config(
        token=token,
        engineers=engineers,
        team_id=str(raw["team_id"]) if raw.get("team_id") else None,
        db_dsn=db_dsn,
        gitlab=gitlab,
    )


def _parse_gitlab(section: dict) -> GitlabConfig | None:
    gl_token = os.environ.get("GITLAB_TOKEN") or section.get("token") or ""
    gl_token = gl_token.strip()
    projects = [str(p) for p in (section.get("projects") or [])]
    url = (section.get("url") or "https://git.bluebird.id").strip()
    if not gl_token or not projects:
        return None  # GitLab tidak aktif tanpa token + daftar project
    aliases = {
        str(k).lower(): str(v).lower()
        for k, v in (section.get("aliases") or {}).items()
    }
    noise = [str(p) for p in (section.get("noise_patterns") or [])]
    return GitlabConfig(url=url, token=gl_token, projects=projects, aliases=aliases, noise_patterns=noise)
