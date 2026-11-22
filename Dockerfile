# Same base image the track labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# src/ holds sqlglot at the pinned commit eb11aec4 (.py files and tests/ with fixtures).
COPY src/ /app/sqlglot/
COPY selector.py verify.py map.pkl /app/
RUN python -m compileall -q /app/sqlglot /app/selector.py /app/verify.py; chmod -R a+rX /app
CMD ["python", "-S", "-B", "/app/selector.py"]
