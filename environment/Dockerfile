FROM python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux asciinema \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN mkdir -p /data/output /data/reports /logs/verifier && chmod 0755 /logs/verifier
COPY generate_data.py /tmp/generate_data.py
COPY wheels/ /opt/wheels/
COPY app/ /app/
RUN python3 /tmp/generate_data.py \
    && rm /tmp/generate_data.py \
    && chown -R nobody:nogroup /data \
    && chmod 0777 /data /data/output /data/reports \
    && chmod 0555 /app/air_settlement.py
CMD ["sleep", "infinity"]
