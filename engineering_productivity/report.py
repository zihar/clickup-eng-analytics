"""Render ReportData menjadi laporan Markdown."""

from __future__ import annotations

import statistics

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
    if data.has_last_done:
        lines.append(
            f"- **Kolom 'Selesai terakhir':** tanggal task terakhir berstatus done per engineer "
            f"(lookback {data.last_done_lookback_days} hari; '—' = tak ada di rentang itu)"
        )
    lines.append(f"- **Dibuat:** {generated_at}")
    lines.append("")
    lines.append(
        "> Catatan: atribusi task mengikuti kolom Developer (custom field). Task dengan "
        "banyak Developer dihitung untuk tiap engineer di kolom itu (shared credit). "
        "Cycle time hanya tersedia pada mode `--deep`."
    )
    lines.append("")

    _summary_table(lines, data.engineers, data.has_last_done)
    _throughput_table(lines, data)
    if data.has_commit_data:
        _commit_table(lines, data.engineers, data.commit_source, data.commit_noise_filtered)
        _quadrant_table(lines, data.engineers)
    if data.has_utilization:
        _underutilized_table(lines, data)
    if data.deep:
        _status_flow_table(lines, data)
    _per_engineer_detail(lines, data.engineers, data.has_commit_data, data.has_last_done)

    return "\n".join(lines) + "\n"


