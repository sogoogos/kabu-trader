FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Paper trading state and models persist via mounted volume
VOLUME ["/app/paper_trading", "/app/models", "/app/config"]

ENTRYPOINT ["python", "-m", "kabu_trader.cli"]
CMD ["monitor", "--paper"]
