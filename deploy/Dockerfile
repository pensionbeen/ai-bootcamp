FROM python:3.13-slim
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
COPY run.py ./
COPY src/ ./src/
COPY data/ ./data/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY deploy/ ./deploy/
RUN mkdir -p /app/data/processed/local_rag && chown -R 10001:10001 /app
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGINT
CMD ["python","-B","run.py","serve","--host","0.0.0.0","--port","8000"]
