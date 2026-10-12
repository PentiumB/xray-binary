#!/usr/bin/env python3
"""Mirror official Xray Linux x86_64 binaries into this repository's releases."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

UPSTREAM_REPOSITORY = "XTLS/Xray-core"
ASSET_NAME = "Xray-linux-64.zip"
DIGEST_NAME = f"{ASSET_NAME}.dgst"
API_ROOT = "https://api.github.com"
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
BINARY_SHA_RE = re.compile(r"SHA-256 бинарника: `([0-9a-fA-F]{64})`")
PROVENANCE_RE = re.compile(r"<!-- xray-mirror-provenance:(\{.*?\}) -->")
MAX_ARCHIVE_SIZE = 128 * 1024 * 1024
MAX_DIGEST_SIZE = 64 * 1024
MAX_BINARY_SIZE = 128 * 1024 * 1024


def version_from_tag(tag: str) -> str:
    version = tag[1:] if tag.startswith("v") else tag
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"unsafe version derived from tag {tag!r}")
    return version


def asset_by_name(release: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((asset for asset in release.get("assets", []) if asset.get("name") == name), None)


def is_mirrorable(release: dict[str, Any]) -> bool:
    return (
        not release.get("draft", False)
        and asset_by_name(release, ASSET_NAME) is not None
        and asset_by_name(release, DIGEST_NAME) is not None
    )


def plan_releases(
    upstream_releases: list[dict[str, Any]],
    destination_tags: set[str],
    start_published_at: str,
) -> list[dict[str, Any]]:
    candidates = sorted(
        (
            release
            for release in upstream_releases
            if is_mirrorable(release)
            and (release.get("published_at") or "") >= start_published_at
        ),
        key=lambda release: release["published_at"],
        reverse=True,
    )
    if not candidates:
        return []

    # The durable timestamp boundary is the source of truth. Restore every
    # missing in-range release, including when the destination is completely empty.
    missing = [
        release for release in candidates if release["tag_name"] not in destination_tags
    ]
    return list(reversed(missing))


def parse_sha256(digest_text: str) -> str:
    match = re.search(r"^SHA2-256=\s*([0-9a-fA-F]{64})\s*$", digest_text, re.MULTILINE)
    if not match:
        raise ValueError("official digest does not contain SHA2-256")
    return match.group(1).lower()


def extract_xray(zip_bytes: bytes, expected_sha256: str, version: str, output_dir: Path) -> Path:
    if len(zip_bytes) > MAX_ARCHIVE_SIZE:
        raise ValueError("official archive exceeds size limit")
    actual_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    if actual_sha256 != expected_sha256.lower():
        raise ValueError(
            f"archive SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / version
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        if names.count("xray") != 1:
            raise ValueError("official archive must contain exactly one root entry named 'xray'")
        info = archive.getinfo("xray")
        if info.file_size > MAX_BINARY_SIZE:
            raise ValueError("xray binary exceeds uncompressed size limit")
        binary = archive.read("xray")

    output_path.write_bytes(binary)
    output_path.chmod(0o755)
    return output_path


def upstream_provenance(release: dict[str, Any]) -> dict[str, Any]:
    zip_asset = asset_by_name(release, ASSET_NAME)
    digest_asset = asset_by_name(release, DIGEST_NAME)
    if zip_asset is None or digest_asset is None:
        raise ValueError(f"release {release.get('tag_name')} lacks required official assets")
    return {
        "zip_asset_id": zip_asset.get("id"),
        "zip_asset_digest": zip_asset.get("digest"),
        "zip_asset_size": zip_asset.get("size"),
        "digest_asset_id": digest_asset.get("id"),
        "digest_asset_digest": digest_asset.get("digest"),
        "digest_asset_size": digest_asset.get("size"),
    }


def build_release_payload(release: dict[str, Any], binary_sha256: str) -> dict[str, Any]:
    tag = release["tag_name"]
    source_url = release.get("html_url", f"https://github.com/{UPSTREAM_REPOSITORY}/releases/tag/{tag}")
    provenance = json.dumps(upstream_provenance(release), sort_keys=True, separators=(",", ":"))
    return {
        "tag_name": tag,
        "name": release.get("name") or tag,
        "body": (
            "Автоматически извлечённый без изменений файл `xray` из официального "
            f"архива `{ASSET_NAME}`.\n\n"
            f"SHA-256 бинарника: `{binary_sha256}`\n\nИсточник: {source_url}\n\n"
            f"<!-- xray-mirror-provenance:{provenance} -->"
        ),
        "draft": False,
        "prerelease": bool(release.get("prerelease", False)),
    }


def release_needs_sync(upstream: dict[str, Any], destination: dict[str, Any]) -> bool:
    version = version_from_tag(upstream["tag_name"])
    expected_name = upstream.get("name") or upstream["tag_name"]
    if destination.get("name") != expected_name:
        return True
    if bool(destination.get("draft")):
        return True
    if bool(destination.get("prerelease")) != bool(upstream.get("prerelease")):
        return True

    assets = destination.get("assets", [])
    if len(assets) != 1 or assets[0].get("name") != version:
        return True

    body = destination.get("body") or ""
    binary_match = BINARY_SHA_RE.search(body)
    provenance_match = PROVENANCE_RE.search(body)
    if not binary_match or not provenance_match:
        return True
    try:
        recorded_provenance = json.loads(provenance_match.group(1))
    except json.JSONDecodeError:
        return True
    if recorded_provenance != upstream_provenance(upstream):
        return True

    expected_digest = f"sha256:{binary_match.group(1).lower()}"
    return (assets[0].get("digest") or "").lower() != expected_digest


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        content_type: str = "application/vnd.github+json",
        raw_body: bytes | None = None,
    ) -> tuple[Any, dict[str, str]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PentiumB-xray-binary-mirror",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        hostname = urllib.parse.urlparse(url).hostname
        if self.token and hostname in {"api.github.com", "uploads.github.com"}:
            headers["Authorization"] = f"Bearer {self.token}"

        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                response_headers = dict(response.headers.items())
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {error.code}: {details}") from error

        if not data:
            return None, response_headers
        if "json" in response_headers.get("Content-Type", ""):
            return json.loads(data), response_headers
        return data, response_headers

    def get_json(self, url: str) -> Any:
        return self.request("GET", url)[0]

    def paginate(self, url: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in url else "?"
            batch = self.get_json(f"{url}{separator}per_page=100&page={page}")
            if not batch:
                return items
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def paginate_releases_since(self, url: str, start_published_at: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in url else "?"
            batch = self.get_json(f"{url}{separator}per_page=100&page={page}")
            if not batch:
                return items
            items.extend(batch)
            if len(batch) < 100 or any(
                (release.get("published_at") or "") < start_published_at
                for release in batch
            ):
                return items
            page += 1

    def download(self, url: str) -> bytes:
        data, _ = self.request("GET", url)
        if not isinstance(data, bytes):
            raise TypeError(f"expected binary response from {url}")
        return data


def download_asset(client: GitHubClient, asset: dict[str, Any], max_size: int) -> bytes:
    size = asset.get("size")
    if not isinstance(size, int) or size < 0 or size > max_size:
        raise ValueError(f"asset {asset.get('name')} has invalid size: {size}")
    data = client.download(asset["browser_download_url"])
    if len(data) != size:
        raise ValueError(
            f"asset {asset.get('name')} size mismatch: API says {size}, downloaded {len(data)}"
        )
    api_digest = asset.get("digest")
    if api_digest:
        actual_digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if actual_digest.lower() != api_digest.lower():
            raise ValueError(f"asset {asset.get('name')} does not match GitHub API digest")
    return data


def upload_url(release: dict[str, Any], filename: str) -> str:
    base = release["upload_url"].split("{", 1)[0]
    return f"{base}?{urllib.parse.urlencode({'name': filename})}"


def prepare_binary(
    client: GitHubClient, upstream: dict[str, Any]
) -> tuple[str, bytes, str]:
    tag = upstream["tag_name"]
    version = version_from_tag(tag)
    zip_asset = asset_by_name(upstream, ASSET_NAME)
    digest_asset = asset_by_name(upstream, DIGEST_NAME)
    if zip_asset is None or digest_asset is None:
        raise ValueError(f"release {tag} lacks required official assets")

    digest_bytes = download_asset(client, digest_asset, MAX_DIGEST_SIZE)
    digest_text = digest_bytes.decode("utf-8")
    expected_archive_sha256 = parse_sha256(digest_text)
    archive = download_asset(client, zip_asset, MAX_ARCHIVE_SIZE)
    with tempfile.TemporaryDirectory() as directory:
        binary_path = extract_xray(
            archive, expected_archive_sha256, version, Path(directory)
        )
        binary = binary_path.read_bytes()
    return version, binary, hashlib.sha256(binary).hexdigest()


def ensure_release(
    client: GitHubClient,
    destination: str,
    upstream: dict[str, Any],
    existing: dict[str, Any] | None,
) -> None:
    tag = upstream["tag_name"]

    # Verify and prepare the official binary before creating or modifying a
    # public destination release. A failed download or digest check leaves the
    # destination untouched.
    version, binary, binary_sha256 = prepare_binary(client, upstream)
    payload = build_release_payload(upstream, binary_sha256)

    staging_payload = dict(payload)
    staging_payload["draft"] = True
    if existing is None:
        release, _ = client.request(
            "POST", f"{API_ROOT}/repos/{destination}/releases", payload=staging_payload
        )
        print(f"Created destination draft {tag}")
    else:
        release, _ = client.request(
            "PATCH",
            f"{API_ROOT}/repos/{destination}/releases/{existing['id']}",
            payload=staging_payload,
        )
        print(f"Moved {tag} to draft for safe reconciliation")

    # Enforce exactly one release asset. Existing files are never trusted by
    # name alone: a repair replaces them with the freshly verified binary.
    for asset in release.get("assets", []):
        client.request(
            "DELETE", f"{API_ROOT}/repos/{destination}/releases/assets/{asset['id']}"
        )
        print(f"Deleted existing asset {asset.get('name')} from {tag}")

    client.request(
        "POST",
        upload_url(release, version),
        content_type="application/octet-stream",
        raw_body=binary,
    )
    client.request(
        "PATCH",
        f"{API_ROOT}/repos/{destination}/releases/{release['id']}",
        payload=payload,
    )

    encoded_tag = urllib.parse.quote(tag, safe="")
    published = client.get_json(
        f"{API_ROOT}/repos/{destination}/releases/tags/{encoded_tag}"
    )
    published_assets = published.get("assets", [])
    if published.get("draft") or bool(published.get("prerelease")) != payload["prerelease"]:
        raise RuntimeError(f"published release {tag} has incorrect release status")
    if len(published_assets) != 1 or published_assets[0].get("name") != version:
        raise RuntimeError(f"published release {tag} does not contain exactly asset {version}")
    if (published_assets[0].get("digest") or "").lower() != f"sha256:{binary_sha256}":
        raise RuntimeError(f"published release {tag} asset digest does not match upload")
    print(f"Published and verified official binary {version} in {tag}")


def draft_withdrawn_release(
    client: GitHubClient, destination: str, release: dict[str, Any]
) -> None:
    if release.get("draft"):
        return
    client.request(
        "PATCH",
        f"{API_ROOT}/repos/{destination}/releases/{release['id']}",
        payload={"draft": True},
    )
    print(f"Moved withdrawn or incomplete upstream release {release['tag_name']} to draft")


def main() -> int:
    destination = os.environ.get("GITHUB_REPOSITORY", "PentiumB/xray-binary")
    token = os.environ.get("GITHUB_TOKEN")
    dry_run = os.environ.get("DRY_RUN") == "1"
    if not token and not dry_run:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    state_path = Path(__file__).resolve().parents[1] / "mirror-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    start_published_at = state["start_published_at"]

    client = GitHubClient(token)
    upstream_releases = client.paginate_releases_since(
        f"{API_ROOT}/repos/{UPSTREAM_REPOSITORY}/releases", start_published_at
    )

    in_scope_upstream = sorted(
        (
            release
            for release in upstream_releases
            if is_mirrorable(release)
            and (release.get("published_at") or "") >= start_published_at
        ),
        key=lambda release: release["published_at"],
        reverse=True,
    )

    if dry_run:
        if not in_scope_upstream:
            raise RuntimeError("no mirrorable upstream releases found in configured range")
        latest = in_scope_upstream[0]
        zip_asset = asset_by_name(latest, ASSET_NAME)
        digest_asset = asset_by_name(latest, DIGEST_NAME)
        assert zip_asset is not None and digest_asset is not None
        print(
            json.dumps(
                {
                    "tag": latest["tag_name"],
                    "prerelease": bool(latest["prerelease"]),
                    "binary_name": version_from_tag(latest["tag_name"]),
                    "zip_asset": zip_asset["name"],
                    "digest_asset": digest_asset["name"],
                },
                ensure_ascii=False,
            )
        )
        return 0

    destination_releases = client.paginate(f"{API_ROOT}/repos/{destination}/releases")
    by_tag = {release["tag_name"]: release for release in destination_releases}

    planned = plan_releases(upstream_releases, set(by_tag), start_published_at)
    planned_tags = {release["tag_name"] for release in planned}
    upstream_by_tag = {release["tag_name"]: release for release in in_scope_upstream}
    repairs = [
        upstream
        for tag, upstream in upstream_by_tag.items()
        if tag in by_tag
        and tag not in planned_tags
        and release_needs_sync(upstream, by_tag[tag])
    ]
    ordered = planned + sorted(
        repairs, key=lambda release: release["published_at"]
    )

    for upstream in ordered:
        ensure_release(client, destination, upstream, by_tag.get(upstream["tag_name"]))

    withdrawn = [
        release for tag, release in by_tag.items() if tag not in upstream_by_tag
    ]
    for release in withdrawn:
        draft_withdrawn_release(client, destination, release)

    if not ordered and not withdrawn:
        print("All mirrored releases already match upstream metadata and verified assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
