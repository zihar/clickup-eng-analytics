"""Halaman Detail per Engineer — profil lengkap satu engineer.

Filter sidebar & fetch dibagi dengan Overview lewat dashboard_lib (cache sama,
tidak fetch ulang selama filter sama). Pilihan engineer ter-share via
st.session_state["detail_engineer"] saat dinavigasi dari Overview.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Detail Engineer", page_icon="🔎", layout="wide")

# Pastikan modul bersama & paket lokal bisa diimpor saat dijalankan sebagai page.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard_lib import coverage_note, load_base_config, load_data, render_sidebar, tgl  # noqa: E402
from engineering_productivity.metrics import _percentile  # noqa: E402

st.title("🔎 Detail per Engineer")

base_config = load_base_config()
filters = render_sidebar(base_config)
data = load_data(filters)
if data is None:
    st.stop()
if not data.engineers:
    st.warning("Tidak ada engineer pada filter ini. Sesuaikan filter di sidebar.")
    st.stop()

names = [e.name for e in data.engineers]
# Honor pilihan dari Overview ("detail_engineer"); fallback ke pilihan sebelumnya / pertama.
target = st.session_state.get("detail_engineer")
if target not in names:
    target = st.session_state.get("detail_pick")
if target not in names:
    target = names[0]
st.session_state["detail_pick"] = target

who = st.selectbox("Engineer", names, key="detail_pick")
st.session_state["detail_engineer"] = who
e = {x.name: x for x in data.engineers}[who]
chapter = filters["name_to_chapter"].get(who, "—")

st.subheader(f"{who}  ·  {chapter}")
st.caption(f"Periode {tgl(data.since)} – {tgl(data.until)}")
coverage_note(data, filters)

# -------------------------------------------------------------------- KPI ringkas
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

# -------------------------------------------------------------------- lead & cycle time
st.header("⏱️ Lead time & Cycle time")
cc = st.columns(2)
with cc[0]:
    st.metric("Lead median (hari)", e.lead_median)
    p90 = round(_percentile(e.lead_times_days, 90), 1) if e.lead_times_days else 0.0
    st.caption(f"mean {e.lead_mean} · p90 {p90} · n={len(e.lead_times_days)}")
    if e.lead_times_days:
        st.plotly_chart(
            px.histogram(pd.DataFrame({"Lead (hari)": e.lead_times_days}), x="Lead (hari)",
                         nbins=20, title="Distribusi lead time"),
            width="stretch",
        )
    else:
        st.info("Belum ada data lead time pada periode ini.")
with cc[1]:
    if data.deep and e.cycle_times_days:
        st.metric("Cycle median (hari)", e.cycle_median)
        st.caption(f"n={len(e.cycle_times_days)}")
        st.plotly_chart(
            px.histogram(pd.DataFrame({"Cycle (hari)": e.cycle_times_days}), x="Cycle (hari)",
                         nbins=20, title="Distribusi cycle time"),
            width="stretch",
        )
    else:
        st.metric("Cycle median (hari)", "—")
        st.caption("Nyalakan toggle **Deep** di sidebar untuk cycle time per status.")

# -------------------------------------------------------------------- throughput mingguan
st.header("📈 Throughput per minggu")
if data.weeks:
    st.bar_chart(pd.Series({w: e.per_week.get(w, 0) for w in data.weeks}))
else:
    st.info("Tidak ada data mingguan pada periode ini.")

# -------------------------------------------------------------------- daftar task selesai (periode)
st.header("📋 Task selesai (periode)")
if e.tasks:
    rows = sorted(e.tasks, key=lambda t: t["date_done"], reverse=True)
    st.caption(f"{len(rows)} task selesai oleh **{who}** dalam periode ini "
               "— klik nama task untuk buka di ClickUp.")

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
        lines.append(
            f"| {label} | {_cell(t['status'])} | {t['date_done']} | "
            f"{_num(t['lead_days'])} | {_num(t.get('cycle_days'))} | {_num(t['points'])} |"
        )
    st.markdown("\n".join(lines))
else:
    st.info("Tidak ada task selesai pada periode ini.")

# -------------------------------------------------------------------- commit
st.header("💻 Aktivitas commit (GitLab)")
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
        # Repo aktif dalam periode — agregasi dari baris commit.
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
        st.subheader(f"📦 Repo aktif ({len(agg)})")
        rdf = pd.DataFrame([{
            "Repo": _repo(pid), "Commits": v["commits"],
            "+Baris": v["add"], "-Baris": v["del"], "Push terakhir": v["last"],
        } for pid, v in agg.items()]).sort_values("Commits", ascending=False)
        st.dataframe(rdf, width="stretch", hide_index=True)

        # Daftar commit.
        st.subheader(f"📝 Daftar commit ({len(e.commit_rows)})")
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

# -------------------------------------------------------------------- utilisasi & posisi relatif tim
st.header("🎯 Utilisasi & posisi relatif tim")
if data.has_utilization and e.utilization_score is not None:
    st.markdown(
        f"Skor utilisasi: **{e.utilization_score:.0f}** / 100  ·  "
        f"sinyal rendah: {', '.join(e.low_signals) or '—'}"
    )
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
    df = pd.DataFrame(rows, columns=["Metrik", who, "Median tim"])
    st.dataframe(df, width="stretch", hide_index=True)
    st.caption("Bandingkan angka engineer ini dengan median tim ter-filter. "
               "Skor utilisasi rendah = pemicu obrolan kapasitas, **bukan vonis kinerja**.")
else:
    st.info("Analisis utilisasi tidak aktif untuk filter ini.")
