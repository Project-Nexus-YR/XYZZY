FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src

# Non-editable: the web client is package data under src/multiplayer/web (see
# pyproject's package-data), and the server resolves it with
# importlib.resources, which works the same whether the package is an
# editable checkout or, as here, installed into site-packages.
RUN pip install --no-cache-dir -c constraints.txt . \
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

# Reads XYZZY_PORT itself (default 8000) so the check still hits the right
# port when a deployment overrides it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('XYZZY_PORT', '8000') + '/api/v1/health')"

CMD ["python", "-m", "multiplayer.server", "/data/multiplayer.db"]
