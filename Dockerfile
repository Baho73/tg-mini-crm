FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY crm.py .
ENV DB_PATH=/data/crm.db
CMD ["uvicorn", "crm:app", "--host", "0.0.0.0", "--port", "8000"]
