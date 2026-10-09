from nonebot import get_driver, get_plugin_config
from nonebot.plugin import PluginMetadata

from .commands import register
from .config import Config
from .worker import Worker

__plugin_meta__ = PluginMetadata(
    name="VR/PSP入场订阅",
    description="每30秒查询 VR/PSP 入场事件，按群订阅房间或用户，Redis5持久化去重",
    usage="/入场房间订阅 <房间号>\n/入场用户订阅 <UID>\n"
    "/入场房间订阅取消 <房间号>\n/入场用户订阅取消 <UID>\n/入场订阅",
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

config = get_plugin_config(Config)
worker = Worker(config)
commands = register(worker)
driver = get_driver()
driver.on_startup(worker.start)
driver.on_shutdown(worker.stop)
