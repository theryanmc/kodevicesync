FROM python:3.12-slim

# Which release of the KOReader plugin to bundle (served at /plugin.zip).
# "latest" or a tag such as "v1.0.0".
ARG PLUGIN_REPO=theryanmc/kodevicesync.koplugin
ARG PLUGIN_VERSION=latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KDS_DATA_DIR=/data \
    KDS_PLUGIN_DIR=/app/plugin/kodevicesync.koplugin \
    PUID=99 \
    PGID=100

WORKDIR /app
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN python -c "import io,os,urllib.request,zipfile; \
repo,ver=os.environ['PLUGIN_REPO'],os.environ['PLUGIN_VERSION']; \
url=f'https://github.com/{repo}/releases/'+('latest/download' if ver=='latest' else 'download/'+ver)+'/kodevicesync.koplugin.zip'; \
print('Bundling plugin from',url); \
zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url,timeout=60).read())).extractall('/app/plugin'); \
assert os.path.isfile('/app/plugin/kodevicesync.koplugin/main.lua'),'plugin zip has unexpected layout'"

COPY server/app /app/app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME /data
EXPOSE 1379
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1379/healthz')"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "1379", "--proxy-headers", "--forwarded-allow-ips", "*"]
