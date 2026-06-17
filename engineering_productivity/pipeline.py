"""Pipeline pengambilan data yang reusable (dipakai CLI maupun dashboard).

Mengorkestrasi: resolve engineer -> tarik task ClickUp -> time_in_status (deep) ->
time entries -> aktivitas commit (GitLab live / DB scorecard) -> build_report_data.
Semua progres dilaporkan lewat callback `progress` agar bebas dari I/O (CLI cetak
ke stderr, Streamlit tampilkan di spinner/status).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import ClickUpClient, ClickUpError
from .config import Config
from .db import DBError, fetch_commit_freshness
from .db import fetch_commit_stats as db_fetch_commit_stats
from .gitlab import GitLabClient, GitLabError, discover_project_ids
from .gitlab import fetch_commit_stats as gl_fetch_commit_stats
from .metrics import ReportData, build_report_data

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


@dataclass
class GatherOptions:
    since: str | None = None
    until: str | None = None
    days: int = 30
    tz: float = 7.0
    deep: bool = False
    max_age: int | None = None
    commits_source: str = "auto"  # auto | gitlab | db | none
    no_discover: bool = False
    exclude_noise: bool = False
    no_commits: bool = False
    last_done: bool = False          # hitung tanggal task terakhir selesai (lintas periode)
    last_done_lookback: int = 365    # batas mundur pencarian last-done (hari)
    utilization: bool = False        # analisis utilisasi (WIP + story point + skor relatif)


def parse_date(text: str, tz_offset: float, *, end_of_day: bool = False) -> int:
    tz = timezone(timedelta(hours=tz_offset))
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def resolve_targets(config: Config, members: list[dict], progress: Progress = _noop) -> tuple[set[int], dict[int, str]]:
    """Petakan engineer di config -> id ClickUp + nama tampilan."""
    email_to_member = {(m.get("email") or "").lower(): m for m in members}
    target_ids: set[int] = set()
    id_to_name: dict[int, str] = {}
    unresolved: list[str] = []

    for eng in config.engineers:
        uid = eng.id
        if uid is None and eng.email:
            member = email_to_member.get(eng.email.lower())
            if member:
                uid = member.get("id")
        if uid is None:
            unresolved.append(eng.name)
            continue
        target_ids.add(uid)
        id_to_name[uid] = eng.name

    if unresolved:
        progress(f"[!] Engineer tidak ketemu di workspace (cek email/id): {', '.join(unresolved)}")
    return target_ids, id_to_name


def resolve_commit_source(choice: str, config: Config) -> str:
    """Tentukan sumber commit efektif. 'auto' utamakan GitLab (live) lalu DB."""
    if choice == "gitlab":
        return "gitlab" if config.gitlab else "none"
    if choice == "db":
        return "db" if config.db_dsn else "none"
    if choice == "none":
        return "none"
    # auto
    if config.gitlab:
        return "gitlab"
    if config.db_dsn:
        return "db"
    return "none"


def build_gitlab_email_map(config: Config, members: list[dict]) -> dict[str, int]:
    """Petakan email penulis commit -> id engineer ClickUp (termasuk alias)."""
    email_to_member = {(m.get("email") or "").lower(): m for m in members}
    out: dict[str, int] = {}
    for eng in config.engineers:
        uid = eng.id
        if uid is None and eng.email:
            member = email_to_member.get(eng.email.lower())
            if member:
                uid = member.get("id")
        if uid is not None and eng.email:
            out[eng.email.lower()] = uid
    if config.gitlab:
        for alias, canonical in config.gitlab.aliases.items():
            if canonical in out:
                out[alias] = out[canonical]
    return out


def resolve_window(opts: GatherOptions) -> tuple[str, str]:
    """Hitung (since, until) string YYYY-MM-DD dari opsi."""
    now = datetime.now(timezone(timedelta(hours=opts.tz)))
    until_str = opts.until or now.strftime("%Y-%m-%d")
    since_str = opts.since or (now - timedelta(days=opts.days)).strftime("%Y-%m-%d")
    return since_str, until_str


def gather_report(
    config: Config,
    opts: GatherOptions,
    *,
    client: ClickUpClient | None = None,
    members: list[dict] | None = None,
    progress: Progress = _noop,
) -> ReportData:
    """Jalankan seluruh pipeline dan kembalikan ReportData siap render."""
    client = client or ClickUpClient(config.token)
    team_id = client.resolve_team_id(config.team_id)
    if members is None:
        members = client.get_members(team_id)

    target_ids, id_to_name = resolve_targets(config, members, progress)
    if not target_ids:
        raise ClickUpError("Tidak ada engineer yang ter-resolve. Periksa daftar engineer di config.")

    since_str, until_str = resolve_window(opts)
    date_done_gt = parse_date(since_str, opts.tz)
    date_done_lt = parse_date(until_str, opts.tz, end_of_day=True)

    progress(f"[*] Menarik task {len(target_ids)} engineer, {since_str} s/d {until_str} ...")
    tasks = list(
        client.iter_team_tasks(
            team_id,
            assignee_ids=sorted(target_ids),
            date_done_gt=date_done_gt,
            date_done_lt=date_done_lt,
        )
    )
    progress(f"[*] {len(tasks)} task selesai ditemukan.")

    time_in_status = None
    if opts.deep:
        time_in_status = {}
        progress(f"[*] Mode deep: mengambil riwayat status {len(tasks)} task ...")
        for i, task in enumerate(tasks, 1):
            try:
                time_in_status[task["id"]] = client.get_time_in_status(task["id"])
            except ClickUpError as exc:
                progress(f"    [!] gagal time_in_status {task['id']}: {exc}")
            if i % 25 == 0:
                progress(f"    ... {i}/{len(tasks)}")

    progress("[*] Menarik time entries ...")
    try:
        time_entries = list(
            client.iter_time_entries(
                team_id,
                start_date=date_done_gt,
                end_date=date_done_lt,
                assignee_ids=sorted(target_ids),
            )
        )
    except ClickUpError as exc:
        progress(f"    [!] Time entries dilewati (metrik 'time tracked' kosong): {exc}")
        time_entries = []

    commit_stats = None
    commit_through = commit_synced_at = commit_source = None
    source = "none" if opts.no_commits else resolve_commit_source(opts.commits_source, config)

    if source == "gitlab":
        commit_source = "GitLab API (live)"
        try:
            gl = GitLabClient(config.gitlab.url, config.gitlab.token)
            projects = {str(p) for p in config.gitlab.projects}
            if not opts.no_discover:
                progress("[*] Auto-discover repo per engineer dari GitLab ...")
                discovered = discover_project_ids(
                    gl,
                    [(e.email, e.name) for e in config.engineers if e.email],
                    since_str, until_str, on_warn=progress,
                )
                progress(f"    {len(discovered)} repo dari aktivitas push + {len(projects)} dari seed.")
                projects |= discovered
            noise_msg = " (filter noise: ambil diff tiap commit, agak lambat)" if opts.exclude_noise else ""
            progress(f"[*] Menarik commit langsung dari GitLab API ({len(projects)} repo){noise_msg} ...")
            email_map = build_gitlab_email_map(config, members)
            progress_state = {"n": 0}

            def _tick() -> None:
                progress_state["n"] += 1
                if progress_state["n"] % 100 == 0:
                    progress(f"    ... {progress_state['n']} diff diproses")

            commit_stats = gl_fetch_commit_stats(
                gl, sorted(projects), email_map, since_str, until_str,
                exclude_noise=opts.exclude_noise,
                noise_patterns=config.gitlab.noise_patterns,
                on_warn=progress,
                on_progress=_tick if opts.exclude_noise else None,
            )
        except GitLabError as exc:
            progress(f"    [!] Commit GitLab dilewati: {exc}")
            commit_stats = None
    elif source == "db":
        commit_source = "DB squad-scorecard"
        progress("[*] Menarik aktivitas commit dari DB squad-scorecard ...")
        try:
            commit_stats = db_fetch_commit_stats(config.db_dsn, sorted(target_ids), since_str, until_str)
            commit_through, commit_synced_at = fetch_commit_freshness(config.db_dsn)
            if commit_through and commit_through < until_str:
                progress(
                    f"    [!] Commit hanya tersinkron s/d {commit_through} "
                    f"(periode s/d {until_str}) — data ETL belum mutakhir."
                )
        except DBError as exc:
            progress(f"    [!] Commit dilewati (DB tak terjangkau): {exc}")
            commit_stats = None

    last_done_ms: dict[int, int] | None = None
    if opts.last_done:
        lookback_lo = (
            datetime.strptime(until_str, "%Y-%m-%d") - timedelta(days=opts.last_done_lookback)
        ).strftime("%Y-%m-%d")
        progress(f"[*] Mencari tanggal task terakhir selesai (lookback {opts.last_done_lookback} hari) ...")
        last_done_ms = {}
        for t in client.iter_team_tasks(
            team_id,
            assignee_ids=sorted(target_ids),
            date_done_gt=parse_date(lookback_lo, opts.tz),
            date_done_lt=date_done_lt,
        ):
            raw = t.get("date_done") or t.get("date_closed")
            try:
                dd = int(raw)
            except (TypeError, ValueError):
                continue
            for a in t.get("assignees") or []:
                aid = a.get("id")
                if aid in target_ids and dd > last_done_ms.get(aid, 0):
                    last_done_ms[aid] = dd

    open_tasks_count: dict[int, int] | None = None
    open_story_points: dict[int, float] | None = None
    if opts.utilization:
        progress("[*] Menarik task open (WIP & story point) ...")
        open_tasks_count, open_story_points = {}, {}
        for t in client.iter_team_tasks(team_id, assignee_ids=sorted(target_ids), include_closed=False):
            raw = t.get("points")
            try:
                pts = float(raw) if raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pts = 0.0
            for a in t.get("assignees") or []:
                aid = a.get("id")
                if aid in target_ids:
                    open_tasks_count[aid] = open_tasks_count.get(aid, 0) + 1
                    open_story_points[aid] = open_story_points.get(aid, 0.0) + pts

    return build_report_data(
        tasks,
        id_to_name=id_to_name,
        target_ids=target_ids,
        time_in_status=time_in_status,
        time_entries=time_entries,
        since=since_str,
        until=until_str,
        tz_offset=opts.tz,
        max_age_days=opts.max_age,
        commit_stats=commit_stats,
        commit_through=commit_through,
        commit_synced_at=commit_synced_at,
        commit_source=commit_source,
        commit_noise_filtered=bool(commit_stats is not None and source == "gitlab" and opts.exclude_noise),
        last_done_ms=last_done_ms,
        last_done_lookback_days=opts.last_done_lookback if opts.last_done else None,
        open_tasks=open_tasks_count,
        open_story_points=open_story_points,
        utilization=opts.utilization,
    )
