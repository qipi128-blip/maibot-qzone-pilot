# QQ空间动态管家（MaiBot 插件）

让麦麦拥有自己的 QQ 空间生活：

- **自主发说说**：定时收集最近聊天、历史说说和好友动态灵感，由模型判断“现在想不想发、发什么”，确认后发布到 QQ 空间。
- **看好友动态**：定时读取好友的说说（文字、图片数量、已有评论）。
- **点赞 / 评论**：模型判断某条动态是否值得点赞、是否值得评论；评论默认关闭。
- **回复自己的评论**：定时检查自己说说下的新评论，由模型决定是否回复、回复什么。
- **防刷屏**：活动时间、最小间隔、每日上限、内容去重、已读去重、错误节流。
- **演练模式**：只生成内容 / 点赞 / 评论 / 回复并通知管理员，不真正执行。

> 适用平台：NapCat（Napcat 适配器）。插件直接连接 NapCat 的正向 WebSocket，
> 用 `get_cookies` 获取 QQ 空间 Cookie，不依赖扫码登录；读取和发布通过 QQ 空间
> Web 接口完成。

## 安装

1. 在 MaiBot WebUI 的插件市场安装，或把本仓库克隆/解压到 `MaiBot/plugins/` 下。
2. 确认 `Napcat_Adapter` 已安装并正常工作，机器人 QQ 已登录。
3. 重载插件或重启 MaiBot。
4. 打开插件配置，至少确认：
   - `connection` 能自动读到 NapCat 地址和 token（或手动填写）。
   - `notify.admin_qqs` 填管理员 QQ，便于接收发布 / 错误通知。
   - 想先观察效果时打开 `plugin.dry_run = true`。
   - 确认人设和风格后，再把 `publish.enabled` 打开；它默认关闭，避免刚装上就自动发内容。
   - 想评论好友动态时，再把 `browse.comment_enabled` 打开（默认关闭）。
   - 点赞默认开启，可用 `browse.like_enabled` 关闭；回复自己的评论默认开启，可用 `reply.enabled` 关闭。

插件会自动读取 `plugins/Napcat-Adapter/config.toml` 的 `[napcat_server]`。
如果自动发现失败，手动填写：

```toml
[connection]
ws_url = "ws://127.0.0.1:7998"
access_token = "你的 NapCat token"
auto_discover = false
```

## 工作方式

### 发说说

每 `publish.check_interval_minutes` 分钟检查一次。只有同时满足以下条件才会真正发布：

- 当前在 `active_hours_start` ~ `active_hours_end` 内；
- 距离上次发说说超过 `min_interval_minutes`；
- 今天发布数小于 `max_posts_per_day`；
- 模型判断 `should_post = true`，且置信度不低于 `min_confidence`；
- 内容与最近说说不够相似（字符二元组 Jaccard 去重）。

模型会参考：

- 当前时间、星期；
- 最近 `context_hours` 小时内的**群聊**（最多 `context_message_limit` 条；不会使用私聊内容）；
- 最近发过的说说；
- 最近浏览到的好友动态（只作灵感）。

### 看好友动态 / 点赞 / 评论

每 `browse.check_interval_minutes` 分钟检查一次：

1. 目标来源：`watch_friends` 指定 QQ；为空且 `auto_friends = true` 时自动取好友列表，并轮流检查。
2. 读取每个好友最近 `feeds_per_friend` 条说说。
3. 过滤：已看过、太旧、仅自己可见、部分好友可见、疑似广告。
4. 只有 `browse.like_enabled` 或 `browse.comment_enabled` 开启时才会让模型看候选动态。
5. 点赞受 `max_likes_per_day`、`like_min_confidence` 限制；已经点过赞的动态会写入 `liked_feeds`，不会重复点赞。
6. 评论受 `max_comments_per_day`、`min_comment_interval_minutes`、`min_confidence` 限制；已经评论过的动态不会重复评论。

> **评论默认关闭**。自动评论是社交行为，建议先开 `dry_run` 观察模型输出，
> 再开启 `comment_enabled`，并从小范围 `watch_friends` 开始。

