FROM python:3.12-alpine

RUN addgroup -S splash && adduser -S -G splash splash

WORKDIR /app
# /data/logs is the trace log mount point; the unquoted chown is deliberate so the
# directory exists (with the right owner) even when the host bind-mount shadows it.
RUN mkdir -p /data/logs && chown -R splash:splash /data
COPY --chown=splash:splash splash_obs_proxy.py /app/splash_obs_proxy.py

USER splash

ENV PYTHONUNBUFFERED=1
EXPOSE 8100 8101

ENTRYPOINT ["python", "/app/splash_obs_proxy.py"]
