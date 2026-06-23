"""Kode bersama untuk dashboard multipage (overview + detail per engineer).

Berisi: pemuatan config, sidebar filter (state ter-share lintas page via
st.session_state), fetch ter-cache (gather_cached — DEFINISIKAN SEKALI di sini
supaya cache @st.cache_data dipakai bersama semua page), dan helper presentasi.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Pastikan paket lokal bisa diimpor apa pun launcher-nya (streamlit run / AppTest).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engineering_productivity.config import ConfigError, load_config
from engineering_productivity.metrics import ReportData
from engineering_productivity.pipeline import GatherOptions, gather_report

CONFIG_PATH = os.environ.get("EP_CONFIG", "config.yaml")

# Sumber data tiap kolom — dipakai sebagai tooltip "?" di header tabel.
COLUMN_HELP = {
    "Engineer": "Nama dari daftar engineer (member ClickUp).",
    "Chapter": "Chapter/disiplin engineer (dari config).",
    "Selesai": "ClickUp — task berstatus done dengan tanggal selesai dalam periode.",
    "Selesai terakhir": "ClickUp — tanggal task terakhir berstatus done (lintas periode, mode --last-done).",
    "Lead median (hari)": "ClickUp — median (tanggal selesai − tanggal dibuat).",
    "Cycle median (hari)": "ClickUp time_in_status (mode Deep) — median waktu di status aktif.",
    "Commits": "GitLab — jumlah commit (dicocokkan via email penulis).",
    "Hari aktif": "GitLab — jumlah hari yang ada commit.",
    "Repo": "GitLab — jumlah repo yang disentuh.",
    "+Baris": "GitLab — baris ditambah (mentah, atau tanpa noise bila filter aktif).",
    "-Baris": "GitLab — baris dihapus (mentah, atau tanpa noise bila filter aktif).",
    "WIP": "ClickUp — jumlah task open (belum done) yang di-assign ke engineer.",
    "Story point": "ClickUp — field native 'points' (sprint point) dari task selesai + open.",
    "Skor": "Dihitung — rata-rata percentile lintas sinyal (0–100; makin rendah = makin underutilized).",
    "Sinyal rendah": "Sinyal di mana engineer ada di sepertiga terbawah tim.",
}
_NUMERIC = {"Selesai", "Lead median (hari)", "Cycle median (hari)", "Commits",
            "Hari aktif", "Repo", "+Baris", "-Baris", "WIP", "Story point"}

_BULAN = ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def tgl(iso: str) -> str:
    """Format 'YYYY-MM-DD' jadi '23 Jun 2026'."""
    y, m, d = iso.split("-")
    return f"{int(d)} {_BULAN[int(m)]} {y}"


def cols(df: pd.DataFrame) -> dict:
    """Bangun column_config (tooltip sumber data + format) untuk kolom yang ada di df."""
    cfg = {}
    for c in df.columns:
        h = COLUMN_HELP.get(c)
        if c == "Skor":
            cfg[c] = st.column_config.ProgressColumn(c, help=h, min_value=0, max_value=100, format="%.0f")
        elif c in _NUMERIC:
            cfg[c] = st.column_config.NumberColumn(c, help=h)
        else:
            cfg[c] = st.column_config.TextColumn(c, help=h)
    return cfg


def add_chapter(df: pd.DataFrame, name_to_chapter: dict) -> pd.DataFrame:
    """Sisipkan kolom Chapter (dari config) setelah kolom Engineer."""
    if "Engineer" in df.columns and "Chapter" not in df.columns:
        df.insert(1, "Chapter", df["Engineer"].map(name_to_chapter))
    return df


def topn_bar(df: pd.DataFrame, col: str, n: int, top: bool, title: str):
    """Bar horizontal Top/Bottom-N untuk satu metrik (skalabel ke banyak engineer)."""
    d = df[["Engineer", col]].dropna().sort_values(col, ascending=not top).head(n)
    fig = px.bar(d, x=col, y="Engineer", orientation="h", title=title, text=col)
    fig.update_layout(
        yaxis={"categoryorder": "total ascending" if top else "total descending"},
        height=max(280, 26 * len(d) + 80), margin=dict(t=40, b=10),
    )
    return fig


@st.cache_data(show_spinner="Menarik data ...", persist="disk", max_entries=128)
def gather_cached(
    config_path: str,
    engineer_names: tuple[str, ...],
    since: str,
    until: str,
    deep: bool,
    max_age: int | None,
    no_discover: bool,
    exclude_noise: bool,
    last_done: bool,
    offline: bool,
) -> ReportData:
    cfg = load_config(config_path)
    if engineer_names:
        chosen = set(engineer_names)
        cfg = dataclasses.replace(cfg, engineers=[e for e in cfg.engineers if e.name in chosen])
    opts = GatherOptions(
        since=since, until=until, deep=deep, max_age=max_age,
        no_discover=no_discover, exclude_noise=exclude_noise, last_done=last_done,
        offline=offline,
    )  # tz=+7, utilisasi & commit GitLab selalu nyala (default)
    return gather_report(cfg, opts)


def load_base_config():
    """Muat config dasar (semua engineer); hentikan halaman dgn pesan bila gagal."""
    try:
        return load_config(CONFIG_PATH)
    except ConfigError as exc:
        st.error(f"Konfigurasi belum siap: {exc}")
        st.stop()


def render_topbar(base_config) -> dict:
    """Render bar filter di atas (bukan sidebar) -> dict pilihan. State ter-share via key.

    Hanya filter inti + toggle 'Data live'; opsi lanjutan (deep/discover/noise/last-done)
    tak relevan di mode cache default, jadi dipakai nilai tetap.
    """
    name_to_chapter = {e.name: (e.chapter or "(tanpa chapter)") for e in base_config.engineers}
    all_chapters = sorted(set(name_to_chapter.values()))
    # Default awal: tampilkan chapter Golang saja (kalau ada); selain itu fallback ke semua chapter.
    default_chapters = [c for c in all_chapters if "Golang" in c] or all_chapters

    st.session_state.setdefault("flt_chapters", default_chapters)
    # Sanitasi terhadap perubahan config supaya tak error "value bukan opsi valid".
    st.session_state["flt_chapters"] = [c for c in st.session_state["flt_chapters"] if c in all_chapters]

    today = date.today()
    st.session_state.setdefault("flt_period", (today - timedelta(days=30), today))
    st.session_state.setdefault("flt_live", False)

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([2, 3, 2.2, 1.4], vertical_alignment="bottom")
        with c1:
            sel_chapters = st.multiselect("Chapter", all_chapters, key="flt_chapters",
                                          placeholder="Semua chapter")
        in_chapter = [n for n, ch in name_to_chapter.items() if ch in sel_chapters]
        st.session_state.setdefault("flt_engineers", in_chapter)
        st.session_state["flt_engineers"] = [n for n in st.session_state["flt_engineers"] if n in in_chapter]
        with c2:
            sel_names = st.multiselect("Engineer", in_chapter, key="flt_engineers",
                                       placeholder="Pilih engineer")
        with c3:
            rng = st.date_input("Periode", max_value=today, key="flt_period")
        with c4:
            live = st.toggle("🛰️ Data live", key="flt_live",
                             help="Default baca cache DB (cepat, di-refresh tiap malam). "
                                  "Nyalakan untuk data terbaru / periode di luar cache.")
            refresh = st.button("🔄 Refresh", width="stretch")

    if isinstance(rng, (tuple, list)) and len(rng) == 2:
        since_d, until_d = rng
    else:
        since_d, until_d = today - timedelta(days=30), today
    if refresh:
        gather_cached.clear()
        st.rerun()

    return {
        "sel_names": sel_names, "since_d": since_d, "until_d": until_d,
        "offline": not live, "name_to_chapter": name_to_chapter,
        # Opsi lanjutan: nilai tetap (tak ada UI-nya lagi).
        "deep": False, "max_age": 60, "no_discover": False,
        "exclude_noise": False, "last_done": False,
    }


def load_data(filters: dict) -> ReportData | None:
    """Tarik ReportData via cache untuk pilihan filter; None bila kosong/gagal (sudah lapor ke UI)."""
    if not filters["sel_names"]:
        st.warning("Pilih minimal satu engineer di sidebar.")
        return None
    try:
        return gather_cached(
            CONFIG_PATH, tuple(filters["sel_names"]),
            filters["since_d"].isoformat(), filters["until_d"].isoformat(),
            filters["deep"], filters["max_age"], filters["no_discover"],
            filters["exclude_noise"], filters["last_done"], filters["offline"],
        )
    except Exception as exc:  # noqa: BLE001 — tampilkan error apa pun ke UI
        st.error(f"Gagal menarik data: {exc}")
        return None


def coverage_note(data, filters) -> None:
    """Tampilkan info mode cache + peringatan bila periode melebihi data ter-cache."""
    if not getattr(data, "offline", False):
        st.caption("🛰️ Data live (baru ditarik dari ClickUp/GitLab).")
        return
    floor = getattr(data, "cache_since", None)
    if floor and filters["since_d"].isoformat() < floor:
        st.warning(
            f"⚠️ Mode cache: data sebelum **{tgl(floor)}** belum tentu lengkap "
            "(di luar window refresh nightly). Nyalakan **🛰️ Tarik data live** di sidebar "
            "untuk memuat & menyimpan periode ini."
        )
    else:
        st.caption("📦 Data dari cache DB (di-refresh tiap malam). "
                   "Nyalakan **🛰️ Tarik data live** untuk data hari ini.")
