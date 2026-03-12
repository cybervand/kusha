FROM python:3.14.0-alpine3.22

LABEL maintainer="Sens AS"
LABEL version="1.0.2"
LABEL description="Kusha - SMS Gateway API, twin of Lava"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV SMS_DB_PATH=/app/data/sms.db
ENV SMS_SSL_CERT=/app/data/cert.pem
ENV SMS_SSL_KEY=/app/data/key.pem

WORKDIR /app

# Install openssl for certificate generation
RUN apk add --no-cache openssl

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY start.sh .
RUN chmod +x start.sh

RUN addgroup -S dialout 2>/dev/null || true && \
    adduser -D appuser && \
    adduser appuser dialout && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 6969

# Use startup script that handles SSL
CMD ["./start.sh"]
