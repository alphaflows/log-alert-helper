import datetime
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
from typing import List, Optional

import requests

DEFAULT_CONTAINERS: List[str] = []
DEFAULT_HOSTNAME = socket.gethostname()
MONITOR_HOST = os.getenv("MONITOR_HOST", DEFAULT_HOSTNAME)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000/api/logs")
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "")

ERROR_PATTERN = os.getenv("ERROR_PATTERN", r"(?<![\"'])ERROR(?![A-Za-z])")
TRACEBACK_PATTERN = os.getenv("TRACEBACK_PATTERN", r"Traceback \(most recent call last\):")
LOG_MATCH_PATTERNS = os.getenv("LOG_MATCH_PATTERNS", "")

TRACEBACK_MAX_LINES = int(os.getenv("TRACEBACK_MAX_LINES", "400"))
QUEUE_MAX_SIZE = int(os.getenv("QUEUE_MAX_SIZE", "2000"))
MAX_SEND_RETRIES = int(os.getenv("MAX_SEND_RETRIES", "6"))
SEND_BASE_BACKOFF = float(os.getenv("SEND_BASE_BACKOFF", "1.5"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "2"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "5"))
CONTAINER_RESTART_DELAY = float(os.getenv("CONTAINER_RESTART_DELAY", "3"))

TRACEBACK_BRIDGE_PREFIXES = (
    "During handling of the above exception",
    "The above exception was the direct cause",
    "Caused by",
)

NEW_LOG_LINE_PATTERN = re.compile(
    r"^(\[|\d{4}-\d{2}-\d{2}[ T]|\d{2}:\d{2}:\d{2}|INFO\b|WARN(?:ING)?\b|ERROR\b|DEBUG\b|TRACE\b|FATAL\b|CRITICAL\b)"
)

LOG_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAX_SIZE)
STOP_EVENT = threading.Event()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)


def _env_list(key: str, fallback: List[str]) -> List[str]:
    raw = os.getenv(key)
    if not raw:
        return fallback
    containers = [item.strip() for item in raw.split(",")]
    return [c for c in containers if c]


def _compile_matchers() -> List[re.Pattern[str]]:
    patterns: List[str] = []
    if LOG_MATCH_PATTERNS:
        patterns = [part.strip() for part in LOG_MATCH_PATTERNS.split(",") if part.strip()]
    if not patterns:
        patterns = [ERROR_PATTERN]
    return [re.compile(pattern) for pattern in patterns]


CONTAINERS = _env_list("MONITOR_CONTAINERS", DEFAULT_CONTAINERS)
MATCH_REGEXES = _compile_matchers()
TRACEBACK_REGEX = re.compile(TRACEBACK_PATTERN)


def is_match_line(line: str) -> bool:
    return any(regex.search(line) for regex in MATCH_REGEXES)


def is_traceback_start(line: str) -> bool:
    return bool(TRACEBACK_REGEX.search(line))


def is_traceback_bridge(line: str) -> bool:
    normalized = line.strip()
    return any(normalized.startswith(prefix) for prefix in TRACEBACK_BRIDGE_PREFIXES)


def looks_like_new_log_line(line: str) -> bool:
    candidate = line.lstrip()
    return bool(NEW_LOG_LINE_PATTERN.match(candidate))


def should_extend_traceback(line: str) -> bool:
    if line == "" or not line.strip():
        return True
    if is_traceback_bridge(line):
        return True
    if line.startswith(" ") or line.startswith("\t"):
        return True
    if TRACEBACK_REGEX.search(line):
        return True
    return not looks_like_new_log_line(line)


def _enqueue_log(container: str, line: str, severity: str) -> None:
    payload = {
        "host": MONITOR_HOST,
        "container": container,
        "severity": severity,
        "message": line,
        "occurred_at": datetime.datetime.utcnow().isoformat(),
    }
    try:
        LOG_QUEUE.put(payload, timeout=1)
    except queue.Full:
        logging.warning("Dropping log; queue full (size=%s)", LOG_QUEUE.qsize())


