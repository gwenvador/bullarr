"""Killable subprocess boundary for slow import filesystem work.

The Flask process never performs network reads, archive validation, conversion, or
large transfers directly. A worker can be SIGKILLed at its wall-clock deadline,
which a Python thread cannot guarantee.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


class ImportWorkerError(RuntimeError):
    pass


class ImportWorkerTimeout(ImportWorkerError, TimeoutError):
    pass


def run_killable_command(command, timeout):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Never wait indefinitely for a task stuck in uninterruptible NFS I/O.
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise ImportWorkerTimeout(
            f"Import worker exceeded {timeout}s and was terminated"
        )
    if process.returncode:
        raise ImportWorkerError(
            (stderr or stdout or f"worker exited {process.returncode}").strip()
        )
    return stdout


def _atomic_json_write(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _call_worker(action, payload, staging_dir, timeout):
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload_path = staging / f".{token}.payload.json"
    result_path = staging / f".{token}.result.json"
    payload = dict(payload, result_path=str(result_path))
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        run_killable_command(
            [sys.executable, "-m", "blueprints.library.import_worker", action, str(payload_path)],
            timeout=timeout,
        )
        if not result_path.exists():
            raise ImportWorkerError("worker produced no result")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("error"):
            raise ImportWorkerError(result["error"])
        return result
    finally:
        for path in (payload_path, result_path):
            try:
                path.unlink()
            except OSError:
                pass


def prepare_import_file(source_path, fmt, auto_convert, staging_dir, timeout=300):
    return _call_worker(
        "prepare",
        {
            "source_path": source_path,
            "format": (fmt or "").lower(),
            "auto_convert": bool(auto_convert),
            "staging_dir": staging_dir,
            "conversion_lock": str(Path(staging_dir) / ".conversion.lock"),
        },
        staging_dir,
        timeout,
    )


def transfer_import_file(
    staged_path, target_path, staging_dir, timeout=300, link_source_path=None
):
    return _call_worker(
        "transfer",
        {
            "staged_path": staged_path,
            "target_path": target_path,
            "link_source_path": link_source_path,
        },
        staging_dir,
        timeout,
    )


def cleanup_stale_staging(staging_dir, max_age_seconds=86400):
    root = Path(staging_dir)
    if not root.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in root.iterdir():
        if path.name == ".conversion.lock":
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _convert_if_requested(path, fmt, enabled, lock_path):
    if not enabled:
        return path, "", fmt
    converters = {}
    if fmt == "pdf":
        from blueprints.bedetheque.pdf_converter import convert_pdf_to_cbz
        converters[fmt] = convert_pdf_to_cbz
    elif fmt in ("cbr", "rar"):
        from blueprints.bedetheque.cbr_converter import convert_cbr_to_cbz
        converters[fmt] = convert_cbr_to_cbz
    elif fmt == "zip":
        from blueprints.library.zip_converter import convert_zip_to_cbz
        converters[fmt] = convert_zip_to_cbz
    converter = converters.get(fmt)
    if not converter:
        return path, "", fmt
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        new_path = converter(path)
    return new_path, f"Converti {fmt.upper()} → CBZ", "cbz"


def _prepare_child(payload):
    source = Path(payload["source_path"])
    if not source.is_file():
        raise FileNotFoundError(str(source))
    root = Path(payload["staging_dir"])
    token = uuid.uuid4().hex
    partial_dir = root / f"{token}.partial"
    ready_dir = root / f"{token}.ready"
    partial_dir.mkdir(parents=True, exist_ok=False)
    copied = partial_dir / source.name
    shutil.copy2(source, copied)
    os.replace(partial_dir, ready_dir)
    staged = ready_dir / source.name

    fmt = payload.get("format") or source.suffix.lstrip(".").lower()
    message = ""
    try:
        staged, message, fmt = _convert_if_requested(
            str(staged), fmt, payload.get("auto_convert", False),
            payload["conversion_lock"],
        )
    except Exception as exc:
        # Preserve established behavior: failed optional conversion imports the
        # original format, while integrity validation still decides if it is safe.
        message = f"Conversion {fmt.upper()} → CBZ impossible: {exc}"
        staged = str(ready_dir / source.name)

    from blueprints.settings.routes import _check_volume_file_validity
    validation_error = _check_volume_file_validity(str(staged), fmt)
    return {
        "path": str(staged),
        "filename": Path(staged).name,
        "format": fmt,
        "conversion_message": message,
        "validation_error": validation_error,
    }


def _transfer_child(payload):
    staged = Path(payload["staged_path"])
    target = Path(payload["target_path"])
    link_source = payload.get("link_source_path")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.bullarr-partial-{uuid.uuid4().hex}")
    method = "copy"
    try:
        if link_source:
            try:
                os.link(link_source, partial)
                method = "hardlink"
            except OSError:
                # Hardlinks require the same filesystem and suitable permissions.
                # Fall back safely to a copy while preserving the original source.
                shutil.copy2(staged, partial)
                method = "copy-fallback"
        else:
            shutil.copy2(staged, partial)
        os.replace(partial, target)
        staged.unlink()
        try:
            staged.parent.rmdir()
        except OSError:
            pass
        return {"path": str(target), "method": method}
    finally:
        try:
            partial.unlink()
        except OSError:
            pass


def _main():
    action, payload_path = sys.argv[1:3]
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    try:
        if action == "prepare":
            result = _prepare_child(payload)
        elif action == "transfer":
            result = _transfer_child(payload)
        else:
            raise ValueError(f"unknown worker action: {action}")
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    _atomic_json_write(payload["result_path"], result)


if __name__ == "__main__":
    _main()
