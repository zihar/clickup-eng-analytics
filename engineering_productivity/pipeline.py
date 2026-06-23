"""Pipeline pengambilan data yang reusable (dipakai CLI maupun dashboard).

Mengorkestrasi: resolve engineer -> tarik task ClickUp -> time_in_status (deep) ->
time entries -> aktivitas commit (GitLab live) -> build_report_data.
Semua progres dilaporkan lewat callback `progress` agar bebas dari I/O (CLI cetak
ke stderr, Streamlit tampilkan di spinner/status).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import ClickUpClient, ClickUpError
from .config import Config
from .gitlab import GitLabClient, GitLabError, discover_project_ids
from .gitlab import fetch_commit_stats as gl_fetch_commit_stats
from .metrics import (
    TERMINAL_STATUS_TYPES,
    ReportData,
    build_report_data,
    task_developer_ids,
    to_int_ms,
)
from .models import CommitStats
from .store import Store, StoreError

Progress = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


@dataclass
class GatherOptions:
    since: str | None = None
    until: str | None = None
    days: int = 30
    tz: float = 7.0                   # WIB tetap (+7); tidak diekspos ke UI/CLI
    deep: bool = False
    max_age: int | None = None
    no_discover: bool = False
    exclude_noise: bool = False
    no_commits: bool = False          # commit selalu dari GitLab; True hanya untuk mematikan
    last_done: bool = False          # hitung tanggal task terakhir selesai (lintas periode)
    last_done_lookback: int = 365    # batas mundur pencarian last-done (hari)
    utilization: bool = True         # fitur utama: selalu nyala
    offline: bool = False            # baca dari cache DB saja (tanpa fetch live ClickUp/GitLab)


def parse_date(text: str, tz_offset: float, *, end_of_day: bool = False) -> int:
    tz = timezone(timedelta(hours=tz_offset))
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _resolve_developer_field(client: ClickUpClient, team_id: str, field_name: str) -> str:
    """Cari id custom field 'Developer' by name (case-insensitive) di workspace."""
    fields = client.get_team_fields(team_id)
    target = (field_name or "").strip().lower()
    for f in fields:
        if (f.get("name") or "").strip().lower() == target:
            return str(f["id"])
    available = [f.get("name") for f in fields]
    raise ClickUpError(
        f"Custom field '{field_name}' tidak ditemukan di workspace {team_id}. "
        f"Set 'developer_field_id' di config atau perbaiki 'developer_field_name'. "
        f"Field tersedia: {available}"
    )


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


def resolve_commit_source(config: Config) -> str:
    """Sumber commit hanya GitLab (live) — aktif bila GitLab terkonfigurasi."""
    return "gitlab" if config.gitlab else "none"


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
    store: Store | None = None,
    progress: Progress = _noop,
) -> ReportData:
    """Jalankan seluruh pipeline dan kembalikan ReportData siap render."""
    # Mode offline: baca semua dari cache DB, tanpa fetch live (dipakai dashboard
    # sehari-hari; refresh DB dilakukan job nightly yang jalan dengan offline=False).
    if opts.offline:
        if store is None and config.store_dsn:
            try:
                store = Store.connect(config.store_dsn)
                own_store_off = True
            except StoreError as exc:
                raise ClickUpError(f"Mode offline butuh cache DB, tapi gagal konek: {exc}") from exc
        else:
            own_store_off = False
        if store is None:
            raise ClickUpError("Mode offline butuh cache DB (set store.dsn / EP_STORE_DSN).")
        try:
            return _gather_offline(config, opts, store, progress)
        finally:
            if own_store_off:
                store.close()

    client = client or ClickUpClient(config.token)
    team_id = client.resolve_team_id(config.team_id)
    if members is None:
        members = client.get_members(team_id)

    # Atribusi task→engineer lewat custom field "Developer". Field id di-resolve
    # sekali per run: override config kalau ada, jika tidak auto-discover by name.
    dev_field_id = config.developer_field_id or _resolve_developer_field(
        client, team_id, config.developer_field_name
    )

    # Cache DB (opsional): time_in_status + commit. Fallback live bila tak terjangkau.
    own_store = False
    if store is None and config.store_dsn:
        try:
            store = Store.connect(config.store_dsn)
            own_store = True
            progress("[*] Cache DB aktif.")
        except StoreError as exc:
            progress(f"    [!] Cache DB nonaktif (live): {exc}")
            store = None

    # Simpan meta workspace ke cache supaya mode offline tak perlu panggil ClickUp.
    if store is not None:
        try:
            store.set_meta("workspace", {"team_id": team_id, "members": members,
                                         "dev_field_id": dev_field_id})
            store.commit()
        except StoreError as exc:
            progress(f"    [!] gagal simpan meta workspace: {exc}")

    target_ids, id_to_name = resolve_targets(config, members, progress)
    if not target_ids:
        raise ClickUpError("Tidak ada engineer yang ter-resolve. Periksa daftar engineer di config.")

    since_str, until_str = resolve_window(opts)
    date_done_gt = parse_date(since_str, opts.tz)
    date_done_lt = parse_date(until_str, opts.tz, end_of_day=True)

    # Sumber task: bila store aktif, sinkron incremental ke cache lalu derive dari cache
    # (menghilangkan 3 query Developer yang lambat). Tanpa store → jalur live (fallback).
    stored_tasks: list[dict] | None = None
    if store is not None:
        backfill_since_ms = parse_date(config.task_backfill_since, opts.tz)
        _sync_tasks(client, store, team_id, dev_field_id, target_ids, backfill_since_ms, progress)
        stored_tasks = store.get_tasks(sorted(target_ids))
        progress(f"[*] {len(stored_tasks)} task dari cache (semua status).")

    if stored_tasks is not None:
        # Task selesai-di-window: date_done dalam [date_done_gt, date_done_lt].
        tasks = [
            t for t in stored_tasks
            if (dd := (to_int_ms(t.get("date_done")) or to_int_ms(t.get("date_closed")))) is not None
            and date_done_gt <= dd <= date_done_lt
        ]
    else:
        progress(f"[*] Menarik task {len(target_ids)} engineer, {since_str} s/d {until_str} ...")
        tasks = list(
            client.iter_team_tasks(
                team_id,
                developer_field_id=dev_field_id,
                developer_ids=sorted(target_ids),
                date_done_gt=date_done_gt,
                date_done_lt=date_done_lt,
            )
        )
    progress(f"[*] {len(tasks)} task selesai ditemukan.")

    time_in_status = _fetch_time_in_status(client, tasks, store, progress) if opts.deep else None

    commit_stats = None
    commit_source = None
    repo_names: dict[str, str] = {}
    source = "none" if opts.no_commits else resolve_commit_source(config)

    if source == "gitlab":
        commit_source = "GitLab API (live)"
        try:
            gl = GitLabClient(config.gitlab.url, config.gitlab.token)
            projects = {str(p) for p in config.gitlab.projects}
            if not opts.no_discover:
                eng_pairs = [(e.email, e.name) for e in config.engineers if e.email]
                if store is not None:
                    progress("[*] Auto-discover repo (incremental via cache DB) ...")
                    discovered = _discover_via_store(gl, store, eng_pairs, since_str, until_str, progress)
                else:
                    progress("[*] Auto-discover repo per engineer dari GitLab ...")
                    discovered = discover_project_ids(gl, eng_pairs, since_str, until_str, on_warn=progress)
                progress(f"    {len(discovered)} repo dari aktivitas push + {len(projects)} dari seed.")
                projects |= discovered

            # Resolve nama repo + liveness (cache di store). Buang repo mati (404) dari
            # fetch commit supaya tak boros 1 request/run untuk repo yang sudah dihapus.
            resolved = _resolve_repos(gl, store, projects, progress)
            repo_names = {p: r["path"] for p, r in resolved.items() if r.get("path")}
            dead = {p for p, r in resolved.items() if r.get("alive") is False}
            if dead:
                progress(f"    {len(dead)} repo mati (404) di-skip.")
                projects -= dead

            email_map = build_gitlab_email_map(config, members)
            if store and not opts.exclude_noise:
                # Jalur cache: fetch hanya rentang yang belum ter-cover, agregasi dari DB.
                progress(f"[*] Commit GitLab via cache DB ({len(projects)} repo, incremental) ...")
                commit_stats = _commits_via_store(
                    gl, store, sorted(projects), email_map, since_str, until_str, progress,
                )
            else:
                noise_msg = " (filter noise: ambil diff tiap commit, agak lambat)" if opts.exclude_noise else ""
                progress(f"[*] Menarik commit langsung dari GitLab API ({len(projects)} repo){noise_msg} ...")
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

    last_done_ms: dict[int, int] | None = None
    if opts.last_done:
        lookback_lo = (
            datetime.strptime(until_str, "%Y-%m-%d") - timedelta(days=opts.last_done_lookback)
        ).strftime("%Y-%m-%d")
        lookback_lo_ms = parse_date(lookback_lo, opts.tz)
        progress(f"[*] Mencari tanggal task terakhir selesai (lookback {opts.last_done_lookback} hari) ...")
        last_done_ms = {}
        if stored_tasks is not None:
            ld_source = stored_tasks
        else:
            ld_source = client.iter_team_tasks(
                team_id,
                developer_field_id=dev_field_id,
                developer_ids=sorted(target_ids),
                date_done_gt=lookback_lo_ms,
                date_done_lt=date_done_lt,
            )
        for t in ld_source:
            dd = to_int_ms(t.get("date_done")) or to_int_ms(t.get("date_closed"))
            if dd is None or dd < lookback_lo_ms or dd > date_done_lt:
                continue
            for aid in task_developer_ids(t, dev_field_id):
                if aid in target_ids and dd > last_done_ms.get(aid, 0):
                    last_done_ms[aid] = dd

    open_tasks_count: dict[int, int] | None = None
    open_story_points: dict[int, float] | None = None
    if opts.utilization:
        progress("[*] Menghitung task open (WIP & story point) ...")
        open_tasks_count, open_story_points = {}, {}
        if stored_tasks is not None:
            open_source = (
                t for t in stored_tasks
                if ((t.get("status") or {}).get("type") or "").lower() not in TERMINAL_STATUS_TYPES
            )
        else:
            open_source = client.iter_team_tasks(
                team_id,
                developer_field_id=dev_field_id,
                developer_ids=sorted(target_ids),
                include_closed=False,
            )
        for t in open_source:
            raw = t.get("points")
            try:
                pts = float(raw) if raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pts = 0.0
            for aid in task_developer_ids(t, dev_field_id):
                if aid in target_ids:
                    open_tasks_count[aid] = open_tasks_count.get(aid, 0) + 1
                    open_story_points[aid] = open_story_points.get(aid, 0.0) + pts

    data = build_report_data(
        tasks,
        developer_field_id=dev_field_id,
        id_to_name=id_to_name,
        target_ids=target_ids,
        time_in_status=time_in_status,
        since=since_str,
        until=until_str,
        tz_offset=opts.tz,
        max_age_days=opts.max_age,
        commit_stats=commit_stats,
        commit_source=commit_source,
        commit_noise_filtered=bool(commit_stats is not None and source == "gitlab" and opts.exclude_noise),
        last_done_ms=last_done_ms,
        last_done_lookback_days=opts.last_done_lookback if opts.last_done else None,
        open_tasks=open_tasks_count,
        open_story_points=open_story_points,
        utilization=opts.utilization,
        repo_names=repo_names,
    )
    data.cache_since = config.task_backfill_since if store is not None else None
    if own_store and store is not None:
        store.commit()
        store.close()
    return data


def _gather_offline(config: Config, opts: GatherOptions, store: Store, progress: Progress) -> ReportData:
    """Bangun ReportData murni dari cache DB — tanpa fetch live ClickUp/GitLab.

    Meta workspace (members, dev_field_id) diambil dari cache; bila belum ada,
    resolve sekali via ClickUp lalu simpan (one-off ringan).
    """
    progress("[*] Mode offline: baca dari cache DB (tanpa fetch live).")
    meta = store.get_meta("workspace")
    if not meta or not meta.get("members") or not meta.get("dev_field_id"):
        progress("    [*] Meta workspace belum ter-cache; resolve sekali via ClickUp ...")
        client = ClickUpClient(config.token)
        team_id = client.resolve_team_id(config.team_id)
        members = client.get_members(team_id)
        dev_field_id = config.developer_field_id or _resolve_developer_field(
            client, team_id, config.developer_field_name)
        store.set_meta("workspace", {"team_id": team_id, "members": members,
                                     "dev_field_id": dev_field_id})
        store.commit()
    else:
        members = meta["members"]
        dev_field_id = meta["dev_field_id"]

    target_ids, id_to_name = resolve_targets(config, members, progress)
    if not target_ids:
        raise ClickUpError("Tidak ada engineer yang ter-resolve. Periksa daftar engineer di config.")

    since_str, until_str = resolve_window(opts)
    date_done_gt = parse_date(since_str, opts.tz)
    date_done_lt = parse_date(until_str, opts.tz, end_of_day=True)

    stored_tasks = store.get_tasks(sorted(target_ids))
    progress(f"[*] {len(stored_tasks)} task dari cache (semua status).")
    tasks = [
        t for t in stored_tasks
        if (dd := (to_int_ms(t.get("date_done")) or to_int_ms(t.get("date_closed")))) is not None
        and date_done_gt <= dd <= date_done_lt
    ]
    progress(f"[*] {len(tasks)} task selesai ditemukan.")

    time_in_status = store.get_time_in_status([t["id"] for t in tasks]) if opts.deep else None

    # Commit: agregasi langsung dari cache (tanpa GitLab). Set repo = seed ∪ repo
    # yang pernah ter-discover untuk engineer terpilih (last_seen >= since).
    commit_stats = None
    commit_source = None
    repo_names: dict[str, str] = {}
    if not opts.no_commits and config.gitlab:
        emails = [e.email.lower() for e in config.engineers if e.email]
        projects = {str(p) for p in config.gitlab.projects}
        for repos in store.get_engineer_repos(emails).values():
            for r in repos:
                if (r.get("last_seen") or "") >= since_str:
                    projects.add(str(r["project_id"]))
        email_map = build_gitlab_email_map(config, members)
        rows = store.get_commits(sorted(projects), since_str, until_str)
        commit_stats = _aggregate_commit_rows(rows, email_map)
        commit_source = "Cache DB (offline)"
        resolved = store.get_projects(sorted(projects))
        repo_names = {p: r["path"] for p, r in resolved.items() if r.get("path")}

    last_done_ms: dict[int, int] | None = None
    if opts.last_done:
        lookback_lo = (
            datetime.strptime(until_str, "%Y-%m-%d") - timedelta(days=opts.last_done_lookback)
        ).strftime("%Y-%m-%d")
        lookback_lo_ms = parse_date(lookback_lo, opts.tz)
        last_done_ms = {}
        for t in stored_tasks:
            dd = to_int_ms(t.get("date_done")) or to_int_ms(t.get("date_closed"))
            if dd is None or dd < lookback_lo_ms or dd > date_done_lt:
                continue
            for aid in task_developer_ids(t, dev_field_id):
                if aid in target_ids and dd > last_done_ms.get(aid, 0):
                    last_done_ms[aid] = dd

    open_tasks_count: dict[int, int] | None = None
    open_story_points: dict[int, float] | None = None
    if opts.utilization:
        open_tasks_count, open_story_points = {}, {}
        for t in stored_tasks:
            if ((t.get("status") or {}).get("type") or "").lower() in TERMINAL_STATUS_TYPES:
                continue
            raw = t.get("points")
            try:
                pts = float(raw) if raw not in (None, "") else 0.0
            except (TypeError, ValueError):
                pts = 0.0
            for aid in task_developer_ids(t, dev_field_id):
                if aid in target_ids:
                    open_tasks_count[aid] = open_tasks_count.get(aid, 0) + 1
                    open_story_points[aid] = open_story_points.get(aid, 0.0) + pts

    data = build_report_data(
        tasks,
        developer_field_id=dev_field_id,
        id_to_name=id_to_name,
        target_ids=target_ids,
        time_in_status=time_in_status,
        since=since_str,
        until=until_str,
        tz_offset=opts.tz,
        max_age_days=opts.max_age,
        commit_stats=commit_stats,
        commit_source=commit_source,
        commit_noise_filtered=False,
        last_done_ms=last_done_ms,
        last_done_lookback_days=opts.last_done_lookback if opts.last_done else None,
        open_tasks=open_tasks_count,
        open_story_points=open_story_points,
        utilization=opts.utilization,
        repo_names=repo_names,
    )
    data.offline = True
    data.cache_since = config.task_backfill_since
    return data


def _fetch_time_in_status(client, tasks: list[dict], store, progress: Progress) -> dict[str, dict]:
    """Ambil time_in_status tiap task; pakai cache DB bila ada (task done = immutable)."""
    out: dict[str, dict] = {}
    cached = store.get_time_in_status([t["id"] for t in tasks]) if store else {}
    n_cache = n_new = 0
    progress(f"[*] Mode deep: riwayat status {len(tasks)} task ({len(cached)} dari cache) ...")
    for task in tasks:
        tid = task["id"]
        if tid in cached:
            out[tid] = cached[tid]
            n_cache += 1
            continue
        try:
            tis = client.get_time_in_status(tid)
            out[tid] = tis
            if store:  # task selesai = immutable → aman disimpan permanen
                store.put_time_in_status(tid, tis)
            n_new += 1
        except ClickUpError as exc:
            progress(f"    [!] gagal time_in_status {tid}: {exc}")
        if n_new and n_new % 25 == 0:
            progress(f"    ... {n_new} ditarik baru")
    progress(f"    deep: {n_cache} dari cache, {n_new} ditarik baru.")
    return out


def _task_to_row(task: dict, dev_field_id: str) -> dict:
    """Ekstrak kolom yang di-index untuk satu task; payload = task dict utuh."""
    return {
        "task_id": task["id"],
        "payload": task,
        "date_updated": to_int_ms(task.get("date_updated")),
        "developer_ids": task_developer_ids(task, dev_field_id),
        "date_done": to_int_ms(task.get("date_done")) or to_int_ms(task.get("date_closed")),
        "status_type": ((task.get("status") or {}).get("type") or "").lower(),
    }


def _sync_tasks(client, store, team_id, dev_field_id, target_ids, backfill_since_ms, progress) -> None:
    """Sinkronkan cache task ke store: backfill engineer baru sekali, lalu incremental via watermark."""
    target_sorted = sorted(target_ids)

    # 1. Backfill engineer baru (belum pernah di-cover oleh query Developer yang lambat).
    backfilled = store.get_backfilled_engineers()
    new = [i for i in target_sorted if i not in backfilled]
    if new:
        progress(f"[*] Backfill cache task {len(new)} engineer baru (sekali, agak lambat) ...")
        rows = [
            _task_to_row(t, dev_field_id)
            for t in client.iter_team_tasks(
                team_id,
                developer_field_id=dev_field_id,
                developer_ids=new,
                date_updated_gt=backfill_since_ms,
                include_closed=True,
            )
        ]
        store.upsert_tasks(rows)
        store.mark_backfilled(new)
        progress(f"    backfill: {len(rows)} task tersimpan.")

    # 2. Incremental: tarik hanya task yang berubah sejak watermark.
    wm = store.get_task_watermark() or backfill_since_ms
    progress("[*] Sinkron incremental task (delta sejak terakhir) ...")
    max_seen = wm
    n = 0
    rows = []
    for t in client.iter_team_tasks(
        team_id,
        developer_field_id=dev_field_id,
        developer_ids=target_sorted,
        date_updated_gt=wm,
        include_closed=True,
    ):
        row = _task_to_row(t, dev_field_id)
        rows.append(row)
        n += 1
        if row["date_updated"] and row["date_updated"] > max_seen:
            max_seen = row["date_updated"]
    store.upsert_tasks(rows)
    store.set_task_watermark(max_seen)
    store.commit()
    progress(f"    incremental: {n} task delta tersimpan.")


def _coverage_gaps(cov: tuple[str, str] | None, since: str, until: str) -> list[tuple[str, str]]:
    """Rentang [since,until] yang belum ter-cover oleh (earliest,latest). Tanggal YYYY-MM-DD.

    Hari terakhir (until) SELALU dianggap belum ter-cover supaya commit terbaru hari ini
    ikut ter-tarik walau di-run berkali-kali di hari yang sama.
    """
    if cov is None:
        return [(since, until)]
    earliest, latest = cov
    until_prev = (datetime.strptime(until, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if latest > until_prev:
        latest = until_prev  # batasi agar [latest, until] selalu mencakup hari terakhir
    gaps = []
    if since < earliest:
        gaps.append((since, earliest))
    if until > latest:
        gaps.append((latest, until))
    return gaps


def _discover_via_store(gl, store, engineers, since: str, until: str, progress: Progress) -> set[str]:
    """Discover repo per engineer secara incremental: hanya scan rentang yang belum
    ter-cover (mencermin _commits_via_store), persist mapping engineer→repo ke store,
    lalu kembalikan repo yang aktif di window [since, until] dari cache.
    """
    fetched = 0
    emails: list[str] = []
    for email, name in engineers:
        if not email:
            continue
        em = email.lower()
        emails.append(em)
        try:
            uid = gl.find_user_id(email=email, name=name)
        except GitLabError as exc:
            progress(f"    [!] cari user {name}: {exc}")
            continue
        if not uid:
            progress(f"    [!] user GitLab tak ditemukan: {name}")
            continue
        cov = store.get_discovery_coverage(em)
        for gs, gu in _coverage_gaps(cov, since, until):
            after = (datetime.strptime(gs, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            before = (datetime.strptime(gu, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            rows = []
            try:
                for ev in gl.iter_push_events(uid, after, before):
                    pid = ev.get("project_id")
                    if pid is None:
                        continue
                    day = (ev.get("created_at") or "")[:10] or gu
                    rows.append({"engineer_email": em, "project_id": str(pid), "seen_date": day})
            except GitLabError as exc:
                progress(f"    [!] events {name}: {exc}")
                continue
            store.upsert_engineer_repos(rows)
            fetched += len(rows)
        store.set_discovery_coverage(em, since, until)
    store.commit()
    progress(f"    discover: {fetched} push-event delta; mapping repo dari cache.")
    # Repo aktif di window = yang last_seen >= since (untuk engineer terpilih).
    out: set[str] = set()
    for repos in store.get_engineer_repos(emails).values():
        for r in repos:
            if (r.get("last_seen") or "") >= since:
                out.add(str(r["project_id"]))
    return out


def _resolve_repos(gl, store, pids, progress: Progress) -> dict[str, dict]:
    """Resolve nama repo (path_with_namespace) + liveness, pakai cache store bila ada.

    404 → ditandai mati (alive=False) supaya tidak di-query lagi. Error transien lain
    tidak di-cache (dicoba lagi run berikutnya). Mengembalikan {pid: {path, alive}}.
    """
    pids = [str(p) for p in pids]
    cached: dict[str, dict] = dict(store.get_projects(pids)) if store else {}
    missing = [p for p in pids if p not in cached]
    if missing:
        progress(f"[*] Resolve nama {len(missing)} repo baru ...")
    for pid in missing:
        path, alive = None, True
        try:
            path = gl.get_project_path(pid)
        except GitLabError as exc:
            if "404" in str(exc):
                alive = False  # repo dihapus/tak ada akses → tandai mati, skip ke depan
            else:
                progress(f"    [!] nama repo {pid}: {exc}")
                continue  # transien → jangan cache
        if store:
            store.upsert_project(pid, path, alive)
        cached[pid] = {"path": path, "alive": alive}
    if store:
        store.commit()
    return cached


def _aggregate_commit_rows(rows: list[dict], email_to_engineer: dict[str, int]) -> dict[int, CommitStats]:
    acc: dict[int, dict] = {}
    for r in rows:
        eng = email_to_engineer.get((r.get("author_email") or "").lower())
        if eng is None:
            continue
        a = acc.setdefault(eng, {"commits": 0, "adds": 0, "dels": 0, "days": set(),
                                 "repos": set(), "shas": set(), "rows": []})
        sha = r.get("sha")
        if sha in a["shas"]:
            continue
        a["shas"].add(sha)
        a["commits"] += 1
        adds = int(r.get("additions") or 0)
        dels = int(r.get("deletions") or 0)
        a["adds"] += adds
        a["dels"] += dels
        day = (r.get("committed_date") or "")[:10]
        if day:
            a["days"].add(day)
        a["repos"].add(r.get("project_id"))
        a["rows"].append({
            "sha": sha, "project_id": str(r.get("project_id")),
            "committed_date": r.get("committed_date"),
            "additions": adds, "deletions": dels, "title": r.get("title"),
        })
    return {
        eng: CommitStats(commits=a["commits"], additions=a["adds"], deletions=a["dels"],
                         active_days=len(a["days"]), repos=len(a["repos"]), commit_rows=a["rows"])
        for eng, a in acc.items()
    }


def _commits_via_store(gl, store, projects, email_map, since, until, progress) -> dict[int, CommitStats]:
    """Fetch hanya rentang yang belum ter-cover per project, simpan, agregasi dari DB."""
    fetched = 0
    for pid in projects:
        cov = store.get_commit_coverage(pid)
        for gs, gu in _coverage_gaps(cov, since, until):
            rows = []
            try:
                for c in gl.iter_commits(pid, f"{gs}T00:00:00Z", f"{gu}T23:59:59Z", with_stats=True):
                    st = c.get("stats") or {}
                    rows.append({
                        "sha": c.get("id"), "project_id": str(pid),
                        "author_email": (c.get("author_email") or "").lower(),
                        "committed_date": c.get("committed_date") or c.get("created_at"),
                        "additions": int(st.get("additions") or 0),
                        "deletions": int(st.get("deletions") or 0),
                        "title": c.get("title"),
                    })
            except GitLabError as exc:
                progress(f"    [!] project {pid}: {exc}")
                continue
            store.upsert_commits(rows)
            fetched += len(rows)
        store.set_commit_coverage(pid, since, until)
    store.commit()
    progress(f"    commit: {fetched} ditarik (delta), sisanya dari cache.")
    return _aggregate_commit_rows(store.get_commits([str(p) for p in projects], since, until), email_map)
