FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir telethon ijson google-cloud-storage tzdata requests
# processing/* files are staged in by deploy_job_agora.ps1
COPY main.py alerts.py telegram_pull.py process_upwork.py score_jobs.py agora_job_fit_brief.md ./
ENV PYTHONUNBUFFERED=1
# ENTRYPOINT (not CMD): Cloud Run's --args must APPEND to the interpreter —
# with CMD they would REPLACE it and the container execs "--force" as a binary
ENTRYPOINT ["python", "main.py"]
