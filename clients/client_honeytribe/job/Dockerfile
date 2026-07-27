FROM python:3.12.13-slim
# Unbuffered stdout: without it Python holds every print until exit, so a long pull looks like a
# hung job in Cloud Logging (the first honeytribe run logged nothing for 15 minutes).
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 10001 appuser
USER appuser
CMD ["python", "main.py"]
