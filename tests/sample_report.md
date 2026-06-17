# Laporan Produktivitas Engineering

- **Periode:** 2024-05-01 s/d 2024-05-31
- **Engineer dianalisis:** 2
- **Total task selesai:** 4
- **Zona waktu bucket:** UTC+7
- **Dibuat:** 2024-05-31 09:00 WIB

> Catatan: atribusi task mengikuti kolom Developer (custom field). Task dengan banyak Developer dihitung untuk tiap engineer di kolom itu (shared credit). Cycle time hanya tersedia pada mode `--deep`.

## Ringkasan per Engineer

| Engineer | Selesai | Lead time median (hari) | Cycle time median (hari) | Tracked (jam) | Estimasi (jam) | Akurasi estimasi |
|---|--:|--:|--:|--:|--:|--:|
| Sari | 2 | 3.0 | 2.5 | 6.0 | 4.0 | 1.5× |
| Budi | 2 | 1.5 | 1.75 | 10.0 | 12.0 | 0.83× |

## Throughput per Minggu (jumlah task selesai)

| Engineer | 2024-W21 | 2024-W22 | Total |
|---|--:|--:|--:|
| Sari | 1 | 1 | 2 |
| Budi | 2 | 0 | 2 |

## Aktivitas Commit (GitLab)

Sumber: GitLab. **Hari aktif** (jumlah hari ada commit) lebih bermakna daripada total commit yang mudah diakali. Bandingkan dengan kolom *Selesai* di ringkasan: timpang besar = task ClickUp tidak mencerminkan kerja kode (atau sebaliknya). +/- baris mentah (termasuk vendor/lock/generated — gunakan `--exclude-noise` untuk menyaring).

| Engineer | Commits | Hari aktif | Repo | +Baris | -Baris |
|---|--:|--:|--:|--:|--:|
| Budi | 20 | 5 | 2 | 100 | 10 |
| Sari | 0 | 0 | 0 | 0 | 0 |

## Matriks Task vs Commit

Sumbu: **task selesai** (ambang median 2) × **hari aktif commit** (ambang median 2.5). Untuk melihat pola, **bukan ranking** — selalu baca dengan konteks (peran, jenis kerja, email commit yang mungkin belum ter-alias).

|  | Commit rendah | Commit tinggi |
|---|---|---|
| **Task tinggi** | — | — |
| **Task rendah** | Sari | Budi |

- **Task tinggi · commit rendah:** banyak task ditutup, sedikit kode — cek kerja non-kode atau commit di email yang belum ter-alias.
- **Task rendah · commit tinggi:** aktif ngoding tapi jarang update ClickUp — soal higiene task, bukan output.
- **Task rendah · commit rendah:** aktivitas rendah di dua sistem — perlu klarifikasi langsung.

## Status Flow / Bottleneck

Lama task berada di tiap status (semua task pada periode), diurutkan dari **median** tertinggi. Median lebih tahan outlier daripada rata-rata; selisih besar antara median dan p90/rata-rata menandakan ada beberapa task ekstrem. Status terminal (Done/Closed/Drop) dikecualikan karena bukan bottleneck.

| Status | Median (jam) | p90 (jam) | Rata-rata (jam) | Jumlah task |
|---|--:|--:|--:|--:|
| Review | 54.0 | 87.6 | 54.0 | 2 |
| In Progress | 36.0 | 45.6 | 36.0 | 2 |
| To Do | 24.0 | 24.0 | 24.0 | 1 |

## Detail per Engineer

### Sari

- Task selesai: **2**
- Lead time: median 3.0 hari · rata-rata 3.0 hari
- Cycle time (waktu aktif dikerjakan): median 2.5 hari
- Time tracked: 6.0 jam (estimasi 4.0 jam)
- Commit GitLab: 0 commit · 0 hari aktif · 0 repo
- Akurasi estimasi: 1.5× (lebih lama dari estimasi)

### Budi

- Task selesai: **2**
- Lead time: median 1.5 hari · rata-rata 1.5 hari
- Cycle time (waktu aktif dikerjakan): median 1.75 hari
- Time tracked: 10.0 jam (estimasi 12.0 jam)
- Commit GitLab: 20 commit · 5 hari aktif · 2 repo
- Akurasi estimasi: 0.83× (lebih cepat dari estimasi)

