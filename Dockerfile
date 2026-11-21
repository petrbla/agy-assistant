FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    AGY_CLI_DISABLE_AUTO_UPDATE=true \
    TZ=Europe/Prague \
    HOME=/home/asistent

# Systémové závislosti pro Antigravity CLI, MCP (Node/npx), Git a zpracování PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    gosu \
    nodejs \
    npm \
    poppler-utils \
    sudo \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Vytvoření non-root uživatele `asistent` s domovským adresářem /home/asistent
RUN groupadd --gid 1000 asistent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/asistent --shell /bin/bash asistent

WORKDIR /app

# Python závislosti
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Instalace Google Antigravity CLI (agy) mimo /home/asistent, aby jej nepřekryl volume mount
RUN mkdir -p /opt/agy-install \
    && HOME=/opt/agy-install bash -c 'curl -fsSL https://antigravity.google/cli/install.sh | bash' \
    && AGY_FOUND="$(find /opt/agy-install /usr/local/bin -name agy \( -type f -o -type l \) -executable 2>/dev/null | head -n 1)" \
    && echo "Nalezeno agy: $AGY_FOUND" \
    && test -n "$AGY_FOUND" \
    && if [ "$AGY_FOUND" != "/usr/local/bin/agy" ]; then ln -sf "$AGY_FOUND" /usr/local/bin/agy; fi \
    && chmod -R a+rX /opt/agy-install \
    && (agy --version || true)
ENV PATH="/usr/local/bin:/opt/agy-install/.local/bin:${PATH}"

# Bezpečný root wrapper pro aktualizaci agy CLI z Web UI
COPY docker/agy-cli-update /usr/local/sbin/agy-cli-update
RUN sed -i 's/\r$//' /usr/local/sbin/agy-cli-update \
    && chmod 0755 /usr/local/sbin/agy-cli-update \
    && printf '%s\n' \
        'asistent ALL=(root) NOPASSWD: /usr/local/sbin/agy-cli-update' \
        > /etc/sudoers.d/asistent \
    && chmod 0440 /etc/sudoers.d/asistent

# Kód aplikace a výchozí šablony workspace
COPY app /app/app
COPY workspace_template /app/workspace_template
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN sed -i 's/\r$//' /app/docker-entrypoint.sh \
    && chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /home/asistent/workspace /home/asistent/data /home/asistent/secrets \
    && chown -R asistent:asistent /app /home/asistent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
