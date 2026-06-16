"""Klien tipis untuk ClickUp REST API v2.

Menangani autentikasi, paginasi, dan rate limit (429) secara transparan.
Dok API: https://clickup.com/api
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import requests

BASE_URL = "https://api.clickup.com/api/v2"
PAGE_SIZE = 100  # batas maksimum endpoint filtered team tasks


class ClickUpError(Exception):
    pass


class ClickUpClient:
    def __init__(self, token: str, *, max_retries: int = 5, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": token, "Content-Type": "application/json"})
        self.max_retries = max_retries

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        url = f"{BASE_URL}{path}"
        for attempt in range(self.max_retries):
            resp = self.session.request(method, url, timeout=30, **kwargs)

            if resp.status_code == 429:
                # Rate limited. Hormati header Retry-After bila ada.
                retry_after = float(resp.headers.get("Retry-After", "2"))
                time.sleep(max(retry_after, 1.0) * (attempt + 1))
                continue

            if resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue

            if not resp.ok:
                raise ClickUpError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")

            return resp.json()

        raise ClickUpError(f"Gagal {method} {path} setelah {self.max_retries} percobaan (rate limit / server error).")

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    # --------------------------------------------------------------- Teams
    def get_teams(self) -> list[dict]:
        return self._get("/team").get("teams", [])

    def resolve_team_id(self, team_id: str | None) -> str:
        teams = self.get_teams()
        if not teams:
            raise ClickUpError("Token tidak punya akses ke workspace/team mana pun.")
        if team_id:
            for t in teams:
                if str(t["id"]) == str(team_id):
                    return str(team_id)
            raise ClickUpError(
                f"team_id '{team_id}' tidak ada di daftar team token ini: "
                f"{[ (t['id'], t['name']) for t in teams ]}"
            )
        return str(teams[0]["id"])

    def get_members(self, team_id: str) -> list[dict]:
        """Kembalikan daftar member workspace: [{id, username, email}, ...]."""
        teams = self.get_teams()
        for t in teams:
            if str(t["id"]) == str(team_id):
                return [m["user"] for m in t.get("members", [])]
        return []

    # ---------------------------------------------------------------- Tasks
    def iter_team_tasks(
        self,
        team_id: str,
        *,
        assignee_ids: list[int],
        date_done_gt: int | None = None,
        date_done_lt: int | None = None,
        include_closed: bool = True,
        subtasks: bool = True,
    ) -> Iterator[dict]:
        """Iterasi task pada workspace, terfilter assignee + rentang tanggal selesai.

        Memakai endpoint 'filtered team tasks' yang memang dirancang untuk query
        lintas space dengan filter assignee.
        """
        page = 0
        while True:
            params: dict[str, Any] = {
                "page": page,
                "include_closed": str(include_closed).lower(),
                "subtasks": str(subtasks).lower(),
                "order_by": "updated",
            }
            for uid in assignee_ids:
                params.setdefault("assignees[]", []).append(uid)
            if date_done_gt is not None:
                params["date_done_gt"] = date_done_gt
            if date_done_lt is not None:
                params["date_done_lt"] = date_done_lt

            data = self._get(f"/team/{team_id}/task", params=params)
            tasks = data.get("tasks", [])
            for t in tasks:
                yield t

            if data.get("last_page") or len(tasks) < PAGE_SIZE:
                break
            page += 1

    def get_time_in_status(self, task_id: str) -> dict:
        """Riwayat waktu per status untuk satu task (dipakai untuk cycle time & bottleneck)."""
        return self._get(f"/task/{task_id}/time_in_status")

    # ---------------------------------------------------------- Time entries
    def iter_time_entries(
        self,
        team_id: str,
        *,
        start_date: int,
        end_date: int,
        assignee_ids: list[int],
    ) -> Iterator[dict]:
        """Iterasi entri time-tracking dalam rentang waktu, per engineer.

        Lebih akurat untuk 'time tracked per orang' dibanding field time_spent task,
        karena time_spent task adalah total seluruh assignee.
        """
        params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
        # API menerima assignee sebagai daftar dipisah koma.
        if assignee_ids:
            params["assignee"] = ",".join(str(i) for i in assignee_ids)
        data = self._get(f"/team/{team_id}/time_entries", params=params)
        for entry in data.get("data", []):
            yield entry
