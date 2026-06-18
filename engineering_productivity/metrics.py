"""Perhitungan metrik produktivitas dari data mentah ClickUp.

Semua waktu dari ClickUp dalam Unix epoch milidetik (string). Helper di sini
mengubahnya ke jam/hari dan mengelompokkannya per engineer & per minggu.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000

# Tipe status terminal di ClickUp — bukan bottleneck (hanya "waktu sejak selesai"),
# jadi dikecualikan dari analisis status flow.
TERMINAL_STATUS_TYPES = {"closed", "done"}


# --------------------------------------------------------------------- helpers
def task_developer_ids(task: dict, field_id: str) -> list[int]:
    """User id dari custom field Developer (tipe users). [] bila kosong."""
    for cf in task.get("custom_fields") or []:
        if cf.get("id") == field_id:
            out = []
            for v in cf.get("value") or []:
                vid = v.get("id") if isinstance(v, dict) else v
                try:
                    out.append(int(vid))
                except (TypeError, ValueError):
                    pass
            return out
    return []


def to_int_ms(value) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def local_dt(ms: int, offset_hours: float) -> datetime:
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ms / 1000, tz=tz)


def iso_week_label(ms: int, offset_hours: float) -> str:
    dt = local_dt(ms, offset_hours)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def _percentile(values: list[float], pct: float) -> float:
    """Persentil dengan interpolasi linear (mis. pct=90 untuk p90)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# --------------------------------------------------------------------- results
@dataclass
class EngineerStats:
    engineer_id: int
    name: str
    completed: int = 0
    lead_times_days: list[float] = field(default_factory=list)
    cycle_times_days: list[float] = field(default_factory=list)
    per_week: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Aktivitas commit GitLab (join via id ClickUp).
    commits: int = 0
    commit_additions: int = 0
    commit_deletions: int = 0
    active_days: int = 0
    repos_touched: int = 0
    # Tanggal task terakhir ber-status done (epoch ms), lintas periode bila diaktifkan.
    last_done_ms: int | None = None
    # Utilisasi (diisi bila analisis utilisasi diaktifkan).
    open_tasks: int = 0                       # WIP: task open yg di-assign
    story_points: float = 0.0                 # Σ poin (task selesai di periode + task open)
    utilization_score: float | None = None    # 0..100 relatif tim (rendah = underutilized)
    low_signals: list[str] = field(default_factory=list)

    @property
    def last_done_date(self) -> str | None:
        """Tanggal 'YYYY-MM-DD' task terakhir selesai, atau None bila tak ada data."""
        if self.last_done_ms is None:
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.last_done_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    @property
    def lead_median(self) -> float:
        return _median(self.lead_times_days)

    @property
    def lead_mean(self) -> float:
        return _mean(self.lead_times_days)

    @property
    def cycle_median(self) -> float:
        return _median(self.cycle_times_days)


@dataclass
class StatusBucket:
    status: str
    durations: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.durations)

    @property
    def avg_hours(self) -> float:
        return round(statistics.fmean(self.durations) / MS_PER_HOUR, 1) if self.durations else 0.0

    @property
    def median_hours(self) -> float:
        return round(statistics.median(self.durations) / MS_PER_HOUR, 1) if self.durations else 0.0

    @property
    def p90_hours(self) -> float:
        return round(_percentile(self.durations, 90) / MS_PER_HOUR, 1) if self.durations else 0.0


@dataclass
class ReportData:
    engineers: list[EngineerStats]
    status_flow: list[StatusBucket]
    weeks: list[str]
    total_tasks: int
    deep: bool
    since: str
    until: str
    tz_offset: float
    max_age_days: int | None = None
    filtered_stale: int = 0
    has_commit_data: bool = False
    commit_source: str | None = None
    commit_noise_filtered: bool = False
    has_last_done: bool = False
    last_done_lookback_days: int | None = None
    has_utilization: bool = False
    utilization_signals: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- builder