def _send_payload(payload: dict) -> bool:
    headers = {}
    if BACKEND_API_KEY:
        headers["X-API-Key"] = BACKEND_API_KEY

    attempt = 1
    backoff = SEND_BASE_BACKOFF
    while attempt <= MAX_SEND_RETRIES and not STOP_EVENT.is_set():
        try:
            response = requests.post(
                BACKEND_URL,
                json=payload,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            if response.ok:
                return True
            logging.warning(
                "Backend returned HTTP %s: %s", response.status_code, response.text
            )
        except requests.RequestException as exc:
            logging.warning("Send attempt %s failed: %s", attempt, exc)

        attempt += 1
        time.sleep(backoff)
        backoff *= 2

    logging.error("Failed to send log payload after retries.")
    return False


def _sender_worker() -> None:
    logging.info("Sender worker started.")
    while not STOP_EVENT.is_set():
        try:
            payload = LOG_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue

        if payload is None:
            LOG_QUEUE.task_done()
            break

        _send_payload(payload)
        LOG_QUEUE.task_done()

    logging.info("Sender worker exiting.")


def follow_container(container: str) -> None:
    logging.info("Monitoring container %s", container)
    while not STOP_EVENT.is_set():
        cmd = ["docker", "logs", "-f", "--tail", "0", container]
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            logging.error("docker CLI not found inside container.")
            return
        except Exception as exc:  # noqa: BLE001
            logging.error("Unable to start docker logs for %s: %s", container, exc)
            time.sleep(CONTAINER_RESTART_DELAY)
            continue

        assert process.stdout is not None
        traceback_buffer: List[str] = []

        def flush_traceback_buffer() -> None:
            nonlocal traceback_buffer
            if not traceback_buffer:
                return
            message = "\n".join(traceback_buffer)
            _enqueue_log(container, message, severity="fatal")
            traceback_buffer = []

        for raw_line in process.stdout:
            if STOP_EVENT.is_set():
                break

            line = raw_line.rstrip("\r\n")

            if traceback_buffer:
                if should_extend_traceback(line):
                    traceback_buffer.append(line)
                    if len(traceback_buffer) >= TRACEBACK_MAX_LINES:
                        logging.debug(
                            "Traceback buffer reached %s lines; flushing for %s",
                            TRACEBACK_MAX_LINES,
                            container,
                        )
                        flush_traceback_buffer()
                    continue
                flush_traceback_buffer()

            if not line:
                continue

            if is_traceback_start(line):
                traceback_buffer = [line]
                continue

            if is_match_line(line):
                _enqueue_log(container, line, severity="error")

        exit_code = process.wait()

        if traceback_buffer:
            flush_traceback_buffer()

        if STOP_EVENT.is_set():
            break

        if exit_code != 0:
            logging.warning(
                "docker logs exited for %s (code=%s); retrying after %.1fs",
                container,
                exit_code,
                CONTAINER_RESTART_DELAY,
            )
        time.sleep(CONTAINER_RESTART_DELAY)


def main() -> None:
    logging.info(
        "Starting log detector agent %s, backend=%s, patterns=%s",
        MONITOR_HOST,
        BACKEND_URL,
        [regex.pattern for regex in MATCH_REGEXES],
    )

    if not CONTAINERS:
        logging.error("No containers configured. Set MONITOR_CONTAINERS env var.")
        return

    sender_thread = threading.Thread(target=_sender_worker, daemon=True)
    sender_thread.start()

    threads = []
    for container in CONTAINERS:
        t = threading.Thread(target=follow_container, args=(container,), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutdown requested.")
    finally:
        STOP_EVENT.set()
        LOG_QUEUE.put(None)
        for t in threads:
            t.join(timeout=1)
        sender_thread.join(timeout=5)
        logging.info("Monitor stopped.")


if __name__ == "__main__":
    main()
