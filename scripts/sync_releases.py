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
    upstream_releases: list[dict[str, Any]], destination_tags: set[str]
) -> list[dict[str, Any]]:
    candidates = [release for release in upstream_releases if is_mirrorable(release)]
    if not candidates:
        return []

    # An empty mirror starts from the current upstream release instead of flooding
    # the repository with the complete historical release archive.
    if not destination_tags:
        return [candidates[0]]

    shared_indices = [
        index
        for index, release in enumerate(candidates)
        if release["tag_name"] in destination_tags
    ]
    if shared_indices:
        # The oldest shared release marks the beginning of this mirror's history.
        # Fill every gap inside that range and add every newer release.
        oldest_shared = max(shared_indices)
        missing = [
            release
            for release in candidates[: oldest_shared + 1]
            if release["tag_name"] not in destination_tags
        ]
        return list(reversed(missing))

    # If no common tag exists, fail conservatively and bootstrap only the latest.
    return [candidates[0]]


def parse_sha256(digest_text: str) -> str:
    match = re.search(r"^SHA2-256=\s*([0-9a-fA-F]{64})\s*$", digest_text, re.MULTILINE)
    if not match:
        raise ValueError("official digest does not contain SHA2-256")
    return match.group(1).lower()


def extract_xray(zip_bytes: bytes, expected_sha256: str, version: str, output_dir: Path) -> Path:
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
        binary = archive.read("xray")

    output_path.write_bytes(binary)
    output_path.chmod(0o755)
    return output_path


def build_release_payload(release: dict[str, Any], binary_sha256: str) -> dict[str, Any]:
    tag = release["tag_name"]
    source_url = release.get("html_url", f"https://github.com/{UPSTREAM_REPOSITORY}/releases/tag/{tag}")
    return {
        "tag_name": tag,
        "name": release.get("name") or tag,
        "body": (
            "Автоматически извлечённый без изменений файл `xray` из официального "
            f"архива `{ASSET_NAME}`.\n\n"
            f"SHA-256 бинарника: `{binary_sha256}`\n\nИсточник: {source_url}"
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

    match = BINARY_SHA_RE.search(destination.get("body") or "")
    if not match:
        return True
    expected_digest = f"sha256:{match.group(1).lower()}"
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

    def download(self, url: str) -> bytes:
        data, _ = self.request("GET", url)
        if not isinstance(data, bytes):
            raise TypeError(f"expected binary response from {url}")
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

    digest_text = client.download(digest_asset["browser_download_url"]).decode("utf-8")
    expected_archive_sha256 = parse_sha256(digest_text)
    archive = client.download(zip_asset["browser_download_url"])
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
    print(f"Published verified official binary {version} in {tag}")


def main() -> int:
    destination = os.environ.get("GITHUB_REPOSITORY", "PentiumB/xray-binary")
    token = os.environ.get("GITHUB_TOKEN")
    dry_run = os.environ.get("DRY_RUN") == "1"
    if not token and not dry_run:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    client = GitHubClient(token)
    upstream_releases = client.paginate(
        f"{API_ROOT}/repos/{UPSTREAM_REPOSITORY}/releases"
    )

    if dry_run:
        candidates = [release for release in upstream_releases if is_mirrorable(release)]
        if not candidates:
            raise RuntimeError("no mirrorable upstream releases found")
        latest = candidates[0]
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

    planned = plan_releases(upstream_releases, set(by_tag))
    planned_tags = {release["tag_name"] for release in planned}
    upstream_by_tag = {
        release["tag_name"]: release
        for release in upstream_releases
        if is_mirrorable(release)
    }
    repairs = [
        upstream
        for tag, upstream in upstream_by_tag.items()
        if tag in by_tag
        and tag not in planned_tags
        and release_needs_sync(upstream, by_tag[tag])
    ]
    ordered = planned + sorted(repairs, key=lambda release: release["tag_name"])

    for upstream in ordered:
        ensure_release(client, destination, upstream, by_tag.get(upstream["tag_name"]))

    if not ordered:
        print("All mirrored releases already match upstream metadata and verified assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
