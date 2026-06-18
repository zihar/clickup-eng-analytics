"""Klien tipis untuk ClickUp REST API v2.

Menangani autentikasi, paginasi, dan rate limit (429) secara transparan.
Dok API: https://clickup.com/api
"""

from __future__ import annotations

import json
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

    def get_team_fields(self, team_id: str) -> list[dict]:
        """Kembalikan semua custom field workspace (untuk resolve field 'Developer' by name)."""
        return self._get(f"/team/{team_id}/field").get("fields", [])

    # ---------------------------------------------------------------- Tasks
    def iter_team_tasks(
        self,
        team_id: str,
        *,
        developer_field_id: str,
        developer_ids: list[int],
        date_done_gt: int | None = None,
        date_done_lt: int | None = None,
        date_updated_gt: int | None = None,
        include_closed: bool = True,
        subtasks: bool = True,
    ) -> Iterator[dict]:
        """Iterasi task pada workspace, terfilter custom field 'Developer' + rentang tanggal selesai.

        Memakai endpoint 'filtered team tasks' dengan filter server-side custom_fields
        agar atribusi task→engineer mengikuti kolom Developer (bukan assignee).
        """
        page = 0
        while True:
            params: dict[str, Any] = {
                "page": page,
                "include_closed": str(include_closed).lower(),
                "subtasks": str(subtasks).lower(),
                "order_by": "updated",
            }
            params["custom_fields"] = json.dumps(
                [{"field_id": developer_field_id, "operator": "ANY", "value": list(developer_ids)}]
            )
            if date_done_gt is not None:
                params["date_done_gt"] = date_done_gt
            if date_done_lt is not None:
                params["date_done_lt"] = date_done_lt
            if date_updated_gt is not None:
                params["date_updated_gt"] = date_updated_gt

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

