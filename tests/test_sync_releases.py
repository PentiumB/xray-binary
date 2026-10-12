import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.sync_releases import (
    ASSET_NAME,
    DIGEST_NAME,
    GitHubClient,
    build_release_payload,
    extract_xray,
    parse_sha256,
    plan_releases,
    release_needs_sync,
    version_from_tag,
)


def release(tag, *, prerelease=False, draft=False, with_assets=True):
    assets = []
    if with_assets:
        assets = [
            {"name": ASSET_NAME, "browser_download_url": f"https://example.test/{tag}.zip"},
            {"name": DIGEST_NAME, "browser_download_url": f"https://example.test/{tag}.dgst"},
        ]
    return {
        "tag_name": tag,
        "name": f"Xray {tag}",
        "html_url": f"https://github.com/XTLS/Xray-core/releases/tag/{tag}",
        "prerelease": prerelease,
        "draft": draft,
        "assets": assets,
    }


class VersionTests(unittest.TestCase):
    def test_strips_leading_v_from_release_tag(self):
        self.assertEqual(version_from_tag("v26.10.10"), "26.10.10")

    def test_rejects_tag_that_cannot_be_a_safe_filename(self):
        with self.assertRaises(ValueError):
            version_from_tag("v../../escape")


class PlanningTests(unittest.TestCase):
    def test_empty_destination_bootstraps_only_latest_published_release(self):
        upstream = [
            release("v26.10.10", prerelease=True),
            release("v26.9.30", prerelease=False),
        ]
        plan = plan_releases(upstream, set())
        self.assertEqual([item["tag_name"] for item in plan], ["v26.10.10"])
        self.assertTrue(plan[0]["prerelease"])

    def test_imports_all_releases_newer_than_last_mirrored_oldest_first(self):
        upstream = [
            release("v26.12.1", prerelease=False),
            release("v26.11.2", prerelease=True),
            release("v26.10.10", prerelease=True),
        ]
        plan = plan_releases(upstream, {"v26.10.10"})
        self.assertEqual(
            [item["tag_name"] for item in plan],
            ["v26.11.2", "v26.12.1"],
        )

    def test_fills_gaps_within_existing_mirror_history(self):
        upstream = [release("v4"), release("v3"), release("v2"), release("v1")]
        plan = plan_releases(upstream, {"v3", "v1"})
        self.assertEqual([item["tag_name"] for item in plan], ["v2", "v4"])

    def test_skips_drafts_and_releases_missing_required_assets(self):
        upstream = [
            release("v3", draft=True),
            release("v2", with_assets=False),
            release("v1"),
        ]
        plan = plan_releases(upstream, set())
        self.assertEqual([item["tag_name"] for item in plan], ["v1"])


class ArchiveTests(unittest.TestCase):
    def test_parses_sha256_from_official_digest(self):
        digest = "MD5= deadbeef\nSHA2-256= " + "a" * 64 + "\n"
        self.assertEqual(parse_sha256(digest), "a" * 64)

    def test_extracts_only_xray_and_names_it_as_version(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("xray", b"official binary")
            zf.writestr("geoip.dat", b"not wanted")
            zf.writestr("geosite.dat", b"not wanted")

        expected = hashlib.sha256(archive.getvalue()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            output = extract_xray(archive.getvalue(), expected, "26.10.10", Path(tmp))
            self.assertEqual(output.name, "26.10.10")
            self.assertEqual(output.read_bytes(), b"official binary")
            self.assertEqual(output.stat().st_mode & 0o777, 0o755)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["26.10.10"])

    def test_rejects_archive_with_wrong_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                extract_xray(b"not a zip", "0" * 64, "1.0.0", Path(tmp))


class GitHubClientTests(unittest.TestCase):
    class FakeResponse:
        headers = {"Content-Type": "application/octet-stream"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"data"

    def test_does_not_send_token_to_public_download_host(self):
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return self.FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            GitHubClient("top-secret").download("https://github.com/XTLS/Xray-core/file")

        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_sends_token_to_github_api(self):
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(request)
            return self.FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            GitHubClient("top-secret").download("https://api.github.com/repos/test")

        self.assertEqual(captured[0].get_header("Authorization"), "Bearer top-secret")


class PayloadTests(unittest.TestCase):
    def test_preserves_upstream_prerelease_status(self):
        item = release("v26.10.10", prerelease=True)
        payload = build_release_payload(item, "b" * 64)
        self.assertEqual(payload["tag_name"], "v26.10.10")
        self.assertTrue(payload["prerelease"])
        self.assertFalse(payload["draft"])
        self.assertIn("SHA-256 бинарника: `" + "b" * 64 + "`", payload["body"])


class ReconciliationTests(unittest.TestCase):
    def test_exact_verified_asset_does_not_need_sync(self):
        upstream = release("v26.10.10", prerelease=True)
        destination = {
            "tag_name": "v26.10.10",
            "name": "Xray v26.10.10",
            "draft": False,
            "prerelease": True,
            "body": "SHA-256 бинарника: `" + "c" * 64 + "`",
            "assets": [{"name": "26.10.10", "digest": "sha256:" + "c" * 64}],
        }
        self.assertFalse(release_needs_sync(upstream, destination))

    def test_corrupted_existing_asset_needs_sync(self):
        upstream = release("v26.10.10", prerelease=True)
        destination = {
            "tag_name": "v26.10.10",
            "name": "Xray v26.10.10",
            "draft": False,
            "prerelease": True,
            "body": "SHA-256 бинарника: `" + "c" * 64 + "`",
            "assets": [{"name": "26.10.10", "digest": "sha256:" + "d" * 64}],
        }
        self.assertTrue(release_needs_sync(upstream, destination))

    def test_existing_draft_needs_sync(self):
        upstream = release("v26.10.10", prerelease=False)
        destination = {
            "tag_name": "v26.10.10",
            "name": "Xray v26.10.10",
            "draft": True,
            "prerelease": False,
            "body": "SHA-256 бинарника: `" + "e" * 64 + "`",
            "assets": [{"name": "26.10.10", "digest": "sha256:" + "e" * 64}],
        }
        self.assertTrue(release_needs_sync(upstream, destination))


if __name__ == "__main__":
    unittest.main()
