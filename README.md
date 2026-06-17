# ClickUp Engineering Analytics

[![CI](https://github.com/zihar/engineering-productivity/actions/workflows/ci.yml/badge.svg)](https://github.com/zihar/engineering-productivity/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Tool Python untuk menganalisis produktivitas engineer dari data [ClickUp](https://clickup.com)
lewat REST API. Menarik task per engineer (lintas space), menghitung metrik, lalu
menghasilkan **laporan Markdown** siap di-share ke management.

## Metrik yang dihitung

| Metrik | Penjelasan |
|---|---|
| **Throughput** | Jumlah task selesai per engineer, dipecah per minggu (ISO week). |
| **Lead time** | Waktu dari task dibuat → selesai (hari). Median & rata-rata. |
| **Cycle time** | Waktu task berada di status aktif (mis. In Progress, Review). Butuh `--deep`. |
| **Time tracked** | Jam time-tracking nyata per engineer vs estimasi, plus akurasi estimasi. |
| **Status flow / bottleneck** | Median/p90 lama task nyangkut di tiap status (status terminal dikecualikan). Butuh `--deep`. |
| **Aktivitas commit (GitLab)** | Commit, hari aktif, +/- baris, & repo per engineer. Sumber: GitLab API langsung (live). Opsional. |
| **Matriks task vs commit** | Kuadran 2×2 (throughput ClickUp × hari aktif commit) untuk lihat pola disiplin task vs output kode. |
| **Utilisasi (underutilized)** | Skor 0–100 relatif tim dari 4 sinyal (WIP, hari aktif commit, throughput, story point) untuk menandai engineer berkapasitas nganggur. Opsional. |

## Setup

```bash
cd engineering-productivity
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml   # lalu isi daftar engineer
export CLICKUP_TOKEN="pk_xxxxxxxx"    # token dari ClickUp: Settings -> Apps
```

> Token sebaiknya lewat environment variable `CLICKUP_TOKEN`, bukan ditulis di
> `config.yaml` (file itu sudah di-`.gitignore`).

## Konfigurasi

`config.yaml` cukup berisi daftar engineer (pakai email atau id numerik):

```yaml
team_id: ""               # opsional; kosongkan untuk pakai workspace pertama
engineers:
  - name: "Budi"
    email: "budi@example.com"
  - name: "Sari"
    id: 12345678
```

### (Opsional) Aktivitas commit GitLab

Pilih dengan `--commits-source {auto,gitlab,none}` (default `auto`: GitLab bila terkonfigurasi):

**GitLab API langsung (live)** — selalu mutakhir, plus +/- baris asli.
Generate token di `https://git.bluebird.id/-/user_settings/personal_access_tokens`
(scope `read_api`), lalu `export GITLAB_TOKEN=glpat-...`:

```yaml
gitlab:
  url: "https://git.bluebird.id"
  projects: [692, "da/driverapp-gateway"]   # opsional seed; id atau path
  aliases: {"orang@gmail.com": "orang@bluebirdgroup.com"}  # commit email pribadi
```

Secara default tool **auto-discover** repo tiap engineer (lewat push events GitLab),
digabung dengan `projects` seed — jadi repo yang tidak terdaftar (mis. `argocd/*`)
ikut tertangkap. Matikan dengan `--no-discover` kalau ingin pakai `projects` saja.

Flag `--exclude-noise` menghitung ulang +/- baris **tanpa file noise** (vendor, lockfile,
generated, dll — lihat `DEFAULT_NOISE_PATTERNS`; tambah lewat `gitlab.noise_patterns`).
Ini mengambil diff tiap commit (1 call/commit) sehingga **lebih lambat**, jadi opsional.

Tool menambah section **Aktivitas Commit** + **Matriks Task vs Commit**, join lewat id ClickUp.
Matikan fitur commit dengan `--no-commits`.

Lihat id/email member workspace:

```bash
python -m engineering_productivity --list-members
python -m engineering_productivity --list-teams
```

## Pemakaian

```bash
# 30 hari terakhir, laporan ringkas (cepat)
python -m engineering_productivity --days 30 -o reports/bulan-ini.md

# Rentang spesifik + analisis mendalam (cycle time & bottleneck)
python -m engineering_productivity --since 2026-05-01 --until 2026-05-31 --deep -o reports/mei.md
```

| Flag | Default | Fungsi |
|---|---|---|
| `--config` | `config.yaml` | Path konfigurasi |
| `--since` / `--until` | — / hari ini | Rentang tanggal `YYYY-MM-DD` |
| `--days` | `30` | Lookback bila `--since` kosong |
| `--tz` | `7` | Offset zona waktu untuk bucket minggu (7 = WIB) |
| `--deep` | off | Ambil `time_in_status` per task → cycle time & bottleneck |
| `--last-done` | off | Tambah kolom *Selesai terakhir* (kapan tiap engineer terakhir menutup task, lintas periode) |
| `--utilization` | off | Section *Engineer Underutilized* (skor relatif tim; tarik WIP & story point) |
| `-o`, `--output` | `reports/report.md` | File output |

> **Utilisasi:** skor 0–100 = rata-rata percentile lintas sinyal (makin rendah makin underutilized).
> Story point dibaca dari field native ClickUp `points` (sprint points); sinyal yang datanya kosong
> otomatis di-skip. Ini pemicu obrolan kapasitas, **bukan** ranking kinerja.

## Dashboard interaktif

Selain laporan Markdown, ada dashboard Streamlit untuk eksplorasi:

```bash
pip install -r requirements.txt
export CLICKUP_TOKEN=pk_...        # dan GITLAB_TOKEN=glpat-... bila pakai sumber GitLab
streamlit run dashboard.py
```

Fitur: filter periode & engineer, toggle `deep`/filter task basi/sumber commit/filter noise,
KPI ringkas, chart throughput & hari-aktif, **matriks Task vs Commit** interaktif (Plotly),
tabel bottleneck, dan tombol unduh laporan Markdown. Tombol **Refresh data** mengosongkan cache.
Dashboard memakai pipeline yang sama dengan CLI (`engineering_productivity.pipeline.gather_report`).

> Jalankan hanya di localhost — berisi data produktivitas karyawan. `deep` & filter noise
> membuat tiap interaksi lebih lambat (default keduanya OFF; hasil di-cache).

## Cache Postgres (opsional, biar load cepat)

Tanpa cache, tiap run/load menarik ulang semua dari ClickUp & GitLab — paling mahal di mode `--deep`
(1 call/task) dan commit GitLab (ratusan call). Aktifkan cache Postgres agar data **immutable**
(time_in_status task *done* & commit per sha) disimpan dan dipakai ulang; tiap load hanya menarik **delta**.

```bash
createdb engineering_productivity          # database terpisah di Postgres-mu
export EP_STORE_DSN=postgres://localhost:5432/engineering_productivity
```

Atau isi `store.dsn` di `config.yaml`. Otomatis aktif bila DSN ada; tanpa DSN = mode live (perilaku lama).
Run/dashboard kedua untuk parameter sama jadi jauh lebih cepat (deep dari cache, commit cuma yang baru).
Catatan: mode `--exclude-noise` belum di-cache (tetap live).

## Catatan akurasi

- **Atribusi via kolom Developer:** "siapa yang mengerjakan task" diambil dari custom field
  **Developer** (tipe users), bukan dari `assignees`. Task dengan Developer kosong dilewati.
  Field di-resolve otomatis by name (`developer_field_name`, default `Developer`); bisa
  dioverride dengan `developer_field_id`.
- **Shared credit:** task dengan banyak Developer dihitung untuk tiap engineer di kolom itu.
- **Time tracked** diambil dari endpoint *time entries* lalu dikreditkan ke **Developer** pada
  task time entry tersebut (bukan si pencatat), bukan dari field `time_spent` task. Catatan:
  hanya time entry pada task yang ikut ter-fetch yang terhitung.
- Mode `--deep` melakukan 1 panggilan API per task → lebih lambat & lebih boros kuota
  (rate limit ClickUp ~100 req/menit, sudah ditangani otomatis dengan retry).
- Metrik ini alat bantu diskusi, **bukan** penilaian kinerja absolut. Throughput tinggi
  belum tentu = produktif; selalu baca bareng konteks (kompleksitas task, dsb).

## Struktur

```
engineering_productivity/
  config.py     # muat & validasi config.yaml (+ token dari env)
  client.py     # klien ClickUp REST API v2 (paginasi + retry rate limit)
  gitlab.py     # sumber commit live dari GitLab API (+ auto-discover, filter noise)
  metrics.py    # perhitungan throughput, lead/cycle time, time tracked, status flow
  pipeline.py   # orkestrasi reusable (dipakai CLI & dashboard)
  report.py     # render Markdown
  __main__.py   # CLI
dashboard.py    # dashboard Streamlit (streamlit run dashboard.py)
```

## Lisensi

[MIT](LICENSE) © Zihar Mehta
