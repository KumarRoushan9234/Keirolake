FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
# Install only the dependencies listed in pyproject.toml, so code changes don't bust this layer.
# uvicorn imports `app` from the working directory; the package itself needs no install.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt
COPY app ./app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/api/stats')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
