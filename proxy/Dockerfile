FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY templates ./templates

ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

EXPOSE 3000

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "app:app", "--workers", "1"]
