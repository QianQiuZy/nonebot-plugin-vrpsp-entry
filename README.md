# nonebot-plugin-vrpsp-entry

NoneBot2 / OneBot v11 的 VR、PSP 入场群订阅插件。默认每 30 秒分别请求：

- `https://vr.qianqiuzy.cn/gift/entry?cache=1`
- `https://psp.qianqiuzy.cn/gift/entry?cache=1`

订阅、消息去重、待发送队列和重试状态持久化到 Redis，兼容 Redis **5.0.14.1**。
只使用独立键前缀，不清空数据库、不迁移数据、不修改 Redis 服务配置。

## 安装与加载

Python 3.10+；在 **NoneBot 宿主的环境** 中安装本仓库：

```bash
python -m pip install /path/to/nonebot-plugin-vrpsp-entry
```

在宿主 `bot.py` 中加载（先初始化 NoneBot、注册 OneBot v11 adapter）：

```python
nonebot.load_plugin("nonebot_plugin_vrpsp_entry")
```

也可将仓库内的 `nonebot_plugin_vrpsp_entry/` 整个目录放入宿主插件目录，由
`nonebot.load_plugins()` 加载；同时安装 `pyproject.toml` 中的运行依赖。
不能把仓库名中的连字符用作 Python 包名，也不能通过直接运行插件启动完整 Bot。

将 `.env.example` 的相关变量加入 **宿主启动目录的 `.env`**，不要覆盖宿主已有配置。
`COMMAND_START=["/"]` 用于接受下列带斜线指令。Redis 默认连接本机 DB0；如有密码，
只在宿主配置中填写 URL。Windows 宿主使用 Windows Python 环境，勿复用 WSL `.venv`。

## 指令与权限

在需要接收通知的群内发送：

```text
/入场房间订阅 1820703922
/入场用户订阅 1048135385
/入场房间订阅取消 1820703922
/入场用户订阅取消 1048135385
/入场订阅
```

房间订阅接收该房间内的访客入场；用户订阅接收该 UID 在 VR、PSP 接口覆盖的所有房间中的入场。
订阅按群隔离，重复订阅不会重置订阅时间。仅推送**订阅创建时间之后**的事件，
因此第一次订阅不会补发最近 10 分钟的旧记录。私聊中不能管理群订阅。

权限唯一来源是 `config.py` 的 `vrpsp_entry_allowed_users`，默认只允许 QQ **308025580**。
可用宿主 `.env` 的 `VRPSP_ENTRY_ALLOWED_USERS=[308025580]` 覆盖。
群主、管理员、NoneBot SUPERUSER 都不会自动获得权限，空白名单表示全部拒绝。
非白名单用户使用管理指令统一收到：`无权限订阅，请联系千秋紫莹`。

`/入场订阅` **向所有群成员开放**，只查询当前群，不要求白名单权限。示例输出：

```text
当前群聊订阅情况
房间订阅：花礼Harei
用户订阅：泽音Melody
```

同类多个订阅以 `、` 分隔，无订阅时显示 `无`。兼容原指令 `/入场房间取消`、
`/入场用户取消`；原 `/入场订阅列表` 提供带 ID 的管理列表，仍受白名单限制。

通知示例：

```text
泽音Melody在22:30:00进入了花礼Harei的直播间
```

未知用户或房间仍允许订阅，通知以 `用户<UID>` / `房间<房间号>` 显示。
但订阅不会使上游增加采集：用户不在上游访客名单、或房间未被上游连接时，不会产生记录。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VRPSP_ENTRY_ALLOWED_USERS` | `[308025580]` | 唯一管理白名单 |
| `VRPSP_ENTRY_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接 |
| `VRPSP_ENTRY_KEY_PREFIX` | `vrpsp_entry:` | 本插件独立键前缀，不可为空 |
| `VRPSP_ENTRY_POLL_SECONDS` | `30` | 轮询间隔，5～300 秒 |
| `VRPSP_ENTRY_HTTP_TIMEOUT` | `10` | 单次请求超时，最多 20 秒 |
| `VRPSP_ENTRY_SOURCES` | `["vr","psp"]` | 启用的数据源 |
| `VRPSP_ENTRY_VR_URL` | `https://vr.qianqiuzy.cn/gift/entry` | VR 入场接口，不带 query |
| `VRPSP_ENTRY_PSP_URL` | `https://psp.qianqiuzy.cn/gift/entry` | PSP 入场接口，不带 query |

