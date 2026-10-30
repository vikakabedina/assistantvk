FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --only-binary=:all: pydantic-core && \
    pip install -r requirements.txt

COPY bot.py miniapp.py miniapp.html ./

CMD ["python", "bot.py"]
