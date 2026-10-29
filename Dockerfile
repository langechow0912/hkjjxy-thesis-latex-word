FROM python:3.12-slim-bookworm

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/thesis \
    TMPDIR=/data/tmp

RUN groupadd --gid 10001 thesis \
    && useradd --uid 10001 --gid thesis --create-home --shell /usr/sbin/nologin thesis \
    && mkdir -p /app/core /app/web/static /app/schools /app/samples /data/tmp /data/cache \
    && chown -R thesis:thesis /app /data /home/thesis

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY --chown=thesis:thesis core/ ./core/
COPY --chown=thesis:thesis web/ ./web/
COPY --chown=thesis:thesis schools/ ./schools/

USER thesis:thesis
EXPOSE 8000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "172.30.0.0/24"]
