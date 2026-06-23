"""Dashboard Streamlit untuk engineering-productivity (satu halaman, filter di atas).

Jalankan:
    streamlit run dashboard.py

Default baca cache DB (mode offline) — ringan tiap ganti filter. Toggle "Data live"
di bar atas untuk menarik data terbaru. Refresh DB dilakukan job nightly (lihat deploy/).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Engineering Productivity", page_icon="📊",
                   layout="wide", initial_sidebar_state="collapsed")

from dashboard_lib import (
    add_chapter,
    cols,
    coverage_note,
    engineer_links,
    load_base_config,
    load_data,
    render_roster_editor,
    render_topbar,
    tgl,
    topn_bar,
)
from engineering_productivity.metrics import ReportData, _percentile
from engineering_productivity.report import render_markdown

VIEW_TEAM = "📊 Tim"
VIEW_DETAIL = "🔎 Detail engineer"
_NUMERIC = {"Selesai", "Lead median (hari)", "Cycle median (hari)", "Commits",
            "Hari aktif", "Repo", "+Baris", "-Baris", "WIP", "Story point"}

# ------------------------------------------------------------------ tampilan (CSS)
st.markdown(
    """
    <style>
      /* Hilangkan sidebar & kontrol buka-sidebar — semua filter pindah ke atas. */
      section[data-testid="stSidebar"] {display: none !important;}
      [data-testid="stSidebarCollapsedControl"],
      [data-testid="collapsedControl"] {display: none !important;}
      [data-testid="stHeader"] {background: transparent;}

      .block-container {padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1480px;}

      /* Kartu metrik: tenang, rapi, ber-border tipis. */
      [data-testid="stMetric"] {
        background: #F8FAFC; border: 1px solid #E2E8F0;
        border-radius: 14px; padding: 16px 18px;
      }
      [data-testid="stMetricValue"] {font-weight: 650; letter-spacing: -0.02em;}
      [data-testid="stMetricLabel"] p {color: #64748B; font-weight: 500;}

      h1 {font-weight: 720; letter-spacing: -0.03em;}
      h2, h3 {letter-spacing: -0.015em; margin-top: 0.4rem;}

      /* Segmented control (pemilih tampilan) sedikit lebih lega. */
      [data-testid="stSegmentedControl"] button {padding: 6px 18px;}

      /* Tombol Filter (popover) jadi pill rapi ber-aksen. */
      [data-testid="stPopover"] button {
        border-radius: 10px; border: 1px solid #CBD5E1;
        font-weight: 600; color: #2563EB;
      }
      [data-testid="stPopover"] button:hover {border-color: #2563EB; background: #EFF4FF;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- frame helpers
def summary_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name,
        "Selesai": e.completed,
        **({"Selesai terakhir": e.last_done_date or "—"} if data.has_last_done else {}),
        "Lead median (hari)": e.lead_median,
        **({"Cycle median (hari)": e.cycle_median} if data.deep else {}),
        "Commits": e.commits,
        "Hari aktif": e.active_days,
        "Repo": e.repos_touched,
    } for e in data.engineers]
    return pd.DataFrame(rows)


def weekly_frame(data: ReportData) -> pd.DataFrame:
    rows = {e.name: {w: e.per_week.get(w, 0) for w in data.weeks} for e in data.engineers}
    return pd.DataFrame(rows).T


def bottleneck_frame(data: ReportData) -> pd.DataFrame:
    return pd.DataFrame([{
        "Status": b.status, "Median (jam)": b.median_hours, "p90 (jam)": b.p90_hours,
        "Rata-rata (jam)": b.avg_hours, "Jumlah task": b.count,
    } for b in data.status_flow])


def util_frame(data: ReportData) -> pd.DataFrame:
    rows = [{
        "Engineer": e.name, "Skor": e.utilization_score, "WIP": e.open_tasks,
        "Hari aktif": e.active_days, "Selesai": e.completed, "Story point": e.story_points,
        "Sinyal rendah": ", ".join(e.low_signals) or "—",
    } for e in data.engineers]
    return pd.DataFrame(rows).sort_values("Skor", na_position="last").reset_index(drop=True)


def _slider(label, total, key, default=15):
    hi = max(1, total)
    return st.slider(label, 1, hi, min(default, hi), key=key) if hi > 1 else hi


