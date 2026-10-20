"""QQ 空间动态管家插件测试。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from maibot_sdk import PluginPaths

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "maibot_qzone_pilot_test"


def load_plugin_package() -> None:
    """把插件目录作为包加载，便于相对导入。"""

    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件包")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


load_plugin_package()
plugin_module = importlib.import_module(f"{PACKAGE_NAME}.plugin")
config_module = importlib.import_module(f"{PACKAGE_NAME}.config")
decision_module = importlib.import_module(f"{PACKAGE_NAME}.decision")
qzone_module = importlib.import_module(f"{PACKAGE_NAME}.qzone")


class FakeNapCatServer:
    """模拟 NapCat 正向 WebSocket 服务。"""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/", self._handle_ws)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.port = 0
        self.ws: web.WebSocketResponse | None = None
        self.connected = asyncio.Event()
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.friend_list = [
            {"user_id": 3864117714, "nickname": "麦麦"},
            {"user_id": 3213691924, "nickname": "好友A"},
            {"user_id": 3568584791, "nickname": "好友B"},
        ]

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                action = str(payload.get("action") or "")
                params = payload.get("params") or {}
                echo = payload.get("echo")
                self.actions.append((action, params))
                data: Any = None
                if action == "get_login_info":
                    data = {"user_id": 3864117714, "nickname": "麦麦"}
                elif action == "get_cookies":
                    data = {
                        "cookies": "uin=o3864117714; skey=abc; p_skey=xyz",
                        "bkn": "12345",
                    }
                elif action == "get_friend_list":
                    data = list(self.friend_list)
                elif action == "send_private_msg":
                    data = {"message_id": 1}
                await ws.send_json(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "message": "",
                        "echo": echo,
                    }
                )
        finally:
            self.ws = None
        return ws

    def action_count(self, name: str) -> int:
        """统计某个动作调用次数。"""

        return sum(1 for action, _params in self.actions if action == name)


class FakeQZoneServer:
    """模拟 QQ 空间 Web 接口。"""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/list", self._handle_list)
        self.app.router.add_post("/publish", self._handle_publish)
        self.app.router.add_post("/comment", self._handle_comment)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.port = 0
        self.feeds: list[dict[str, Any]] = []
        self.published: list[str] = []
        self.comments: list[dict[str, str]] = []
        self.list_error_code: int | None = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    @property
    def base_url(self) -> str:
        """返回本地服务地址。"""

        return f"http://127.0.0.1:{self.port}"

    def urls(self) -> dict[str, str]:
        """返回插件使用的接口覆盖表。"""

        return {
            "list": f"{self.base_url}/list",
            "publish": f"{self.base_url}/publish",
            "comment": f"{self.base_url}/comment",
        }

    async def _handle_list(self, request: web.Request) -> web.Response:
        payload = {
            "code": 0,
            "message": "",
            "logininfo": {"name": "麦麦"},
            "msglist": self.feeds,
        }
        if self.list_error_code is not None:
            payload = {"code": self.list_error_code, "message": "not login", "msglist": []}
        return web.Response(
            text=f"_preloadCallback({json.dumps(payload, ensure_ascii=False)});",
            content_type="text/plain",
        )

    async def _handle_publish(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.published.append(str(form.get("con") or ""))
        return web.json_response({"code": 0, "tid": f"tid-{len(self.published)}"})

    async def _handle_comment(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.comments.append(
            {
                "topicId": str(form.get("topicId") or ""),
                "content": str(form.get("content") or ""),
            }
        )
        return web.Response(
            text='<script>frameElement.callback({"code":0,"message":""});</script>',
            content_type="text/html",
        )


class FakeContext:
    """最小的 PluginContext 替身。"""

    def __init__(self, data_dir: Path) -> None:
        self.paths = PluginPaths(data_dir=data_dir, runtime_dir=data_dir)
        self.logger = logging.getLogger("test.qzone_pilot")
        self.logger.setLevel(logging.DEBUG)
        self.bot_config = {"nickname": "麦麦"}
        self.personality_config = {"personality": "一个有点文艺的 QQ 机器人"}
        self.llm_responses: list[dict[str, Any]] = []
        self.llm_calls: list[Any] = []
        self.messages: list[dict[str, Any]] = []

        class LLM:
            def __init__(self, outer: "FakeContext") -> None:
                self.outer = outer

            async def generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
                self.outer.llm_calls.append((prompt, kwargs))
                if self.outer.llm_responses:
                    return self.outer.llm_responses.pop(0)
                return {"success": True, "response": '{"should_post": false, "confidence": 0.9, "reason": "无感"}'}

        class Config:
            def __init__(self, outer: "FakeContext") -> None:
                self.outer = outer

            async def get(self, key: str, default: Any = None) -> Any:
                if key == "bot":
                    return dict(self.outer.bot_config)
                if key == "personality":
                    return dict(self.outer.personality_config)
                return default

        class Message:
            def __init__(self, outer: "FakeContext") -> None:
                self.outer = outer

            async def get_by_time(self, start_time: str, end_time: str, **kwargs: Any) -> list[dict[str, Any]]:
                del start_time, end_time, kwargs
                return list(self.outer.messages)

        self.llm = LLM(self)
        self.config = Config(self)
        self.message = Message(self)


class QZonePilotTestCase(unittest.IsolatedAsyncioTestCase):
    """端到端测试。"""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.napcat = FakeNapCatServer()
        await self.napcat.start()
        self.qzone = FakeQZoneServer()
        await self.qzone.start()
        self.context = FakeContext(self.data_dir)
        self.plugin = plugin_module.QZonePilotPlugin(
            state_path=self.data_dir / "state.json",
            qzone_urls=self.qzone.urls(),
        )
        self.plugin._set_context(self.context)
        self.config_data = self.plugin.build_default_config()
        self.config_data["connection"]["ws_url"] = f"ws://127.0.0.1:{self.napcat.port}"
        self.config_data["connection"]["auto_discover"] = False
        self.config_data["connection"]["action_timeout_seconds"] = 5
        self.config_data["notify"]["admin_qqs"] = ["99999"]
        self.config_data["publish"]["initial_delay_seconds"] = 3600
        self.config_data["browse"]["initial_delay_seconds"] = 3600
        self.config_data["publish"]["min_interval_minutes"] = 5
        self.config_data["browse"]["min_comment_interval_minutes"] = 1
        self.config_data["browse"]["watch_friends"] = ["3213691924"]
        self.config_data["browse"]["auto_friends"] = False
        self.config_data["browse"]["comment_enabled"] = True
        self.plugin.set_plugin_config(self.config_data)

        now = int(time.time())
        self.qzone.feeds = [
            {
                "tid": "feed-1",
                "uin": 3213691924,
                "name": "好友A",
                "created_time": now - 60,
                "content": "今天吃了火锅，好满足",
                "secret": 0,
                "ugc_right": 1,
                "commentlist": [],
            }
        ]

    async def asyncTearDown(self) -> None:
        await self.plugin.on_unload()
        await self.qzone.stop()
        await self.napcat.stop()
        self.temp_dir.cleanup()

    async def _start(self) -> None:
        await self.plugin.on_load()
        await asyncio.wait_for(self.napcat.connected.wait(), timeout=5)
        await asyncio.sleep(0.2)

    async def _publish_response(self, content: str, *, should_post: bool = True, confidence: float = 0.9) -> None:
        self.context.llm_responses.append(
            {
                "success": True,
                "response": json.dumps(
                    {
                        "should_post": should_post,
                        "confidence": confidence,
                        "content": content,
                        "reason": "测试",
                        "topic": "测试",
                    },
                    ensure_ascii=False,
                ),
            }
        )

    async def _comment_response(self, comment: str, *, should_comment: bool = True, confidence: float = 0.9) -> None:
        self.context.llm_responses.append(
            {
                "success": True,
                "response": json.dumps(
                    {
                        "should_comment": should_comment,
                        "confidence": confidence,
                        "comment": comment,
                        "reason": "测试",
                    },
                    ensure_ascii=False,
                ),
            }
        )

    async def test_publish_success(self) -> None:
        await self._start()
        await self._publish_response("今天风很舒服，想出去走走")
        success, message = await self.plugin._maybe_publish(force=True)
        self.assertTrue(success, message)
        self.assertEqual(self.qzone.published, ["今天风很舒服，想出去走走"])
        self.assertEqual(self.plugin._state["stats"]["posts"], 1)
        self.assertEqual(len(self.plugin._state["publish_history"]), 1)
        self.assertGreaterEqual(self.napcat.action_count("send_private_msg"), 1)

    async def test_publish_declined(self) -> None:
        await self._start()
        await self._publish_response("", should_post=False)
        success, _message = await self.plugin._maybe_publish(force=True)
        self.assertFalse(success)
        self.assertEqual(self.qzone.published, [])

    async def test_publish_low_confidence(self) -> None:
        await self._start()
        await self._publish_response("测试内容", confidence=0.1)
        success, _message = await self.plugin._maybe_publish(force=True)
        self.assertFalse(success)
        self.assertEqual(self.qzone.published, [])

    async def test_publish_dry_run(self) -> None:
        self.config_data["plugin"]["dry_run"] = True
        self.plugin.set_plugin_config(self.config_data)
        await self._start()
        await self._publish_response("演练内容")
        success, _message = await self.plugin._maybe_publish(force=True)
        self.assertFalse(success)
        self.assertEqual(self.qzone.published, [])
        self.assertGreaterEqual(self.napcat.action_count("send_private_msg"), 1)

    async def test_publish_duplicate_content(self) -> None:
        await self._start()
        self.plugin._state["publish_history"] = [
            {"time": time.time() - 3600, "content": "今天风很舒服，想出去走走", "tid": "old"}
        ]
        await self._publish_response("今天风很舒服，想出去走走")
        success, _message = await self.plugin._maybe_publish(force=True)
        self.assertFalse(success)
        self.assertEqual(self.qzone.published, [])

    async def test_comment_success(self) -> None:
        await self._start()
        await self._comment_response("这火锅看着也太香了，蘸料是灵魂")
        success, message = await self.plugin._maybe_browse(force=True)
        self.assertTrue(success, message)
        self.assertEqual(len(self.qzone.comments), 1)
        self.assertEqual(self.qzone.comments[0]["content"], "这火锅看着也太香了，蘸料是灵魂")
        self.assertEqual(self.plugin._state["stats"]["comments"], 1)
        self.assertEqual(len(self.plugin._state["comment_history"]), 1)

    async def test_comment_only_once(self) -> None:
        await self._start()
        await self._comment_response("这火锅看着也太香了，蘸料是灵魂")
        await self.plugin._maybe_browse(force=True)
        await self._comment_response("再来一条")
        await self.plugin._maybe_browse(force=True)
        self.assertEqual(len(self.qzone.comments), 1)

    async def test_comment_disabled(self) -> None:
        self.config_data["browse"]["comment_enabled"] = False
        self.plugin.set_plugin_config(self.config_data)
        await self._start()
        await self.plugin._maybe_browse(force=True)
        self.assertEqual(self.qzone.comments, [])
        self.assertEqual(self.context.llm_calls, [])

    async def test_comment_declined(self) -> None:
        await self._start()
        await self._comment_response("", should_comment=False)
        await self.plugin._maybe_browse(force=True)
        self.assertEqual(self.qzone.comments, [])

    async def test_ad_feed_skipped(self) -> None:
        await self._start()
        self.qzone.feeds[0]["content"] = "兼职刷单 加微信 日结 广告"
        await self._comment_response("看起来不错")
        await self.plugin._maybe_browse(force=True)
        self.assertEqual(self.qzone.comments, [])
        self.assertEqual(self.context.llm_calls, [])

    async def test_status_command(self) -> None:
        allowed, message, intercept = await self.plugin.handle_status_command(
            user_id="99999",
            group_id="",
            is_local_operator=False,
        )
        self.assertTrue(allowed)
        self.assertTrue(intercept)
        self.assertIn("QQ 空间动态管家状态", message or "")

    async def test_non_admin_command_ignored(self) -> None:
        allowed, message, intercept = await self.plugin.handle_status_command(
            user_id="12345",
            group_id="",
            is_local_operator=False,
        )
        self.assertFalse(allowed)
        self.assertIsNone(message)
        self.assertFalse(intercept)


class QZoneClientTestCase(unittest.IsolatedAsyncioTestCase):
    """QQ 空间 HTTP 客户端测试。"""

    async def asyncSetUp(self) -> None:
        self.server = FakeQZoneServer()
        await self.server.start()
        self.auth = qzone_module.QZoneAuth(
            cookies={"uin": "o3864117714", "skey": "abc", "p_skey": "xyz"},
            gtk="12345",
            uin="3864117714",
            nickname="麦麦",
        )
        self.client = qzone_module.QZoneClient(
            self.auth,
            logger=logging.getLogger("test.qzone_client"),
            urls={
                "list": f"{self.server.base_url}/list",
                "publish": f"{self.server.base_url}/publish",
                "comment": f"{self.server.base_url}/comment",
            },
        )

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_get_feeds(self) -> None:
        now = int(time.time())
        self.server.feeds = [
            {
                "tid": "f1",
                "uin": 3213691924,
                "name": "好友A",
                "created_time": now - 60,
                "content": "今天吃了火锅",
                "secret": 0,
                "ugc_right": 1,
                "commentlist": [
                    {"uin": 3825491374, "name": "海", "content": "看着不错", "tid": 1},
                ],
            }
        ]
        feeds = await self.client.get_feeds("3213691924")
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0].tid, "f1")
        self.assertEqual(feeds[0].content, "今天吃了火锅")
        self.assertEqual(len(feeds[0].comments), 1)
        self.assertFalse(feeds[0].self_commented("3864117714"))

    async def test_publish(self) -> None:
        tid = await self.client.publish("测试说说")
        self.assertEqual(tid, "tid-1")
        self.assertEqual(self.server.published, ["测试说说"])

    async def test_comment(self) -> None:
        await self.client.comment("f1", "3213691924", "好吃")
        self.assertEqual(len(self.server.comments), 1)
        self.assertEqual(self.server.comments[0]["content"], "好吃")

    async def test_list_error(self) -> None:
        self.server.list_error_code = -3000
        with self.assertRaises(qzone_module.QZoneError):
            await self.client.get_feeds("3213691924")


class UtilityTestCase(unittest.TestCase):
    """工具函数测试。"""

    def test_cookie_parse(self) -> None:
        auth = qzone_module.QZoneAuth.from_cookies("uin=o123456; skey=abc; p_skey=xyz", "999")
        self.assertEqual(auth.uin, "123456")
        self.assertEqual(auth.gtk, "999")

    def test_gtk(self) -> None:
        self.assertEqual(qzone_module.generate_gtk(""), "5381")
        self.assertTrue(qzone_module.generate_gtk("abc").isdigit())

    def test_active_hours(self) -> None:
        plugin = plugin_module.QZonePilotPlugin()
        plugin.set_plugin_config(plugin.build_default_config())
        self.assertTrue(plugin._in_active_hours("00:00", "00:00"))
        self.assertTrue(plugin._in_active_hours("00:00", "23:59"))

    def test_similarity(self) -> None:
        self.assertGreater(decision_module.content_similarity("今天天气很好", "今天天气很好"), 0.99)
        self.assertLess(decision_module.content_similarity("今天天气很好", "完全不同的内容"), 0.2)


if __name__ == "__main__":
    unittest.main()
