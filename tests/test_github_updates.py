import hashlib
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_modules(data_root: Path):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for module_name in ("github_updates", "update_engine", "app_paths"):
        sys.modules.pop(module_name, None)
    return importlib.import_module("github_updates"), importlib.import_module("update_engine")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def release_manifest(version: str, files: dict[str, bytes]) -> dict:
    return {
        "schema_version": 1,
        "version": version,
        "files": [
            {
                "path": path,
                "sha256": sha256_bytes(payload),
                "size": len(payload),
            }
            for path, payload in files.items()
        ],
    }


def make_release_zip(
    version: str = "1.0.0",
    files: dict[str, bytes] | None = None,
    *,
    manifest: dict | None = None,
    entries: list[tuple[str, bytes, int | None]] | None = None,
) -> bytes:
    payload_files = files or {"DarkAbyss_Core/example.py": b"print('ok')\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if manifest is not None:
            archive.writestr("release.json", json.dumps(manifest).encode("utf-8"))
        elif manifest is not False:
            archive.writestr("release.json", json.dumps(release_manifest(version, payload_files)).encode("utf-8"))
        for path, payload in payload_files.items():
            archive.writestr(path, payload)
        for name, payload, mode in entries or []:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def github_release(version: str = "1.0.0", *, draft=False, prerelease=False, assets=None, tag=None):
    return {
        "tag_name": tag or f"v{version}",
        "name": f"Release {version}",
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets
        if assets is not None
        else [
            {
                "name": f"darkabyss-release-{version}.zip",
                "browser_download_url": f"https://github.com/example/project/releases/download/v{version}/darkabyss-release-{version}.zip",
                "size": 1,
            }
        ],
    }


class ChunkedResponse:
    def __init__(self, status_code=200, headers=None, chunks=None, error_after_chunks=False):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks if chunks is not None else [b""]
        self.error_after_chunks = error_after_chunks
        self.closed = False

    def iter_bytes(self, _chunk_size):
        for chunk in self.chunks:
            yield chunk
        if self.error_after_chunks:
            raise OSError("stream interrupted")

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def __call__(self, url, headers, timeout):
        self.requests.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GitHubUpdatesTests(unittest.TestCase):
    def with_modules(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        github_updates, update_engine = load_modules(root / "user-data")
        return github_updates, update_engine, root / "install-root", root / "user-data"

    def api_response(self, payload):
        return ChunkedResponse(headers={"Content-Type": "application/json"}, chunks=[json.dumps(payload).encode("utf-8")])

    def asset_response(self, payload: bytes, *, headers=None):
        return ChunkedResponse(headers=headers or {"Content-Length": str(len(payload))}, chunks=[payload])

    def test_latest_stable_release_parsed(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([self.api_response([github_release("1.2.3")])])

        release = github_updates.fetch_latest_release("owner", "repo", transport=transport)

        self.assertEqual(release.tag_name, "v1.2.3")
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.assets[0].name, "darkabyss-release-1.2.3.zip")

    def test_draft_release_rejected_for_latest(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([self.api_response([github_release("1.0.0", draft=True)])])

        with self.assertRaisesRegex(github_updates.ReleaseSelectionError, "No supported"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_prerelease_excluded_by_default(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([self.api_response([github_release("1.0.0-rc1", prerelease=True)])])

        with self.assertRaisesRegex(github_updates.ReleaseSelectionError, "No supported"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_prerelease_allowed_explicitly(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([self.api_response([github_release("1.0.0-rc1", prerelease=True)])])

        release = github_updates.fetch_latest_release("owner", "repo", allow_prerelease=True, transport=transport)

        self.assertEqual(release.version, "1.0.0-rc1")

    def test_exact_tag_lookup_works(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([self.api_response(github_release("2.0.0"))])

        release = github_updates.fetch_release_by_tag("owner", "repo", "v2.0.0", transport=transport)

        self.assertEqual(release.version, "2.0.0")
        self.assertIn("/releases/tags/v2.0.0", transport.requests[0][0])

    def test_malformed_api_json_rejected(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([ChunkedResponse(chunks=[b"{bad"])])

        with self.assertRaisesRegex(github_updates.GitHubAPIError, "Malformed"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_missing_required_asset_rejected(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        release = github_release("1.0.0", assets=[])
        transport = FakeTransport([self.api_response([release])])

        with self.assertRaisesRegex(github_updates.ReleaseSelectionError, "Missing"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_duplicate_matching_assets_rejected(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        asset = github_release("1.0.0")["assets"][0]
        transport = FakeTransport([self.api_response([github_release("1.0.0", assets=[asset, dict(asset)])])])

        with self.assertRaisesRegex(github_updates.ReleaseSelectionError, "Duplicate"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_http_asset_url_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        release = github_updates.GitHubReleaseInfo(
            tag_name="v1.0.0",
            version="1.0.0",
            name=None,
            draft=False,
            prerelease=False,
            assets=(
                github_updates.GitHubReleaseAsset(
                    name="darkabyss-release-1.0.0.zip",
                    download_url="http://github.com/example/project/releases/download/v1.0.0/darkabyss-release-1.0.0.zip",
                    size=1,
                ),
            ),
        )

        with self.assertRaisesRegex(github_updates.DownloadError, "HTTPS"):
            github_updates.download_release(release, install_root, transport=FakeTransport())

    def test_unrelated_redirect_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        release = self.release_info(github_updates, payload)
        transport = FakeTransport([ChunkedResponse(status_code=302, headers={"Location": "https://evil.example/file.zip"})])

        with self.assertRaisesRegex(github_updates.DownloadError, "not allowed"):
            github_updates.download_release(release, install_root, transport=transport)

    def test_authorization_not_forwarded_to_redirected_asset_host(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport(
            [
                ChunkedResponse(status_code=302, headers={"Location": "https://objects.githubusercontent.com/file.zip"}),
                ChunkedResponse(status_code=200, headers={}, chunks=[b"ok"]),
            ]
        )

        response = github_updates._request_with_redirects(
            "https://github.com/owner/repo/releases/download/v1/file.zip",
            headers={"Authorization": "Bearer SECRET", "User-Agent": "test"},
            timeout=1,
            transport=transport,
            purpose="download",
        )
        response.close()

        self.assertIn("Authorization", transport.requests[0][1])
        self.assertNotIn("Authorization", transport.requests[1][1])

    def test_network_failure_wrapped(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport(error=OSError("network down"))

        with self.assertRaisesRegex(github_updates.GitHubAPIError, "network down"):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_api_redirect_without_location_raises_api_error(self):
        github_updates, _update_engine, _install_root, _data_root = self.with_modules()
        transport = FakeTransport([ChunkedResponse(status_code=302, headers={})])

        with self.assertRaises(github_updates.GitHubAPIError):
            github_updates.fetch_latest_release("owner", "repo", transport=transport)

    def test_partial_download_cleaned(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        release = self.release_info(github_updates, payload)
        transport = FakeTransport(
            [
                ChunkedResponse(
                    headers={"Content-Length": str(len(payload))},
                    chunks=[payload[:5]],
                    error_after_chunks=True,
                )
            ]
        )

        with self.assertRaisesRegex(github_updates.DownloadError, "stream interrupted"):
            github_updates.download_release(release, install_root, transport=transport)

        downloads_dir = install_root / "updates" / "downloads"
        self.assertEqual(list(downloads_dir.iterdir()), [])

    def test_size_limit_enforced_from_header(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        release = self.release_info(github_updates, payload, size=5)
        transport = FakeTransport([ChunkedResponse(headers={"Content-Length": "50"}, chunks=[payload])])

        with self.assertRaisesRegex(github_updates.DownloadError, "limit"):
            github_updates.download_release(release, install_root, max_bytes=10, transport=transport)

    def test_size_limit_enforced_during_stream(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        release = self.release_info(github_updates, b"abcdef", size=5)
        transport = FakeTransport([ChunkedResponse(headers={}, chunks=[b"abc", b"def"])])

        with self.assertRaisesRegex(github_updates.DownloadError, "exceeded"):
            github_updates.download_release(release, install_root, max_bytes=5, transport=transport)

    def test_declared_asset_size_mismatch_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        release = self.release_info(github_updates, b"abc", size=10)
        transport = FakeTransport([ChunkedResponse(headers={}, chunks=[b"abc"])])

        with self.assertRaisesRegex(github_updates.DownloadError, "size mismatch"):
            github_updates.download_release(release, install_root, transport=transport)

    def test_invalid_content_length_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        release = self.release_info(github_updates, payload)
        transport = FakeTransport([ChunkedResponse(headers={"Content-Length": "not-an-int"}, chunks=[payload])])

        with self.assertRaisesRegex(github_updates.DownloadError, "Content-Length"):
            github_updates.download_release(release, install_root, transport=transport)

    def test_transport_digest_mismatch_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        release = self.release_info(github_updates, b"abc", sha256="0" * 64)
        transport = FakeTransport([ChunkedResponse(headers={}, chunks=[b"abc"])])

        with self.assertRaisesRegex(github_updates.DownloadError, "digest"):
            github_updates.download_release(release, install_root, transport=transport)

    def test_malformed_zip_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        downloaded = self.downloaded(github_updates, install_root, b"not a zip")

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "Malformed"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_invalid_extraction_limits_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        downloaded = self.downloaded(github_updates, install_root, make_release_zip())

        for kwargs in (
            {"max_extracted_bytes": True},
            {"max_extracted_bytes": 0},
            {"max_zip_entries": False},
            {"max_zip_entries": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(github_updates.ArchiveExtractionError):
                    github_updates.prepare_downloaded_release(downloaded, install_root, **kwargs)

    def test_declared_total_uncompressed_size_limit_rejected_before_extraction(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(files={"DarkAbyss_Core/large.txt": b"x" * 64})
        downloaded = self.downloaded(github_updates, install_root, payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            declared_sizes = [info.file_size for info in archive.infolist() if not info.is_dir()]
        total_declared = sum(declared_sizes)
        limit = total_declared - 1
        self.assertGreaterEqual(limit, max(declared_sizes))

        with mock.patch.object(github_updates.zipfile.ZipFile, "open", side_effect=AssertionError("should not extract")):
            with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "declared uncompressed"):
                github_updates.prepare_downloaded_release(downloaded, install_root, max_extracted_bytes=limit)

        self.assertEqual(list((install_root / "updates" / "prepared").iterdir()), [])
        self.assertFalse((install_root / "versions" / "1.0.0").exists())

    def test_single_zip_entry_larger_than_extraction_limit_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(files={"DarkAbyss_Core/large.txt": b"x" * 64})
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "entry exceeds"):
            github_updates.prepare_downloaded_release(downloaded, install_root, max_extracted_bytes=40)

        self.assertFalse((install_root / "versions" / "1.0.0").exists())

    def test_zip_entry_count_limit_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        files = {f"DarkAbyss_Core/file_{index}.txt": b"x" for index in range(5)}
        payload = make_release_zip(files=files)
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "too many entries"):
            github_updates.prepare_downloaded_release(downloaded, install_root, max_zip_entries=3)

        self.assertFalse((install_root / "versions" / "1.0.0").exists())

    def test_archive_below_extraction_limits_prepares_and_stages(self):
        github_updates, update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(files={"DarkAbyss_Core/small.txt": b"small"})
        downloaded = self.downloaded(github_updates, install_root, payload)

        prepared = github_updates.prepare_downloaded_release(
            downloaded,
            install_root,
            max_extracted_bytes=1024,
            max_zip_entries=4,
        )
        staged = update_engine.stage_release(prepared.release_root, install_root)

        self.assertEqual(staged.version, "1.0.0")

    def test_extraction_stream_exceeding_declared_entry_size_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        archive_path = install_root / "fake.zip"
        archive_path.parent.mkdir(parents=True)
        archive_path.write_bytes(b"fake")
        release = self.release_info(github_updates, b"fake")
        downloaded = github_updates.DownloadedRelease(
            release=release,
            asset=release.assets[0],
            archive_path=archive_path,
            byte_count=4,
            sha256=sha256_bytes(b"fake"),
        )

        class FakeArchive:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def infolist(self):
                info = zipfile.ZipInfo("release.json")
                info.file_size = 2
                return [info]

            def open(self, _info, _mode):
                return io.BytesIO(b"{}x")

        with mock.patch.object(github_updates.zipfile, "ZipFile", return_value=FakeArchive()):
            with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "exceeded declared"):
                github_updates.prepare_downloaded_release(downloaded, install_root)

        self.assertEqual(list((install_root / "updates" / "prepared").iterdir()), [])

    def test_partial_prepared_directory_cleaned_on_extraction_limit_failure(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        archive_path = install_root / "fake.zip"
        archive_path.parent.mkdir(parents=True)
        archive_path.write_bytes(b"fake")
        release = self.release_info(github_updates, b"fake")
        downloaded = github_updates.DownloadedRelease(
            release=release,
            asset=release.assets[0],
            archive_path=archive_path,
            byte_count=4,
            sha256=sha256_bytes(b"fake"),
        )

        class FakeArchive:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def infolist(self):
                manifest = zipfile.ZipInfo("release.json")
                manifest.file_size = 2
                payload = zipfile.ZipInfo("DarkAbyss_Core/payload.txt")
                payload.file_size = 5
                return [manifest, payload]

            def open(self, info, _mode):
                if info.filename == "release.json":
                    return io.BytesIO(b"{}")
                return io.BytesIO(b"x" * 6)

        with mock.patch.object(github_updates.zipfile, "ZipFile", return_value=FakeArchive()):
            with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "exceeded"):
                github_updates.prepare_downloaded_release(downloaded, install_root, max_extracted_bytes=7)

        self.assertEqual(list((install_root / "updates" / "prepared").iterdir()), [])
        self.assertFalse((install_root / "versions" / "1.0.0").exists())
        self.assertFalse((install_root / "current.json").exists())

    def test_rejected_archive_preserves_current_pointer_and_user_sentinels(self):
        github_updates, update_engine, install_root, data_root = self.with_modules()
        token = data_root / "instances" / "admin-main" / "secrets" / "token.txt"
        config = data_root / "instances" / "admin-main" / "config.json"
        database = data_root / "instances" / "admin-main" / "data" / "state.sqlite"
        token.parent.mkdir(parents=True)
        database.parent.mkdir(parents=True)
        token.write_bytes(b"TOKEN_SENTINEL")
        config.write_bytes(b'{"config": true}')
        database.write_bytes(b"DB_SENTINEL")
        before = {path: path.read_bytes() for path in (token, config, database)}
        current_release = self.downloaded(github_updates, install_root, make_release_zip(version="0.9.0"), version="0.9.0")
        prepared = github_updates.prepare_downloaded_release(current_release, install_root)
        update_engine.stage_release(prepared.release_root, install_root)
        update_engine.activate_staged_release("0.9.0", install_root)
        current_before = (install_root / "current.json").read_bytes()
        rejected = self.downloaded(
            github_updates,
            install_root,
            make_release_zip(version="1.0.0", files={"DarkAbyss_Core/large.txt": b"x" * 64}),
        )

        with self.assertRaises(github_updates.ArchiveExtractionError):
            github_updates.prepare_downloaded_release(rejected, install_root, max_extracted_bytes=32)

        self.assertEqual((install_root / "current.json").read_bytes(), current_before)
        self.assertEqual(update_engine.get_current_version(install_root), "0.9.0")
        self.assertFalse((install_root / "versions" / "1.0.0").exists())
        self.assertEqual({path: path.read_bytes() for path in (token, config, database)}, before)

    def test_absolute_zip_path_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = self.zip_with_entry("/evil.txt", b"x")
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "relative"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_parent_traversal_zip_path_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = self.zip_with_entry("../evil.txt", b"x")
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "traversal"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_windows_drive_zip_path_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = self.zip_with_entry("C:/evil.txt", b"x")
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "relative"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_symlink_zip_entry_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        symlink_mode = stat.S_IFLNK | 0o777
        payload = make_release_zip(entries=[("linked.txt", b"target", symlink_mode)])
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "symlink"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_duplicate_case_colliding_zip_entries_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(entries=[("App/File.txt", b"one", None), ("app/file.txt", b"two", None)])
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "Duplicate"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_release_json_missing_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(manifest=False)
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "release.json"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_release_tag_version_mismatch_rejected(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(version="2.0.0")
        downloaded = self.downloaded(github_updates, install_root, payload, version="1.0.0")

        with self.assertRaisesRegex(github_updates.ReleaseSelectionError, "mismatch"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

    def test_extracted_release_passed_through_update_engine_inspect(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        downloaded = self.downloaded(github_updates, install_root, payload)

        with mock.patch.object(github_updates.update_engine, "inspect_release", wraps=github_updates.update_engine.inspect_release) as inspect:
            github_updates.prepare_downloaded_release(downloaded, install_root)

        self.assertEqual(inspect.call_count, 1)

    def test_invalid_per_file_sha_prevents_staging(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        manifest = release_manifest("1.0.0", {"DarkAbyss_Core/example.py": b"payload"})
        manifest["files"][0]["sha256"] = "0" * 64
        payload = make_release_zip(files={"DarkAbyss_Core/example.py": b"payload"}, manifest=manifest)
        release = self.release_info(github_updates, payload)
        transport = FakeTransport([self.asset_response(payload)])

        with self.assertRaisesRegex(github_updates.ArchiveExtractionError, "Phase 6"):
            downloaded = github_updates.download_release(release, install_root, transport=transport)
            github_updates.prepare_downloaded_release(downloaded, install_root)

        self.assertFalse((install_root / "versions" / "1.0.0").exists())

    def test_valid_github_artifact_can_be_prepared_and_staged(self):
        github_updates, update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip(files={"DarkAbyss_Core/example.py": b"program"})
        release = self.release_info(github_updates, payload)
        transport = FakeTransport([self.asset_response(payload)])

        downloaded = github_updates.download_release(release, install_root, transport=transport)
        prepared = github_updates.prepare_downloaded_release(downloaded, install_root)
        staged = update_engine.stage_release(prepared.release_root, install_root)

        self.assertEqual(staged.version, "1.0.0")
        self.assertEqual((staged.version_dir / "DarkAbyss_Core" / "example.py").read_bytes(), b"program")

    def test_download_and_stage_uses_phase6_staging(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        payload = make_release_zip()
        api_payload = [github_release("1.0.0", assets=[self.asset_payload(payload)])]
        transport = FakeTransport([self.api_response(api_payload), self.asset_response(payload)])

        staged = github_updates.download_and_stage_release("owner", "repo", install_root, transport=transport)

        self.assertEqual(staged.version, "1.0.0")
        self.assertTrue((install_root / "versions" / "1.0.0" / "release.json").is_file())

    def test_symlinked_downloads_directory_rejected_before_write(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        updates_dir = install_root / "updates"
        updates_dir.mkdir(parents=True)
        outside = install_root.parent / "outside-downloads"
        outside.mkdir()
        try:
            (updates_dir / "downloads").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks are not supported for this test environment: {exc}")
        payload = make_release_zip()
        release = self.release_info(github_updates, payload)

        with self.assertRaisesRegex(github_updates.GitHubUpdateError, "symlink"):
            github_updates.download_release(release, install_root, transport=FakeTransport([self.asset_response(payload)]))

        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_prepared_directory_rejected_before_extract(self):
        github_updates, _update_engine, install_root, _data_root = self.with_modules()
        updates_dir = install_root / "updates"
        updates_dir.mkdir(parents=True)
        outside = install_root.parent / "outside-prepared"
        outside.mkdir()
        try:
            (updates_dir / "prepared").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks are not supported for this test environment: {exc}")
        payload = make_release_zip()
        downloaded = self.downloaded(github_updates, install_root, payload)

        with self.assertRaisesRegex(github_updates.GitHubUpdateError, "symlink"):
            github_updates.prepare_downloaded_release(downloaded, install_root)

        self.assertEqual(list(outside.iterdir()), [])

    def test_user_data_sentinels_remain_byte_identical(self):
        github_updates, update_engine, install_root, data_root = self.with_modules()
        token = data_root / "instances" / "admin-main" / "secrets" / "token.txt"
        config = data_root / "instances" / "admin-main" / "config.json"
        database = data_root / "instances" / "admin-main" / "data" / "state.sqlite"
        token.parent.mkdir(parents=True)
        database.parent.mkdir(parents=True)
        token.write_bytes(b"TOKEN_SENTINEL")
        config.write_bytes(b'{"config": true}')
        database.write_bytes(b"DB_SENTINEL")
        before = {path: path.read_bytes() for path in (token, config, database)}
        payload = make_release_zip(files={"DarkAbyss_Core/example.py": b"program"})
        transport = FakeTransport(
            [
                self.api_response([github_release("1.0.0", assets=[self.asset_payload(payload)])]),
                self.asset_response(payload),
            ]
        )

        staged = github_updates.download_and_stage_release("owner", "repo", install_root, transport=transport)
        update_engine.activate_staged_release(staged.version, install_root)

        self.assertEqual({path: path.read_bytes() for path in (token, config, database)}, before)
        self.assertFalse((data_root / "updates").exists())
        self.assertFalse((data_root / "versions").exists())

    def release_info(self, github_updates, payload: bytes, *, version="1.0.0", size=None, sha256=None):
        return github_updates.GitHubReleaseInfo(
            tag_name=f"v{version}",
            version=version,
            name=None,
            draft=False,
            prerelease=False,
            assets=(
                github_updates.GitHubReleaseAsset(
                    name=f"darkabyss-release-{version}.zip",
                    download_url=f"https://github.com/example/project/releases/download/v{version}/darkabyss-release-{version}.zip",
                    size=len(payload) if size is None else size,
                    sha256=sha256,
                ),
            ),
        )

    def asset_payload(self, payload: bytes, *, version="1.0.0"):
        return {
            "name": f"darkabyss-release-{version}.zip",
            "browser_download_url": f"https://github.com/example/project/releases/download/v{version}/darkabyss-release-{version}.zip",
            "size": len(payload),
        }

    def downloaded(self, github_updates, install_root: Path, payload: bytes, *, version="1.0.0"):
        install_root.mkdir(parents=True, exist_ok=True)
        archive_path = install_root / f"artifact-{uuid_like(version)}.zip"
        archive_path.write_bytes(payload)
        release = self.release_info(github_updates, payload, version=version)
        return github_updates.DownloadedRelease(
            release=release,
            asset=release.assets[0],
            archive_path=archive_path,
            byte_count=len(payload),
            sha256=sha256_bytes(payload),
        )

    def zip_with_entry(self, name: str, payload: bytes):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, payload)
        return buffer.getvalue()


def uuid_like(value: str) -> str:
    return value.replace(".", "-").replace("+", "-")


if __name__ == "__main__":
    unittest.main()