# ====================================================================== OVERVIEW
def render_overview(data: ReportData, name_to_chapter: dict) -> None:
    summary = add_chapter(summary_frame(data), name_to_chapter)

    st.subheader("🎯 Utilisasi tim")
    if data.has_utilization:
        uf = add_chapter(util_frame(data), name_to_chapter)
        st.caption(
            f"Skor 0–100 relatif tim (sinyal: {', '.join(data.utilization_signals) or '—'}). "
            "Makin rendah = makin underutilized — pemicu obrolan kapasitas, **bukan vonis kinerja**."
        )
        flagged = uf[uf["Skor"].notna() & (uf["Skor"] <= 33.3)]
        if len(flagged):
            st.warning("⚠️ Perlu perhatian (sepertiga terbawah): "
                       + ", ".join(f"{r.Engineer} ({r.Skor:.0f})" for r in flagged.itertuples()))
        else:
            st.success("Tidak ada engineer di sepertiga terbawah.")
        uc1, uc2 = st.columns([3, 2])
        with uc1:
            n = _slider("Tampilkan N skor terendah", len(uf), "util_n")
            st.plotly_chart(topn_bar(uf, "Skor", n, top=False, title=f"{n} skor terendah"), width="stretch")
        with uc2:
            st.plotly_chart(px.histogram(uf, x="Skor", nbins=10, title="Distribusi skor"), width="stretch")
        st.caption("🔗 Klik nama engineer untuk membuka Detail.")
        st.dataframe(engineer_links(uf), column_config=cols(uf), width="stretch", hide_index=True)
    else:
        st.info("Analisis utilisasi tidak aktif untuk filter ini.")

    st.subheader("📋 Ringkasan metrik")
    metric_opts = [c for c in summary.columns if c in _NUMERIC]
    if metric_opts:
        m1, m2, m3 = st.columns([2, 1, 2])
        metric = m1.selectbox("Metrik", metric_opts,
                              index=metric_opts.index("Selesai") if "Selesai" in metric_opts else 0)
        top = m2.radio("Urutan", ["Top", "Bottom"], horizontal=True) == "Top"
        n2 = m3.slider("N", 1, max(1, len(summary)), min(15, len(summary)), key="sum_n") if len(summary) > 1 else 1
        st.plotly_chart(topn_bar(summary, metric, n2, top, f"{'Top' if top else 'Bottom'} {n2} — {metric}"),
                        width="stretch")
    st.caption("🔗 Klik nama engineer untuk membuka Detail.")
    st.dataframe(engineer_links(summary), column_config=cols(summary), width="stretch", hide_index=True)

    if data.has_commit_data:
        st.subheader("🔭 Matriks task vs commit")
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
        fig.update_layout(height=460, margin=dict(t=30))
        st.plotly_chart(fig, width="stretch")
        st.caption(f"Garis = median (task {t_med:g}, hari aktif {a_med:g}). "
                   "Kanan-bawah = banyak task sedikit commit; kiri-atas = aktif ngoding jarang update task.")

    if data.weeks:
        st.subheader("📈 Throughput per minggu (tim)")
        st.bar_chart(weekly_frame(data).sum(axis=0))

    if data.deep and data.status_flow:
        st.subheader("🧱 Bottleneck (median jam per status)")
        bf = bottleneck_frame(data)
        st.plotly_chart(px.bar(bf.sort_values("Median (jam)"), x="Median (jam)", y="Status",
                               orientation="h", height=max(280, 26 * len(bf) + 80)), width="stretch")
        st.dataframe(bf, width="stretch", hide_index=True)

    now = datetime.now(timezone(timedelta(hours=7)))
    md = render_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M %Z"))
    st.download_button("⬇️ Download laporan Markdown", md, file_name="report.md", mime="text/markdown")


