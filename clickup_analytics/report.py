"""Render ReportData menjadi laporan Markdown."""

from __future__ import annotations

from .metrics import ReportData, EngineerStats


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value}{suffix}"


def render_markdown(data: ReportData, *, generated_at: str) -> str:
    lines: list[str] = []
    lines.append("# Laporan Produktivitas Engineering")
    lines.append("")
    lines.append(f"- **Periode:** {data.since} s/d {data.until}")
    lines.append(f"- **Engineer dianalisis:** {len(data.engineers)}")
    lines.append(f"- **Total task selesai:** {data.total_tasks}")
    lines.append(f"- **Zona waktu bucket:** UTC{data.tz_offset:+g}")
    if data.max_age_days is not None:
        lines.append(
            f"- **Filter task basi:** {data.filtered_stale} task dengan lead time "
            f"> {data.max_age_days} hari diabaikan"
        )
    lines.append(f"- **Dibuat:** {generated_at}")
    lines.append("")
    lines.append(
        "> Catatan: task dengan banyak assignee dihitung untuk tiap engineer yang "
        "ditugaskan (shared credit). Cycle time hanya tersedia pada mode `--deep`."
    )
    lines.append("")

    _summary_table(lines, data.engineers)
    _throughput_table(lines, data)
    if data.deep:
        _status_flow_table(lines, data)
    _per_engineer_detail(lines, data.engineers)

    return "\n".join(lines) + "\n"


def _summary_table(lines: list[str], engineers: list[EngineerStats]) -> None:
    lines.append("## Ringkasan per Engineer")
    lines.append("")
    lines.append(
        "| Engineer | Selesai | Lead time median (hari) | Cycle time median (hari) "
        "| Tracked (jam) | Estimasi (jam) | Akurasi estimasi |"
    )
    lines.append("|---|--:|--:|--:|--:|--:|--:|")
    for e in engineers:
        lines.append(
            f"| {e.name} | {e.completed} | {_fmt(e.lead_median)} | "
            f"{_fmt(e.cycle_median) if e.cycle_times_days else '—'} | "
            f"{_fmt(e.tracked_hours)} | {_fmt(e.estimate_hours)} | "
            f"{_fmt(e.estimate_accuracy, '×')} |"
        )
    lines.append("")


def _throughput_table(lines: list[str], data: ReportData) -> None:
    lines.append("## Throughput per Minggu (jumlah task selesai)")
    lines.append("")
    if not data.weeks:
        lines.append("_Tidak ada data minggu pada periode ini._")
        lines.append("")
        return
    header = "| Engineer | " + " | ".join(data.weeks) + " | Total |"
    sep = "|---|" + "--:|" * (len(data.weeks) + 1)
    lines.append(header)
    lines.append(sep)
    for e in data.engineers:
        cells = [str(e.per_week.get(w, 0)) for w in data.weeks]
        lines.append(f"| {e.name} | " + " | ".join(cells) + f" | {e.completed} |")
    lines.append("")


def _status_flow_table(lines: list[str], data: ReportData) -> None:
    lines.append("## Status Flow / Bottleneck")
    lines.append("")
    lines.append(
        "Lama task berada di tiap status (semua task pada periode), diurutkan dari "
        "**median** tertinggi. Median lebih tahan outlier daripada rata-rata; selisih "
        "besar antara median dan p90/rata-rata menandakan ada beberapa task ekstrem."
    )
    lines.append("")
    if not data.status_flow:
        lines.append("_Tidak ada data status (butuh mode --deep)._")
        lines.append("")
        return
    lines.append("| Status | Median (jam) | p90 (jam) | Rata-rata (jam) | Jumlah task |")
    lines.append("|---|--:|--:|--:|--:|")
    for b in data.status_flow:
        lines.append(f"| {b.status} | {b.median_hours} | {b.p90_hours} | {b.avg_hours} | {b.count} |")
    lines.append("")


def _per_engineer_detail(lines: list[str], engineers: list[EngineerStats]) -> None:
    lines.append("## Detail per Engineer")
    lines.append("")
    for e in engineers:
        lines.append(f"### {e.name}")
        lines.append("")
        lines.append(f"- Task selesai: **{e.completed}**")
        lines.append(f"- Lead time: median {_fmt(e.lead_median)} hari · rata-rata {_fmt(e.lead_mean)} hari")
        if e.cycle_times_days:
            lines.append(f"- Cycle time (waktu aktif dikerjakan): median {_fmt(e.cycle_median)} hari")
        lines.append(f"- Time tracked: {_fmt(e.tracked_hours)} jam (estimasi {_fmt(e.estimate_hours)} jam)")
        if e.estimate_accuracy is not None:
            arah = "lebih lama dari" if e.estimate_accuracy > 1 else "lebih cepat dari"
            lines.append(f"- Akurasi estimasi: {e.estimate_accuracy}× ({arah} estimasi)")
        lines.append("")
