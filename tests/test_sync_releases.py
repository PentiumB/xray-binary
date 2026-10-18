import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.sync_releases import (
    ASSET_NAME,
    BINARY_NAME,
    DIGEST_NAME,
    GitHubClient,
    build_release_payload,
    extract_xray,
    parse_sha256,
    plan_releases,
    release_needs_sync,
    version_from_tag,
)


def release(
    tag,
    *,
    prerelease=False,
    draft=False,
    with_assets=True,
    published_at="2026-10-10T09:22:39Z",
    zip_id=101,
    digest_id=102,
    zip_digest="a" * 64,
    digest_digest="b" * 64,
):
    assets = []
    if with_assets:
        assets = [
            {
                "id": zip_id,
                "name": ASSET_NAME,
                "size": 100,
                "digest": f"sha256:{zip_digest}",
                "updated_at": published_at,
                "browser_download_url": f"https://example.test/{tag}.zip",
            },
            {
                "id": digest_id,
                "name": DIGEST_NAME,
                "size": 100,
                "digest": f"sha256:{digest_digest}",
                "updated_at": published_at,
                "browser_download_url": f"https://example.test/{tag}.dgst",
            },
        ]
    return {
        "tag_name": tag,
        "name": f"Xray {tag}",
        "html_url": f"https://github.com/XTLS/Xray-core/releases/tag/{tag}",
        "published_at": published_at,
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
    START = "2026-10-10T09:22:39Z"

    def test_empty_destination_restores_every_release_in_range(self):
        upstream = [
            release("v2", prerelease=True, published_at="2026-10-12T00:00:00Z"),
            release("v1", published_at=self.START),
            release("v0", published_at="2026-09-01T00:00:00Z"),
        ]
        plan = plan_releases(upstream, set(), self.START)
        self.assertEqual([item["tag_name"] for item in plan], ["v1", "v2"])
        self.assertTrue(plan[-1]["prerelease"])

    def test_imports_all_newer_releases_oldest_first(self):
        upstream = [
            release("v3", published_at="2026-10-13T00:00:00Z"),
            release("v2", prerelease=True, published_at="2026-10-12T00:00:00Z"),
            release("v1", published_at=self.START),
        ]
        plan = plan_releases(upstream, {"v1"}, self.START)
        self.assertEqual([item["tag_name"] for item in plan], ["v2", "v3"])

    def test_fills_gaps_within_mirror_range(self):
        upstream = [
            release("v4", published_at="2026-10-14T00:00:00Z"),
            release("v3", published_at="2026-10-13T00:00:00Z"),
            release("v2", published_at="2026-10-12T00:00:00Z"),
            release("v1", published_at=self.START),
        ]
        plan = plan_releases(upstream, {"v3", "v1"}, self.START)
        self.assertEqual([item["tag_name"] for item in plan], ["v2", "v4"])

    def test_no_shared_tag_recovers_every_release_since_durable_boundary(self):
        upstream = [
            release("v4", published_at="2026-10-14T00:00:00Z"),
            release("v3", published_at="2026-10-13T00:00:00Z"),
            release("v2", published_at="2026-10-12T00:00:00Z"),
        ]
        plan = plan_releases(upstream, {"withdrawn-v1"}, "2026-10-12T00:00:00Z")
        self.assertEqual([item["tag_name"] for item in plan], ["v2", "v3", "v4"])

    def test_sorts_by_published_time_instead_of_api_order(self):
        upstream = [
            release("v2", published_at="2026-10-12T00:00:00Z"),
            release("v3", published_at="2026-10-13T00:00:00Z"),
            release("v1", published_at=self.START),
        ]
        plan = plan_releases(upstream, {"v1"}, self.START)
        self.assertEqual([item["tag_name"] for item in plan], ["v2", "v3"])

    def test_skips_drafts_missing_assets_and_pre_boundary_releases(self):
        upstream = [
            release("v3", draft=True, published_at="2026-10-13T00:00:00Z"),
            release("v2", with_assets=False, published_at="2026-10-12T00:00:00Z"),
            release("v1", published_at=self.START),
            release("v0", published_at="2026-09-01T00:00:00Z"),
        ]
        plan = plan_releases(upstream, set(), self.START)
        self.assertEqual([item["tag_name"] for item in plan], ["v1"])


class ArchiveTests(unittest.TestCase):
    def test_parses_sha256_from_official_digest(self):
        digest = "MD5= deadbeef\nSHA2-256= " + "a" * 64 + "\n"
        self.assertEqual(parse_sha256(digest), "a" * 64)

    def test_extracts_only_xray_with_fixed_asset_name(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("xray", b"official binary")
            zf.writestr("geoip.dat", b"not wanted")
            zf.writestr("geosite.dat", b"not wanted")

        expected = hashlib.sha256(archive.getvalue()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            output = extract_xray(archive.getvalue(), expected, "26.10.10", Path(tmp))
            self.assertEqual(output.name, "Xray-linux-64")
            self.assertEqual(output.read_bytes(), b"official binary")
            self.assertEqual(output.stat().st_mode & 0o777, 0o755)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["Xray-linux-64"])

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
    def test_preserves_status_and_records_upstream_provenance(self):
        item = release("v26.10.10", prerelease=True)
        payload = build_release_payload(item, "c" * 64)
        self.assertEqual(payload["tag_name"], "v26.10.10")
        self.assertTrue(payload["prerelease"])
        self.assertFalse(payload["draft"])
        self.assertIn("SHA-256 бинарника: `" + "c" * 64 + "`", payload["body"])
        self.assertIn('"zip_asset_id":101', payload["body"])
        self.assertIn('"digest_asset_id":102', payload["body"])


class ReconciliationTests(unittest.TestCase):
    def destination(self, upstream, binary_sha="c" * 64):
        payload = build_release_payload(upstream, binary_sha)
        return {
            "tag_name": upstream["tag_name"],
            "name": upstream["name"],
            "draft": False,
            "prerelease": upstream["prerelease"],
            "body": payload["body"],
            "assets": [
                {
                    "name": BINARY_NAME,
                    "digest": "sha256:" + binary_sha,
                }
            ],
        }

    def test_fixed_asset_name_is_accepted_and_legacy_name_is_migrated(self):
        upstream = release("v26.10.10", prerelease=True)
        destination = self.destination(upstream)
        destination["assets"][0]["name"] = "Xray-linux-64"
        self.assertFalse(release_needs_sync(upstream, destination))
        destination["assets"][0]["name"] = "26.10.10"
        self.assertTrue(release_needs_sync(upstream, destination))

    def test_exact_verified_asset_does_not_need_sync(self):
        upstream = release("v26.10.10", prerelease=True)
        self.assertFalse(release_needs_sync(upstream, self.destination(upstream)))

    def test_corrupted_existing_asset_needs_sync(self):
        upstream = release("v26.10.10", prerelease=True)
        destination = self.destination(upstream)
        destination["assets"][0]["digest"] = "sha256:" + "d" * 64
        self.assertTrue(release_needs_sync(upstream, destination))

    def test_upstream_asset_replacement_needs_sync(self):
        original = release("v26.10.10", prerelease=True)
        destination = self.destination(original)
        replaced = release(
            "v26.10.10",
            prerelease=True,
            zip_id=201,
            digest_id=202,
            zip_digest="d" * 64,
            digest_digest="e" * 64,
        )
        self.assertTrue(release_needs_sync(replaced, destination))

    def test_existing_draft_needs_sync(self):
        upstream = release("v26.10.10")
        destination = self.destination(upstream)
        destination["draft"] = True
        self.assertTrue(release_needs_sync(upstream, destination))


if __name__ == "__main__":
    unittest.main()
