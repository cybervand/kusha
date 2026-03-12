#!/bin/sh

# SMS Gateway Startup Script
# Generates self-signed SSL cert if needed, then runs uvicorn with HTTPS

CERT_PATH="${SMS_SSL_CERT:-/app/data/cert.pem}"
KEY_PATH="${SMS_SSL_KEY:-/app/data/key.pem}"

# Generate self-signed certificate if it doesn't exist
if [ ! -f "$CERT_PATH" ] || [ ! -f "$KEY_PATH" ]; then
    echo "[INFO] Generating self-signed SSL certificate..."
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$KEY_PATH" \
        -out "$CERT_PATH" \
        -days 365 \
        -nodes \
        -subj "/CN=sms-gateway/O=Local/C=NO"
    
    if [ $? -eq 0 ]; then
        echo "[INFO] SSL certificate generated successfully"
    else
        echo "[ERROR] Failed to generate SSL certificate"
        exit 1
    fi
else
    echo "[INFO] Using existing SSL certificate"
fi

# Check if SSL should be disabled (for testing)
if [ "$SMS_DISABLE_SSL" = "true" ]; then
    echo "[WARN] SSL disabled - running in HTTP mode"
    exec uvicorn main:app --host 0.0.0.0 --port 6969
else
    echo "[INFO] Starting with HTTPS on port 6969"
    exec uvicorn main:app --host 0.0.0.0 --port 6969 \
        --ssl-keyfile "$KEY_PATH" \
        --ssl-certfile "$CERT_PATH"
fi
