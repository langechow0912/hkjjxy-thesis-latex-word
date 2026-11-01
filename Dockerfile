FROM python:3.12-slim-bookworm

# true: install LibreOffice + Chinese fonts for the Haikou local formatter.
# false: API-only image for small servers (set THESIS_LOCAL_FORMAT=false at runtime too).
# The site no longer compiles LaTeX, so TeX Live is not installed.
ARG THESIS_LOCAL_FORMAT=true

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/thesis \
    TMPDIR=/data/tmp \
    THESIS_LOCAL_FORMAT=${THESIS_LOCAL_FORMAT}

# fonts-local.conf maps Word's Chinese font names (宋体/黑体/楷体) to installed fonts.
COPY deploy/fonts-local.conf /tmp/fonts-local.conf
RUN mkdir -p /data/tmp \
    && if [ "$THESIS_LOCAL_FORMAT" = "true" ]; then \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
        && printf 'force-unsafe-io\n' > /etc/dpkg/dpkg.cfg.d/01-force-unsafe-io \
        && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=60 update \
        && apt-get -y -o Acquire::Retries=5 -o Acquire::https::Timeout=60 --no-install-recommends install \
            ca-certificates fontconfig fonts-arphic-ukai fonts-arphic-uming \
            fonts-liberation fonts-noto-cjk fonts-noto-core libreoffice-writer \
        && rm -rf /var/lib/apt/lists/* \
        && cp /tmp/fonts-local.conf /etc/fonts/local.conf \
        && fc-cache -f; \
    fi \
    && rm -f /tmp/fonts-local.conf

RUN groupadd --gid 10001 thesis \
    && useradd --uid 10001 --gid thesis --create-home --shell /usr/sbin/nologin thesis \
    && mkdir -p /app/core /app/web/static /app/schools /app/samples /data/tmp /data/cache \
    && chown -R thesis:thesis /app /data /home/thesis

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY requirements-local.txt ./requirements-local.txt
RUN python -m pip install --no-cache-dir --prefer-binary -r requirements-local.txt

COPY --chown=thesis:thesis core/ ./core/
COPY --chown=thesis:thesis web/ ./web/
COPY --chown=thesis:thesis schools/ ./schools/

USER thesis:thesis
EXPOSE 8000

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "172.30.0.0/24"]
