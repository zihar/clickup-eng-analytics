# Image tunggal untuk dashboard (Deployment) maupun nightly refresh (CronJob).
FROM python:3.12-slim

WORKDIR /app

# Dependency dulu (layer cache).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kode aplikasi.
COPY engineering_productivity/ engineering_productivity/
COPY pages/ pages/
COPY dashboard.py dashboard_lib.py ./

EXPOSE 8501

# Default: dashboard. Token & DSN via env (CLICKUP_TOKEN, GITLAB_TOKEN, EP_STORE_DSN);
# config non-rahasia (engineers, gitlab url/projects) di-mount ke /app/config.yaml.
# Nightly CronJob meng-override command jadi: python -m engineering_productivity ...
CMD ["streamlit", "run", "dashboard.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
