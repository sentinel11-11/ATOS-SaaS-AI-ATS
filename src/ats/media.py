"""Files shared with local Asterisk; credentials are never passed through AGI."""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid5

from sqlalchemy import select

from ats.adapters import Rejected
from ats.config import settings
from ats.models import Attempt, Recording


def prepare_media(attempt, campaign, template, contact, config=None):
    config = config or settings()
    if not config.media_enabled:
        if template:
            raise Rejected("enable_media_before_dialing_template")
        return False
    directory = Path(config.media_dir)
    if not directory.is_dir():
        raise Rejected("media_directory_missing")
    data = {
        "name": contact.name,
        "mode": campaign.mode,
        "text": template.text if template else "Здравствуйте! Это тестовый информационный звонок.",
        "scenario": template.scenario if template else {},
    }
    raw = json.dumps(data, ensure_ascii=False).encode()
    if len(raw) > 128 * 1024:
        raise Rejected("media_template_too_large")
    fd, path = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o640)
            stream.write(raw)
        os.replace(path, directory / f"{attempt.id}.json")
    finally:
        Path(path).unlink(missing_ok=True)
    return True


def attach_recordings(factory):
    config = settings()
    if not config.media_enabled:
        return
    with factory() as db:
        calls = db.scalars(
            select(Attempt)
            .where(
                Attempt.tenant_id == config.telephony_tenant_id,
                Attempt.route == "multicom_ami",
                Attempt.status.in_(["completed", "failed"]),
            )
            .order_by(Attempt.created_at.desc())
            .limit(100)
        ).all()
    for call in calls:
        key = uuid5(call.id, "asterisk-recording").hex
        with factory() as db:
            if db.scalar(select(Recording.id).where(Recording.storage_key == key)):
                continue
        source = Path(config.asterisk_recordings_dir) / f"{call.id}.wav"
        if not source.is_file() or source.is_symlink():
            continue
        stat = source.stat()
        if stat.st_size > 10 * 1024 * 1024 or time.time() - stat.st_mtime < 10:
            continue
        with source.open("rb") as stream:
            header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            continue
        directory = Path(config.recordings_dir) / str(call.tenant_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / key
        with target.open("wb") as output, source.open("rb") as input_stream:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(input_stream, output)
        with factory.begin() as db:
            if not db.scalar(select(Recording.id).where(Recording.storage_key == key)):
                db.add(
                    Recording(
                        tenant_id=call.tenant_id,
                        attempt_id=call.id,
                        storage_key=key,
                        content_type="audio/wav",
                        size_bytes=target.stat().st_size,
                    )
                )
