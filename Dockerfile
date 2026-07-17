FROM python:3.12-slim

LABEL org.opencontainers.image.title="f1coach"
LABEL org.opencontainers.image.description="F1 25 AI telemetry coach — writer + processor"
LABEL org.opencontainers.image.source="https://github.com/joelllach/f1-25-telemetry-coach"
LABEL org.opencontainers.image.licenses="MIT"

# ── System deps ────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ────────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
COPY f1coach/ ./f1coach/
COPY setups/   ./setups/
COPY tools/    ./tools/

# ── Lap data volume ────────────────────────────────────────────────────────────
# Mount a volume here so .lap files + index.jsonl survive container restarts.
RUN mkdir -p /data/laps
VOLUME ["/data/laps"]

# ── Environment defaults ───────────────────────────────────────────────────────
# LAPS_DIR     : where raw.jsonl and .lap files are written
# UDP_PORT     : PS5 telemetry port (default 20777)
# PROCESS_MODE : "writer" | "processor" | "both"
#                "both" runs writer + processor in the same container via tini
#                "writer" / "processor" run one process (use in multi-container pods)
# PROCESS_INTERVAL : seconds between processor runs when PROCESS_MODE=both (default 60)
# ANTHROPIC_API_KEY / AWS_PROFILE / AWS_REGION etc: set at runtime
ENV LAPS_DIR=/data/laps \
    UDP_PORT=20777 \
    PROCESS_MODE=both \
    PROCESS_INTERVAL=60

# ── Supervisor script ──────────────────────────────────────────────────────────
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# UDP port for PS5 telemetry
EXPOSE 20777/udp

# tini as PID 1 so signals propagate cleanly to child processes
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
