# Deploy ke Kubernetes

Arsitektur:
- **Dashboard** (Deployment + Service) — Streamlit, jalan **mode offline** (baca cache DB), jadi ganti filter ringan.
- **Nightly refresh** (CronJob, tiap tengah malam `Asia/Jakarta`) — `python -m engineering_productivity --days 60` menarik data live semua engineer → warm cache DB (`ep_tasks`, `ep_commits`, `ep_engineer_repos`, `ep_projects`, `ep_meta`).
- **Postgres** (StatefulSet) — penyimpan cache. Boleh diganti DB terkelola (cukup ubah `EP_STORE_DSN` di Secret, hapus blok Postgres).

## Langkah

```bash
# 1. Build & push image
docker build -t <registry>/engineering-productivity:latest .
docker push <registry>/engineering-productivity:latest

# 2. Namespace
kubectl create namespace engineering-productivity

# 3. Edit deploy/k8s.yaml: ganti semua CHANGE_ME (token, password, image registry)
kubectl apply -f deploy/k8s.yaml

# 4. (opsional) warm cache pertama kali tanpa nunggu tengah malam
kubectl -n engineering-productivity create job --from=cronjob/ep-nightly-refresh ep-warm-now

# 5. Akses dashboard (port-forward / Ingress sesuai kebutuhan)
kubectl -n engineering-productivity port-forward svc/ep-dashboard 8501:80
```

## Catatan

- **Rahasia**: `CLICKUP_TOKEN`, `GITLAB_TOKEN`, `EP_STORE_DSN` dari Secret (env). `config.yaml` di ConfigMap **tanpa token**. `config.py` membaca env lebih dulu, jadi token tak perlu ada di file.
- **Mode offline default**: dashboard tidak fetch live; data di-refresh oleh CronJob. Toggle **🛰️ Tarik data live** di sidebar untuk menarik data terbaru / memuat periode di luar window cache (akan tersimpan ke DB).
- **Periode > window nightly (60 hari)**: dashboard menampilkan peringatan; nyalakan "Tarik data live" sekali untuk memuat & menyimpan periode itu. Atau perbesar `--days` di CronJob.
- **Timezone CronJob**: `timeZone` butuh Kubernetes ≥ 1.27. Bila lebih lama, set `schedule: "0 17 * * *"` (00:00 WIB = 17:00 UTC).
- **Backfill awal**: run pertama (cold) menarik task & commit ~`task_backfill_since`; berikutnya incremental (ringan).
