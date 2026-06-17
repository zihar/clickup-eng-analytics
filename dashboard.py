"""Dashboard Streamlit untuk engineering-productivity.

Jalankan:
    export CLICKUP_TOKEN=pk_...        # dan GITLAB_TOKEN=glpat-... bila pakai sumber GitLab
    streamlit run dashboard.py

Membaca config.yaml (atau path di env EP_CONFIG). Memakai pipeline yang sama
dengan CLI (engineering_productivity.pipeline.gather_report), hasilnya di-cache.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Pastikan paket lokal bisa diimpor apa pun launcher-nya (streamlit run / AppTest).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engineering_productivity.config import ConfigError, load_config
from engineering_productivity.metrics import ReportData
from engineering_productivity.pipeline import GatherOptions, gather_report
from engineering_productivity.report import render_markdown

CONFIG_PATH = os.environ.get("EP_CONFIG", "config.yaml")

st.set_page_config(page_title="Engineering Productivity", page_icon="📊", layout="wide")


@st.cache_data(show_spinner="Menarik data dari ClickUp/GitLab ...")
def gather_cached(
    config_path: str,
    engineer_names: tuple[str, ...],
    since: str,
    until: str,
    tz: float,
    deep: bool,
    max_age: int | None,
    source: str,
    no_discover: bool,
    exclude_noise: bool,
    no_commits: bool,
    last_done: bool,
    utilization: bool,
) -> ReportData:
    cfg = load_config(config_path)
    if engineer_names:
        chosen = set(engineer_names)
        cfg = dataclasses.replace(cfg, engineers=[e for e in cfg.engineers if e.name in chosen])
    opts = GatherOptions(
        since=since, until=until, tz=tz, deep=deep, max_age=max_age,
        commits_source=source, no_discover=no_discover,
        exclude_noise=exclude_noise, no_commits=no_commits, last_done=last_done,
        utilization=utilization,
    )
    return gather_report(cfg, opts)


def summary_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Selesai": e.completed,
        **({"Selesai terakhir": e.last_done_date or "—"} if data.has_last_done else {}),
        "Lead median (hari)": e.lead_median,
        "Cycle median (hari)": e.cycle_median if e.cycle_times_days else None,
        "Tracked (jam)": e.tracked_hours,
        "Commits": e.commits,
        "Hari aktif": e.active_days,
        "Repo": e.repos_touched,
    } for e in data.engineers]
    return pd.DataFrame(rows)


def weekly_frame(data: ReportData) -> pd.DataFrame:
    rows = {e.name: {w: e.per_week.get(w, 0) for w in data.weeks} for e in data.engineers}
    return pd.DataFrame(rows).T  # baris=engineer, kolom=minggu


def bottleneck_frame(data: ReportData) -> pd.DataFrame:
    return pd.DataFrame([{
        "Status": b.status,
        "Median (jam)": b.median_hours,
        "p90 (jam)": b.p90_hours,
        "Rata-rata (jam)": b.avg_hours,
        "Jumlah task": b.count,
    } for b in data.status_flow])


# ---------------------------------------------------------------- sidebar
try:
    base_config = load_config(CONFIG_PATH)
except ConfigError as exc:
    st.error(f"Konfigurasi belum siap: {exc}")
    st.stop()

st.sidebar.title("⚙️ Filter")
all_names = [e.name for e in base_config.engineers]
sel_names = st.sidebar.multiselect("Engineer", all_names, default=all_names)

today = date.today()
default_start = today - timedelta(days=30)
rng = st.sidebar.date_input("Periode", value=(default_start, today), max_value=today)
if isinstance(rng, tuple) and len(rng) == 2:
    since_d, until_d = rng
else:
    since_d, until_d = default_start, today

tz = st.sidebar.number_input("Offset zona waktu (UTC+)", value=7.0, step=1.0)
deep = st.sidebar.toggle("Deep (cycle time & bottleneck)", value=False, help="Lebih lambat: 1 API call per task")
max_age_in = st.sidebar.number_input("Abaikan task basi > N hari (0 = nonaktif)", value=60, min_value=0, step=10)
source = st.sidebar.selectbox("Sumber commit", ["auto", "gitlab", "db", "none"], index=0)
no_discover = st.sidebar.toggle("Jangan auto-discover repo", value=False)
exclude_noise = st.sidebar.toggle("Filter file noise (+/- baris)", value=False, help="Lebih lambat: ambil diff tiap commit")
last_done = st.sidebar.toggle("Tanggal selesai terakhir", value=False, help="Query ekstra: kapan tiap engineer terakhir menutup task (lintas periode)")
utilization = st.sidebar.toggle("Analisis utilisasi", value=False, help="Skor underutilized relatif tim (WIP + hari aktif + throughput + story point)")

if st.sidebar.button("🔄 Refresh data", width="stretch"):
    gather_cached.clear()
    st.rerun()

# ---------------------------------------------------------------- body
st.title("📊 Engineering Productivity")

if not sel_names:
    st.warning("Pilih minimal satu engineer di sidebar.")
    st.stop()

try:
    data = gather_cached(
        CONFIG_PATH, tuple(sel_names),
        since_d.isoformat(), until_d.isoformat(), float(tz),
        deep, (max_age_in or None), source, no_discover, exclude_noise, source == "none", last_done, utilization,
    )
except Exception as exc:  # noqa: BLE001 — tampilkan error apa pun ke UI
    st.error(f"Gagal menarik data: {exc}")
    st.stop()

# KPI
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total task selesai", data.total_tasks)
c2.metric("Engineer", len(data.engineers))
total_commits = sum(e.commits for e in data.engineers)
c3.metric("Total commit", total_commits if data.has_commit_data else "—")
c4.metric("Periode", f"{data.since} → {data.until}")

if data.has_commit_data:
    st.caption(f"Sumber commit: {data.commit_source}")
    if data.commit_through and data.commit_through < data.until:
        st.warning(
            f"⚠️ Data commit hanya tersinkron s/d **{data.commit_through}** "
            f"(periode s/d {data.until}). Angka commit terbaru belum lengkap."
        )
if data.max_age_days is not None and data.filtered_stale:
    st.caption(f"🧹 {data.filtered_stale} task basi (lead time > {data.max_age_days} hari) diabaikan.")

summary = summary_frame(data)

# Chart baris 1: throughput & commit
col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Task selesai per engineer")
    st.bar_chart(summary.set_index("Engineer")["Selesai"])
with col_b:
    st.subheader("Hari aktif commit per engineer" if data.has_commit_data else "Commit (tidak ada data)")
    if data.has_commit_data:
        st.bar_chart(summary.set_index("Engineer")["Hari aktif"])
    else:
        st.info("Aktivitas commit tidak diambil (sumber = none / gagal).")

# Matriks task vs commit
if data.has_commit_data:
    st.subheader("Matriks Task vs Commit")
    scatter = summary[["Engineer", "Selesai", "Hari aktif"]].copy()
    t_med = scatter["Selesai"].median()
    a_med = scatter["Hari aktif"].median()
    fig = px.scatter(
        scatter, x="Selesai", y="Hari aktif", text="Engineer",
        labels={"Selesai": "Task selesai (ClickUp)", "Hari aktif": "Hari aktif commit (GitLab)"},
    )
    fig.update_traces(textposition="top center", marker=dict(size=12))
    fig.add_vline(x=t_med, line_dash="dash", line_color="gray")
    fig.add_hline(y=a_med, line_dash="dash", line_color="gray")
    fig.update_layout(height=480, margin=dict(t=30))
    st.plotly_chart(fig, width="stretch")
    st.caption(
        f"Garis = median (task {t_med:g}, hari aktif {a_med:g}). Kanan-bawah = banyak task tapi sedikit "
        "commit; kiri-atas = aktif ngoding tapi jarang update task. Pola, bukan ranking."
    )

# Throughput per minggu
if data.weeks:
    st.subheader("Throughput per minggu")
    st.bar_chart(weekly_frame(data).T)  # index=minggu, kolom=engineer (stacked)

# Engineer underutilized
if data.has_utilization:
    st.subheader("Engineer Underutilized")
    st.caption(
        f"Skor 0–100 relatif tim (sinyal: {', '.join(data.utilization_signals) or '—'}). "
        "Makin rendah = makin underutilized. Bukan vonis kinerja — pemicu obrolan kapasitas."
    )
    uframe = pd.DataFrame([{
        "Engineer": e.name,
        "Skor": e.utilization_score,
        "WIP": e.open_tasks,
        "Hari aktif": e.active_days,
        "Selesai": e.completed,
        "Story point": e.story_points,
        "Sinyal rendah": ", ".join(e.low_signals) or "—",
    } for e in sorted(
        data.engineers,
        key=lambda e: e.utilization_score if e.utilization_score is not None else 999,
    )])
    st.bar_chart(uframe.set_index("Engineer")["Skor"])
    st.dataframe(uframe, width="stretch", hide_index=True)

# Bottleneck
if data.deep and data.status_flow:
    st.subheader("Bottleneck (median jam per status, status terminal dikecualikan)")
    bf = bottleneck_frame(data)
    st.bar_chart(bf.set_index("Status")["Median (jam)"])
    st.dataframe(bf, width="stretch", hide_index=True)

# Tabel ringkasan
st.subheader("Ringkasan per engineer")
st.dataframe(summary, width="stretch", hide_index=True)

# Commit detail
if data.has_commit_data:
    with st.expander("Detail commit (+/- baris)"):
        cdf = pd.DataFrame([{
            "Engineer": e.name, "Commits": e.commits, "Hari aktif": e.active_days,
            "Repo": e.repos_touched, "+Baris": e.commit_additions, "-Baris": e.commit_deletions,
        } for e in sorted(data.engineers, key=lambda x: x.commits, reverse=True)])
        note = "sudah disaring noise" if data.commit_noise_filtered else "mentah (aktifkan filter noise di sidebar)"
        st.caption(f"+/- baris: {note}.")
        st.dataframe(cdf, width="stretch", hide_index=True)

# Download Markdown
now = datetime.now(timezone(timedelta(hours=float(tz))))
md = render_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M %Z"))
st.download_button("⬇️ Download laporan Markdown", md, file_name="report.md", mime="text/markdown")
