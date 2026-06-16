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
    estimate_ms: int = 0
    tracked_ms: int = 0
    per_week: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # Aktivitas commit GitLab (dari DB squad-scorecard, join via id ClickUp).
    commits: int = 0
    commit_additions: int = 0
    commit_deletions: int = 0
    active_days: int = 0
    repos_touched: int = 0

    @property
    def lead_median(self) -> float:
        return _median(self.lead_times_days)

    @property
    def lead_mean(self) -> float:
        return _mean(self.lead_times_days)

    @property
    def cycle_median(self) -> float:
        return _median(self.cycle_times_days)

    @property
    def tracked_hours(self) -> float:
        return round(self.tracked_ms / MS_PER_HOUR, 1)

    @property
    def estimate_hours(self) -> float:
        return round(self.estimate_ms / MS_PER_HOUR, 1)

    @property
    def estimate_accuracy(self) -> float | None:
        """Rasio tracked/estimate. >1 berarti lebih lama dari estimasi."""
        if self.estimate_ms <= 0:
            return None
        return round(self.tracked_ms / self.estimate_ms, 2)


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
    commit_through: str | None = None
    commit_synced_at: str | None = None
    commit_source: str | None = None
    commit_noise_filtered: bool = False


# --------------------------------------------------------------------- builder
def build_report_data(
    tasks: list[dict],
    *,
    id_to_name: dict[int, str],
    target_ids: set[int],
    time_in_status: dict[str, dict] | None,
    time_entries: list[dict],
    since: str,
    until: str,
    tz_offset: float,
    max_age_days: int | None = None,
    commit_stats: dict | None = None,
    commit_through: str | None = None,
    commit_synced_at: str | None = None,
    commit_source: str | None = None,
    commit_noise_filtered: bool = False,
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

        assignees = task.get("assignees") or []
        relevant = [a for a in assignees if a.get("id") in target_ids]
        if not relevant:
            continue

        week = iso_week_label(date_done, tz_offset)
        weeks.add(week)

        cycle_days = None
        if deep:
            cycle_days = _cycle_time_days(task, time_in_status)
            _accumulate_status_flow(task, time_in_status, status_flow)

        estimate = to_int_ms(task.get("time_estimate")) or 0

        for a in relevant:
            s = stats[a["id"]]
            s.completed += 1
            s.per_week[week] += 1
            s.estimate_ms += estimate
            if lead_days is not None:
                s.lead_times_days.append(lead_days)
            if cycle_days is not None:
                s.cycle_times_days.append(cycle_days)

    # Time tracked per engineer dari entri time-tracking (akurat per orang).
    for entry in time_entries:
        user = entry.get("user") or {}
        uid = user.get("id")
        if uid not in stats:
            continue
        dur = to_int_ms(entry.get("duration")) or 0
        if dur > 0:  # abaikan timer berjalan / nilai negatif
            stats[uid].tracked_ms += dur

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
        commit_through=commit_through,
        commit_synced_at=commit_synced_at,
        commit_source=commit_source,
        commit_noise_filtered=commit_noise_filtered,
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
