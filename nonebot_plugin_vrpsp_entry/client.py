import httpx

from .config import Config
from .models import Entry, parse_entries


class Client:
    def __init__(self, config: Config):
        self.urls = {"vr": config.vrpsp_entry_vr_url, "psp": config.vrpsp_entry_psp_url}
        # 两个已知中国大陆接口直连，不继承宿主外网代理。
        self.http = httpx.AsyncClient(
            timeout=config.vrpsp_entry_http_timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "nonebot-plugin-vrpsp-entry/0.1"},
        )

    async def fetch(self, source: str) -> list[Entry]:
        response = await self.http.get(self.urls[source], params={"cache": 1})
        response.raise_for_status()
        return parse_entries(response.json())

    async def close(self) -> None:
        await self.http.aclose()
