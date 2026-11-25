FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN rm -rf .env .venv data/raw data/normalized __pycache__
ENV PYTHONUNBUFFERED=1 SIAT_DATA_DIR=/data
CMD ["python", "telegram_bot.py"]
