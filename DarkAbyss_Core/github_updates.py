from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, build_opener, HTTPSHandler, HTTPErrorProcessor

import app_paths
import update_engine


GITHUB_API_ROOT = "https://api.github.com"
DEFAULT_USER_AGENT = "DarkAbyssBotManager-Updater/1"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_EXTRACTED_BYTES = 300 * 1024 * 1024
DEFAULT_MAX_ZIP_ENTRIES = 10_000
DOWNLOADS_DIR_NAME = "downloads"
PREPARED_DIR_NAME = "prepared"
UPDATES_DIR_NAME = "updates"
ZIP_CHUNK_SIZE = 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
ALLOWED_ASSET_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}


class GitHubUpdateError(RuntimeError):
    pass


class GitHubAPIError(GitHubUpdateError):
    pass


class ReleaseSelectionError(GitHubUpdateError):
    pass


class DownloadError(GitHubUpdateError):
    pass


class ArchiveExtractionError(GitHubUpdateError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = b""

    def iter_bytes(self, chunk_size: int = ZIP_CHUNK_SIZE) -> Iterable[bytes]:
        del chunk_size
        yield self.body

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class GitHubReleaseAsset:
    name: str
    download_url: str
    size: int
    sha256: str | None = None


@dataclass(frozen=True)
class GitHubReleaseInfo:
    tag_name: str
    version: str
    name: str | None
    draft: bool
    prerelease: bool
    assets: tuple[GitHubReleaseAsset, ...]


@dataclass(frozen=True)
class DownloadedRelease:
    release: GitHubReleaseInfo
    asset: GitHubReleaseAsset
    archive_path: Path
    byte_count: int
    sha256: str


HttpTransport = Callable[[str, Mapping[str, str], float], object]


class _NoRedirectProcessor(HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


class _UrllibResponse:
    def __init__(self, handle):
        self._handle = handle
        self.status_code = int(handle.status)
        self.headers = dict(handle.headers.items())

    def iter_bytes(self, chunk_size: int = ZIP_CHUNK_SIZE) -> Iterable[bytes]:
        while True:
            chunk = self._handle.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self._handle.close()


def fetch_latest_release(
    owner: str,
    repo: str,
    *,
    allow_prerelease: bool = False,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: HttpTransport | None = None,
) -> GitHubReleaseInfo:
    payload = _api_json(
        _api_url(owner, repo, "releases"),
        token=token,
        timeout=timeout,
        transport=transport,
    )
    if not isinstance(payload, list):
        raise GitHubAPIError("Malformed GitHub releases response: root value must be an array.")
    for item in payload:
        release = _parse_release(item)
        if release.draft:
            continue
        if release.prerelease and not allow_prerelease:
            continue
        _select_release_asset(release)
        return release
    raise ReleaseSelectionError("No supported GitHub release found.")


def fetch_release_by_tag(
    owner: str,
    repo: str,
    tag: str,
    *,
    allow_prerelease: bool = False,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: HttpTransport | None = None,
) -> GitHubReleaseInfo:
    if not isinstance(tag, str) or not tag.strip() or "/" in tag or "\\" in tag:
        raise ReleaseSelectionError(f"Invalid release tag: {tag!r}")
    payload = _api_json(
        _api_url(owner, repo, "releases", "tags", quote(tag, safe="")),
        token=token,
        timeout=timeout,
        transport=transport,
    )
    release = _parse_release(payload)
    if release.draft:
        raise ReleaseSelectionError(f"GitHub release {release.tag_name!r} is a draft.")
    if release.prerelease and not allow_prerelease:
        raise ReleaseSelectionError(f"GitHub release {release.tag_name!r} is a prerelease.")
    _select_release_asset(release)
    return release


def download_release(
    release: GitHubReleaseInfo,
    install_root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: HttpTransport | None = None,
) -> DownloadedRelease:
    asset = _select_release_asset(release)
    _validate_max_bytes(max_bytes)
    downloads_dir = _ensure_structural_directory(_prepare_install_root(install_root), UPDATES_DIR_NAME, DOWNLOADS_DIR_NAME, create=True)
    declared_length = asset.size
    if declared_length > max_bytes:
        raise DownloadError(f"Release artifact is larger than the configured limit: {declared_length} > {max_bytes}")

    temp_path = downloads_dir / f".{asset.name}.{uuid.uuid4().hex}.tmp"
    final_path = downloads_dir / f"{release.version}.{uuid.uuid4().hex}.{asset.name}"
    response = None
    digest = hashlib.sha256()
    bytes_written = 0
    try:
        response = _request_with_redirects(
            asset.download_url,
            headers=_base_headers(),
            timeout=timeout,
            transport=transport,
            purpose="download",
        )
        if _status_code(response) != 200:
            raise DownloadError(f"GitHub asset download failed with HTTP status {_status_code(response)}.")
        header_length = _content_length(_headers(response).get("content-length"))
        if header_length is not None and header_length > max_bytes:
            raise DownloadError(f"Release artifact is larger than the configured limit: {header_length} > {max_bytes}")
        if header_length is not None and header_length != declared_length:
            raise DownloadError(
                f"GitHub asset Content-Length mismatch: expected {declared_length}, got {header_length}"
            )
        with temp_path.open("xb") as handle:
            for chunk in _iter_response_bytes(response):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise DownloadError("Release artifact exceeded the configured size limit during download.")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if bytes_written != declared_length:
            raise DownloadError(f"GitHub asset size mismatch: expected {declared_length}, got {bytes_written}")
        archive_sha256 = digest.hexdigest()
        if asset.sha256 is not None and archive_sha256 != asset.sha256:
            raise DownloadError("GitHub asset digest mismatch.")
        temp_path.replace(final_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        if isinstance(exc, GitHubUpdateError):
            raise
        raise DownloadError(f"Failed to download GitHub release artifact: {exc}") from exc
    finally:
        if response is not None:
            _close_response(response)

    return DownloadedRelease(
        release=release,
        asset=asset,
        archive_path=final_path,
        byte_count=bytes_written,
        sha256=archive_sha256,
    )


def prepare_downloaded_release(
    downloaded: DownloadedRelease,
    install_root: Path | str,
    *,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
) -> update_engine.ReleaseInfo:
    _validate_max_extracted_bytes(max_extracted_bytes)
    _validate_max_zip_entries(max_zip_entries)
    prepared_parent = _ensure_structural_directory(_prepare_install_root(install_root), UPDATES_DIR_NAME, PREPARED_DIR_NAME, create=True)
    prepared_root = prepared_parent / f"{downloaded.release.version}.{uuid.uuid4().hex}.prepared"
    prepared_root.mkdir()
    try:
        _extract_zip_safely(downloaded.archive_path, prepared_root, max_extracted_bytes, max_zip_entries)
        release = update_engine.inspect_release(prepared_root)
        if release.version != downloaded.release.version:
            raise ReleaseSelectionError(
                f"Release version mismatch: GitHub tag {downloaded.release.version!r}, release.json {release.version!r}."
            )
        _reject_unmanifested_files(prepared_root, release)
        return release
    except Exception as exc:
        shutil.rmtree(prepared_root, ignore_errors=True)
        if isinstance(exc, GitHubUpdateError):
            raise
        if isinstance(exc, update_engine.UpdateEngineError):
            raise ArchiveExtractionError(f"Extracted release failed Phase 6 verification: {exc}") from exc
        raise ArchiveExtractionError(f"Failed to prepare GitHub release artifact: {exc}") from exc


def download_and_stage_release(
    owner: str,
    repo: str,
    install_root: Path | str,
    *,
    tag: str | None = None,
    allow_prerelease: bool = False,
    token: str | None = None,
    max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: HttpTransport | None = None,
) -> update_engine.StageResult:
    if tag is None:
        release = fetch_latest_release(
            owner,
            repo,
            allow_prerelease=allow_prerelease,
            token=token,
            timeout=timeout,
            transport=transport,
        )
    else:
        release = fetch_release_by_tag(
            owner,
            repo,
            tag,
            allow_prerelease=allow_prerelease,
            token=token,
            timeout=timeout,
            transport=transport,
        )
    downloaded = download_release(release, install_root, max_bytes=max_bytes, timeout=timeout, transport=transport)
    prepared = prepare_downloaded_release(
        downloaded,
        install_root,
        max_extracted_bytes=max_extracted_bytes,
        max_zip_entries=max_zip_entries,
    )
    return update_engine.stage_release(prepared.release_root, install_root)


def _api_url(owner: str, repo: str, *parts: str) -> str:
    safe_owner = _validate_repo_part(owner, "owner")
    safe_repo = _validate_repo_part(repo, "repo")
    suffix = "/".join(parts)
    return f"{GITHUB_API_ROOT}/repos/{quote(safe_owner, safe='')}/{quote(safe_repo, safe='')}/{suffix}"


def _validate_repo_part(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value:
        raise GitHubAPIError(f"Invalid GitHub repository {label}.")
    return value


def _api_json(
    url: str,
    *,
    token: str | None,
    timeout: float,
    transport: HttpTransport | None,
) -> object:
    headers = _base_headers()
    resolved_token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    response = None
    try:
        response = _request_with_redirects(url, headers=headers, timeout=timeout, transport=transport, purpose="api")
        if _status_code(response) != 200:
            raise GitHubAPIError(f"GitHub API request failed with HTTP status {_status_code(response)}.")
        body = b"".join(_iter_response_bytes(response))
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubAPIError("Malformed GitHub API JSON response.") from exc
    except GitHubUpdateError:
        raise
    except (OSError, URLError) as exc:
        raise GitHubAPIError(f"GitHub API request failed: {exc}") from exc
    finally:
        if response is not None:
            _close_response(response)


def _parse_release(payload: object) -> GitHubReleaseInfo:
    if not isinstance(payload, dict):
        raise GitHubAPIError("Malformed GitHub release response: release must be an object.")
    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise GitHubAPIError("Malformed GitHub release response: missing tag_name.")
    draft = payload.get("draft")
    prerelease = payload.get("prerelease")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise GitHubAPIError("Malformed GitHub release response: draft/prerelease must be booleans.")
    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise GitHubAPIError("Malformed GitHub release response: name must be a string or null.")
    assets_payload = payload.get("assets")
    if not isinstance(assets_payload, list):
        raise GitHubAPIError("Malformed GitHub release response: assets must be an array.")
    assets = tuple(_parse_asset(asset) for asset in assets_payload)
    return GitHubReleaseInfo(
        tag_name=tag_name,
        version=_normalize_tag_version(tag_name),
        name=name,
        draft=draft,
        prerelease=prerelease,
        assets=assets,
    )


def _parse_asset(payload: object) -> GitHubReleaseAsset:
    if not isinstance(payload, dict):
        raise GitHubAPIError("Malformed GitHub release asset: asset must be an object.")
    name = payload.get("name")
    url = payload.get("browser_download_url")
    size = payload.get("size")
    if not isinstance(name, str) or not name:
        raise GitHubAPIError("Malformed GitHub release asset: missing name.")
    if not isinstance(url, str) or not url:
        raise GitHubAPIError("Malformed GitHub release asset: missing browser_download_url.")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise GitHubAPIError("Malformed GitHub release asset: invalid size.")
    digest = payload.get("digest")
    sha256 = None
    if digest is not None:
        if not isinstance(digest, str):
            raise GitHubAPIError("Malformed GitHub release asset: invalid digest.")
        sha256 = _parse_sha256_digest(digest)
    return GitHubReleaseAsset(name=name, download_url=url, size=size, sha256=sha256)


def _parse_sha256_digest(value: str) -> str | None:
    digest = value.lower()
    if digest.startswith("sha256:"):
        digest = digest.removeprefix("sha256:")
    if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
        return digest
    raise GitHubAPIError("Malformed GitHub release asset: unsupported digest format.")


def _normalize_tag_version(tag_name: str) -> str:
    candidate = tag_name[1:] if tag_name.startswith("v") and len(tag_name) > 1 else tag_name
    return update_engine._validate_version(candidate, error_type=ReleaseSelectionError)


def _expected_asset_name(version: str) -> str:
    return f"darkabyss-release-{version}.zip"


def _select_release_asset(release: GitHubReleaseInfo) -> GitHubReleaseAsset:
    expected_name = _expected_asset_name(release.version)
    matches = [asset for asset in release.assets if asset.name == expected_name]
    if not matches:
        raise ReleaseSelectionError(f"Missing required release asset: {expected_name}")
    if len(matches) > 1:
        raise ReleaseSelectionError(f"Duplicate release asset: {expected_name}")
    asset = matches[0]
    _validate_download_url(asset.download_url)
    return asset


def _base_headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def _request_with_redirects(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    transport: HttpTransport | None,
    purpose: str,
) -> object:
    _validate_timeout(timeout)
    current_url = url
    current_headers = dict(headers)
    current_response = None
    for _ in range(MAX_REDIRECTS + 1):
        if purpose == "api":
            _validate_api_request_url(current_url)
        else:
            _validate_download_url(current_url)
        try:
            current_response = _transport(transport)(current_url, current_headers, timeout)
        except Exception as exc:
            if purpose == "api":
                raise GitHubAPIError(f"GitHub API request failed: {exc}") from exc
            raise DownloadError(f"GitHub asset download failed: {exc}") from exc
        status_code = _status_code(current_response)
        if status_code not in REDIRECT_STATUSES:
            return current_response
        location = _headers(current_response).get("location")
        _close_response(current_response)
        if not location:
            raise _redirect_error(purpose, "GitHub redirect response did not include Location.")
        previous_host = urlparse(current_url).hostname
        next_url = urljoin(current_url, location)
        try:
            _validate_download_url(next_url)
        except DownloadError as exc:
            if purpose == "api":
                raise GitHubAPIError(str(exc)) from exc
            raise
        next_host = urlparse(next_url).hostname
        if next_host != previous_host:
            current_headers.pop("Authorization", None)
        current_url = next_url
    raise _redirect_error(purpose, "GitHub request exceeded maximum redirect count.")


def _redirect_error(purpose: str, message: str) -> GitHubUpdateError:
    if purpose == "api":
        return GitHubAPIError(message)
    return DownloadError(message)


def _transport(transport: HttpTransport | None) -> HttpTransport:
    return transport if transport is not None else _urllib_transport


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> _UrllibResponse:
    opener = build_opener(HTTPSHandler(), _NoRedirectProcessor())
    request = Request(url, headers=dict(headers), method="GET")
    try:
        return _UrllibResponse(opener.open(request, timeout=timeout))
    except HTTPError as exc:
        return _UrllibResponse(exc)


def _status_code(response: object) -> int:
    return int(getattr(response, "status_code"))


def _headers(response: object) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in dict(getattr(response, "headers")).items()}


def _iter_response_bytes(response: object) -> Iterable[bytes]:
    yield from response.iter_bytes(ZIP_CHUNK_SIZE)


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        close()


def _validate_api_request_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise GitHubAPIError("GitHub API requests must use https://api.github.com.")


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise DownloadError("GitHub release asset downloads must use HTTPS.")
    host = parsed.hostname
    if host is None or not _is_allowed_asset_host(host):
        raise DownloadError(f"GitHub release asset redirect host is not allowed: {host}")


def _is_allowed_asset_host(host: str) -> bool:
    normalized = host.lower()
    return normalized in ALLOWED_ASSET_HOSTS or normalized.endswith(".githubusercontent.com")


def _validate_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0 or not math.isfinite(timeout):
        raise GitHubUpdateError(f"Invalid network timeout: {timeout!r}")


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise DownloadError(f"Invalid maximum artifact size: {max_bytes!r}")


def _validate_max_extracted_bytes(max_extracted_bytes: int) -> None:
    if isinstance(max_extracted_bytes, bool) or not isinstance(max_extracted_bytes, int) or max_extracted_bytes <= 0:
        raise ArchiveExtractionError(f"Invalid maximum extracted ZIP size: {max_extracted_bytes!r}")


def _validate_max_zip_entries(max_zip_entries: int) -> None:
    if isinstance(max_zip_entries, bool) or not isinstance(max_zip_entries, int) or max_zip_entries <= 0:
        raise ArchiveExtractionError(f"Invalid maximum ZIP entry count: {max_zip_entries!r}")


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError as exc:
        raise DownloadError(f"Invalid Content-Length: {value!r}") from exc
    if length < 0:
        raise DownloadError(f"Invalid Content-Length: {value!r}")
    return length


def _prepare_install_root(install_root: Path | str) -> Path:
    root = Path(install_root).resolve()
    _ensure_install_root_disjoint_from_data_root(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ensure_install_root_disjoint_from_data_root(install_root: Path) -> None:
    data_root = app_paths.DATA_ROOT.resolve()
    try:
        install_root.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise GitHubUpdateError(f"Install root must be disjoint from DATA_ROOT: {install_root}")
    try:
        data_root.relative_to(install_root)
    except ValueError:
        pass
    else:
        raise GitHubUpdateError(f"DATA_ROOT must not be inside install root: {data_root}")


def _ensure_structural_directory(root: Path, *relative_parts: str, create: bool) -> Path:
    current = root
    for part in relative_parts:
        current = current / part
        # Symlink first: resolving it would only report "escapes root".
        if current.is_symlink():
            raise GitHubUpdateError(f"GitHub update structural directory must not be a symlink: {current}")
        _ensure_target_inside(root, current, "GitHub update structural directory")
        if current.exists():
            if not current.is_dir():
                raise GitHubUpdateError(f"GitHub update structural path must be a directory: {current}")
            _ensure_target_inside(root, current.resolve(), "GitHub update structural directory")
            continue
        if not create:
            raise GitHubUpdateError(f"Required GitHub update structural directory is missing: {current}")
        current.mkdir()
        if current.is_symlink() or not current.is_dir():
            raise GitHubUpdateError(f"GitHub update structural directory was not created safely: {current}")
        _ensure_target_inside(root, current.resolve(), "GitHub update structural directory")
    return current


def _ensure_target_inside(root: Path, target: Path, label: str) -> None:
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise GitHubUpdateError(f"{label} escapes root {root}: {target}") from exc


def _extract_zip_safely(
    archive_path: Path,
    destination_root: Path,
    max_extracted_bytes: int,
    max_zip_entries: int,
) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            file_paths = _validate_zip_infos(infos, max_extracted_bytes, max_zip_entries)
            if "release.json" not in file_paths:
                raise ArchiveExtractionError("GitHub release ZIP must contain release.json at archive root.")
            total_written = 0
            for info in infos:
                normalized_path = _normalize_zip_name(info)
                if normalized_path is None:
                    continue
                target = destination_root / normalized_path
                _ensure_target_inside(destination_root, target, "extracted ZIP entry")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    file_written = 0
                    for chunk in iter(lambda: source.read(ZIP_CHUNK_SIZE), b""):
                        file_written += len(chunk)
                        total_written += len(chunk)
                        if file_written > info.file_size:
                            raise ArchiveExtractionError(
                                f"ZIP entry exceeded declared uncompressed size: {normalized_path}"
                            )
                        if total_written > max_extracted_bytes:
                            raise ArchiveExtractionError("ZIP extracted payload exceeded configured size limit.")
                        output.write(chunk)
                    if file_written != info.file_size:
                        raise ArchiveExtractionError(
                            f"ZIP entry extracted size mismatch for {normalized_path}: "
                            f"expected {info.file_size}, got {file_written}"
                        )
                    output.flush()
                    os.fsync(output.fileno())
    except zipfile.BadZipFile as exc:
        raise ArchiveExtractionError("Malformed GitHub release ZIP.") from exc


def _validate_zip_infos(
    infos: list[zipfile.ZipInfo],
    max_extracted_bytes: int,
    max_zip_entries: int,
) -> set[str]:
    if len(infos) > max_zip_entries:
        raise ArchiveExtractionError(
            f"GitHub release ZIP has too many entries: {len(infos)} > {max_zip_entries}"
        )
    seen = set()
    file_paths = set()
    total_declared_size = 0
    for info in infos:
        normalized_path = _normalize_zip_name(info)
        if normalized_path is None:
            continue
        key = normalized_path.lower()
        if key in seen:
            raise ArchiveExtractionError(f"Duplicate or case-colliding ZIP path: {normalized_path}")
        seen.add(key)
        _validate_zip_mode(info, normalized_path)
        if not info.is_dir():
            file_size = info.file_size
            if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
                raise ArchiveExtractionError(f"Invalid ZIP entry uncompressed size: {normalized_path}")
            if file_size > max_extracted_bytes:
                raise ArchiveExtractionError(
                    f"ZIP entry exceeds configured extracted size limit: {normalized_path}"
                )
            total_declared_size += file_size
            if total_declared_size > max_extracted_bytes:
                raise ArchiveExtractionError("ZIP declared uncompressed size exceeds configured limit.")
            file_paths.add(normalized_path)
    return file_paths


def _normalize_zip_name(info: zipfile.ZipInfo) -> str | None:
    name = info.filename
    if not isinstance(name, str) or not name:
        raise ArchiveExtractionError("ZIP entry path must be non-empty.")
    if "\\" in name:
        raise ArchiveExtractionError(f"ZIP entry uses unsafe separators: {name!r}")
    windows_path = PureWindowsPath(name)
    posix_path = PurePosixPath(name)
    if windows_path.is_absolute() or posix_path.is_absolute() or windows_path.drive:
        raise ArchiveExtractionError(f"ZIP entry path must be relative: {name!r}")
    parts = posix_path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveExtractionError(f"ZIP entry path must not contain traversal: {name!r}")
    normalized = "/".join(parts)
    if info.is_dir():
        return None if not normalized else normalized
    return normalized


def _validate_zip_mode(info: zipfile.ZipInfo, normalized_path: str) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if stat.S_ISLNK(mode):
        raise ArchiveExtractionError(f"ZIP entry must not be a symlink: {normalized_path}")
    if file_type and not info.is_dir() and not stat.S_ISREG(mode):
        raise ArchiveExtractionError(f"ZIP entry must be a regular file: {normalized_path}")


def _reject_unmanifested_files(root: Path, release: update_engine.ReleaseInfo) -> None:
    expected = {update_engine.RELEASE_MANIFEST_NAME}
    expected.update(item.path for item in release.files)
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(actual - expected)
    if unexpected:
        raise ArchiveExtractionError(f"Unexpected files in GitHub release ZIP: {', '.join(unexpected)}")
