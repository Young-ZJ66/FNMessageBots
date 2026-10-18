# 配置通知渠道

[← 返回 README](../README.md)

至少配置一个推送渠道后，监控事件才会向外部推送。配置方式二选一（或混用：环境变量优先生效，未被环境变量覆盖的项可读 `config.json` / Web 页面）。


## 推送渠道一览

| 渠道 | 说明 | 详细文档 |
| --- | --- | --- |
| 企业微信（可以在微信接收消息） | 群机器人 Webhook | [企业微信](channels/wechat.md) |
| 钉钉 | 群机器人 Webhook | [钉钉](channels/dingtalk.md) |
| 飞书 | 群机器人 Webhook | [飞书](channels/feishu.md) |
| 通用 Webhook | 自定义 HTTP 请求，对接任意平台 | [通用 Webhook](channels/webhook.md) |

> 若需对接其它平台（Telegram、ntfy、Bark、PushPlus、自建推送服务等），用「通用 Webhook」即可。

## 按渠道打开文档

- [企业微信](channels/wechat.md)
- [钉钉](channels/dingtalk.md)
- [飞书](channels/feishu.md)
- [通用 Webhook](channels/webhook.md)

## 环境变量速查

| 环境变量 | 对应配置项 |
| --- | --- |
| `WECHAT_WEBHOOK_URL` 或兼容名 `WEBHOOK_URL` | 企业微信 |
| `DINGTALK_WEBHOOK_URL` | 钉钉 |
| `FEISHU_WEBHOOK_URL` | 飞书 |
| `WEBHOOK_PARAMS` | 通用 Webhook（`webhook_params`） |

> 历史版本支持过 Bark / SMTP 邮件 / PushPlus / MeoW，现已移除；旧 `config.json` 中这些字段会被忽略，可安全删除。
