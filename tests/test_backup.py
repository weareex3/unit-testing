"""_backup_learned_data must upload the learned-data files to S3 under the
client's backup prefix, and stay silent/fail-soft when S3 isn't configured."""

import sys
import types

import ui.server as server


class _FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, filename, bucket, key):
        self.uploads.append((filename, bucket, key))


def _install_fake_boto3(monkeypatch, fake_s3):
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *a, **k: fake_s3
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)


def test_backup_uploads_existing_files(monkeypatch, tmp_path):
    lib = tmp_path / "step_library.json"
    lib.write_text("{}")
    fb = tmp_path / "step_feedback.json"
    fb.write_text("{}")
    monkeypatch.setattr(server, "LIBRARY_FILE", lib)
    monkeypatch.setattr(server, "FEEDBACK_FILE", fb)
    monkeypatch.setattr(server, "APPROVED_FILE", tmp_path / "missing.json")
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    fake = _FakeS3()
    _install_fake_boto3(monkeypatch, fake)

    server._backup_learned_data()

    assert len(fake.uploads) == 2
    for filename, bucket, key in fake.uploads:
        assert bucket == "test-bucket"
        assert key.startswith(f"backups/{server.CLIENT_ID}/")
    assert {k.rsplit("/", 1)[1] for _, _, k in fake.uploads} == {
        "step_library.json", "step_feedback.json",
    }


def test_backup_skips_without_bucket(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    fake = _FakeS3()
    _install_fake_boto3(monkeypatch, fake)

    server._backup_learned_data()

    assert fake.uploads == []


def test_backup_is_fail_soft(monkeypatch, tmp_path):
    lib = tmp_path / "step_library.json"
    lib.write_text("{}")
    monkeypatch.setattr(server, "LIBRARY_FILE", lib)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")

    class _Boom:
        def upload_file(self, *a):
            raise RuntimeError("s3 down")

    _install_fake_boto3(monkeypatch, _Boom())

    server._backup_learned_data()