def build_report_data(
    tasks: list[dict],
    *,
    developer_field_id: str,
    id_to_name: dict[int, str],
    target_ids: set[int],
    time_in_status: dict[str, dict] | None,
    since: str,
    until: str,
    tz_offset: float,
    max_age_days: int | None = None,
    commit_stats: dict | None = None,
    commit_source: str | None = None,
    commit_noise_filtered: bool = False,
    last_done_ms: dict[int, int] | None = None,
    last_done_lookback_days: int | None = None,
    open_tasks: dict[int, int] | None = None,
    open_story_points: dict[int, float] | None = None,
    utilization: bool = False,
) -> ReportData:
    stats: dict[int, EngineerStats] = {
        uid: EngineerStats(engineer_id=uid, name=id_to_name.get(uid, str(uid)))
        for uid in target_ids
    }
    weeks: set[str] = set()
    status_flow: dict[str, StatusBucket] = {}
    deep = time_in_status is not None
    filtered_stale = 0
    # Nama status terminal dikumpulkan dari field status task (punya 'type' valid).
    # current_status di time_in_status sering bertype None, jadi nama lebih andal.
    terminal_names: set[str] = set()

    for task in tasks:
        date_created = to_int_ms(task.get("date_created"))
        date_done = to_int_ms(task.get("date_done")) or to_int_ms(task.get("date_closed"))
        if date_done is None:
            continue  # hanya hitung task yang benar-benar selesai

        st = task.get("status") or {}
        if (st.get("type") or "").lower() in TERMINAL_STATUS_TYPES:
            terminal_names.add((st.get("status") or "").title())

        lead_days = None
        if date_created is not None and date_done >= date_created:
            lead_days = round((date_done - date_created) / MS_PER_DAY, 2)

        # Abaikan task backlog basi (lead time > max_age): baru ditutup tapi
        # sebenarnya nganggur berbulan-bulan, mengaburkan metrik tim.
        if max_age_days is not None and lead_days is not None and lead_days > max_age_days:
            filtered_stale += 1
            continue

        dev_ids = task_developer_ids(task, developer_field_id)
        relevant = [d for d in dev_ids if d in target_ids]
        if not relevant:
            continue

        week = iso_week_label(date_done, tz_offset)
        weeks.add(week)

        cycle_days = None
        if deep:
            cycle_days = _cycle_time_days(task, time_in_status)
            _accumulate_status_flow(task, time_in_status, status_flow)

        points = _task_points(task)

        for d in relevant:
            s = stats[d]
            s.completed += 1
            s.per_week[week] += 1
            s.story_points += points
            if lead_days is not None:
                s.lead_times_days.append(lead_days)
            if cycle_days is not None:
                s.cycle_times_days.append(cycle_days)

    # Gabungkan aktivitas commit GitLab (join via id ClickUp).
    if commit_stats:
        for uid, cs in commit_stats.items():
            if uid in stats:
                s = stats[uid]
                s.commits = cs.commits
                s.commit_additions = cs.additions
                s.commit_deletions = cs.deletions
                s.active_days = cs.active_days
                s.repos_touched = cs.repos

    # Tanggal task terakhir selesai (lintas periode), bila disediakan pipeline.
    if last_done_ms:
        for uid, ms in last_done_ms.items():
            if uid in stats:
                stats[uid].last_done_ms = ms

    # WIP & story point dari task open (snapshot), bila disediakan pipeline.
    if open_tasks:
        for uid, n in open_tasks.items():
            if uid in stats:
                stats[uid].open_tasks = n
    if open_story_points:
        for uid, pts in open_story_points.items():
            if uid in stats:
                stats[uid].story_points += pts

    utilization_signals: list[str] = []
    if utilization:
        utilization_signals = _compute_utilization(list(stats.values()), commit_stats is not None)

    # Buang status terminal (mis. Done/Drop) yang lolos lewat current_status bertype None.
    for name in terminal_names:
        status_flow.pop(name, None)

    engineers_sorted = sorted(stats.values(), key=lambda e: e.completed, reverse=True)
    # Urut berdasarkan median (lebih tahan outlier) — bottleneck tipikal di atas.
    flow_sorted = sorted(status_flow.values(), key=lambda b: b.median_hours, reverse=True)

    return ReportData(
        engineers=engineers_sorted,
        status_flow=flow_sorted,
        weeks=sorted(weeks),
        total_tasks=sum(e.completed for e in engineers_sorted),
        deep=deep,
        since=since,
        until=until,
        tz_offset=tz_offset,
        max_age_days=max_age_days,
        filtered_stale=filtered_stale,
        has_commit_data=commit_stats is not None,
        commit_source=commit_source,
        commit_noise_filtered=commit_noise_filtered,
        has_last_done=last_done_ms is not None,
        last_done_lookback_days=last_done_lookback_days,
        has_utilization=utilization,
        utilization_signals=utilization_signals,
    )


