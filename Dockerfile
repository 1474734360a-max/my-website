FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8686 DEMO_MODE=1
EXPOSE 8686
CMD ["python", "app.py"]
