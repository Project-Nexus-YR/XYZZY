FROM python:3.11-slim@sha256:d1e9ca7c4e78d1e8ecadb5d44bfc8e956e7a65b659a9950f569f243d72b326d0

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
COPY web ./web

# Installed editable on purpose: the server resolves web/index.html relative to
# the repository layout, so a copied-into-site-packages install serves the API
# and 404s the UI.
RUN pip install --no-cache-dir -c constraints.txt -e . \
    && useradd --system --uid 10001 xyzzy \
    && mkdir /data && chown xyzzy /data
USER xyzzy

# The database is a file. Mount a volume here or the room history dies with the
# container.
VOLUME ["/data"]

# Binding every interface is correct inside a container and wrong on a laptop,
# which is why the default in the code is loopback and the override lives here.
ENV XYZZY_HOST=0.0.0.0
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"

CMD ["python", "-m", "multiplayer.server", "/data/multiplayer.db"]
