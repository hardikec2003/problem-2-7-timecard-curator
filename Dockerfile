FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY data ./data
COPY config ./config

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "src/main.py"]
CMD ["run", "--reasoner", "local", "--out", "out/demo"]
