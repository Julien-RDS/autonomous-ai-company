# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY company company
COPY migrations migrations
COPY alembic.ini .
RUN useradd --uid 10001 --create-home company && mkdir -p reports data artifacts/prototypes && chown -R company:company /app
USER company
ENTRYPOINT ["python", "-m", "company.container_entrypoint"]
CMD ["python", "-m", "company.worker", "run"]
