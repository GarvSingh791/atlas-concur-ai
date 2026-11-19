"""Smoke tests for services/storage_provider."""

import tempfile
import time

import pytest


class TestLocalStorageProvider:
    """Hermetic tests — filesystem-backed, no MinIO required."""

    def setup_method(self) -> None:
        import os

        self._tmp = tempfile.mkdtemp(prefix="atlas-test-storage-")
        os.environ["STORAGE_LOCAL_PATH"] = self._tmp
        import config.settings as cs

        cs.get_settings.cache_clear()

    def teardown_method(self) -> None:
        import os

        os.environ.pop("STORAGE_LOCAL_PATH", None)
        import config.settings as cs

        cs.get_settings.cache_clear()

    def test_put_get_delete_round_trip(self) -> None:
        from services.storage_provider import get_storage_provider

        p = get_storage_provider()
        key = "test/smoke/hello.txt"
        data = b"hello world"
        p.put_object(key, data, content_type="text/plain")
        assert p.get_object(key) == data
        p.delete_object(key)
        from services.exceptions import ProviderError

        with pytest.raises(ProviderError):
            p.get_object(key)

    def test_presigned_url_is_time_bounded(self) -> None:
        """Presigned URL must include an expiry timestamp in the future."""
        import urllib.parse

        from services.storage_provider import get_storage_provider

        p = get_storage_provider()
        key = "test/smoke/presign.bin"
        p.put_object(key, b"data")
        pr = p.generate_presigned_url(key, expires_in=60)
        assert pr.expires_in == 60
        assert "expires=" in pr.url
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(pr.url).query)
        ts = int(qs["expires"][0])
        assert ts > int(time.time()), "Presigned URL expiry is in the past"
        p.delete_object(key)

    def test_path_traversal_rejected(self) -> None:
        from services.exceptions import ProviderError
        from services.storage_provider import get_storage_provider

        p = get_storage_provider()
        with pytest.raises(ProviderError):
            p.put_object("../../etc/passwd", b"bad")


@pytest.mark.storage
class TestRealStorageProviderLive:
    """Live MinIO round-trip tests — self-skip when MinIO is unreachable."""

    @pytest.fixture(autouse=True)
    def require_minio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Skip this test class if MinIO is not reachable."""
        import os

        monkeypatch.setenv("STORAGE_PROVIDER", "minio")
        monkeypatch.setenv(
            "STORAGE_SECRET_ACCESS_KEY", os.environ.get("STORAGE_SECRET_ACCESS_KEY", "minioadmin")
        )
        monkeypatch.setenv(
            "STORAGE_ACCESS_KEY_ID", os.environ.get("STORAGE_ACCESS_KEY_ID", "minioadmin")
        )
        import config.settings as cs

        cs.get_settings.cache_clear()

        try:
            import boto3
            from botocore.config import Config

            import config.settings

            s = config.settings.get_settings()
            client = boto3.client(
                "s3",
                endpoint_url=s.STORAGE_ENDPOINT_URL,
                aws_access_key_id=s.STORAGE_ACCESS_KEY_ID,
                aws_secret_access_key=s.STORAGE_SECRET_ACCESS_KEY,
                region_name=s.STORAGE_REGION,
                config=Config(s3={"addressing_style": "path"}),
            )
            client.list_buckets()
        except Exception:
            pytest.skip("MinIO not reachable — skipping live storage round-trip tests")

    def test_real_put_presign_get_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full round-trip against real MinIO."""
        import boto3
        from botocore.config import Config

        import config.settings as cs

        s = cs.get_settings()

        from services.storage_provider import get_storage_provider

        p = get_storage_provider()

        # Ensure bucket exists
        try:
            boto3.client(
                "s3",
                endpoint_url=s.STORAGE_ENDPOINT_URL,
                aws_access_key_id=s.STORAGE_ACCESS_KEY_ID,
                aws_secret_access_key=s.STORAGE_SECRET_ACCESS_KEY,
                region_name=s.STORAGE_REGION,
                config=Config(s3={"addressing_style": "path"}),
            ).create_bucket(Bucket=s.STORAGE_BUCKET)
        except Exception:
            pass  # bucket likely already exists

        key = "test/smoke/live-round-trip.bin"
        data = b"live-test-data"

        p.put_object(key, data)
        assert p.get_object(key) == data

        pr = p.generate_presigned_url(key, expires_in=30)
        assert "?" in pr.url  # has query params (expiry)
        assert pr.expires_in == 30

        p.delete_object(key)


class TestRealStorageProviderClientConfig:
    """Hermetic — boto3/botocore are stubbed; asserts the R2-compat client Config."""

    def test_client_config_disables_default_checksums(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        captured: dict = {}

        class FakeConfig:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda *a, **k: object()  # type: ignore[attr-defined]
        fake_botocore = types.ModuleType("botocore")
        fake_config = types.ModuleType("botocore.config")
        fake_config.Config = FakeConfig  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
        monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
        monkeypatch.setitem(sys.modules, "botocore.config", fake_config)
        monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "x")
        import config.settings as cs

        cs.get_settings.cache_clear()
        try:
            from services.storage_provider.real import RealStorageProvider

            RealStorageProvider()._get_client()
        finally:
            cs.get_settings.cache_clear()

        assert captured["request_checksum_calculation"] == "when_required"
        assert captured["response_checksum_validation"] == "when_required"
        assert captured["signature_version"] == "s3v4"
