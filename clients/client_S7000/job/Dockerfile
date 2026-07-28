FROM python:3.12.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m -u 10001 appuser
USER appuser
# Unbuffered: Cloud Run Jobs logs are line-buffered, and a buffered run looks hung.
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
