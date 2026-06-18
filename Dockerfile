FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Outbound-only worker: no ports to expose.
CMD ["python", "-u", "notifier.py"]