def _cycle_time_days(task: dict, time_in_status: dict[str, dict]) -> float | None:
    """Cycle time = total waktu di status aktif (type 'custom'), mis. In Progress, Review.

    Status 'open' (backlog/to-do) dan 'closed/done' dikecualikan agar mengukur
    waktu pengerjaan nyata, bukan waktu menunggu di backlog.
    """
    tis = time_in_status.get(task["id"])
    if not tis:
        return None
    total_ms = 0
    history = tis.get("status_history", [])
    for entry in history:
        status_type = (entry.get("type") or "").lower()
        if status_type == "custom":
            total_ms += _status_entry_ms(entry)
    return round(total_ms / MS_PER_DAY, 2) if total_ms else 0.0


def _accumulate_status_flow(task: dict, time_in_status: dict[str, dict], flow: dict[str, StatusBucket]) -> None:
    tis = time_in_status.get(task["id"])
    if not tis:
        return
    entries = list(tis.get("status_history", []))
    current = tis.get("current_status")
    if current:
        entries.append(current)
    for entry in entries:
        if (entry.get("type") or "").lower() in TERMINAL_STATUS_TYPES:
            continue  # lewati status terminal (Done/Closed/Drop) — bukan bottleneck
        name = (entry.get("status") or "unknown").title()
        ms = _status_entry_ms(entry)
        if ms <= 0:
            continue
        bucket = flow.setdefault(name, StatusBucket(status=name))
        bucket.durations.append(ms)


def _status_entry_ms(entry: dict) -> int:
    """ClickUp menaruh durasi di total_time.by_minute (menit) atau total_time.since (ms)."""
    total = entry.get("total_time") or {}
    by_minute = total.get("by_minute")
    if by_minute is not None:
        try:
            return int(by_minute) * 60_000
        except (TypeError, ValueError):
            return 0
    return to_int_ms(total.get("since")) or 0


def _task_points(task: dict) -> float:
    """Story point dari field native ClickUp `points` (sprint points). 0 bila kosong."""
    p = task.get("points")
    if p in (None, ""):
        return 0.0
    try:
        return float(p)
    except (TypeError, ValueError):
        return 0.0


def _percentile_rank(value: float, sorted_vals: list[float]) -> float:
    """Posisi relatif value dalam tim: 0=terendah, 1=tertinggi. (# < value)/(n-1)."""
    n = len(sorted_vals)
    if n <= 1:
        return 0.5
    below = sum(1 for v in sorted_vals if v < value)
    return below / (n - 1)


# Sinyal utilisasi: (key, label, fungsi pengambil nilai). Nilai RENDAH = underutilized.
_UTIL_SIGNALS = [
    ("wip", "WIP", lambda e: float(e.open_tasks)),
    ("active_days", "hari aktif", lambda e: float(e.active_days)),
    ("throughput", "throughput", lambda e: float(e.completed)),
    ("story_points", "story point", lambda e: float(e.story_points)),
]


def _compute_utilization(engineers: list[EngineerStats], has_commit_data: bool) -> list[str]:
    """Hitung utilization_score (0..100, relatif tim) + low_signals per engineer.

    Skor = rata-rata percentile rank lintas sinyal yang tersedia × 100 (rendah = underutilized).
    Sinyal di-skip otomatis bila tak relevan/kosong. Mengembalikan daftar sinyal yang dipakai.
    """
    if not engineers:
        return []

    used: list[tuple[str, str, object]] = []
    for key, label, getter in _UTIL_SIGNALS:
        if key == "active_days" and not has_commit_data:
            continue
        vals = [getter(e) for e in engineers]
        if sum(vals) <= 0:  # semua nol → tak ada data pembeda, skip
            continue
        used.append((key, label, getter))

    if not used:
        for e in engineers:
            e.utilization_score = None
        return []

    sorted_by_signal = {key: sorted(getter(e) for e in engineers) for key, _, getter in used}
    for e in engineers:
        ranks = []
        low = []
        for key, label, getter in used:
            r = _percentile_rank(getter(e), sorted_by_signal[key])
            ranks.append(r)
            if r <= 1 / 3:  # sepertiga terbawah pada sinyal ini
                low.append(label)
        e.utilization_score = round(sum(ranks) / len(ranks) * 100, 1)
        e.low_signals = low
    return [label for _, label, _ in used]
