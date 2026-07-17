#!/bin/sh
# f1coach container entrypoint.
# Behaviour controlled by PROCESS_MODE env var:
#   both      (default) run writer + processor in parallel; exit if either dies
#   writer    run only the UDP writer (use in multi-container setups)
#   processor run only the processor watch loop

set -e

LAPS_DIR="${LAPS_DIR:-/data/laps}"
UDP_PORT="${UDP_PORT:-20777}"
PROCESS_INTERVAL="${PROCESS_INTERVAL:-60}"

mkdir -p "$LAPS_DIR"

echo "[f1coach] mode=${PROCESS_MODE} laps_dir=${LAPS_DIR} port=${UDP_PORT}"

case "$PROCESS_MODE" in

  writer)
    echo "[f1coach] starting writer on UDP :${UDP_PORT}"
    exec python -m f1coach --port "$UDP_PORT" write --laps-dir "$LAPS_DIR"
    ;;

  processor)
    echo "[f1coach] starting processor (watch every ${PROCESS_INTERVAL}s)"
    exec python -m f1coach process --laps-dir "$LAPS_DIR" --watch "$PROCESS_INTERVAL"
    ;;

  both|*)
    echo "[f1coach] starting writer + processor"
    python -m f1coach --port "$UDP_PORT" write --laps-dir "$LAPS_DIR" &
    WRITER_PID=$!

    python -m f1coach process --laps-dir "$LAPS_DIR" --watch "$PROCESS_INTERVAL" &
    PROCESSOR_PID=$!

    # Exit the container if either process dies (POSIX-compatible poll loop)
    while kill -0 "$WRITER_PID" 2>/dev/null && kill -0 "$PROCESSOR_PID" 2>/dev/null; do
        sleep 5
    done
    echo "[f1coach] a child process exited — stopping container"
    kill "$WRITER_PID" "$PROCESSOR_PID" 2>/dev/null || true
    wait
    ;;
esac
