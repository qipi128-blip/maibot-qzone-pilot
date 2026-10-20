"""QQ 空间 Web 接口客户端。

只实现插件需要的三个能力：
- 读取指定 QQ 的说说列表（含文字、图片 URL、评论）
- 发表纯文字说说
- 评论指定说说

Cookie 与 ``g_tk`` 由 NapCat 的 ``get_cookies`` 动作提供，插件不再单独扫码登录。
相关接口格式参考了 Maizone（MIT）与 QQ 空间 Web 端的实际请求。
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp

_FEED_EMOJI_RE = re.compile(r"\[em\](?:e\d+|[\w-]+)\[/em\]", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class QZoneError(RuntimeError):
    """QQ 空间接口调用失败。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        """初始化错误。

        Args:
            message: 错误描述。
            code: QQ 空间返回码（如果有）。
        """

        super().__init__(message)
        self.code = code


@dataclass
class QZoneAuth:
    """QQ 空间登录凭据。"""

    cookies: dict[str, str]
    gtk: str
    uin: str
    nickname: str = ""

    @classmethod
    def from_cookies(cls, cookies_text: str, bkn: str = "") -> "QZoneAuth":
        """从 NapCat 返回的 Cookie 字符串构造凭据。

        Args:
            cookies_text: ``key=value; key=value`` 格式的 Cookie。
            bkn: NapCat 计算好的 g_tk；为空时从 p_skey / skey 计算。

        Returns:
            QZoneAuth: 登录凭据。

        Raises:
            QZoneError: 缺少 uin 或 skey 时。
        """

        cookies = parse_cookie_string(cookies_text)
        raw_uin = str(cookies.get("uin") or cookies.get("p_uin") or "").strip()
        uin = re.sub(r"^[oO]?0*", "", raw_uin)
        if not uin.isdigit():
            raise QZoneError("QQ 空间 Cookie 中缺少有效的 uin，请检查 NapCat 登录状态")

        gtk = str(bkn or "").strip()
        if not gtk:
            skey = str(cookies.get("p_skey") or cookies.get("skey") or "").strip()
            if not skey:
                raise QZoneError("QQ 空间 Cookie 中缺少 p_skey / skey，无法计算 g_tk")
            gtk = generate_gtk(skey)

        return cls(cookies=cookies, gtk=gtk, uin=uin, nickname=str(cookies.get("ptnick_" + uin) or "").strip())


@dataclass
class FeedComment:
    """一条说说评论。"""

    uin: str
    nickname: str
    content: str
    tid: int | None = None
    created_time: str = ""
    parent_tid: int | None = None
    is_self: bool = False


@dataclass
class Feed:
    """一条 QQ 空间说说。"""

    target_qq: str
    tid: str
    author: str = ""
    created_at: int = 0
    content: str = ""
    images: list[str] = field(default_factory=list)
    comments: list[FeedComment] = field(default_factory=list)
    rt_con: str = ""
    secret: int = 0
    ugc_right: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def created_text(self) -> str:
        """返回可读发布时间。"""

        if self.created_at <= 0:
            return "未知时间"
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        except (OverflowError, OSError, ValueError):
            return "未知时间"

    def self_commented(self, self_uin: str) -> bool:
        """判断自己是否已经评论过这条说说。"""

        normalized = str(self_uin or "").strip()
        return any(comment.is_self or (normalized and comment.uin == normalized) for comment in self.comments)

    def to_prompt_text(self, *, max_content: int = 500, include_comments: bool = True) -> str:
        """转换为提示词中的动态描述。"""

        lines = [
            f"作者：{self.author or self.target_qq}（QQ {self.target_qq}）",
            f"发布时间：{self.created_text}",
            f"内容：{self.content[:max_content] or '（空）'}",
        ]
        if self.rt_con:
            lines.append(f"转发内容：{self.rt_con[:max_content]}")
        if self.images:
            lines.append(f"图片：{len(self.images)} 张")
        if include_comments and self.comments:
            lines.append("已有评论：")
            for comment in self.comments[:8]:
                prefix = "（我）" if comment.is_self else ""
                lines.append(f"- {prefix}{comment.nickname or comment.uin}：{comment.content[:120]}")
        return "\n".join(lines)


