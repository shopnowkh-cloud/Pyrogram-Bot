FROM python:3.11-slim

WORKDIR /app

# System deps:
#  - libglib2.0-0, libgl1, libgomp1  → OpenCV headless
#  - libfreetype6, libjpeg62-turbo   → Pillow / PyMuPDF
#  - libsm6, libxext6, libxrender1   → OpenCV GUI stubs (headless still needs them)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libfreetype6 \
    libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Smoke-test every critical import so a broken dep fails at build time, not runtime
RUN python -c "\
import kurigram; \
from PIL import Image; \
import qrcode; \
import cv2; \
import numpy; \
import fitz; \
import httpx; \
print('All imports OK')"

COPY bot.py ./

CMD ["python", "bot.py"]
