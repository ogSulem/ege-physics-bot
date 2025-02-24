FROM python:3.11-slim
WORKDIR /app

# Копируем только нужные файлы
COPY requirements.txt .
COPY bot.py .
COPY tasks.json .
COPY theory/ ./theory/

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]