from asyncio import Lock, sleep
from contextlib import asynccontextmanager
from time import monotonic

from nonebot.matcher import Matcher

SEND_INTERVAL = 0.3


class SendLimiter:
    def __init__(self):
        self.lock = Lock()
        self.next_send_at = 0.0

    @asynccontextmanager
    async def slot(self):
        # 本插件所有群、Bot 和回复共用；单调时钟不受系统校时影响。
        async with self.lock:
            remaining = self.next_send_at - monotonic()
            if remaining > 0:
                await sleep(remaining)
            try:
                yield
            finally:
                # 失败、超时和 finish 的控制流异常同样保留发送间隔。
                self.next_send_at = monotonic() + SEND_INTERVAL


limiter = SendLimiter()


async def send(matcher: Matcher, text: str) -> None:
    async with limiter.slot():
        await matcher.send(text)


async def finish(matcher: Matcher, text: str) -> None:
    async with limiter.slot():
        await matcher.finish(text)
