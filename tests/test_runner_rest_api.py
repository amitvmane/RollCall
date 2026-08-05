"""
REST API server startup must trust X-Forwarded-For from its direct peer so
request.client.host (and every rate-limit bucket keyed on it) reflects the
real visitor, not the internal proxy hop every request arrives through in
the documented deployment (REST_API_PORT is never published to the host —
docker-compose.yml). Without proxy_headers=True, every anonymous rate-limit
bucket collapses onto one shared bucket for the whole user base.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class TestRestApiProxyHeaders(unittest.IsolatedAsyncioTestCase):

    async def _run_with_mocked_uvicorn(self):
        import runner

        mock_server_instance = MagicMock()
        mock_server_instance.serve = AsyncMock()
        with patch("uvicorn.Config") as mock_config, \
             patch("uvicorn.Server", return_value=mock_server_instance):
            await runner._run_rest_api_server()
        return mock_config

    async def test_proxy_headers_enabled_by_default(self):
        mock_config = await self._run_with_mocked_uvicorn()
        _, kwargs = mock_config.call_args
        self.assertTrue(kwargs.get("proxy_headers"))
        self.assertEqual(kwargs.get("forwarded_allow_ips"), "*")

    async def test_trusted_proxy_ips_env_override(self):
        with patch.dict(os.environ, {"TRUSTED_PROXY_IPS": "172.20.0.5"}):
            mock_config = await self._run_with_mocked_uvicorn()
        _, kwargs = mock_config.call_args
        self.assertEqual(kwargs.get("forwarded_allow_ips"), "172.20.0.5")


if __name__ == "__main__":
    unittest.main()
