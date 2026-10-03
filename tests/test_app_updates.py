"""Self-update: version order, GitHub check, a full update of an installed
copy from release artifacts built like CI builds them, data kept, resume."""

import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
OWNER_REPO = "Grollex/DarkAbyss-Bot-Manager"
MODULES = (
    "app_updates",
    "github_updates",
    "update_engine",
    "launcher",
    "release_manifest",
    "runtime_layout",
    "app_paths",
    "assemble_distribution",
    "build_release_artifacts",
)


def load(data_root: Path):
    sys.path.insert(0, str(CORE_ROOT))
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for name in MODULES:
        sys.modules.pop(name, None)
    return importlib.import_module("app_updates")


def load_packaging(name: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, body: bytes, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers if headers is not None else {"Content-Length": str(len(body))}
        self.body = body

    def iter_bytes(self, _chunk_size):
        yield self.body

    def close(self):
        pass


class FakeGitHub:
    """api.github.com + release asset downloads, served from local files."""

    def __init__(self):
        self.releases: list[dict] = []
        self.assets: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.status = 200

    def publish(self, version: str, update_zip: Path, *, prerelease=False, draft=False, digest=None):
        payload = update_zip.read_bytes()
        url = f"https://github.com/{OWNER_REPO}/releases/download/v{version}/{update_zip.name}"
        self.assets[url] = payload
        self.releases.insert(
            0,
            {
                "tag_name": f"v{version}",
                "name": f"DarkAbyss Bot Manager {version}",
                "draft": draft,
                "prerelease": prerelease,
                "assets": [
                    {
                        "name": update_zip.name,
                        "browser_download_url": url,
                        "size": len(payload),
                        "digest": digest or "sha256:" + hashlib.sha256(payload).hexdigest(),
                    }
                ],
            },
        )

    def __call__(self, url, headers, timeout):
        self.requests.append(url)
        if "Authorization" in headers and not url.startswith("https://api.github.com/"):
            raise AssertionError("credentials must never be sent to asset hosts")
        if self.status != 200:
            return Response(b"{}", status_code=self.status)
        api = f"https://api.github.com/repos/{OWNER_REPO}/releases"
        if url == api:
            return Response(json.dumps(self.releases).encode())
        if url.startswith(api + "/tags/"):
            tag = url.rsplit("/", 1)[1]
            release = next(item for item in self.releases if item["tag_name"] == tag)
            return Response(json.dumps(release).encode())
        if url in self.assets:
            return Response(self.assets[url])
        raise AssertionError(f"unexpected request {url}")


class VersionOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_updates = load(Path(self.temp.name) / "data")

    def test_release_beats_its_prereleases_and_older_builds(self):
        newer = self.app_updates.is_newer
        self.assertTrue(newer("1.0.0", "0.9.0-ai7-rc4"))
        self.assertTrue(newer("1.0.0", "1.0.0-rc2"))
        self.assertTrue(newer("1.0.1", "1.0.0"))
        self.assertTrue(newer("1.10.0", "1.9.3"))
        self.assertTrue(newer("v2.0", "1.99.99"))
        self.assertTrue(newer("1.0.0-rc.10", "1.0.0-rc.2"))
        self.assertFalse(newer("1.0.0", "1.0.0"))
        self.assertFalse(newer("0.9.9", "1.0.0"))
        self.assertFalse(newer("1.0.0-rc1", "1.0.0"))
        self.assertFalse(newer("garbage", "0.0.1"))
        self.assertTrue(newer("0.0.1", "garbage"))


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app_updates = load(self.root / "data")
        self.github = FakeGitHub()
        zip_path = self.root / "darkabyss-release-1.0.1.zip"
        zip_path.write_bytes(b"zip")
        self.zip_path = zip_path

    def test_newer_published_release_is_offered(self):
        self.github.publish("1.0.1", self.zip_path)
        update = self.app_updates.check_for_update("1.0.0", transport=self.github)
        self.assertEqual((update.version, update.tag), ("1.0.1", "v1.0.1"))
        self.assertEqual(update.url, f"https://github.com/{OWNER_REPO}/releases/tag/v1.0.1")
        self.assertEqual(self.github.requests, [f"https://api.github.com/repos/{OWNER_REPO}/releases"])

    def test_same_or_older_version_is_not_offered(self):
        self.github.publish("1.0.1", self.zip_path)
        self.assertIsNone(self.app_updates.check_for_update("1.0.1", transport=self.github))
        self.assertIsNone(self.app_updates.check_for_update("1.2.0", transport=self.github))

    def test_drafts_and_prereleases_are_skipped(self):
        draft = self.root / "darkabyss-release-2.0.0.zip"
        draft.write_bytes(b"d")
        pre = self.root / "darkabyss-release-1.1.0-rc1.zip"
        pre.write_bytes(b"p")
        self.github.publish("1.0.1", self.zip_path)
        self.github.publish("1.1.0-rc1", pre, prerelease=True)
        self.github.publish("2.0.0", draft, draft=True)
        update = self.app_updates.check_for_update("1.0.0", transport=self.github)
        self.assertEqual(update.version, "1.0.1")

    def test_no_release_yet_means_no_update(self):
        self.assertIsNone(self.app_updates.check_for_update("1.0.0", transport=self.github))

    def test_network_problem_is_reported(self):
        self.github.status = 503
        with self.assertRaises(self.app_updates.AppUpdateError) as caught:
            self.app_updates.check_for_update("1.0.0", transport=self.github)
        self.assertIn("503", str(caught.exception))


class InstallUpdateTests(unittest.TestCase):
    """An installed copy (Launcher.exe + versions/ + current.json) is updated from
    the same artifacts the release workflow publishes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "LocalAppData" / "DarkAbyssBotManager"
        self.app_updates = load(self.data_root)
        self.assembler = load_packaging("assemble_distribution")
        self.artifacts = load_packaging("build_release_artifacts")
        self.update_engine = self.app_updates.update_engine
        self.github = FakeGitHub()
        self.install_root = self.distribution("0.9.0-ai7-rc4", self.root / "Programs" / "DarkAbyssBotManager")
        self.user_files = self.write_user_data()

    def distribution(self, version: str, output: Path, extra: dict[str, bytes] | None = None) -> Path:
        bundle = self.root / f"bundle-{version}"
        (bundle / "_internal" / "bots" / "admin").mkdir(parents=True)
        (bundle / self.assembler.runtime_layout.app_executable_name()).write_bytes(f"app {version}".encode())
        (bundle / "_internal" / "bots" / "admin" / "manifest.json").write_text('{"id": "admin"}', encoding="utf-8")
        for relative, payload in (extra or {}).items():
            (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
            (bundle / relative).write_bytes(payload)
        launcher = self.root / f"launcher-{version}" / self.assembler.runtime_layout.launcher_executable_name()
        launcher.parent.mkdir()
        launcher.write_bytes(b"launcher")
        return self.assembler.assemble_distribution(
            version=version, app_bundle_dir=bundle, launcher_executable=launcher, output_dir=output
        )

    def release(self, version: str, extra: dict[str, bytes] | None = None) -> Path:
        built = self.distribution(version, self.root / "ci" / version / "DarkAbyssBotManager", extra)
        artifacts = self.artifacts.build_release_artifacts(
            version=version, tag=f"v{version}", distribution=built, output=self.root / "ci" / version / "out"
        )
        self.github.publish(version, artifacts.update_zip)
        return artifacts.update_zip

    def write_user_data(self) -> dict[Path, bytes]:
        files = {
            "instances/admin-main/instance.json": b'{"id": "admin-main", "bot_type": "admin"}',
            "instances/admin-main/config.json": b'{"allowed_user_ids": ["1"]}',
            "instances/admin-main/secrets/token.txt": b"not-a-real-token",
            "instances/group-main/data/game_presence_state.json": b"{}",
            "config/ai_connections.json": b'{"connections": []}',
            "secrets/ai_connections/groq/c1.secret": b"not-a-real-key",
        }
        written = {}
        for relative, payload in files.items():
            path = self.data_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            written[path] = payload
        return written

    def assert_user_data_untouched(self):
        for path, payload in self.user_files.items():
            self.assertEqual(path.read_bytes(), payload, path)

    def test_update_installs_new_version_next_to_old_and_keeps_user_data(self):
        self.release("1.0.0", {"_internal/bots/new_bot/manifest.json": b'{"id": "new_bot"}'})
        update = self.app_updates.check_for_update("0.9.0-ai7-rc4", transport=self.github)
        self.assertEqual(update.version, "1.0.0")

        result = self.app_updates.install_update(update, self.install_root, transport=self.github)

        self.assertEqual((result.version, result.previous_version), ("1.0.0", "0.9.0-ai7-rc4"))
        self.assertEqual(self.update_engine.get_current_version(self.install_root), "1.0.0")
        target = importlib.import_module("launcher").resolve_current_app(self.install_root)
        self.assertEqual(target.version, "1.0.0")
        self.assertEqual(target.command[1:], ("--manager",))
        new_dir = self.install_root / "versions" / "1.0.0"
        # A bot type added in the new version ships with the program.
        self.assertTrue((new_dir / "_internal" / "bots" / "new_bot" / "manifest.json").is_file())
        # The previous version stays for rollback; the update archives are cleaned up.
        self.assertTrue((self.install_root / "versions" / "0.9.0-ai7-rc4").is_dir())
        self.assertEqual(list((self.install_root / "updates" / "downloads").iterdir()), [])
        self.assertEqual(list((self.install_root / "updates" / "prepared").iterdir()), [])
        self.assert_user_data_untouched()
        # Nothing of the user's data ended up in the program folder.
        installed_names = {path.name for path in self.install_root.rglob("*")}
        self.assertFalse({"token.txt", "c1.secret", "ai_connections.json", "config.json"} & installed_names)

        rolled = self.update_engine.rollback_to_previous(self.install_root)
        self.assertEqual(rolled.version, "0.9.0-ai7-rc4")
        self.assert_user_data_untouched()

    def test_failed_download_changes_nothing(self):
        zip_path = self.release("1.0.0")
        self.github.releases[0]["assets"][0]["digest"] = "sha256:" + "0" * 64
        update = self.app_updates.check_for_update("0.9.0-ai7-rc4", transport=self.github)
        with self.assertRaises(self.app_updates.AppUpdateError):
            self.app_updates.install_update(update, self.install_root, transport=self.github)
        self.assertTrue(zip_path.is_file())
        self.assertEqual(self.update_engine.get_current_version(self.install_root), "0.9.0-ai7-rc4")
        self.assertFalse((self.install_root / "versions" / "1.0.0").exists())
        self.assert_user_data_untouched()

    def test_interrupted_update_is_finished_by_the_next_attempt(self):
        self.release("1.0.0")
        update = self.app_updates.check_for_update("0.9.0-ai7-rc4", transport=self.github)
        with mock.patch.object(self.update_engine, "activate_staged_release", side_effect=self.update_engine.ActivationError("disk full")):
            with self.assertRaises(self.app_updates.AppUpdateError):
                self.app_updates.install_update(update, self.install_root, transport=self.github)
        self.assertEqual(self.update_engine.get_current_version(self.install_root), "0.9.0-ai7-rc4")
        self.github.requests.clear()
        self.app_updates.install_update(update, self.install_root, transport=self.github)
        self.assertEqual(self.update_engine.get_current_version(self.install_root), "1.0.0")
        self.assertEqual(self.github.requests, [], "an already staged version is not downloaded again")

    def test_only_current_and_previous_versions_are_kept(self):
        for version in ("1.0.0", "1.0.1"):
            self.release(version)
            current = self.update_engine.get_current_version(self.install_root)
            update = self.app_updates.check_for_update(current, transport=self.github)
            self.app_updates.install_update(update, self.install_root, transport=self.github)
        self.assertEqual(self.update_engine.list_installed_versions(self.install_root), ["1.0.0", "1.0.1"])
        state = self.update_engine.get_activation_state(self.install_root)
        self.assertEqual((state.version, state.previous_version), ("1.0.1", "1.0.0"))
        self.assert_user_data_untouched()

    def test_install_root_inside_user_data_is_refused(self):
        self.release("1.0.0")
        update = self.app_updates.check_for_update("0.9.0-ai7-rc4", transport=self.github)
        with self.assertRaises(self.app_updates.AppUpdateError):
            self.app_updates.install_update(update, self.data_root / "app", transport=self.github)
        self.assert_user_data_untouched()

    def test_current_install_is_found_only_in_the_packaged_layout(self):
        exe = self.install_root / "versions" / "0.9.0-ai7-rc4" / self.app_updates.runtime_layout.app_executable_name()
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe)):
            installed = self.app_updates.current_install()
        self.assertEqual(installed.version, "0.9.0-ai7-rc4")
        self.assertEqual(installed.install_root, self.install_root.resolve())
        self.assertEqual(installed.launcher.name, self.app_updates.runtime_layout.launcher_executable_name())
        self.assertIsNone(self.app_updates.current_install())  # running from source

        calls = []
        self.app_updates.start_launcher(installed, popen=lambda *args, **kwargs: calls.append((args, kwargs)))
        self.assertEqual(calls[0][0][0], [str(installed.launcher)])
        self.assertEqual(calls[0][1]["cwd"], str(installed.install_root))
        self.assertFalse(calls[0][1]["shell"])


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_updates = load(Path(self.temp.name) / "data")

    def test_running_bots_are_handed_to_the_new_version_once(self):
        self.app_updates.save_resume(["group-main", "admin-main", "admin-main"], "1.0.0")
        self.assertEqual(self.app_updates.take_resume(), ["admin-main", "group-main"])
        self.assertEqual(self.app_updates.take_resume(), [])
        self.assertFalse(self.app_updates.resume_path().exists())

    def test_stale_or_broken_resume_is_ignored(self):
        self.app_updates.save_resume(["admin-main"], "1.0.0")
        self.assertEqual(self.app_updates.take_resume(now=time.time() + 3600), [])
        self.app_updates.resume_path().write_text("{broken", encoding="utf-8")
        self.assertEqual(self.app_updates.take_resume(), [])
        self.assertFalse(self.app_updates.resume_path().exists())

    def test_resume_file_lives_in_user_data(self):
        self.assertEqual(self.app_updates.resume_path().parent, self.app_updates.app_paths.CONFIG_DIR)


class ReleaseContentTests(unittest.TestCase):
    """What PyInstaller bundles from the repository holds no user data."""

    def test_bundled_folders_hold_no_secrets_or_user_files(self):
        spec = (PROJECT_ROOT / "packaging" / "DarkAbyssApp.spec").read_text(encoding="utf-8")
        datas = spec.split("datas=[", 1)[1].split("]", 1)[0]
        self.assertNotIn("token", datas)
        self.assertNotIn("admin_config.json", datas.replace("defaults", ""))
        forbidden = ("token.txt", ".secret", ".env", "ai_keys", "admin_bot_token")
        for folder in (PROJECT_ROOT / "bots", CORE_ROOT / "defaults"):
            for path in folder.rglob("*"):
                self.assertFalse(any(part in path.name for part in forbidden), path)


if __name__ == "__main__":
    unittest.main()