# ======================================================================== DETAIL
def render_detail(data: ReportData, name_to_chapter: dict) -> None:
    names = [e.name for e in data.engineers]
    target = st.session_state.get("detail_engineer")
    if target not in names:
        target = st.session_state.get("detail_pick")
    if target not in names:
        target = names[0]
    st.session_state["detail_pick"] = target

    who = st.selectbox("Engineer", names, key="detail_pick")
    st.session_state["detail_engineer"] = who
    e = {x.name: x for x in data.engineers}[who]
    st.subheader(f"{who}  ·  {name_to_chapter.get(who, '—')}")

    k = st.columns(3)
    k[0].metric("Task selesai", e.completed)
    k[1].metric("WIP (open)", e.open_tasks if data.has_utilization else "—")
    k[2].metric("Story point", f"{e.story_points:g}" if data.has_utilization else "—")
    k2 = st.columns(3)
    k2[0].metric("Commits", e.commits if data.has_commit_data else "—")
    k2[1].metric("Hari aktif", e.active_days if data.has_commit_data else "—")
    k2[2].metric("Skor utilisasi",
                 f"{e.utilization_score:.0f}" if (data.has_utilization and e.utilization_score is not None) else "—")
    if data.has_last_done:
        st.caption(f"Task terakhir selesai: **{e.last_done_date or '—'}**")

    st.subheader("⏱️ Lead time & cycle time")
    cc = st.columns(2)
    with cc[0]:
        st.metric("Lead median (hari)", e.lead_median)
        p90 = round(_percentile(e.lead_times_days, 90), 1) if e.lead_times_days else 0.0
        st.caption(f"mean {e.lead_mean} · p90 {p90} · n={len(e.lead_times_days)}")
        if e.lead_times_days:
            st.plotly_chart(px.histogram(pd.DataFrame({"Lead (hari)": e.lead_times_days}),
                                         x="Lead (hari)", nbins=20, title="Distribusi lead time"), width="stretch")
        else:
            st.info("Belum ada data lead time pada periode ini.")
    with cc[1]:
        if data.deep and e.cycle_times_days:
            st.metric("Cycle median (hari)", e.cycle_median)
            st.caption(f"n={len(e.cycle_times_days)}")
            st.plotly_chart(px.histogram(pd.DataFrame({"Cycle (hari)": e.cycle_times_days}),
                                         x="Cycle (hari)", nbins=20, title="Distribusi cycle time"), width="stretch")
        else:
            st.metric("Cycle median (hari)", "—")
            st.caption("Cycle time butuh data deep (di-fetch saat mode live + Deep).")

    st.subheader("📈 Throughput per minggu")
    if data.weeks:
        st.bar_chart(pd.Series({w: e.per_week.get(w, 0) for w in data.weeks}))
    else:
        st.info("Tidak ada data mingguan pada periode ini.")

    st.subheader("📋 Task selesai (periode)")
    if e.tasks:
        rows = sorted(e.tasks, key=lambda t: t["date_done"], reverse=True)
        st.caption(f"{len(rows)} task selesai oleh **{who}** — klik nama task untuk buka di ClickUp.")

        def _cell(v) -> str:
            return (str(v).replace("|", "\\|").replace("[", "\\[")
                    .replace("]", "\\]").replace("\n", " "))

        def _num(v) -> str:
            return "—" if v is None else f"{v:g}"

        lines = ["| Task | Status | Selesai | Lead (hari) | Cycle (hari) | Point |",
                 "|---|---|---|--:|--:|--:|"]
        for t in rows:
            name = _cell(t["name"])
            label = f"[{name}]({t['url']})" if t.get("url") else name
            lines.append(f"| {label} | {_cell(t['status'])} | {t['date_done']} | "
                         f"{_num(t['lead_days'])} | {_num(t.get('cycle_days'))} | {_num(t['points'])} |")
        st.markdown("\n".join(lines))
    else:
        st.info("Tidak ada task selesai pada periode ini.")

    st.subheader("💻 Aktivitas commit (GitLab)")
    if data.has_commit_data:
        note = "tanpa noise" if data.commit_noise_filtered else "mentah"
        cm = st.columns(4)
        cm[0].metric("Commits", e.commits)
        cm[1].metric("Hari aktif", e.active_days)
        cm[2].metric("Repo disentuh", e.repos_touched)
        cm[3].metric(f"±Baris ({note})", f"+{e.commit_additions:,} / -{e.commit_deletions:,}")

        def _repo(pid) -> str:
            return data.repo_names.get(str(pid), str(pid))

        if e.commit_rows:
            agg: dict[str, dict] = {}
            for r in e.commit_rows:
                pid = str(r["project_id"])
                a = agg.setdefault(pid, {"commits": 0, "add": 0, "del": 0, "last": ""})
                a["commits"] += 1
                a["add"] += int(r.get("additions") or 0)
                a["del"] += int(r.get("deletions") or 0)
                d = (r.get("committed_date") or "")[:10]
                if d > a["last"]:
                    a["last"] = d
            st.markdown(f"**📦 Repo aktif ({len(agg)})**")
            rdf = pd.DataFrame([{
                "Repo": _repo(pid), "Commits": v["commits"],
                "+Baris": v["add"], "-Baris": v["del"], "Push terakhir": v["last"],
            } for pid, v in agg.items()]).sort_values("Commits", ascending=False)
            st.dataframe(rdf, width="stretch", hide_index=True)

            st.markdown(f"**📝 Daftar commit ({len(e.commit_rows)})**")
            cdf = pd.DataFrame([{
                "Tanggal": (r.get("committed_date") or "")[:10],
                "Repo": _repo(r["project_id"]),
                "Commit": (r.get("title") or "—"),
                "SHA": (r.get("sha") or "")[:8],
                "+Baris": int(r.get("additions") or 0),
                "-Baris": int(r.get("deletions") or 0),
            } for r in sorted(e.commit_rows, key=lambda r: r.get("committed_date") or "", reverse=True)])
            st.dataframe(cdf, width="stretch", hide_index=True)
        else:
            st.caption("Tidak ada commit untuk engineer ini pada periode ini.")
    else:
        st.info("Sumber commit GitLab tidak aktif / tidak ada data pada periode ini.")

    st.subheader("🎯 Posisi relatif tim")
    if data.has_utilization and e.utilization_score is not None:
        st.markdown(f"Skor utilisasi: **{e.utilization_score:.0f}** / 100  ·  "
                    f"sinyal rendah: {', '.join(e.low_signals) or '—'}")
        comp = data.engineers

        def _med(vals: list[float]) -> float:
            return round(statistics.median(vals), 2) if vals else 0.0

        rows = [
            ("Task selesai", e.completed, _med([x.completed for x in comp])),
            ("WIP", e.open_tasks, _med([x.open_tasks for x in comp])),
            ("Commits", e.commits, _med([x.commits for x in comp])),
            ("Hari aktif", e.active_days, _med([x.active_days for x in comp])),
            ("Story point", e.story_points, _med([x.story_points for x in comp])),
        ]
        st.dataframe(pd.DataFrame(rows, columns=["Metrik", who, "Median tim"]),
                     width="stretch", hide_index=True)
    else:
        st.info("Analisis utilisasi tidak aktif untuk filter ini.")