### 回复自己说说下的评论

每 `reply.check_interval_minutes` 分钟检查一次：

1. 读取自己最近 `own_post_limit` 条说说。
2. 找出 `max_comment_age_hours` 内、非自己发的、且自己还没有回复过的新评论。
3. 模型结合原说说和评论内容，决定是否回复、回复什么。
4. 受 `max_replies_per_day`、`max_replies_per_cycle`、`min_confidence` 限制；已经处理过的评论会写入 `reply_seen`，不会重复回复。
5. 回复内容由 QQ 空间自动带上被回复者，不需要手动 @。

## 配置说明

### `[plugin]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否启用插件 |
| `dry_run` | `false` | 演练模式：只通知，不发布 / 评论 |
| `timezone` | `Asia/Shanghai` | 活动时间与统计时区 |

### `[connection]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `ws_url` | 空 | NapCat 正向 WS；留空自动读取适配器配置 |
| `access_token` | 空 | NapCat token；留空自动读取 |
| `auto_discover` | `true` | 自动发现 |
| `action_timeout_seconds` | `20` | NapCat 动作超时 |
| `cookie_ttl_minutes` | `20` | Cookie 缓存时间，过期自动刷新 |

### `[llm]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `task_name` | `utils` | 模型任务名 |
| `model_name` | 空 | 指定模型；留空用任务模型 |
| `max_tokens` | `700` | 最大输出 token |
| `publish_temperature` | `0.85` | 发说说温度 |
| `comment_temperature` | `0.7` | 评论温度 |

### `[publish]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 是否自动发说说；确认风格后再开启 |
| `check_interval_minutes` | `60` | 检查间隔 |
| `active_hours_start` / `active_hours_end` | `08:00` / `23:30` | 活动时间 |
| `min_interval_minutes` | `120` | 两条说说最小间隔 |
| `max_posts_per_day` | `3` | 每日上限 |
| `min_confidence` | `0.6` | 最低“想发”置信度 |
| `context_hours` | `6` | 参考最近多少小时聊天 |
| `context_message_limit` | `60` | 最多参考多少条 |
| `content_max_chars` | `180` | 正文最大长度 |
| `ugc_right` | `1` | 可见范围：1 所有人、4 好友、64 仅自己 |
| `initial_delay_seconds` | `45` | 加载后首次检查延迟 |
| `jitter_seconds` | `120` | 每轮随机抖动 |

### `[browse]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否浏览好友动态 |
| `check_interval_minutes` | `30` | 浏览间隔 |
| `active_hours_start` / `active_hours_end` | `08:00` / `23:30` | 活动时间 |
| `watch_friends` | `[]` | 指定好友 QQ；为空则用好友列表 |
| `auto_friends` | `true` | 是否自动取好友列表 |
| `max_friends_per_cycle` | `3` | 每轮最多几个好友 |
| `feeds_per_friend` | `3` | 每人几条 |
| `max_candidates_per_cycle` | `6` | 每轮最多让模型看几条候选 |
| `max_feed_age_hours` | `72` | 只考虑多少小时内 |
| `like_enabled` | `true` | 是否让模型决定点赞 |
| `max_likes_per_day` | `5` | 每日点赞上限 |
| `like_min_confidence` | `0.7` | 点赞最低置信度 |
| `comment_enabled` | `false` | 是否真的评论 |
| `max_comments_per_day` | `2` | 每日评论上限 |
| `min_comment_interval_minutes` | `30` | 评论最小间隔 |
| `min_confidence` | `0.72` | 评论最低置信度 |
| `skip_if_commented` | `true` | 自己已评论过就跳过 |
| `comment_max_chars` | `80` | 评论最大长度 |

