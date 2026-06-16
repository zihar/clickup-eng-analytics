"""Sumber data commit GitLab dari DB squad-scorecard (tabel engineer_commit_days).

squad-scorecard sudah meng-ETL commit per engineer per repo per hari. Yang penting:
kolom `engineer_id` di tabel itu = **id user ClickUp**, jadi bisa di-join langsung
dengan engineer di tool ini tanpa perlu memanggil GitLab API sama sekali.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import psycopg
except ImportError:  # driver opsional — fitur commit dilewati kalau tak ada
    psycopg = None


class DBError(Exception):
    pass


@dataclass
class CommitStats:
    commits: int = 0
    additions: int = 0
    deletions: int = 0
    active_days: int = 0
    repos: int = 0


_SQL = """
    SELECT engineer_id,
           COALESCE(SUM(commit_count), 0),
           COALESCE(SUM(additions), 0),
           COALESCE(SUM(deletions), 0),
           COUNT(DISTINCT commit_date),
           COUNT(DISTINCT gitlab_project_id)
    FROM engineer_commit_days
    WHERE engineer_id = ANY(%s)
      AND commit_date >= %s
      AND commit_date <= %s
    GROUP BY engineer_id
"""


def fetch_commit_stats(
    dsn: str,
    engineer_ids: list[int],
    since_date: str,
    until_date: str,
) -> dict[int, CommitStats]:
    """Agregasi commit per engineer dari DB. Key hasil = id ClickUp (int)."""
    if psycopg is None:
        raise DBError("Driver psycopg tidak terpasang (pip install 'psycopg[binary]').")

    ids = [str(i) for i in engineer_ids]
    out: dict[int, CommitStats] = {}
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL, (ids, since_date, until_date))
                for eid, commits, adds, dels, days, repos in cur.fetchall():
                    try:
                        key = int(eid)
                    except (TypeError, ValueError):
                        continue
                    out[key] = CommitStats(commits, adds, dels, days, repos)
    except psycopg.Error as exc:  # type: ignore[union-attr]
        raise DBError(str(exc)) from exc
    return out
