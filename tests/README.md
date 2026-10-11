# 测试

针对 `src/` 下真实模块的回归测试。**不需要**安装第三方依赖、不需要网络：
`_support/stub_deps.py` 提供 `requests` / `urllib3` / `docker` / `psutil` / `flask` / `croniter`
的最小桩，仅用于让模块可导入与断言请求发往何处，不模拟真实 HTTP 行为。

## 运行

```bash
# 全部（任选其一）
python -m unittest discover -s tests -p "test_*.py"
python tests/run_all.py

# 单个
python tests/test_db_log_poller.py
```

测试会在 `tests/_tmp/` 下创建临时库与状态文件，结束后自动清理（该目录已被 `.gitignore` 忽略）。

## 覆盖范围

| 文件 | 覆盖内容 |
| --- | --- |
| `test_db_log_poller.py` | 主日志轮询器的游标语义：脏行不再卡住游标、单行异常跳过、`MAX(id)` 读取失败不退回 0、失败重试、每轮行数上限、批量投递失败重试上限 |
| `test_notifier_disk_merge.py` | 磁盘事件「入队 ≠ 已发送」语义（`skipped=deferred`）、合并发送成功后补记推送统计与推送记录、回调异常不影响推送、`push_stats.json` 原子写入 |
| `test_structured_push.py` | 结构化消息：企业微信 markdown、钉钉 markdown（带 title）、飞书 post；渠道拒绝富文本时自动回退纯文本；Bark/PushPlus 仍为纯文本；标题与正文默认去 emoji；纯文本正文格式不被 markdown 改动影响 |
| `test_url_redaction.py` | 日志/接口输出脱敏：webhook key、`access_token`、URL 路径中的令牌、URL userinfo；PushPlus 走 HTTPS |
| `test_timezone_unification.py` | 展示时间统一由 `DISPLAY_TIMEZONE` 决定；各轮询器在同一时区下输出一致；`push_stats` 当日键跟随该时区 |
| `test_event_processor_logging.py` | 「会推送的事件同时落原始日志」的约定（含扫描式守卫，新增事件漏落盘会被测出） |
| `test_media_delete_events.py` | 影视库 `TRIM_RESOURCE_DELETED`：`media_delete` 表列名自适应、无表/无时间列时静默降级、水位推进不重复、入库/刮削事件不受影响 |
| `test_web_access_guard.py` | `FNMB_DISABLE_AUTH` 必须配合 `FNMB_DISABLE_AUTH_ACK` 才生效；初始化码与登录限流的既有行为不变 |
| `test_config_fields.py` | 新增配置项默认值、布尔字符串归一化（`"0"` 不得被当真实值）、环境变量优先级、热加载成功与校验失败回滚 |
| `test_dedup_store.py` | 共享去重缓存语义：TTL 过滤、损坏文件容错、键值类型归一、UTF-8 落盘；6 个轮询器确实委托给同一实现（源码级断言） |
| `test_state_file_encoding.py` | 状态/游标文件必须按 UTF-8 读写（含中文时不得因 locale 变成 GBK 导致状态失效）；全部状态 IO 的源码级守卫 |
| `test_channel_capability_memory.py` | 渠道富文本能力记忆：格式类拒绝才记忆、后续直接发纯文本；网络/鉴权/限流/超时**不**记忆；重置可重新探测；进程级记忆跨实例生效，且不跨用例泄漏 |
| `test_payload_limits.py` | 按官方文档核对的各渠道消息体上限：企业微信 ≤4096 字节、钉钉 `text` ≤5000 字符（含预留标题）、飞书请求体 ≤20KB；超长按 UTF-8 边界安全截断并标注；飞书 `post` 结构符合官方 `content.post.zh_cn.{title,content}` |
| `test_schema_probe.py` | 各飞牛库的 schema 自检：实际列打 INFO、缺失表/列打 WARNING、换库重新自检、`media_delete` 时间列变体接受、注册表完整性 |
| `test_retired_channels.py` | 已移除渠道（魔法推送、企业微信应用）的残留配置优雅降级：不报错、不改动用户文件、启动提示；残留环境变量同理；渠道清单多处副本（UI / config_store / 校验器）保持一致；`config.json` 必须按 UTF-8 读（含 GBK 回退） |
| `test_channel_registry.py` | 渠道分发注册表：每个 `param_attr` 都是真实构造参数（拼错会导致渠道被静默跳过）、三个发送入口都走同一分发函数、未配置渠道跳过、Bark/PushPlus 用 Bark 形态消息、结果顺序与注册表一致 |
| `test_repo_hygiene.py` | 仓库卫生：界面/文档不得含上游或捐赠内容（上游署名仅允许留在 `LICENSE` 与 README 许可证小节，MIT 强制）；`/support` 页面与收款码已彻底移除；agent/IDE 本地文件不在仓库且被 ignore；模板与文档引用的本地图片都存在、无孤儿图片、无「扩展名写错」、无指向已删文件的悬空链接；workflows 只剩构建流程 |

## 已知覆盖缺口

- **`src/web/ui_app.py` 无法导入**：`_support/stub_deps.py` 的 flask 桩不支持
  `from flask import Flask`（`_make_stub` 未实现模块级 `__getattr__`），因此整个 Web UI
  模块目前**没有测试覆盖**。相关约定（如渠道清单一致性）只能在 `test_retired_channels.py`
  里用源码解析间接验证。若要补 UI 测试，需先给 flask 桩加上按属性返回可调用对象的
  `__getattr__`。

## 注意

- 这些测试验证的是**逻辑与约定**，不能替代在真实飞牛 NAS / 容器环境上的联调（数据库 schema、权限、journal、Docker socket 等）。
- 若某个测试因为读取到陈旧的 `__pycache__` 字节码而表现异常，先清理缓存再跑：
  `Get-ChildItem -Recurse -Path src,tests -Filter __pycache__ -Directory | Remove-Item -Recurse -Force`
