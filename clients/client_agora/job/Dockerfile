FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir telethon ijson google-cloud-storage tzdata
# telegram_pull.py + process_upwork.py are staged in by deploy_job_agora.ps1
COPY main.py telegram_pull.py process_upwork.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