def _summary_table(lines: list[str], engineers: list[EngineerStats], has_last_done: bool = False) -> None:
    lines.append("## Ringkasan per Engineer")
    lines.append("")
    last_col = " Selesai terakhir |" if has_last_done else ""
    last_sep = "--:|" if has_last_done else ""
    lines.append(
        "| Engineer | Selesai |" + last_col + " Lead time median (hari) | Cycle time median (hari) "
        "| Tracked (jam) | Estimasi (jam) | Akurasi estimasi |"
    )
    lines.append("|---|--:|" + last_sep + "--:|--:|--:|--:|--:|")
    for e in engineers:
        last_cell = f" {e.last_done_date or '—'} |" if has_last_done else ""
        lines.append(
            f"| {e.name} | {e.completed} |" + last_cell + f" {_fmt(e.lead_median)} | "
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


def _commit_table(lines: list[str], engineers: list[EngineerStats], source: str | None, noise_filtered: bool) -> None:
    lines.append("## Aktivitas Commit (GitLab)")
    lines.append("")
    baris_note = (
        "+/- baris **sudah mengecualikan file noise** (vendor/lock/generated)."
        if noise_filtered
        else "+/- baris mentah (termasuk vendor/lock/generated — gunakan `--exclude-noise` untuk menyaring)."
    )
    lines.append(
        f"Sumber: {source or 'GitLab'}. **Hari aktif** (jumlah hari ada commit) lebih bermakna "
        "daripada total commit yang mudah diakali. Bandingkan dengan kolom *Selesai* di "
        f"ringkasan: timpang besar = task ClickUp tidak mencerminkan kerja kode (atau sebaliknya). {baris_note}"
    )
    lines.append("")
    lines.append("| Engineer | Commits | Hari aktif | Repo | +Baris | -Baris |")
    lines.append("|---|--:|--:|--:|--:|--:|")
    for e in sorted(engineers, key=lambda x: x.commits, reverse=True):
        lines.append(
            f"| {e.name} | {e.commits} | {e.active_days} | {e.repos_touched} "
            f"| {e.commit_additions} | {e.commit_deletions} |"
        )
    lines.append("")


def _quadrant_table(lines: list[str], engineers: list[EngineerStats]) -> None:
    """Matriks 2x2: throughput task (ClickUp) vs hari aktif commit (GitLab)."""
    if not engineers:
        return
    t_med = statistics.median([e.completed for e in engineers])
    a_med = statistics.median([e.active_days for e in engineers])

    cells: dict[tuple[str, str], list[str]] = {
        ("hi", "lo"): [], ("hi", "hi"): [], ("lo", "lo"): [], ("lo", "hi"): [],
    }
    for e in engineers:
        t = "hi" if e.completed > t_med else "lo"
        a = "hi" if e.active_days > a_med else "lo"
        cells[(t, a)].append(e.name)

    def cell(names: list[str]) -> str:
        return "<br>".join(names) if names else "—"

    lines.append("## Matriks Task vs Commit")
    lines.append("")
    lines.append(
        f"Sumbu: **task selesai** (ambang median {t_med:g}) × **hari aktif commit** "
        f"(ambang median {a_med:g}). Untuk melihat pola, **bukan ranking** — selalu baca "
        "dengan konteks (peran, jenis kerja, email commit yang mungkin belum ter-alias)."
    )
    lines.append("")
    lines.append("|  | Commit rendah | Commit tinggi |")
    lines.append("|---|---|---|")
    lines.append(f"| **Task tinggi** | {cell(cells[('hi', 'lo')])} | {cell(cells[('hi', 'hi')])} |")
    lines.append(f"| **Task rendah** | {cell(cells[('lo', 'lo')])} | {cell(cells[('lo', 'hi')])} |")
    lines.append("")
    lines.append("- **Task tinggi · commit rendah:** banyak task ditutup, sedikit kode — cek kerja non-kode atau commit di email yang belum ter-alias.")
    lines.append("- **Task rendah · commit tinggi:** aktif ngoding tapi jarang update ClickUp — soal higiene task, bukan output.")
    lines.append("- **Task rendah · commit rendah:** aktivitas rendah di dua sistem — perlu klarifikasi langsung.")
    lines.append("")


def _underutilized_table(lines: list[str], data: ReportData) -> None:
    lines.append("## Engineer Utilization")
    lines.append("")
    signals = ", ".join(data.utilization_signals) or "—"
    lines.append(
        f"Skor utilisasi 0–100 **relatif ke tim** (rata-rata percentile lintas sinyal: {signals}). "
        "**Makin rendah = makin underutilized** (kapasitas nganggur). ⚠️ = sepertiga terbawah tim. "
        "Sinyal yang datanya kosong otomatis di-skip."
    )
    lines.append("")
    engs = sorted(
        data.engineers,
        key=lambda e: e.utilization_score if e.utilization_score is not None else 999,
    )
    lines.append("| Engineer | Skor | WIP | Hari aktif | Selesai | Story point | Sinyal rendah |")
    lines.append("|---|--:|--:|--:|--:|--:|---|")
    for e in engs:
        score = e.utilization_score
        flag = "⚠️ " if (score is not None and score <= 33.3) else ""
        low = ", ".join(e.low_signals) or "—"
        lines.append(
            f"| {flag}{e.name} | {_fmt(score)} | {e.open_tasks} | {e.active_days} | "
            f"{e.completed} | {e.story_points:g} | {low} |"
        )
    lines.append("")
    lines.append(
        "> Bukan vonis kinerja: skor rendah bisa berarti **under-assigned**, lagi ke-block, beda peran, "
        "atau email commit belum ter-alias. Pakai sebagai pemicu obrolan kapasitas."
    )
    lines.append("")


def _status_flow_table(lines: list[str], data: ReportData) -> None:
    lines.append("## Status Flow / Bottleneck")
    lines.append("")
    lines.append(
        "Lama task berada di tiap status (semua task pada periode), diurutkan dari "
        "**median** tertinggi. Median lebih tahan outlier daripada rata-rata; selisih "
        "besar antara median dan p90/rata-rata menandakan ada beberapa task ekstrem. "
        "Status terminal (Done/Closed/Drop) dikecualikan karena bukan bottleneck."
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


def _per_engineer_detail(lines: list[str], engineers: list[EngineerStats], has_commit: bool, has_last_done: bool = False) -> None:
    lines.append("## Detail per Engineer")
    lines.append("")
    for e in engineers:
        lines.append(f"### {e.name}")
        lines.append("")
        lines.append(f"- Task selesai: **{e.completed}**")
        if has_last_done:
            lines.append(f"- Task terakhir selesai: {e.last_done_date or '—'}")
        lines.append(f"- Lead time: median {_fmt(e.lead_median)} hari · rata-rata {_fmt(e.lead_mean)} hari")
        if e.cycle_times_days:
            lines.append(f"- Cycle time (waktu aktif dikerjakan): median {_fmt(e.cycle_median)} hari")
        lines.append(f"- Time tracked: {_fmt(e.tracked_hours)} jam (estimasi {_fmt(e.estimate_hours)} jam)")
        if has_commit:
            lines.append(
                f"- Commit GitLab: {e.commits} commit · {e.active_days} hari aktif · "
                f"{e.repos_touched} repo"
            )
        if e.estimate_accuracy is not None:
            arah = "lebih lama dari" if e.estimate_accuracy > 1 else "lebih cepat dari"
            lines.append(f"- Akurasi estimasi: {e.estimate_accuracy}× ({arah} estimasi)")
        lines.append("")
