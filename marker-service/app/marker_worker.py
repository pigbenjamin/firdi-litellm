"""Persistent marker-pdf worker.

Loads marker's model dict once at process start and serves conversions from a
single background thread via a FIFO queue. This intentionally replaces the
"subprocess spawns marker_single per request" approach: the models stay
resident in GPU memory (no reload per request) and, because there is exactly
one worker, concurrent PDF conversions are impossible by construction — this
is the mechanism that keeps marker from competing for GPU memory/compute with
the embedding server sharing this pod.

Trade-off: a single persistent worker can't be force-killed mid-job the way a
subprocess can (subprocess.run(timeout=...) reliably reaps a stuck process; a
stuck in-process call cannot be cancelled from Python). The watchdog thread
below is the safety net for that — if a job runs past MARKER_TIMEOUT plus a
grace period, it kills the process outright so Kubernetes restarts the pod,
rather than leaving the queue permanently stuck behind a hung job.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("marker.worker")

MARKER_TIMEOUT = int(os.getenv("MARKER_TIMEOUT", "1800"))
WATCHDOG_GRACE_SECONDS = int(os.getenv("MARKER_WATCHDOG_GRACE_SECONDS", "60"))
WATCHDOG_POLL_SECONDS = 5


@dataclass
class ConvertJob:
    job_id: str
    doc_id: str
    pdf_path: str
    out_dir: str
    result: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=1))


class MarkerWorker:
    def __init__(self) -> None:
        self._queue: "queue.Queue[ConvertJob]" = queue.Queue()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._current_job: Optional[ConvertJob] = None
        self._current_job_started_at: Optional[float] = None
        self._converter = None
        self._worker_thread = threading.Thread(
            target=self._run_worker, name="marker-worker", daemon=True
        )
        self._watchdog_thread = threading.Thread(
            target=self._run_watchdog, name="marker-watchdog", daemon=True
        )

    def start(self) -> None:
        self._worker_thread.start()
        self._watchdog_thread.start()

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def current_job_elapsed(self) -> Optional[float]:
        with self._lock:
            if self._current_job_started_at is None:
                return None
            return time.monotonic() - self._current_job_started_at

    def is_stuck(self) -> bool:
        elapsed = self.current_job_elapsed()
        return elapsed is not None and elapsed > MARKER_TIMEOUT

    def submit(self, job: ConvertJob, timeout: float) -> str:
        self._queue.put(job)
        try:
            outcome = job.result.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"marker job {job.job_id} timed out after {timeout}s "
                f"(queue_depth={self.queue_depth()})"
            )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    # ── worker thread: loads models once, processes jobs one at a time ──────

    def _run_worker(self) -> None:
        logger.info("Loading marker models (this happens once)...")
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        t0 = time.monotonic()
        artifact_dict = create_model_dict()
        self._converter = PdfConverter(artifact_dict=artifact_dict)
        logger.info("Marker models loaded in %.1fs, worker ready", time.monotonic() - t0)
        self._ready.set()

        while True:
            job = self._queue.get()
            with self._lock:
                self._current_job = job
                self._current_job_started_at = time.monotonic()
            try:
                md_path = self._process(job)
                job.result.put(md_path)
            except Exception as e:  # noqa: BLE001 - report to caller, don't crash worker
                logger.error(
                    "marker job %s (doc_id=%s) failed: %s\n%s",
                    job.job_id, job.doc_id, e, traceback.format_exc(),
                )
                job.result.put(e)
            finally:
                with self._lock:
                    self._current_job = None
                    self._current_job_started_at = None

    def _process(self, job: ConvertJob) -> str:
        from marker.output import text_from_rendered

        pdf_path = Path(job.pdf_path).resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(f"pdf_path does not exist: {pdf_path}")

        doc_dir = Path(job.out_dir).resolve() / job.doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)

        logger.info("marker job %s (doc_id=%s) start: %s", job.job_id, job.doc_id, pdf_path)
        rendered = self._converter(str(pdf_path))
        text, _, images = text_from_rendered(rendered)

        md_path = doc_dir / "raw.md"
        md_path.write_text(text, encoding="utf-8")
        for name, image in (images or {}).items():
            image.save(doc_dir / name)

        logger.info("marker job %s (doc_id=%s) done: %s", job.job_id, job.doc_id, md_path)
        return str(md_path)

    # ── watchdog thread: hard-kill the process if a job hangs past timeout ──

    def _run_watchdog(self) -> None:
        while True:
            time.sleep(WATCHDOG_POLL_SECONDS)
            elapsed = self.current_job_elapsed()
            if elapsed is None:
                continue
            if elapsed > MARKER_TIMEOUT + WATCHDOG_GRACE_SECONDS:
                with self._lock:
                    job = self._current_job
                logger.critical(
                    "marker job %s stuck for %.0fs (limit=%ss+%ss grace) - "
                    "killing process so Kubernetes restarts the pod",
                    getattr(job, "job_id", "?"), elapsed, MARKER_TIMEOUT, WATCHDOG_GRACE_SECONDS,
                )
                os._exit(1)  # noqa: SLF001 - deliberate hard kill, see module docstring
