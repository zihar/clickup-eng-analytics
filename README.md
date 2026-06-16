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
| **Status flow / bottleneck** | Median/p90 lama task nyangkut di tiap status (status terminal dikecualikan). Butuh `--deep`. |
| **Aktivitas commit (GitLab)** | Commit, hari aktif, +/- baris, & repo per engineer. Sumber: GitLab API langsung (live) atau DB squad-scorecard. Opsional. |
| **Matriks task vs commit** | Kuadran 2×2 (throughput ClickUp × hari aktif commit) untuk lihat pola disiplin task vs output kode. |

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

### (Opsional) Aktivitas commit GitLab

Dua sumber, pilih dengan `--commits-source {auto,gitlab,db,none}` (default `auto`: GitLab dulu, fallback DB):

**A. GitLab API langsung (live, disarankan)** — selalu mutakhir, plus +/- baris asli.
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

**B. DB squad-scorecard** — cepat tapi bergantung kesegaran ETL. Isi `db.dsn` atau env `SCORECARD_DSN`:

```yaml
db:
  dsn: "postgres://user:pass@localhost:5432/scorecard?sslmode=disable"
```

Tool menambah section **Aktivitas Commit** + **Matriks Task vs Commit**, join lewat id ClickUp.
Kalau pakai DB dan datanya lebih lama dari periode, laporan memperingatkan otomatis.
Matikan fitur commit dengan `--no-commits`.

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
