FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
COPY src/ /app/sqlglot/
COPY selector.py map.pkl coverage_guard.py coverage.json.gz /app/
RUN chmod -R a+rX /app
CMD ["python", "-S", "-B", "/app/coverage_guard.py"]
