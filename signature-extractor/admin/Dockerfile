# admin/Dockerfile — Signature Management Service (panel admin TTD)
FROM python:3.13-slim

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn==0.34.0 \
    "python-multipart==0.0.20" \
    opencv-python-headless==4.10.0.84 \
    numpy==2.2.1 \
    pillow==11.1.0 \
    qrcode==8.0

WORKDIR /app
COPY app.py process.py db.py migrate.py .
COPY static/ static/
COPY fonts/ fonts/

# PUBLIC_BASE_URL: URL publik verifier, dipakai utk QR & link.
# Contoh: http://dev.rsudkotajambi.id/ttd  atau  https://ttd.rsudkotajambi.id
ENV PUBLIC_BASE_URL=http://dev.rsudkotajambi.id/ttd \
    DATA_ROOT=/data

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
