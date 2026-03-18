FROM python:3.11-slim

# ffmpeg + 한글 폰트 설치
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-nanum \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 커스텀 폰트 설치 (Breip 손글씨체 등)
RUN mkdir -p /usr/share/fonts/truetype/custom \
    && cp /app/fonts/*.ttf /usr/share/fonts/truetype/custom/ 2>/dev/null || true \
    && fc-cache -f -v

RUN mkdir -p uploads outputs emoji_cache

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "--workers", "2", "app:app"]
