# KeiroLake headless server in one container: the engine, the web app, the OpenAI-compatible API, MCP and the
# private internet link. Same engine as the desktop app.
#
#   docker build -t keirolake .
#   docker run -d --name keirolake -p 8443:8443 -v keirolake:/data -e KEIROLAKE_PASSWORD='a long password' keirolake
#
# Then open https://<this machine>:8443 (a certificate made in the container; your browser asks once).
# KEIROLAKE_SHARE=internet adds a private link: docker exec keirolake python -m app.cli link

FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund --loglevel=error
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KEIROLAKE_KEYRING=0 KEIROLAKE_SHARE=wifi \
    OLLAMA_BASE_URL=http://host.docker.internal:11434
WORKDIR /app
COPY backend/pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist /frontend/dist
RUN useradd --create-home --uid 10001 lake && mkdir -p /data && chown lake /data
ENV DATA_DIR=/data
USER lake
VOLUME /data
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import ssl,urllib.request as u; u.urlopen('https://127.0.0.1:8443/', context=ssl._create_unverified_context()) if __import__('os').environ.get('KEIROLAKE_SHARE','wifi')!='local' else u.urlopen('http://127.0.0.1:8443/')"
CMD ["sh", "-c", "exec python -m app.cli serve --share \"$KEIROLAKE_SHARE\" --port 8443 --data /data"]
