"""QQ 空间动态管家插件配置模型。"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "cloud"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    dry_run: bool = Field(
        default=False,
        description="演练模式：只生成内容 / 评论并通知管理员，不真正发到 QQ 空间",
    )
    timezone: str = Field(default="Asia/Shanghai", description="活动时间与统计使用的时区")
    config_version: str = Field(default="1.2.0", description="配置版本")


class ConnectionConfig(PluginConfigBase):
    """NapCat 连接配置。"""

    __ui_label__ = "NapCat 连接"
    __ui_icon__ = "plug"
    __ui_order__ = 1

    ws_url: str = Field(
        default="",
        description="NapCat 正向 WebSocket 地址，例如 ws://127.0.0.1:7998；留空自动读取 Napcat 适配器配置",
    )
    access_token: str = Field(
        default="",
        description="NapCat WebSocket 访问令牌；留空自动读取",
    )
    auto_discover: bool = Field(
        default=True,
        description="自动从 plugins/Napcat-Adapter/config.toml 读取连接信息",
    )
    action_timeout_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=120.0,
        description="调用 NapCat 动作的超时时间（秒）",
    )
    reconnect_initial_seconds: float = Field(default=2.0, ge=0.5, le=60.0, description="初始重连等待")
    reconnect_max_seconds: float = Field(default=60.0, ge=5.0, le=600.0, description="最大重连等待")
    cookie_ttl_minutes: int = Field(
        default=20,
        ge=1,
        le=240,
        description="QQ 空间 Cookie 缓存时间；过期后自动重新获取",
    )


class LlmConfig(PluginConfigBase):
    """模型调用配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "sparkles"
    __ui_order__ = 2

    task_name: str = Field(default="utils", description="模型任务名，例如 utils / replyer / tool_use")
    model_name: str = Field(default="", description="指定模型名称；留空使用 task_name 对应模型")
    max_tokens: int = Field(default=700, ge=128, le=4096, description="最大输出 token")
    publish_temperature: float = Field(default=0.85, ge=0.0, le=2.0, description="发说说时的温度")
    comment_temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="评论时的温度")


class PublishConfig(PluginConfigBase):
    """自动发说说配置。"""

    __ui_label__ = "发说说"
    __ui_icon__ = "send"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="是否让模型自主决定发说说；确认风格后再开启")
    check_interval_minutes: int = Field(default=60, ge=5, le=1440, description="后台检查间隔（分钟）")
    active_hours_start: str = Field(default="08:00", description="允许发说说的开始时间 HH:MM")
    active_hours_end: str = Field(default="23:30", description="允许发说说的结束时间 HH:MM")
    min_interval_minutes: int = Field(default=120, ge=5, le=10080, description="两次发说说的最小间隔")
    max_posts_per_day: int = Field(default=3, ge=1, le=50, description="每天最多发几条")
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0, description="模型“想发”的最低置信度")
    context_hours: float = Field(default=6.0, ge=0.5, le=72.0, description="参考最近多少小时的聊天")
    context_message_limit: int = Field(default=60, ge=0, le=300, description="最多参考多少条聊天消息")
    content_max_chars: int = Field(default=180, ge=20, le=1000, description="说说正文最大长度")
    ugc_right: int = Field(
        default=1,
        ge=1,
        le=128,
        description="说说可见范围：1 所有人，4 好友，64 仅自己（暂不支持 16/128）",
    )
    initial_delay_seconds: int = Field(default=45, ge=0, le=3600, description="插件加载后首次检查的延迟")
    jitter_seconds: int = Field(default=120, ge=0, le=3600, description="每轮检查的随机抖动上限")


class BrowseConfig(PluginConfigBase):
    """浏览好友动态与评论配置。"""

    __ui_label__ = "好友动态"
    __ui_icon__ = "users"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否浏览好友的 QQ 空间动态")
    check_interval_minutes: int = Field(default=30, ge=5, le=1440, description="浏览间隔（分钟）")
    active_hours_start: str = Field(default="08:00", description="允许浏览的开始时间 HH:MM")
    active_hours_end: str = Field(default="23:30", description="允许浏览的结束时间 HH:MM")
    watch_friends: list[str] = Field(
        default_factory=list,
        description="指定要浏览的 QQ 号；留空且 auto_friends=true 时自动取好友列表",
    )
    auto_friends: bool = Field(default=True, description="watch_friends 为空时是否自动使用好友列表")
    max_friends_per_cycle: int = Field(default=3, ge=1, le=50, description="每轮最多检查几个好友")
    feeds_per_friend: int = Field(default=3, ge=1, le=20, description="每个好友最多取几条动态")
    max_candidates_per_cycle: int = Field(default=6, ge=1, le=30, description="每轮最多让模型看几条候选动态")
    max_feed_age_hours: float = Field(default=72.0, ge=1.0, le=720.0, description="只考虑多少小时内的动态")
    like_enabled: bool = Field(
        default=True,
        description="是否让模型决定点赞好友动态",
    )
    max_likes_per_day: int = Field(default=5, ge=1, le=100, description="每天最多点赞几条")
    like_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0, description="点赞的最低置信度")
    comment_enabled: bool = Field(
        default=False,
        description="是否让模型决定评论；默认关闭，确认风格后再开启",
    )
    max_comments_per_day: int = Field(default=2, ge=1, le=50, description="每天最多评论几条")
    min_comment_interval_minutes: int = Field(default=30, ge=1, le=1440, description="两次评论的最小间隔")
    min_confidence: float = Field(default=0.72, ge=0.0, le=1.0, description="评论的最低置信度")
    skip_if_commented: bool = Field(default=True, description="自己已经评论过的动态跳过")
    comment_max_chars: int = Field(default=80, ge=5, le=300, description="评论最大长度")
    initial_delay_seconds: int = Field(default=90, ge=0, le=3600, description="插件加载后首次浏览的延迟")
    jitter_seconds: int = Field(default=180, ge=0, le=3600, description="每轮浏览的随机抖动上限")


