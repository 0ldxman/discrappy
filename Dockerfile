# --- Стадия 1: сборка фронтенда (React + Vite) -------------------------------
# Собираем на платформе сборщика (ассеты платформонезависимы) — не эмулируем
# node под arm64, что заметно ускоряет мультиарх-сборку.
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Стадия 2: рантайм (FastAPI + uvicorn) -----------------------------------
FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Кладём собранный фронтенд — FastAPI отдаёт его из frontend/dist.
COPY --from=frontend /app/frontend/dist ./frontend/dist

# Каталог данных: config.json, discrapp.db (SQLite) и выгрузки — монтируется томом.
ENV DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8000"]
