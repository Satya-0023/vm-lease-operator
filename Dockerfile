FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vmlease/ vmlease/
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000
CMD ["kopf", "run", "--standalone", "--all-namespaces", "--liveness=http://0.0.0.0:8080/healthz", "-m", "vmlease.main"]