def generate_gtk(skey: str) -> str:
    """按 QQ 空间 Web 端算法生成 g_tk。"""

    hash_value = 5381
    for char in str(skey):
        hash_value += (hash_value << 5) + ord(char)
    return str(hash_value & 2147483647)


def parse_cookie_string(cookies_text: str) -> dict[str, str]:
    """解析 Cookie 字符串。"""

    cookies: dict[str, str] = {}
    for part in str(cookies_text or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


def _decode_text(value: Any) -> str:
    """清理说说文本中的表情标记与 HTML 标签。"""

    text = str(value or "")
    text = _FEED_EMOJI_RE.sub("[表情]", text)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _safe_int(value: Any, default: int = 0) -> int:
    """安全转换为整数。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_jsonp(text: str) -> Any:
    """解析 JSONP 或普通 JSON。"""

    payload_text = str(text or "").strip()
    if not payload_text:
        raise QZoneError("QQ 空间返回了空响应")

    if not payload_text.startswith(("{", "[")):
        start = payload_text.find("(")
        end = payload_text.rfind(")")
        if start >= 0 and end > start:
            payload_text = payload_text[start + 1 : end].strip()

    payload_text = payload_text.replace("undefined", "null")
    return json.loads(payload_text)


def _extract_callback_code(text: str) -> int | None:
    """从 QQ 空间 HTML / JS 回调中提取 code。"""

    callback_match = re.search(r"frameElement\.callback\(", text)
    if callback_match:
        payload = _extract_balanced_object(text[callback_match.end() :])
        if isinstance(payload, dict) and "code" in payload:
            return _safe_int(payload.get("code"), default=-1)

    match = re.search(r'"code"\s*:\s*(-?\d+)', text)
    if match:
        return _safe_int(match.group(1), default=-1)
    return None


def _extract_balanced_object(text: str) -> dict[str, Any] | None:
    """从一段文本中提取第一个平衡的 JSON 对象。"""

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start : index + 1])
                except Exception:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


class QZoneClient:
    """QQ 空间 Web 客户端。"""

    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"

    def __init__(
        self,
        auth: QZoneAuth,
        *,
        logger: Any,
        timeout: float = 20.0,
        urls: dict[str, str] | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            auth: QQ 空间登录凭据。
            logger: 日志对象。
            timeout: HTTP 请求超时（秒）。
            urls: 可选的接口地址覆盖，主要用于测试。
        """

        self.auth = auth
        self._logger = logger
        self._timeout = max(3.0, float(timeout))
        urls = urls or {}
        self._list_url = str(urls.get("list") or self.LIST_URL)
        self._publish_url = str(urls.get("publish") or self.PUBLISH_URL)
        self._comment_url = str(urls.get("comment") or self.COMMENT_URL)

    @property
    def uin(self) -> str:
        """当前登录 QQ。"""

        return self.auth.uin

    @property
    def nickname(self) -> str:
        """当前登录昵称（可能为空）。"""

        return self.auth.nickname

    async def get_feeds(
        self,
        target_qq: str,
        *,
        num: int = 5,
        filter_commented: bool = False,
    ) -> list[Feed]:
        """读取指定 QQ 的说说列表。

        Args:
            target_qq: 目标 QQ。
            num: 获取条数。
            filter_commented: 是否过滤掉自己已经评论过的说说。

        Returns:
            list[Feed]: 说说列表。

        Raises:
            QZoneError: 未登录、无权限或响应异常时。
        """

        target = str(target_qq or "").strip()
        if not target.isdigit():
            raise QZoneError(f"无效的目标 QQ：{target_qq}")

        params = {
            "g_tk": self.auth.gtk,
            "uin": target,
            "ftype": 0,
            "sort": 0,
            "pos": 0,
            "num": max(1, min(20, int(num))),
            "replynum": 100,
            "callback": "_preloadCallback",
            "code_version": 1,
            "format": "jsonp",
            "need_comment": 1,
            "need_private_comment": 1,
        }
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": f"https://user.qzone.qq.com/{target}",
            "Connection": "keep-alive",
        }
        text = await self._request_text("GET", self._list_url, params=params, headers=headers)
        try:
            payload = _extract_jsonp(text)
        except Exception as exc:
            raise QZoneError(f"解析说说列表失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise QZoneError("说说列表响应格式异常")

        code = _safe_int(payload.get("code"), default=-1)
        if code != 0:
            message = str(payload.get("message") or payload.get("error") or "未知错误")
            raise QZoneError(f"获取说说列表失败（code={code}）：{message}", code=code)

        login_info = payload.get("logininfo")
        if isinstance(login_info, dict):
            nickname = str(login_info.get("name") or "").strip()
            if nickname:
                self.auth.nickname = nickname

        feeds: list[Feed] = []
        for message in payload.get("msglist") or []:
            if not isinstance(message, dict):
                continue
            feed = self._parse_feed(message, target)
            if feed is None:
                continue
            if filter_commented and feed.self_commented(self.auth.uin):
                continue
            feeds.append(feed)
        return feeds

    async def publish(self, content: str, *, ugc_right: int = 1) -> str:
        """发表一条纯文字说说。

        Args:
            content: 说说正文。
            ugc_right: 可见范围，1 所有人、4 好友、64 仅自己。

        Returns:
            str: 说说 tid。

        Raises:
            QZoneError: 发布失败时。
        """

        text = str(content or "").strip()
        if not text:
            raise QZoneError("说说内容不能为空")
        if ugc_right not in {1, 4, 64}:
            raise QZoneError("可见范围仅支持 1（所有人）、4（好友）或 64（仅自己）")

        data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": str(ugc_right),
            "to_sign": "0",
            "hostuin": self.auth.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.auth.uin}",
        }
        params = {"g_tk": self.auth.gtk, "uin": self.auth.uin}
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": f"https://user.qzone.qq.com/{self.auth.uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        text_response = await self._request_text("POST", self._publish_url, params=params, data=data, headers=headers)
        try:
            payload = json.loads(text_response)
        except Exception:
            payload = _extract_balanced_object(text_response)
            if payload is None:
                try:
                    payload = _extract_jsonp(text_response)
                except Exception as exc:
                    raise QZoneError(f"解析发表说说响应失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise QZoneError("发表说说响应格式异常")

        code = _safe_int(payload.get("code"), default=-1)
        if code != 0:
            message = str(payload.get("message") or payload.get("error") or "未知错误")
            raise QZoneError(f"发表说说失败（code={code}）：{message}", code=code)
        tid = str(payload.get("tid") or "").strip()
        if not tid:
            raise QZoneError("发表说说成功但响应缺少 tid")
        return tid

    async def comment(self, fid: str, target_qq: str, content: str) -> None:
        """评论一条说说。

        Args:
            fid: 说说 ID（tid）。
            target_qq: 说说作者 QQ。
            content: 评论内容。

        Raises:
            QZoneError: 评论失败时。
        """

        feed_id = str(fid or "").strip()
        target = str(target_qq or "").strip()
        text = str(content or "").strip()
        if not feed_id or not target.isdigit():
            raise QZoneError("评论参数不完整")
        if not text:
            raise QZoneError("评论内容不能为空")

        data = {
            "topicId": f"{target}_{feed_id}__1",
            "uin": self.auth.uin,
            "hostUin": target,
            "feedsType": 100,
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "format": "fs",
            "ref": "feeds",
            "content": text,
        }
        params = {"g_tk": self.auth.gtk}
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": f"https://user.qzone.qq.com/{self.auth.uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        response_text = await self._request_text("POST", self._comment_url, params=params, data=data, headers=headers)
        code = _extract_callback_code(response_text)
        if code is None:
            snippet = re.sub(r"\s+", " ", response_text)[:200]
            raise QZoneError(f"评论响应无法解析: {snippet}")
        if code != 0:
            raise QZoneError(f"评论失败（code={code}）", code=code)

    async def _request_text(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """发送 HTTP 请求并返回文本。"""

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout, cookies=self.auth.cookies) as session:
                async with session.request(method, url, params=params, data=data, headers=headers) as response:
                    text = await response.text()
                    if response.status != 200:
                        raise QZoneError(f"QQ 空间 HTTP {response.status}: {text[:200]}")
                    return text
        except QZoneError:
            raise
        except aiohttp.ClientError as exc:
            raise QZoneError(f"QQ 空间网络请求失败: {exc}") from exc
        except Exception as exc:
            raise QZoneError(f"QQ 空间请求异常: {exc}") from exc

    def _parse_feed(self, message: dict[str, Any], target_qq: str) -> Feed | None:
        """把原始 msglist 项解析为 Feed。"""

        tid = str(message.get("tid") or "").strip()
        if not tid:
            return None

        created_at = _safe_int(message.get("created_time"))
        if created_at <= 0 and message.get("createTime"):
            created_at = _safe_int(message.get("createTime"))

        content = _decode_text(message.get("content"))
        rt_con = ""
        rt_data = message.get("rt_con")
        if isinstance(rt_data, dict):
            rt_con = _decode_text(rt_data.get("content"))

        images: list[str] = []
        for picture in message.get("pic") or []:
            if isinstance(picture, dict):
                url = picture.get("url1") or picture.get("pic_id") or picture.get("smallurl")
                if isinstance(url, str) and url.startswith("http"):
                    images.append(url)

        comments: list[FeedComment] = []
        for raw_comment in message.get("commentlist") or []:
            if not isinstance(raw_comment, dict):
                continue
            comment_tid = _safe_int(raw_comment.get("tid"))
            comment = FeedComment(
                uin=str(raw_comment.get("uin") or "").strip(),
                nickname=_decode_text(raw_comment.get("name")),
                content=_decode_text(raw_comment.get("content")),
                tid=comment_tid or None,
                created_time=str(raw_comment.get("createTime2") or raw_comment.get("createTime") or ""),
                is_self=str(raw_comment.get("uin") or "").strip() == self.auth.uin,
            )
            comments.append(comment)

            for sub_comment in raw_comment.get("list_3") or []:
                if not isinstance(sub_comment, dict):
                    continue
                comments.append(
                    FeedComment(
                        uin=str(sub_comment.get("uin") or "").strip(),
                        nickname=_decode_text(sub_comment.get("name")),
                        content=_decode_text(sub_comment.get("content")),
                        tid=_safe_int(sub_comment.get("tid")) or None,
                        created_time=str(
                            sub_comment.get("createTime2") or sub_comment.get("createTime") or ""
                        ),
                        parent_tid=comment_tid or None,
                        is_self=str(sub_comment.get("uin") or "").strip() == self.auth.uin,
                    )
                )

        return Feed(
            target_qq=str(message.get("uin") or target_qq).strip(),
            tid=tid,
            author=_decode_text(message.get("name")),
            created_at=created_at,
            content=content,
            images=images,
            comments=comments,
            rt_con=rt_con,
            secret=_safe_int(message.get("secret")),
            ugc_right=_safe_int(message.get("ugc_right"), default=1),
            raw=message,
        )


def describe_qzone_url(url: str) -> str:
    """返回适合日志展示的 URL（去掉查询参数中的敏感信息）。"""

    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