VR 暂未部署时可设置 `VRPSP_ENTRY_SOURCES=["psp"]`。两源并行查询，一个返回 404、429、503、
超时或非法 JSON 时另一个仍可正常处理；失败源在下一轮重试。两个已知大陆接口直连，
客户端不继承宿主外网代理。不将开发用本机代理写入业务配置。

## 名称资源与上游契约

`nonebot_plugin_vrpsp_entry/resource/` 内含来自本次 `VR_douchong` 工作树的：

- `entry_users.json`：`[{"uid":1048135385,"name":"花礼Harei"}, ...]`
- `rooms.json`、`psp_rooms.json`：`room_ids` 与 `room_anchors` 名称映射。

资源随包安装，按插件文件所在目录读取，不依赖宿主工作目录。维护名称后重启 Bot。
这里的资源用于显示名称，修改本地名单不能改变服务端实际采集范围。

接口响应必须是：

```json
{
  "mode": "cache",
  "cache": 1,
  "items": [
    {"room_id": 1820703922, "uid": 1203217682, "event_time": "2026-10-08T22:30:00.123+08:00"}
  ]
}
```

记录包含时区，毫秒保留用于去重，显示时间转换为上海时区。缺字段、非法 ID、无时区等
错误响应整批拒绝，不覆盖订阅或去重状态。空 `items` 是正常无事件。

## 去重与发送恢复

每个群以“群号 + 房间号 + UID + 标准化入场时间”标识一条通知。房间订阅和用户订阅重叠、
同一事件反复轮询、VR/PSP 同时返回相同事件，都只入队一次。同 UID 再次进入时若时间不同，
仍视为新的入场；不同群各收到一次。去重键保留 24 小时，超过接口 10 分钟窗口。

Lua 原子检查订阅并创建去重标记与 Redis Stream 任务。Stream 消费组使用 Redis 5 支持的
[`XREADGROUP`](https://redis.io/docs/latest/commands/xreadgroup/)，固定消费者名恢复 pending。
成功收到非零 `message_id` 后才确认并删除任务。发送失败、Bot 离线或缺少有效回执时，
持久化重试，退避 2 秒至最多 300 秒。发送队列独立运行，重试不会阻塞接口轮询。

取消订阅会使该订阅对应的待发送任务失效；若另一条原订阅仍然匹配，则继续发送。
取消后重新订阅产生新标识，不复活旧任务。已经开始向 OneBot 发出的请求无法撤回。
订阅绑定创建它的 Bot QQ；该 Bot 离线时等待其重连，避免误发给另一个 Bot。

60 秒 Redis 租约每 10 秒续期，同一键前缀只允许一个后台工作实例，防止多进程正常运行时
重复投递。多实例必须使用相同配置与数据源，独立 Bot 服务应使用不同键前缀。

正常重复轮询和重启已持久化的任务可以去重。不过 OneBot 没有外部幂等发送事务：
“QQ 已发送成功，但回执丢失或进程在记录成功前崩溃”存在重试重复的极小窗口，无法保证
跨 QQ / Redis 的严格 exactly-once。Redis 持久化与不淘汰配置由宿主维护；本插件不修改它。
持续发送故障会保留任务并增加队列大小，请恢复 Bot 连通性或取消对应订阅。

接口只返回 10 分钟窗口：停机/接口故障后在窗口内恢复可以补抓订阅后的事件，
超过窗口的记录无法从 `cache=1` 补回。首次接入及服务端采集断线之前的记录也无法生成。
宿主和采集端应同步系统时间，否则“订阅后的事件”边界可能受到时钟偏差影响。

## 开发验证

```bash
uv sync --group dev
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
uv build
```

测试自动启动临时目录、随机回环端口的隔离 Redis，不连接部署 Redis，不读取真实 `.env`，
不向真实 QQ 发送。可以指定旧版二进制运行兼容性测试：

```bash
VRPSP_ENTRY_TEST_REDIS_SERVER=/path/to/redis-5.0.14/src/redis-server \
  .venv/bin/python -m pytest -q
```

Linux Redis 5.0.14 的命令兼容性验证不等同于 Windows Redis 5.0.14.1 宿主集成；
实际宿主加载与真实群发送需在部署后验证。本次实际 API 检查优先 PSP，未请求尚未部署的 VR。

本次源仓库检查、测试结果及实际验证范围见 [VALIDATION.md](VALIDATION.md)。
