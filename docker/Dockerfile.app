FROM python:3.12-slim-bookworm
ARG SERVICE
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth curl ca-certificates fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ${SERVICE}/pyproject.toml ${SERVICE}/requirements.lock /build/
RUN python -m pip install --no-cache-dir -r /build/requirements.lock \
    && python -m playwright install --with-deps chromium \
    && if python -c 'import patchright' 2>/dev/null; then python -m patchright install chromium; fi
COPY common /app/common
COPY ${SERVICE} /app/service
COPY docker/container_entry.py docker/preflight.py /app/
ENV PYTHONPATH=/app/service:/app
WORKDIR /app/data
EXPOSE 8089 8090 8091
ENTRYPOINT ["xvfb-run", "-a", "python", "/app/container_entry.py"]
