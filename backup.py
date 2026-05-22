"""
LN Operator — Channel Backup

Pushes LND's channel.backup to a remote host over SSH and records the
result in ln_operator.db (backup_log table). The dashboard reads that
table to show freshness and surface errors.

Destination, port, user, and source path come from .env — see
BACKUP_* keys in config.py. With BACKUP_SSH_HOST unset the script
records a configuration error rather than attempting an upload.

Trigger is one of:
- 'path'   → systemd .path unit fired (LND rewrote the file)
- 'timer'  → systemd .timer heartbeat
- 'manual' → operator ran `python main.py backup` by hand
"""

import os
import subprocess
import time

import db
import config


RSYNC_TIMEOUT_SEC = 30


def _destination():
    return f"{config.BACKUP_SSH_USER}@{config.BACKUP_SSH_HOST}:{config.BACKUP_DEST_DIR}"


def run_backup(trigger: str = "manual") -> bool:
    """Push channel.backup to the remote host. Returns True on success."""
    started = time.monotonic()

    if not (config.BACKUP_SSH_HOST and config.BACKUP_SSH_USER and config.BACKUP_DEST_DIR):
        db.save_backup_attempt(
            success=False, file_mtime=None, file_bytes=None,
            destination="", duration_ms=0, trigger=trigger,
            error="backup not configured — set BACKUP_SSH_HOST/USER/DEST_DIR in .env",
        )
        print("ERROR: backup not configured — set BACKUP_SSH_HOST/USER/DEST_DIR in .env")
        return False

    try:
        st = os.stat(config.BACKUP_SOURCE_PATH)
    except FileNotFoundError:
        db.save_backup_attempt(
            success=False, file_mtime=None, file_bytes=None,
            destination=_destination(),
            duration_ms=0, trigger=trigger,
            error=f"source file not found: {config.BACKUP_SOURCE_PATH}",
        )
        print(f"ERROR: source file not found: {config.BACKUP_SOURCE_PATH}")
        return False

    file_mtime = int(st.st_mtime)
    file_bytes = st.st_size
    destination = _destination()

    # rsync -a preserves mtime so the remote file's mtime matches the
    # source mtime — useful for spot-checking freshness on the remote.
    cmd = [
        "rsync", "-a", "--timeout=20",
        "-e", f"ssh -p {config.BACKUP_SSH_PORT} -o BatchMode=yes -o ConnectTimeout=10 "
              f"-o StrictHostKeyChecking=accept-new",
        config.BACKUP_SOURCE_PATH,
        destination,
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=RSYNC_TIMEOUT_SEC,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        success = proc.returncode == 0
        error = None if success else (proc.stderr.strip() or proc.stdout.strip()
                                       or f"rsync exit {proc.returncode}")
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - started) * 1000)
        success = False
        error = f"rsync timed out after {RSYNC_TIMEOUT_SEC}s"
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - started) * 1000)
        success = False
        error = "rsync binary not found"

    db.save_backup_attempt(
        success=success, file_mtime=file_mtime, file_bytes=file_bytes,
        destination=destination, duration_ms=duration_ms,
        trigger=trigger, error=error,
    )

    if success:
        print(f"OK: uploaded {file_bytes}B to {destination} in {duration_ms}ms "
              f"(trigger={trigger})")
    else:
        print(f"FAIL: {error} (trigger={trigger}, {duration_ms}ms)")

    return success
