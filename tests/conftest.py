import os
import shutil
import socket
import subprocess
import time
import uuid

import nonebot
import pytest
from nonebug import NONEBOT_START_LIFESPAN


def pytest_configure(config):
    # 不读宿主 .env，不启动轮询，也不向真实 QQ 发消息。
    nonebot.init(
        _env_file=(),
        driver="~fastapi",
        command_start={"/"},
        log_level="WARNING",
        superusers={"99"},
    )
    config.stash[NONEBOT_START_LIFESPAN] = False


@pytest.fixture(scope="session")
def redis_url(tmp_path_factory):
    binary = os.environ.get("VRPSP_ENTRY_TEST_REDIS_SERVER") or shutil.which("redis-server")
    if not binary:
        pytest.skip("集成测试需要隔离 redis-server")
    directory = tmp_path_factory.mktemp("entry-redis")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            binary,
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--dir",
            str(directory),
            "--save",
            "",
            "--appendonly",
            "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("隔离 Redis 启动失败")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("隔离 Redis 未就绪")
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def send_limiter(monkeypatch):
    from nonebot_plugin_vrpsp_entry import sending

    instance = sending.SendLimiter()
    monkeypatch.setattr(sending, "limiter", instance)
    return instance


@pytest.fixture(autouse=True)
def isolate_send_limiter(send_limiter):
    # 每个测试的独立事件循环使用新的锁；不更改生产发送间隔。
    return send_limiter


@pytest.fixture
async def store(redis_url):
    from nonebot_plugin_vrpsp_entry.config import Config
    from nonebot_plugin_vrpsp_entry.store import Store

    instance = Store(
        Config(
            vrpsp_entry_redis_url=redis_url,
            vrpsp_entry_key_prefix=f"test:entry:{uuid.uuid4().hex}:",
        )
    )
    await instance.ensure()
    assert await instance.acquire()
    yield instance
    await instance.release()
    await instance.close()
