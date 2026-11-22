# Same base image the track labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The packages the track labeled with, so sqlglot's tests can run at decision time.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
# src/ holds sqlglot at the pinned commit eb11aec4 (.py files and tests/ with fixtures).
COPY src/ /app/sqlglot/
COPY selector.py verify.py map.pkl tpcmap.pkl /app/
RUN python -m compileall -q /app/sqlglot /app/selector.py /app/verify.py; chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
