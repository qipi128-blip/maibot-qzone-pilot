"""NapCat 正向 WebSocket 动作客户端（QQ 空间动态管家专用）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class NapCatError(RuntimeError):
    """NapCat 连接或动作调用失败。"""


@dataclass(frozen=True)
class NapCatEndpoint:
    """NapCat WebSocket 连接信息。"""

    ws_url: str
    access_token: str = ""

    @property
    def safe_url(self) -> str:
        """返回适合写日志的地址（不包含令牌）。"""

        return self.ws_url


def _normalize_ws_url(host: str, port: Any) -> str:
    """根据 host/port 构造 WebSocket 地址。"""

    normalized_host = str(host or "").strip()
    if not normalized_host:
        return ""
    if normalized_host.startswith(("ws://", "wss://")):
        base = normalized_host.rstrip("/")
        if port in (None, "", 0, "0"):
            return base
        tail = base.rsplit("/", 1)[-1]
        if ":" in tail:
            return base
        return f"{base}:{int(port)}"
    return f"ws://{normalized_host}:{int(port)}"


def discover_endpoint(mai_root: Path) -> NapCatEndpoint | None:
    """从 Napcat 适配器配置中发现连接信息。"""

    plugins_dir = Path(mai_root) / "plugins"
    if not plugins_dir.is_dir():
        return None

    candidates: list[Path] = []
    for child in sorted(plugins_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or "napcat" not in child.name.lower():
            continue
        config_path = child / "config.toml"
        if config_path.is_file():
            candidates.append(config_path)

    candidates.sort(key=lambda path: (path.parent.name != "Napcat-Adapter", path.parent.name.lower()))

    for config_path in candidates:
        if tomllib is None:  # pragma: no cover
            return None
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        server = data.get("napcat_server")
        if not isinstance(server, dict):
            continue
        ws_url = _normalize_ws_url(str(server.get("host") or ""), server.get("port"))
        if not ws_url:
            continue
        return NapCatEndpoint(ws_url=ws_url, access_token=str(server.get("token") or "").strip())
    return None


class NapCatClient:
    """带自动重连与动作关联的 NapCat WebSocket 客户端。"""

    def __init__(
        self,
        endpoint: NapCatEndpoint,
        *,
        logger: Any,
        action_timeout: float = 20.0,
        reconnect_initial: float = 2.0,
        reconnect_max: float = 60.0,
        on_connected: Callable[["NapCatClient"], Awaitable[None] | None] | None = None,
    ) -> None:
        """初始化客户端。"""

        self.endpoint = endpoint
        self._logger = logger
        self._action_timeout = max(1.0, float(action_timeout))
        self._reconnect_initial = max(0.5, float(reconnect_initial))
        self._reconnect_max = max(self._reconnect_initial, float(reconnect_max))
        self._on_connected = on_connected

        self.self_id: str = ""
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._echo_counter = 0
        self._stopped = False
        self._connected = False

    @property
    def connected(self) -> bool:
        """当前是否已连接。"""

        return self._connected and self._ws is not None and not self._ws.closed

    def start(self) -> None:
        """启动后台连接任务（幂等）。"""

        if self._run_task is not None and not self._run_task.done():
            return
        self._stopped = False
        self._run_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """停止连接并清理所有等待中的动作。"""

        self._stopped = True
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receive_task
        self._receive_task = None

        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._run_task
        self._run_task = None

        await self._close_transport()
        self._fail_pending(NapCatError("NapCat 连接已关闭"))

    async def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """调用 NapCat 动作并返回原始响应字典。"""

        ws = self._ws
        if ws is None or ws.closed or not self._connected:
            raise NapCatError("NapCat WebSocket 尚未连接")

        self._echo_counter += 1
        echo = f"maibot-qzone-pilot-{self._echo_counter}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        try:
            await ws.send_json({"action": str(action), "params": dict(params or {}), "echo": echo})
        except Exception as exc:
            self._pending.pop(echo, None)
            raise NapCatError(f"向 NapCat 发送动作 {action} 失败: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout or self._action_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(echo, None)
            raise NapCatError(f"调用 NapCat 动作 {action} 超时") from exc

    async def call_data(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """调用 NapCat 动作并返回 ``data`` 字段。"""

        response = await self.call(action, params, timeout=timeout)
        return ensure_action_ok(response, action)

    def _next_backoff(self, backoff: float) -> float:
        """返回带抖动的下一次重连等待时间。"""

        delay = min(self._reconnect_max, max(self._reconnect_initial, backoff))
        return delay * random.uniform(0.8, 1.2)

    async def _run_forever(self) -> None:
        """持续连接，断线后按指数退避重连。"""

        backoff = self._reconnect_initial
        while not self._stopped:
            try:
                await self._connect_once()
                backoff = self._reconnect_initial
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning(
                    "NapCat 连接中断: %s（%.1f 秒后重连）",
                    exc,
                    self._next_backoff(backoff),
                )
            if self._stopped:
                break
            delay = self._next_backoff(backoff)
            await asyncio.sleep(delay)
            backoff = min(self._reconnect_max, backoff * 2.0)

    async def _connect_once(self) -> None:
        """建立一次连接并阻塞到连接关闭。"""

        headers: dict[str, str] = {}
        if self.endpoint.access_token:
            headers["Authorization"] = f"Bearer {self.endpoint.access_token}"

        session = aiohttp.ClientSession()
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            ws = await session.ws_connect(
                self.endpoint.ws_url,
                headers=headers,
                heartbeat=30.0,
                receive_timeout=None,
                max_msg_size=8 * 1024 * 1024,
                timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
            )
            self._session = session
            self._ws = ws
            self._connected = True
            self._logger.info("已连接 NapCat WebSocket: %s", self.endpoint.safe_url)

            self._receive_task = asyncio.create_task(self._receive_loop(ws))
            try:
                await self._notify_connected()
                await self._receive_task
            finally:
                self._receive_task = None

        except aiohttp.WSServerHandshakeError as exc:
            raise NapCatError(
                f"NapCat WebSocket 握手失败（HTTP {exc.status}），请检查 ws_url 与 access_token"
            ) from exc
        finally:
            self._connected = False
            self._ws = None
            self._session = None
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            with contextlib.suppress(Exception):
                await session.close()
            self._fail_pending(NapCatError("NapCat 连接已断开"))
            self._logger.warning("NapCat WebSocket 已断开: %s", self.endpoint.safe_url)

    async def _notify_connected(self) -> None:
        """连接成功后读取登录信息并触发回调。"""

        try:
            data = await self.call_data("get_login_info")
            if isinstance(data, dict):
                self.self_id = str(data.get("user_id") or "").strip()
                nickname = str(data.get("nickname") or "").strip()
                if self.self_id:
                    self._logger.info("NapCat 登录账号: %s（%s）", self.self_id, nickname or "未知昵称")
        except Exception as exc:
            self._logger.warning("读取 NapCat 登录信息失败: %s", exc)

        if self._on_connected is not None:
            try:
                result = self._on_connected(self)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                self._logger.warning("NapCat 连接回调执行失败: %s", exc, exc_info=True)

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """读取 WebSocket 消息并分发动回应答。"""

        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                echo = payload.get("echo")
                if echo is not None:
                    future = self._pending.pop(str(echo), None)
                    if future is not None and not future.done():
                        future.set_result(payload)
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    def _fail_pending(self, error: Exception) -> None:
        """让所有等待中的动作立即失败。"""

        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
                with contextlib.suppress(Exception):
                    future.exception()

    async def _close_transport(self) -> None:
        """关闭 WebSocket 与会话。"""

        ws = self._ws
        self._ws = None
        self._connected = False
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        session = self._session
        self._session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()


def ensure_action_ok(response: Any, action: str) -> Any:
    """校验 NapCat 动作响应并返回 ``data``。"""

    if not isinstance(response, dict):
        raise NapCatError(f"NapCat 动作 {action} 返回了非字典响应")

    status = str(response.get("status") or "").strip().lower()
    retcode = response.get("retcode", 0)
    try:
        retcode_value = int(retcode)
    except (TypeError, ValueError):
        retcode_value = -1

    if status in {"ok", ""} and retcode_value == 0:
        return response.get("data")

    message = str(response.get("wording") or response.get("message") or "").strip()
    raise NapCatError(f"NapCat 动作 {action} 失败（retcode={retcode}）: {message or '未知错误'}")
