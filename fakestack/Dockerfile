# Build from the repo root:  docker build -f fakestack/Dockerfile -t fakestack:dev .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY fakestack/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fakestack/ fakestack/
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000
CMD ["python", "-m", "fakestack.app"]
