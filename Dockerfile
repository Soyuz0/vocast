# Vocast RSS-to-podcast service.
#
# Two stages so the build toolchain does not ship in the runtime image. Python
# is pinned to 3.12 because Kokoro does not support 3.13 yet.
#
# The image is large (~2-3 GB): Kokoro pulls in PyTorch. That is inherent to
# running TTS locally, which is the point of the project.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Some wheels in the torch/kokoro tree still need a compiler on non-amd64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install CPU-only torch first so kokoro's torch dependency is already
# satisfied. The default PyPI wheel bundles CUDA libraries worth several GB
# that are dead weight on a CPU-only home server.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch

RUN pip install --no-cache-dir .

# Kokoro's English G2P lazily pip-installs this spaCy model on first use.
# That fails in the runtime image, which runs as an unprivileged user with no
# write access to site-packages, so install it now and keep first run offline.
RUN python -m spacy download en_core_web_sm


FROM python:3.12-slim AS runtime

# espeak-ng is Kokoro's fallback phonemizer and the only runtime system
# dependency; ffmpeg arrives through imageio-ffmpeg. curl is for the health
# check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VOCAST_DATABASE_PATH=/data/vocast.db \
    VOCAST_STORAGE_LIBRARY_PATH=/data/library \
    VOCAST_SERVER_HOST=0.0.0.0 \
    VOCAST_SERVER_PORT=8000 \
    HF_HOME=/data/cache/huggingface

# VOCAST_CONFIG is deliberately not set: /data/config.yaml is already one of the
# default lookup paths, so it is used when present and simply absent otherwise.
# Setting the variable would turn "no config file yet" into a hard error.

# Run unprivileged. /data is the only writable path the service needs, and the
# model cache is deliberately placed there so weights survive a restart
# instead of being re-downloaded on every container start.
RUN useradd --create-home --uid 10001 vocast \
    && mkdir -p /data/library /data/cache \
    && chown -R vocast:vocast /data

USER vocast
WORKDIR /home/vocast
VOLUME ["/data"]
EXPOSE 8000

# Report unhealthy if the database or the HTTP layer stops responding. The long
# start period covers the first-run Kokoro weight download (~300 MB).
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# vocast run handles SIGTERM by draining the poller and letting the in-flight
# episode finish, so `docker stop` is a clean shutdown.
STOPSIGNAL SIGTERM

ENTRYPOINT ["vocast"]
CMD ["run"]