class ReplyConfig(PluginConfigBase):
    """回复自己说说下评论的配置。"""

    __ui_label__ = "回复评论"
    __ui_icon__ = "message-circle-reply"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否自动回复自己说说下的新评论")
    check_interval_minutes: int = Field(default=15, ge=5, le=1440, description="检查自己说说评论的间隔（分钟）")
    active_hours_start: str = Field(default="08:00", description="允许回复的开始时间 HH:MM")
    active_hours_end: str = Field(default="23:30", description="允许回复的结束时间 HH:MM")
    own_post_limit: int = Field(default=5, ge=1, le=20, description="每轮检查最近几条自己的说说")
    max_post_age_hours: float = Field(default=168.0, ge=1.0, le=720.0, description="只检查多少小时内发布的说说")
    max_comment_age_hours: float = Field(default=48.0, ge=1.0, le=720.0, description="只回复多少小时内的评论")
    max_comments_per_cycle: int = Field(default=3, ge=1, le=20, description="每轮最多让模型看几条候选评论")
    max_replies_per_day: int = Field(default=3, ge=1, le=50, description="每天最多回复几条评论")
    max_replies_per_cycle: int = Field(default=1, ge=1, le=5, description="每轮最多回复几条评论")
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0, description="回复的最低置信度")
    reply_max_chars: int = Field(default=100, ge=5, le=300, description="回复最大长度")
    initial_delay_seconds: int = Field(default=120, ge=0, le=3600, description="插件加载后首次检查的延迟")
    jitter_seconds: int = Field(default=120, ge=0, le=3600, description="每轮检查的随机抖动上限")


class NotifyConfig(PluginConfigBase):
    """管理员通知配置。"""

    __ui_label__ = "通知"
    __ui_icon__ = "bell"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否向管理员发送通知")
    admin_qqs: list[str] = Field(default_factory=list, description="接收通知的 QQ 号")
    notify_on_post: bool = Field(default=True, description="发说说后通知")
    notify_on_like: bool = Field(default=True, description="点赞后通知")
    notify_on_comment: bool = Field(default=True, description="评论后通知")
    notify_on_reply: bool = Field(default=True, description="回复评论后通知")
    notify_on_error: bool = Field(default=True, description="出错时通知")


class PromptConfig(PluginConfigBase):
    """提示词扩展。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "message-square"
    __ui_order__ = 7

    persona_override: str = Field(default="", description="自定义人设；留空读取主程序人格")
    publish_style: str = Field(
        default="",
        description="发说说的额外风格要求，例如：短句、不要 emoji、不要话题标签",
    )
    comment_style: str = Field(
        default="",
        description="评论的额外风格要求，例如：像朋友随口接一句，不要夸张",
    )
    reply_style: str = Field(
        default="",
        description="回复评论的额外风格要求，例如：自然、简短、不要客服腔",
    )


class QZoneConfig(PluginConfigBase):
    """QQ 空间 HTTP 请求配置。"""

    __ui_label__ = "QQ 空间"
    __ui_icon__ = "globe"
    __ui_order__ = 8

    request_timeout_seconds: float = Field(default=20.0, ge=5.0, le=120.0, description="单次 HTTP 请求超时")
    retry_times: int = Field(default=1, ge=0, le=3, description="Cookie 失效时的重试次数")
    history_limit: int = Field(default=30, ge=5, le=100, description="保留多少条说说 / 评论历史用于去重")


class QZonePilotConfig(PluginConfigBase):
    """QQ 空间动态管家配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    connection: ConnectionConfig = Field(default_factory=ConnectionConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    browse: BrowseConfig = Field(default_factory=BrowseConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    qzone: QZoneConfig = Field(default_factory=QZoneConfig)
