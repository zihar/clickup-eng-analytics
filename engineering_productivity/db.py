"""Sumber data commit GitLab dari DB squad-scorecard (tabel engineer_commit_days).

squad-scorecard sudah meng-ETL commit per engineer per repo per hari. Yang penting:
kolom `engineer_id` di tabel itu = **id user ClickUp**, jadi bisa di-join langsung
dengan engineer di tool ini tanpa perlu memanggil GitLab API sama sekali.
"""

from __future__ import annotations

from .models import CommitStats  # re-export untuk kompatibilitas

try:
    import psycopg
except ImportError:  # driver opsional — fitur commit dilewati kalau tak ada
    psycopg = None


class DBError(Exception):
    pass


__all__ = ["CommitStats", "DBError", "fetch_commit_stats", "fetch_commit_freshness"]


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


def fetch_commit_freshness(dsn: str) -> tuple[str | None, str | None]:
    """Kembalikan (commit_date terbaru, last_sync_at terakhir) sebagai string ISO.

    Dipakai untuk memperingatkan kalau data commit basi dibanding jendela ClickUp.
    """
    if psycopg is None:
        raise DBError("Driver psycopg tidak terpasang (pip install 'psycopg[binary]').")
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(commit_date)::text, MAX(last_sync_at)::text FROM engineer_commit_days"
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else (None, None)
    except psycopg.Error as exc:  # type: ignore[union-attr]
        raise DBError(str(exc)) from exc