### `[reply]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否自动回复自己说说下的新评论 |
| `check_interval_minutes` | `15` | 检查间隔 |
| `active_hours_start` / `active_hours_end` | `08:00` / `23:30` | 活动时间 |
| `own_post_limit` | `5` | 每轮检查最近几条自己的说说 |
| `max_post_age_hours` | `168` | 只检查多少小时内发布的说说 |
| `max_comment_age_hours` | `48` | 只回复多少小时内的评论 |
| `max_comments_per_cycle` | `3` | 每轮最多让模型看几条候选评论 |
| `max_replies_per_day` | `3` | 每日回复上限 |
| `max_replies_per_cycle` | `1` | 每轮最多回复几条 |
| `min_confidence` | `0.7` | 回复最低置信度 |
| `reply_max_chars` | `100` | 回复最大长度 |
| `initial_delay_seconds` | `120` | 加载后首次检查延迟 |
| `jitter_seconds` | `120` | 每轮随机抖动 |

### `[notify]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否通知 |
| `admin_qqs` | `[]` | 管理员 QQ |
| `notify_on_post` | `true` | 发说说后通知 |
| `notify_on_like` | `true` | 点赞后通知 |
| `notify_on_comment` | `true` | 评论后通知 |
| `notify_on_reply` | `true` | 回复评论后通知 |
| `notify_on_error` | `true` | 错误通知（5 分钟节流） |

### `[prompt]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `persona_override` | 空 | 自定义人设；留空读取主程序人格 |
| `publish_style` | 空 | 发说说额外风格 |
| `comment_style` | 空 | 评论额外风格 |
| `reply_style` | 空 | 回复评论额外风格 |

### `[qzone]`

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `request_timeout_seconds` | `20` | HTTP 超时 |
| `retry_times` | `1` | Cookie 失效时重试次数 |
| `history_limit` | `30` | 保留历史条数 |

## 命令

命令只在私聊生效，发送者必须在 `notify.admin_qqs` 中（本机控制台操作员除外）。

| 命令 | 说明 |
| --- | --- |
| `/空间状态` | 查看连接、今日计数、上次发布 / 评论 |
| `/发说说 [主题]` | 立即让模型生成并发布一条说说 |
| `/看动态 [QQ] [数量]` | 读取好友动态，不评论 |
| `/评论动态 <QQ> <tid> <内容>` | 手动评论一条动态 |

## 工具

插件向主模型暴露：

- `qzone_publish_moment(topic)`：生成并发布说说；
- `qzone_read_moments(target_qq, limit)`：读取好友动态摘要。

## 安全与隐私

- Cookie 只保存在内存中，不写入 `state.json`。
- 插件只把说说正文、作者昵称、评论内容交给模型；不读取手机号、邮箱等隐私资料。
- 发说说时只参考群聊内容，不读取私聊，降低把私密对话带到公开空间的风险。
- 评论默认关闭；建议先开演练模式。
- 不要设置过于激进的间隔；QQ 空间接口有频率限制，过快可能触发风控。
- 目前只支持纯文字说说，不支持配图。
- QQ 空间 Web 接口属于非公开接口，可能随版本变化失效；插件会记录错误并通知管理员。

## 常见问题

**提示“NapCat 未连接”或读取 Cookie 失败？**

1. 确认 NapCat 已登录、适配器能正常收发消息。
2. 检查 `ws_url` / `access_token`。
3. 确认机器人账号能正常打开 QQ 空间；部分新号或受限号可能拿不到 `p_skey`。

**一直不发说说？**

先看日志里模型的 `reason`：可能是模型判断“现在不适合发”，或置信度不足、内容与历史太像、当天已达上限、不在活动时间。

**评论没有生效？**

默认 `comment_enabled = false`；还需要 `watch_friends` 或 `auto_friends` 能拿到好友，并且动态在 `max_feed_age_hours` 内、未被看过、不是广告。

**Cookie 过期？**

插件每 `cookie_ttl_minutes` 分钟自动刷新；请求失败且返回登录错误时会刷新并重试一次。

## 致谢

QQ 空间 Web 接口的字段与请求格式参考了 [Maizone](https://github.com/internetsb/Maizone)（MIT）和 qzone-toolkit 的开源实现，特此致谢。

## License

MIT
