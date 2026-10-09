# 本次检查与验证（2026-10-08，WSL）

## VR_douchong 未提交部分

只检查源仓库，未修改或提交其源码与配置。检查涵盖新增入场模型、写入与归档仓库、
两个 WebSocket 回调、名单加载、启动与退出、Redis 缓存生成、API 参数及响应、相关测试。

新增契约可以供本插件接入：`GET /gift/entry?cache=1` 返回最近 10 分钟的
`room_id / uid / event_time`，事件时间带上海时区，保留毫秒。插件不依赖 MySQL 或源仓库导入。

相关检查共 **83 passed，6 skipped**：

```bash
.venv/bin/python -m pytest -q \
  tests/test_entry_api.py tests/test_entry_events.py \
  tests/test_api_cache_integration.py tests/test_schema_contract.py tests/test_import_contract.py
# 68 passed, 6 skipped

.venv/bin/python -m pytest -q \
  tests/test_queue_pool_todo4_characterization.py tests/test_queue_pool_todo4_regression.py
# 15 passed
```

6 项跳过需要独立数据库/Redis 集成环境；本次未运行真实 MySQL 归档或实际 B 站采集。
源仓库现有 `.venv` 开启 `include-system-site-packages`，其隔离性有限；未擅自重建。

接入边界：上游只记录访客名单命中且实际连接房间中的入场，排除主播进入自己的房间。
匿名、断线、未部署之前的入场无法补齐；未知房主 UID 时暂不记录。
因此“用户在所有直播间的入场”指 VR / PSP 当前采集覆盖的房间，而不是 B 站全站。

## 新插件

使用本仓库新建的隔离 Python 3.10.20 环境，未复用源仓库环境或 Windows 环境。

- 新编译 Linux Redis **5.0.14**，临时目录与随机回环端口启动测试实例，不接触部署 Redis。
- 实际 Lua、Stream、订阅持久化、pending 恢复、租约、去重与发送重试测试：**71 passed**。
- NoneBug 真实指令匹配测试验证群主、管理员及 SUPERUSER 均不能越过白名单。
- `ruff check .` 与 `ruff format --check .` 通过。
- `uv build` 成功生成 wheel 和源码包。
- wheel 含三个 JSON 文件；解析内容与源仓库一致，包含 **164** 个访客、**80** 个房间配置。
- 默认请求间隔 30 秒；默认白名单仅 `308025580`；配置与名称按插件路径读取。

去重验证覆盖：房间与用户订阅重叠、并发重复入队、重复轮询、VR/PSP 返回同事件、
同群两个 Bot、不同群各自通知、不同毫秒的再次入场、取消后重新订阅、失败任务恢复。

补充指令验证：`/入场房间订阅取消`、`/入场用户订阅取消` 和原取消别名均能匹配，
非白名单仍被拒绝；`/入场订阅` 对普通成员、管理员及群主开放，按指定格式显示名称，
空订阅显示 `无`，私聊无法查询群订阅。

## 实际接口检查与未验证范围

优先请求 PSP，未请求尚未部署的 VR：

```text
https://psp.qianqiuzy.cn/gift/entry?cache=1
HTTP 200
{"mode":"cache","cache":1,"items":[]}
```

使用插件自身 HTTP 客户端再次检查成功，解析结果也是 0 条。因此真实接口验证确认了
连通性和空响应契约；带记录的入场推送由本地模拟数据验证，尚未观测到真实入场记录。

未验证 Windows Redis **5.0.14.1** 宿主加载、实际 NoneBot 部署及真实 QQ 群发送。
OneBot 成功发送与 Redis 记录成功之间不存在跨系统事务，异常断电或回执丢失仍可能
造成重试重复；正常重复轮询、交叉订阅及已持久化任务重启均已验证去重。
