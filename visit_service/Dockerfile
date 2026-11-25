FROM python:3.13-slim
ARG RELEASE_SHA=unknown
LABEL org.opencontainers.image.revision=$RELEASE_SHA
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app/visit_service/
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "-B", "-m", "visit_service.app"]
