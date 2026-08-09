"""Generation-precondition tests for the harvester's shared GCS database."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from google.api_core.exceptions import PreconditionFailed

from ontimeai_scrapper import db, harvester


class FakeBlob:
    def __init__(self, generation: int = 20) -> None:
        self.generation = str(generation)
        self.download_precondition: int | None = None
        self.upload_precondition: int | None = None
        self.fail_upload = False

    def reload(self) -> None:
        return None

    def download_to_filename(self, filename: str, *, if_generation_match: int) -> None:
        self.download_precondition = if_generation_match
        Path(filename).write_bytes(b"immutable generation snapshot")

    def upload_from_filename(self, filename: str, *, if_generation_match: int) -> None:
        self.upload_precondition = if_generation_match
        if self.fail_upload:
            raise PreconditionFailed("simulated concurrent writer")
        assert Path(filename).exists()
        self.generation = str(if_generation_match + 1)


def test_snapshot_download_returns_and_pins_generation(tmp_path: Path, monkeypatch) -> None:
    blob = FakeBlob(generation=25)
    target = tmp_path / "live.db"
    monkeypatch.setattr(db, "_gcs_blob", lambda: blob)

    local, generation = db.download_db_snapshot_from_gcs(target)

    assert local == target
    assert generation == 25
    assert blob.download_precondition == 25


def test_guarded_upload_uses_expected_generation(tmp_path: Path, monkeypatch) -> None:
    blob = FakeBlob(generation=25)
    target = tmp_path / "live.db"
    target.write_bytes(b"local mutation")
    monkeypatch.setattr(db, "_gcs_blob", lambda: blob)

    uploaded_generation = db.upload_db_to_gcs(target, expected_generation=25)

    assert blob.upload_precondition == 25
    assert uploaded_generation == 26


def test_guarded_upload_maps_precondition_failure_to_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    blob = FakeBlob(generation=25)
    blob.fail_upload = True
    target = tmp_path / "live.db"
    target.write_bytes(b"stale local mutation")
    monkeypatch.setattr(db, "_gcs_blob", lambda: blob)

    with pytest.raises(db.GCSGenerationConflict):
        db.upload_db_to_gcs(target, expected_generation=25)

    assert blob.upload_precondition == 25


def test_harvester_retries_whole_run_after_conflict(monkeypatch) -> None:
    args = argparse.Namespace()
    calls: list[int] = []
    monkeypatch.setenv("GCS_GENERATION_RETRIES", "1")
    monkeypatch.setattr(harvester, "parse_args", lambda: args)

    def run_once(_args: argparse.Namespace) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise db.GCSGenerationConflict("simulated winner")
        return 0

    monkeypatch.setattr(harvester, "_run_once", run_once)

    assert harvester.main() == 0
    assert len(calls) == 2
