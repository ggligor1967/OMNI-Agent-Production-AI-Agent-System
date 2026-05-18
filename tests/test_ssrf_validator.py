import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSSRFValidator(unittest.TestCase):
    def test_rejects_loopback_private_linklocal_and_metadata_hosts(self):
        from agent.ssrf import SSRFProtectionError, ensure_public_http_url

        blocked_urls = [
            "http://127.0.0.1/admin",
            "http://10.0.0.5/internal",
            "http://192.168.1.20/internal",
            "http://172.16.4.9/internal",
            "http://169.254.169.254/latest/meta-data",
            "https://metadata.google.internal/computeMetadata/v1/instance",
        ]

        for url in blocked_urls:
            with self.assertRaises(SSRFProtectionError, msg=url):
                ensure_public_http_url(url)

    def test_rejects_hostnames_that_resolve_to_private_addresses(self):
        from agent.ssrf import SSRFProtectionError, ensure_public_http_url

        with self.assertRaises(SSRFProtectionError):
            ensure_public_http_url(
                "https://example.com/resource",
                resolver=lambda hostname: ["10.1.2.3"],
            )

    def test_accepts_public_http_url(self):
        from agent.ssrf import ensure_public_http_url

        url = "https://example.com/resource"
        self.assertEqual(
            ensure_public_http_url(url, resolver=lambda hostname: ["93.184.216.34"]),
            url,
        )


class TestSSRFFetchPaths(unittest.IsolatedAsyncioTestCase):
    async def test_webscraper_fetch_blocks_ssrf_url_before_network(self):
        from agent.tools import WebScraper

        scraper = WebScraper()

        with patch("agent.tools.aiohttp.ClientSession") as client_session:
            result = await scraper.fetch("http://127.0.0.1/admin")

        self.assertIn("blocked outbound url", result["error"].lower())
        client_session.assert_not_called()

    async def test_image_loader_blocks_ssrf_url_before_network(self):
        from agent.multimodal import ImageLoader
        from agent.ssrf import SSRFProtectionError

        loader = ImageLoader()

        with patch("aiohttp.ClientSession") as client_session:
            with self.assertRaises(SSRFProtectionError):
                await loader._from_url("http://169.254.169.254/latest/meta-data")

        client_session.assert_not_called()
