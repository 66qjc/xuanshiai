"""流式语音合成：阿里云 NLS cosyvoice 流式 TTS WebSocket 客户端。

与 :mod:`stream_provider` 里的 :class:`AliyunStreamASRClient` 对称，但方向相反：
ASR 是「客户端发音频→服务端返回文本」，TTS 是「客户端发文本→服务端返回音频」。

鉴权链路（与实时 ASR 共用同一套 NLS Token）：
  1. AccessKey ID + Secret → 调阿里云 Token API 换取 NLS Token（复用
     :func:`stream_provider._fetch_nls_token`，缓存到过期前刷新）。
  2. Token + AppKey → 连接 NLS WebSocket，发送 ``StartSynthesis`` 控制帧。
  3. ``RunSynthesis`` 发送待合成文本 → 服务端返回二进制音频帧（PCM/MP3）。
  4. ``StopSynthesis`` → 等待 ``SynthesisCompleted`` → 关闭连接。

协议参考：阿里云「使用 WebSocket 协议实现 Cosyvoice 大模型长文本语音合成」。
``namespace`` 固定为 ``FlowingSpeechSynthesizer``，指令为 ``StartSynthesis`` /
``RunSynthesis`` / ``StopSynthesis``，事件为 ``SynthesisStarted`` /
``SynthesisCompleted``。二进制帧是音频数据流（分帧下发的一个完整音频文件）。

异常复用 :class:`app.services.voice.providers._AliyunVoiceError` 类族。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.services.voice.providers import (
    _AliyunAPIError,
    _AliyunAuthError,
    _AliyunConnectionError,
    _AliyunRateLimitError,
    _AliyunTimeoutError,
    _AliyunVoiceError,
)
from app.services.voice.stream_provider import (
    _default_ws_connect,
    _fetch_nls_token,
    _secret_value,
)

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

# cosyvoice 流式合成协议常量（与实时 ASR 的 SpeechTranscriber namespace 区分）。
_TTS_NAMESPACE = "FlowingSpeechSynthesizer"
_ACTION_START_SYNTHESIS = "StartSynthesis"
_ACTION_RUN_SYNTHESIS = "RunSynthesis"
_ACTION_STOP_SYNTHESIS = "StopSynthesis"
_NAME_SYNTHESIS_STARTED = "SynthesisStarted"
_NAME_SYNTHESIS_COMPLETED = "SynthesisCompleted"
_NAME_TASK_FAILED = "TaskFailed"

# 等待 SynthesisStarted 握手的超时（秒），与 ASR 的 10s 对齐。
_HANDSHAKE_TIMEOUT = 10.0
# 等待 SynthesisCompleted 的超时（秒）；合成比识别慢，给 30s。
_COMPLETION_TIMEOUT = 30.0


class AliyunStreamTTSClient:
    """阿里云 NLS cosyvoice 流式语音合成 WebSocket 客户端。

    生命周期：``connect`` → ``synthesize(text)`` → ``audio_chunks``（收二进制
    音频帧）→ ``finish``。``audio_chunks`` 是一个 async generator，yield 合成
    的音频二进制块，由编排器落盘成独立小音频文件。

    开发/测试环境可用 mock：测试通过 ``ws_connect`` kwarg 注入 mock WebSocket
    连接工厂，绕过真实阿里云连接。
    """

    def __init__(
        self,
        *,
        access_key_id: Any | None = None,
        access_key_secret: Any | None = None,
        region: str | None = None,
        ws_url: str | None = None,
        ws_connect: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._access_key_id = _secret_value(
            access_key_id
            if access_key_id is not None
            else settings.ai_aliyun_voice_access_key_id
        )
        self._access_key_secret = _secret_value(
            access_key_secret
            if access_key_secret is not None
            else settings.ai_aliyun_voice_access_key_secret
        )
        self._region = region or settings.ai_aliyun_voice_region
        # cosyvoice 流式 TTS 与实时 ASR 共用同一个 NLS WebSocket 接入点。
        self._ws_url = ws_url or settings.ai_aliyun_voice_asr_ws_url
        # 测试注入：mock WebSocket 连接工厂与 http client。
        self._ws_connect = ws_connect
        self._http_client = http_client
        self._connection: ClientConnection | None = None
        self._task_id = uuid.uuid4().hex
        self._connected = False
        # connect 时记录，控制帧的 header 需要 appkey。
        self._app_key = ""
        # 音频块队列与后台 drain task。
        self._chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        # SynthesisStarted 握手事件。
        self._started_event: asyncio.Event | None = None
        self._completed = False
        # Token 缓存：(token, expire_timestamp)。
        self._token_cache: tuple[str, float] | None = None

    async def get_token(self) -> str:
        """AccessKey 换取 NLS Token，缓存到过期前刷新（与 ASR client 同源）。"""
        from app.services.voice.stream_provider import (
            _TOKEN_REFRESH_MARGIN_SECONDS,
            _TOKEN_TTL_SECONDS,
        )

        if self._token_cache is not None:
            token, expires_at = self._token_cache
            margin = min(_TOKEN_REFRESH_MARGIN_SECONDS, (expires_at - time.time()) / 2)
            if time.time() < expires_at - max(0, margin):
                return token
        if not self._access_key_id or not self._access_key_secret:
            raise _AliyunAuthError(
                "流式 TTS 缺少 AccessKey 配置（AI_ALIYUN_VOICE_ACCESS_KEY_ID/"
                "SECRET），请在 .env 配置（仅开发/测试环境）"
            )
        token, expires_in = await _fetch_nls_token(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=self._region,
            http_client=self._http_client,
        )
        self._token_cache = (token, time.time() + expires_in)
        return token

    async def connect(
        self,
        app_key: str,
        *,
        voice: str = "longxiaochun",
        audio_format: str = "mp3",
        sample_rate: int = 16000,
        speed: float = 1.0,
        token: str | None = None,
    ) -> None:
        """建立 NLS WebSocket 连接并发送 ``StartSynthesis`` 配置帧。

        ``voice`` / ``audio_format`` / ``sample_rate`` / ``speed`` 与 cosyvoice
        WS ``StartSynthesis`` 指令的 payload 字段对齐。连接成功后启动后台
        drain task 持续接收服务端推送的音频帧与事件。
        """
        if self._connected:
            raise _AliyunAPIError("TTS client 已连接，不可重复 connect")
        nls_token = token or await self.get_token()
        if not app_key:
            raise _AliyunAuthError("流式 TTS 缺少 AppKey 配置")
        self._app_key = app_key

        connect_fn = self._ws_connect or _default_ws_connect
        # token 必须拼在 URL query（与 ASR 同款，仅放 header 网关不收数据）。
        sep = "&" if "?" in self._ws_url else "?"
        connect_url = f"{self._ws_url}{sep}token={nls_token}"
        try:
            self._connection = await connect_fn(
                connect_url,
                additional_headers={"X-NLS-Token": nls_token},
            )
        except _AliyunVoiceError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise _AliyunTimeoutError(
                f"NLS TTS WebSocket 连接超时: {type(exc).__name__}"
            ) from exc
        except OSError as exc:
            raise _AliyunConnectionError(
                f"NLS TTS WebSocket 连接失败: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"NLS TTS WebSocket 连接失败: {type(exc).__name__}"
            ) from exc

        # 发送 StartSynthesis 控制帧（cosyvoice 流式合成 JSON 协议）。
        # speech_rate: [-500,0,500] 对应 [0.5,1.0,2.0] 倍速。
        start_frame = {
            "header": {
                "message_id": uuid.uuid4().hex,
                "task_id": self._task_id,
                "namespace": _TTS_NAMESPACE,
                "name": _ACTION_START_SYNTHESIS,
                "appkey": app_key,
            },
            "payload": {
                "voice": voice,
                "format": audio_format,
                "sample_rate": sample_rate,
                "volume": 50,
                "speech_rate": int((speed - 1.0) * 100),
                "pitch_rate": 0,
            },
        }
        await self._send_json(start_frame)
        # 等服务端 SynthesisStarted 确认后才算连接就绪。
        self._started_event = asyncio.Event()
        self._drain_task = asyncio.create_task(self._drain_frames())
        try:
            await asyncio.wait_for(
                self._started_event.wait(), timeout=_HANDSHAKE_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            await self._close()
            raise _AliyunTimeoutError(
                "等待 NLS SynthesisStarted 超时"
            ) from exc
        self._connected = True
        logger.info(
            "tts_stream_connected task_id=%s voice=%s", self._task_id, voice
        )

    async def synthesize(self, text: str) -> None:
        """发送 ``RunSynthesis`` 指令，把待合成文本喂给服务端。

        可在一次会话中多次调用（按句喂文本）。音频帧由 ``audio_chunks``
        异步产出。
        """
        if not self._connected or self._connection is None:
            raise _AliyunAPIError("TTS client 未连接，无法发送文本")
        if self._completed:
            raise _AliyunAPIError("TTS client 已结束，无法发送文本")
        if not text:
            return
        run_frame = {
            "header": {
                "message_id": uuid.uuid4().hex,
                "task_id": self._task_id,
                "namespace": _TTS_NAMESPACE,
                "name": _ACTION_RUN_SYNTHESIS,
                "appkey": self._app_key,
            },
            "payload": {"text": text},
        }
        await self._send_json(run_frame)

    async def finish(self) -> None:
        """发送 ``StopSynthesis`` 并等待 ``SynthesisCompleted``。

        阻塞直到所有音频帧下发完毕且 drain task 结束。
        """
        if self._completed:
            return
        if not self._connected or self._connection is None:
            self._completed = True
            return
        stop_frame = {
            "header": {
                "message_id": uuid.uuid4().hex,
                "task_id": self._task_id,
                "namespace": _TTS_NAMESPACE,
                "name": _ACTION_STOP_SYNTHESIS,
                "appkey": self._app_key,
            },
            "payload": {},
        }
        await self._send_json(stop_frame)
        # 等待 drain task 收到 SynthesisCompleted 并结束。
        if self._drain_task is not None:
            try:
                await asyncio.wait_for(
                    self._drain_task, timeout=_COMPLETION_TIMEOUT
                )
            except asyncio.TimeoutError as exc:
                raise _AliyunTimeoutError(
                    "等待 NLS SynthesisCompleted 超时"
                ) from exc
        await self._close()
        self._completed = True

    async def audio_chunks(self) -> Any:
        """AsyncGenerator：yield 合成的音频二进制块。

        每次 yield 一个 ``bytes``（服务端二进制帧）。生成器在 drain task
        结束（入队哨兵 None）后自然结束。连接关闭由 ``finish`` 负责，
        本方法不主动关连接——调用方应在消费完音频后调用 ``finish``。
        """
        while True:
            item = await self._chunk_queue.get()
            if item is None:
                # 哨兵值：drain task 结束。
                break
            yield item

    async def _drain_frames(self) -> None:
        """后台持续接收 NLS WebSocket 推送的音频帧与事件。"""
        if self._connection is None:
            return
        try:
            async for raw in self._connection:
                if isinstance(raw, (bytes, bytearray)):
                    # 二进制帧是音频数据流，入队供 audio_chunks 消费。
                    await self._chunk_queue.put(bytes(raw))
                    continue
                # 文本帧是事件（JSON），解析控制事件。
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.debug(
                        "tts_stream_non_json_frame task_id=%s", self._task_id
                    )
                    continue
                await self._handle_frame(frame)
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "tts_stream_drain_io_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tts_stream_drain_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )
        finally:
            # 哨兵：通知 audio_chunks 生成器结束。
            await self._chunk_queue.put(None)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """解析一帧 cosyvoice 协议事件。"""
        header = frame.get("header", {})
        name = header.get("name")
        payload = frame.get("payload", {})
        if name == _NAME_SYNTHESIS_STARTED:
            # 握手确认：connect() 的等待在此解除。
            if self._started_event is not None:
                self._started_event.set()
        elif name == _NAME_SYNTHESIS_COMPLETED:
            # 服务端确认所有音频已下发完毕：主动关连接让 drain 循环退出。
            logger.info(
                "tts_stream_completed task_id=%s", self._task_id
            )
            await self._close()
        elif name == _NAME_TASK_FAILED:
            error_msg = payload.get("error_message") or header.get(
                "status_text", "NLS TTS 任务失败"
            )
            logger.warning(
                "tts_stream_task_failed task_id=%s msg=%s",
                self._task_id,
                str(error_msg)[:200],
            )

    async def _send_json(self, frame: dict[str, Any]) -> None:
        """发送一个 JSON 控制帧到 NLS WebSocket。"""
        if self._connection is None:
            raise _AliyunAPIError("NLS TTS WebSocket 未连接")
        try:
            await self._connection.send(json.dumps(frame, ensure_ascii=False))
        except (TimeoutError, OSError) as exc:
            raise _AliyunTimeoutError(
                f"发送 TTS 控制帧超时: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"发送 TTS 控制帧失败: {type(exc).__name__}"
            ) from exc

    async def _close(self) -> None:
        """关闭 WebSocket 连接，幂等。"""
        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        self._connected = False
        try:
            await connection.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "tts_stream_close_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )


__all__ = ["AliyunStreamTTSClient"]