# ============================================================================ app
# Klik nama engineer (LinkColumn ?engineer=Nama) → buka Detail untuk engineer itu.
_qp_eng = st.query_params.get("engineer")
if _qp_eng:
    st.session_state["detail_engineer"] = _qp_eng
    st.session_state["view"] = VIEW_DETAIL
    del st.query_params["engineer"]

base_config = load_base_config()

h_title, h_team, h_filter, h_view = st.columns([5, 1.4, 1.7, 2.6], vertical_alignment="center")
with h_title:
    st.markdown("## 📊 Engineering Productivity")
with h_team:
    render_roster_editor(base_config)
with h_filter:
    filters = render_topbar(base_config)
with h_view:
    st.session_state.setdefault("view", VIEW_TEAM)
    view = st.segmented_control("Tampilan", [VIEW_TEAM, VIEW_DETAIL],
                                key="view", label_visibility="collapsed")
NAME_TO_CHAPTER = filters["name_to_chapter"]

data = load_data(filters)
if data is None:
    st.stop()

st.markdown(f"📅 **{tgl(data.since)} – {tgl(data.until)}**  ·  "
            f"{(filters['until_d'] - filters['since_d']).days + 1} hari")
coverage_note(data, filters)
st.divider()

if not data.engineers:
    st.warning("Tidak ada engineer pada filter ini.")
elif view == VIEW_DETAIL:
    render_detail(data, NAME_TO_CHAPTER)
else:
    render_overview(data, NAME_TO_CHAPTER)
