"""Sumber data commit langsung dari GitLab REST API v4.

Alternatif live untuk DB squad-scorecard: tarik commit per project pada rentang
waktu, lalu atribusikan ke engineer lewat email penulis commit (+ alias).
Selalu mutakhir dan bisa membawa additions/deletions asli.
Dok API: https://docs.gitlab.com/ee/api/commits.html
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from urllib.parse import quote

import requests

from .models import CommitStats

PER_PAGE = 100


class GitLabError(Exception):
    pass


class GitLabClient:
    def __init__(self, base_url: str, token: str, *, max_retries: int = 5, session: requests.Session | None = None):
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        self.max_retries = max_retries

    def _get(self, path: str, params: dict) -> list:
        url = f"{self.base}{path}"
        for attempt in range(self.max_retries):
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", "2")) * (attempt + 1))
                continue
            if resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if not resp.ok:
                raise GitLabError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise GitLabError(f"Gagal GET {path} setelah {self.max_retries} percobaan.")

    def find_user_id(self, *, email: str | None = None, name: str | None = None) -> int | None:
        """Cari id user GitLab. Coba username (= bagian lokal email) lalu nama.

        Search by email butuh hak admin, jadi tidak diandalkan.
        """
        if email:
            local = email.split("@")[0]
            res = self._get("/api/v4/users", {"username": local})
            if res:
                return res[0]["id"]
        if name:
            res = self._get("/api/v4/users", {"search": name})
            for u in res:
                if (u.get("name") or "").lower() == name.lower():
                    return u["id"]
            if res:
                return res[0]["id"]
        return None

    def iter_push_events(self, user_id: int, after: str, before: str):
        """Iterasi event push user (after/before eksklusif, format YYYY-MM-DD)."""
        page = 1
        while True:
            data = self._get(
                f"/api/v4/users/{user_id}/events",
                {"action": "pushed", "after": after, "before": before, "per_page": PER_PAGE, "page": page},
            )
            if not data:
                break
            yield from data
            if len(data) < PER_PAGE:
                break
            page += 1

    def iter_commits(self, project: str, since_iso: str, until_iso: str, *, with_stats: bool = True):
        """Iterasi commit satu project pada rentang waktu (semua branch)."""
        pid = quote(str(project), safe="")
        page = 1
        while True:
            params = {
                "since": since_iso,
                "until": until_iso,
                "per_page": PER_PAGE,
                "page": page,
                "with_stats": "true" if with_stats else "false",
                "all": "true",
            }
            data = self._get(f"/api/v4/projects/{pid}/repository/commits", params)
            if not data:
                break
            yield from data
            if len(data) < PER_PAGE:
                break
            page += 1


def discover_project_ids(
    client: GitLabClient,
    engineers: list[tuple[str | None, str]],
    since_date: str,
    until_date: str,
    *,
    on_warn=None,
) -> set[str]:
    """Temukan repo yang di-push tiap engineer pada periode (independen dari scorecard).

    engineers = daftar (email, nama). Mengembalikan himpunan project id (string).
    """
    after = (date.fromisoformat(since_date) - timedelta(days=1)).isoformat()
    before = (date.fromisoformat(until_date) + timedelta(days=1)).isoformat()
    ids: set[str] = set()
    for email, name in engineers:
        try:
            uid = client.find_user_id(email=email, name=name)
        except GitLabError as exc:
            if on_warn:
                on_warn(f"cari user {name}: {exc}")
            continue
        if not uid:
            if on_warn:
                on_warn(f"user GitLab tak ditemukan: {name}")
            continue
        try:
            for ev in client.iter_push_events(uid, after, before):
                pid = ev.get("project_id")
                if pid is not None:
                    ids.add(str(pid))
        except GitLabError as exc:
            if on_warn:
                on_warn(f"events {name}: {exc}")
    return ids


def _accumulator():
    return {"commits": 0, "additions": 0, "deletions": 0, "days": set(), "repos": set(), "shas": set()}


def fetch_commit_stats(
    client: GitLabClient,
    projects: list[str],
    email_to_engineer: dict[str, int],
    since_date: str,
    until_date: str,
    *,
    on_warn=None,
) -> dict[int, CommitStats]:
    """Agregasi commit per engineer dari GitLab. Key hasil = id ClickUp (int).

    email_to_engineer memetakan email penulis commit (lowercase, termasuk alias)
    ke id engineer ClickUp. Commit dari email tak dikenal diabaikan.
    """
    since_iso = f"{since_date}T00:00:00Z"
    until_iso = f"{until_date}T23:59:59Z"
    acc: dict[int, dict] = {}

    for project in projects:
        try:
            for c in client.iter_commits(project, since_iso, until_iso):
                email = (c.get("author_email") or "").lower()
                eng = email_to_engineer.get(email)
                if eng is None:
                    continue
                sha = c.get("id")
                a = acc.setdefault(eng, _accumulator())
                if sha in a["shas"]:
                    continue  # commit yang sama muncul di banyak branch
                a["shas"].add(sha)
                a["commits"] += 1
                stats = c.get("stats") or {}
                a["additions"] += int(stats.get("additions") or 0)
                a["deletions"] += int(stats.get("deletions") or 0)
                day = (c.get("committed_date") or c.get("created_at") or "")[:10]
                if day:
                    a["days"].add(day)
                a["repos"].add(str(project))
        except GitLabError as exc:
            if on_warn:
                on_warn(f"project {project}: {exc}")
            continue

    return {
        eng: CommitStats(
            commits=a["commits"],
            additions=a["additions"],
            deletions=a["deletions"],
            active_days=len(a["days"]),
            repos=len(a["repos"]),
        )
        for eng, a in acc.items()
    }
