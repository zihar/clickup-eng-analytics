"""Dashboard Streamlit untuk engineering-productivity — halaman Overview (tim).

Jalankan:
    export CLICKUP_TOKEN=pk_...        # dan GITLAB_TOKEN=glpat-... bila pakai sumber GitLab
    streamlit run dashboard.py

Membaca config.yaml (atau path di env EP_CONFIG). Filter & fetch dibagi dengan
halaman lain lewat dashboard_lib (lihat pages/ untuk detail per engineer).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Engineering Productivity", page_icon="📊", layout="wide")

from dashboard_lib import (
    add_chapter,
    cols,
    coverage_note,
    load_base_config,
    load_data,
    render_sidebar,
    tgl,
    topn_bar,
)
from engineering_productivity.metrics import ReportData
from engineering_productivity.report import render_markdown

_NUMERIC = {"Selesai", "Lead median (hari)", "Cycle median (hari)", "Commits",
            "Hari aktif", "Repo", "+Baris", "-Baris", "WIP", "Story point"}


def summary_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Selesai": e.completed,
        **({"Selesai terakhir": e.last_done_date or "—"} if data.has_last_done else {}),
        "Lead median (hari)": e.lead_median,
        "Cycle median (hari)": e.cycle_median if e.cycle_times_days else None,
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


def util_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Skor": e.utilization_score,
        "WIP": e.open_tasks,
        "Hari aktif": e.active_days,
        "Selesai": e.completed,
        "Story point": e.story_points,
        "Sinyal rendah": ", ".join(e.low_signals) or "—",
    } for e in data.engineers]
    df = pd.DataFrame(rows)
    return df.sort_values("Skor", na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------- sidebar + data
base_config = load_base_config()
filters = render_sidebar(base_config)
NAME_TO_CHAPTER = filters["name_to_chapter"]

# ---------------------------------------------------------------- body
st.title("📊 Engineering Productivity")

data = load_data(filters)
if data is None:
    st.stop()

st.subheader(
    f"📅 {tgl(data.since)} – {tgl(data.until)}  ·  "
    f"{(filters['until_d'] - filters['since_d']).days + 1} hari"
)
coverage_note(data, filters)

# KPI — angka saja (3 kolom, tak ada yang terpotong)
c1, c2, c3 = st.columns(3)
c1.metric("Total task selesai", data.total_tasks)
c2.metric("Engineer", len(data.engineers))
total_commits = sum(e.commits for e in data.engineers)
c3.metric("Total commit", total_commits if data.has_commit_data else "—")

if data.has_commit_data:
    st.caption(f"Sumber commit: {data.commit_source}")
if data.max_age_days is not None and data.filtered_stale:
    st.caption(f"🧹 {data.filtered_stale} task basi (lead time > {data.max_age_days} hari) diabaikan.")

summary = add_chapter(summary_frame(data), NAME_TO_CHAPTER)
emap = {e.name: e for e in data.engineers}


def _slider(label, total, key, default=15):
    hi = max(1, total)
    return st.slider(label, 1, hi, min(default, hi), key=key) if hi > 1 else hi


def _open_detail(df: pd.DataFrame, event) -> None:
    """Bila ada baris terpilih di tabel engineer → buka halaman Detail untuk engineer itu."""
    rows = event.selection.rows if (event and event.selection) else []
    if rows:
        st.session_state["detail_engineer"] = df.iloc[rows[0]]["Engineer"]
        st.switch_page("pages/1_Detail_Engineer.py")


# ===================================================================== 1) UTILIZATION (paling atas)
st.header("🎯 Engineer Utilization")
if data.has_utilization:
    uf = add_chapter(util_frame(data), NAME_TO_CHAPTER)
    st.caption(
        f"Skor 0–100 relatif tim (sinyal: {', '.join(data.utilization_signals) or '—'}). "
        "Makin rendah = makin underutilized. **Bukan vonis kinerja** — pemicu obrolan kapasitas."
    )
    flagged = uf[uf["Skor"].notna() & (uf["Skor"] <= 33.3)]
    if len(flagged):
        st.warning("⚠️ **Perlu perhatian** (sepertiga terbawah): "
                   + ", ".join(f"{r.Engineer} ({r.Skor:.0f})" for r in flagged.itertuples()))
    else:
        st.success("Tidak ada engineer di sepertiga terbawah.")
    uc1, uc2 = st.columns([3, 2])
    with uc1:
        n = _slider("Tampilkan N skor terendah", len(uf), "util_n")
        st.plotly_chart(topn_bar(uf, "Skor", n, top=False, title=f"{n} skor terendah"), width="stretch")
    with uc2:
        st.plotly_chart(px.histogram(uf, x="Skor", nbins=10, title="Distribusi skor"), width="stretch")
    st.caption("👆 Klik baris engineer untuk membuka halaman **Detail Engineer**.")
    _ev_u = st.dataframe(uf, column_config=cols(uf), width="stretch", hide_index=True,
                         on_select="rerun", selection_mode="single-row", key="util_select")
    _open_detail(uf, _ev_u)
else:
    st.info("Nyalakan **Analisis utilisasi** di sidebar untuk skor utilisasi & daftar engineer underutilized.")

# ===================================================================== 2) RINGKASAN & METRIK (tabel-sentris)
st.header("📋 Ringkasan & Metrik")
metric_opts = [c for c in summary.columns if c in _NUMERIC]
if metric_opts:
    m1, m2, m3 = st.columns([2, 1, 2])
    metric = m1.selectbox("Metrik", metric_opts,
                          index=metric_opts.index("Selesai") if "Selesai" in metric_opts else 0)
    top = m2.radio("Urutan", ["Top", "Bottom"], horizontal=True) == "Top"
    n2 = m3.slider("N", 1, max(1, len(summary)), min(15, len(summary)), key="sum_n") if len(summary) > 1 else 1
    st.plotly_chart(topn_bar(summary, metric, n2, top, f"{'Top' if top else 'Bottom'} {n2} — {metric}"), width="stretch")
st.caption("👆 Klik baris engineer untuk membuka halaman **Detail Engineer**.")
_ev_s = st.dataframe(summary, column_config=cols(summary), width="stretch", hide_index=True,
                     on_select="rerun", selection_mode="single-row", key="summary_select")
_open_detail(summary, _ev_s)

# ===================================================================== 3) MATRIKS TASK vs COMMIT
if data.has_commit_data:
    st.header("🔭 Matriks Task vs Commit")
    sc = pd.DataFrame([{"Engineer": e.name, "Selesai": e.completed, "Hari aktif": e.active_days,
                        "Skor": e.utilization_score} for e in data.engineers])
    t_med, a_med = sc["Selesai"].median(), sc["Hari aktif"].median()
    color = "Skor" if (data.has_utilization and sc["Skor"].notna().any()) else "Selesai"
    fig = px.scatter(sc, x="Selesai", y="Hari aktif", hover_name="Engineer", color=color,
                     color_continuous_scale="RdYlGn",
                     labels={"Selesai": "Task selesai (ClickUp)", "Hari aktif": "Hari aktif commit (GitLab)"})
    fig.add_vline(x=t_med, line_dash="dash", line_color="gray")
    fig.add_hline(y=a_med, line_dash="dash", line_color="gray")
    fig.update_traces(marker=dict(size=11))
    fig.update_layout(height=480, margin=dict(t=30))
    st.plotly_chart(fig, width="stretch")
    st.caption(f"Hover untuk nama. Garis = median (task {t_med:g}, hari aktif {a_med:g}). "
               "Kanan-bawah = banyak task sedikit commit; kiri-atas = aktif ngoding jarang update task.")

# ===================================================================== 4) THROUGHPUT MINGGUAN (total tim)
if data.weeks:
    st.header("📈 Throughput per minggu (total tim)")
    st.bar_chart(weekly_frame(data).sum(axis=0))
    st.caption("Total task selesai tim per minggu. Rincian per engineer ada di halaman Detail.")

# ===================================================================== 5) BOTTLENECK
if data.deep and data.status_flow:
    st.header("🧱 Bottleneck (median jam per status)")
    bf = bottleneck_frame(data)
    st.plotly_chart(px.bar(bf.sort_values("Median (jam)"), x="Median (jam)", y="Status",
                           orientation="h", height=max(280, 26 * len(bf) + 80)), width="stretch")
    st.dataframe(bf, width="stretch", hide_index=True)

# Download Markdown
now = datetime.now(timezone(timedelta(hours=7)))  # WIB
md = render_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M %Z"))
st.download_button("⬇️ Download laporan Markdown", md, file_name="report.md", mime="text/markdown")
