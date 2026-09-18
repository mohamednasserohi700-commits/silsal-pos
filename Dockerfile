FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/instance

ENV FLASK_ENV=production
ENV PORT=5000
EXPOSE 5000

CMD gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120 --access-logfile - --error-logfile -
