# ClickUp Engineering Analytics

[![CI](https://github.com/zihar/clickup-eng-analytics/actions/workflows/ci.yml/badge.svg)](https://github.com/zihar/clickup-eng-analytics/actions/workflows/ci.yml)
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
| **Status flow / bottleneck** | Rata-rata lama task nyangkut di tiap status. Butuh `--deep`. |

## Setup

```bash
cd clickup-eng-analytics
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

Lihat id/email member workspace:

```bash
python -m clickup_analytics --list-members
python -m clickup_analytics --list-teams
```

## Pemakaian

```bash
# 30 hari terakhir, laporan ringkas (cepat)
python -m clickup_analytics --days 30 -o reports/bulan-ini.md

# Rentang spesifik + analisis mendalam (cycle time & bottleneck)
python -m clickup_analytics --since 2026-05-01 --until 2026-05-31 --deep -o reports/mei.md
```

| Flag | Default | Fungsi |
|---|---|---|
| `--config` | `config.yaml` | Path konfigurasi |
| `--since` / `--until` | — / hari ini | Rentang tanggal `YYYY-MM-DD` |
| `--days` | `30` | Lookback bila `--since` kosong |
| `--tz` | `7` | Offset zona waktu untuk bucket minggu (7 = WIB) |
| `--deep` | off | Ambil `time_in_status` per task → cycle time & bottleneck |
| `-o`, `--output` | `reports/report.md` | File output |

## Catatan akurasi

- **Shared credit:** task dengan banyak assignee dihitung untuk tiap engineer yang ditugaskan.
- **Time tracked** diambil dari endpoint *time entries* (akurat per orang), bukan dari
  field `time_spent` task (yang merupakan total semua assignee).
- Mode `--deep` melakukan 1 panggilan API per task → lebih lambat & lebih boros kuota
  (rate limit ClickUp ~100 req/menit, sudah ditangani otomatis dengan retry).
- Metrik ini alat bantu diskusi, **bukan** penilaian kinerja absolut. Throughput tinggi
  belum tentu = produktif; selalu baca bareng konteks (kompleksitas task, dsb).

## Struktur

```
clickup_analytics/
  config.py     # muat & validasi config.yaml (+ token dari env)
  client.py     # klien ClickUp REST API v2 (paginasi + retry rate limit)
  metrics.py    # perhitungan throughput, lead/cycle time, time tracked, status flow
  report.py     # render Markdown
  __main__.py   # CLI
```

## Lisensi

[MIT](LICENSE) © Zihar Mehta
