"""实时半双工语音对话：阿里云 NLS 实时语音识别（paraformer-realtime-v2）流式 ASR client。

用已安装的 ``websockets`` 包（v16，随 ``uvicorn[standard]`` 安装）手写阿里云 NLS
实时语音识别 WebSocket 客户端，不依赖官方 ``nls`` SDK（该 SDK 不在 PyPI 发布、且其
asyncio 兼容性未经验证；PyPI 上的同名 ``nls`` 包是一个不相关的薛定谔方程求解器）。

鉴权链路（阿里云 NLS 实时 ASR）：
  1. AccessKey ID + Secret → 调阿里云 Token API 换取 NLS Token（缓存到过期前刷新）。
  2. Token + AppKey → 连接 NLS WebSocket，发送 ``StartTranscription`` 控制帧。
  3. PCM 音频二进制帧 → 服务端返回 ``SentenceResult``（部分）/``TranscriptionResult``
     （最终）。
  4. ``StopTranscription`` → 等待最终文本 → 关闭连接。

异常复用 :class:`app.services.voice.providers._AliyunVoiceError` 类族，由
:class:`AliyunVoiceProvider` 统一映射为 :class:`ProviderError`。
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

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

# 阿里云 NLS Token API（用 AccessKey 换取临时 Token，TTL 默认 86400s）。
_TOKEN_API_URL = (
    "https://nls-meta.{region}.aliyuncs.com/"
    "pop/api/v1/nls/token/generate"
)
_TOKEN_TTL_SECONDS = 86400
# Token 提前刷新阈值（秒）：过期前 5 分钟刷新，避免边界竞态。
_TOKEN_REFRESH_MARGIN_SECONDS = 300

# 协议消息 header.action / header.name 枚举。
_ACTION_START_TRANSCRIPTION = "StartTranscription"
_ACTION_STOP_TRANSCRIPTION = "StopTranscription"
_NAME_SENTENCE_RESULT = "SentenceResult"
_NAME_TRANSCRIPTION_RESULT = "TranscriptionResult"
_NAME_TRANSCRIPTION_STARTED = "TranscriptionStarted"
_NAME_TRANSCRIPTION_COMPLETED = "TranscriptionCompleted"
_NAME_TASK_FAILED = "TaskFailed"


class AliyunStreamASRClient:
    """阿里云 NLS 实时语音识别 WebSocket 客户端。

    生命周期：``connect`` → ``send_chunk``×N → ``partial_results``（后台）→
    ``finish``。``partial_results`` 是一个 async generator，由 WS 路由层
    并发消费，把部分识别结果实时推给前端。

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
        self._ws_url = ws_url or settings.ai_aliyun_voice_asr_ws_url
        # 测试注入：mock WebSocket 连接工厂与 http client。
        self._ws_connect = ws_connect
        self._http_client = http_client
        self._connection: ClientConnection | None = None
        self._task_id = uuid.uuid4().hex
        self._connected = False
        # partial_results 的结果队列与后台 drain task。
        self._result_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None
        self._final_text = ""
        self._completed = False
        # Token 缓存：(token, expire_timestamp)。
        self._token_cache: tuple[str, float] | None = None

    async def get_token(self) -> str:
        """AccessKey 换取 NLS Token，缓存到过期前刷新。

        调阿里云 Token API（HTTP），返回临时 Token 字符串。Token 不含敏感
        AccessKey，可用于 WebSocket 鉴权。缓存按过期时间戳判断，提前
        :data:`_TOKEN_REFRESH_MARGIN_SECONDS` 刷新避免边界竞态。
        """
        if self._token_cache is not None:
            token, expires_at = self._token_cache
            # 刷新阈值不超过 TTL 的一半，避免短 TTL token 永远缓存失效。
            margin = min(_TOKEN_REFRESH_MARGIN_SECONDS, (expires_at - time.time()) / 2)
            if time.time() < expires_at - max(0, margin):
                return token
        if not self._access_key_id or not self._access_key_secret:
            raise _AliyunAuthError(
                "实时 ASR 缺少 AccessKey 配置（AI_ALIYUN_VOICE_ACCESS_KEY_ID/"
                "SECRET），请在 .env 配置（仅开发/测试环境）"
            )
        token, expires_in = await self._fetch_token()
        self._token_cache = (token, time.time() + expires_in)
        return token

    async def _fetch_token(self) -> tuple[str, int]:
        """调用阿里云 NLS Token API，返回 (token, expires_in_seconds)。"""
        return await _fetch_nls_token(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=self._region,
            http_client=self._http_client,
        )

    async def connect(
        self,
        app_key: str,
        model: str = "paraformer-realtime-v2",
        token: str | None = None,
    ) -> None:
        """建立 NLS WebSocket 连接并发送 ``StartTranscription`` 配置帧。

        ``token`` 为 None 时自动调用 :meth:`get_token` 获取。连接成功后
        启动后台 drain task 持续接收服务端推送的识别结果。
        """
        if self._connected:
            raise _AliyunAPIError("ASR client 已连接，不可重复 connect")
        nls_token = token or await self.get_token()
        if not app_key:
            raise _AliyunAuthError("实时 ASR 缺少 AppKey 配置")

        connect_fn = self._ws_connect or _default_ws_connect
        try:
            self._connection = await connect_fn(
                self._ws_url,
                additional_headers={
                    "X-NLS-Token": nls_token,
                    "Authorization": f"Bearer {nls_token}",
                },
            )
        except _AliyunVoiceError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise _AliyunTimeoutError(
                f"NLS WebSocket 连接超时: {type(exc).__name__}"
            ) from exc
        except OSError as exc:
            raise _AliyunConnectionError(
                f"NLS WebSocket 连接失败: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"NLS WebSocket 连接失败: {type(exc).__name__}"
            ) from exc

        # 发送 StartTranscription 控制帧（阿里云 NLS 实时 ASR JSON 协议）。
        start_frame = {
            "header": {
                "message_id": uuid.uuid4().hex,
                "task_id": self._task_id,
                "namespace": "SpeechTranscriber",
                "name": _ACTION_START_TRANSCRIPTION,
                "appkey": app_key,
            },
            "payload": {
                "format": "pcm",
                "sample_rate": 16000,
                "enable_intermediate_result": True,
                "enable_punctuation_prediction": True,
                "enable_inverse_text_normalization": True,
                "model": model,
            },
            "context": {
                "sdk": {"name": "xuanshiai-asr", "version": "1.0.0"},
            },
        }
        await self._send_json(start_frame)
        self._connected = True
        # 启动后台 drain task 接收识别结果。
        self._drain_task = asyncio.create_task(self._drain_results())
        logger.info(
            "asr_stream_connected task_id=%s model=%s",
            self._task_id,
            model,
        )

    async def send_chunk(self, pcm_bytes: bytes) -> None:
        """发送一帧 PCM 音频二进制数据到 NLS WebSocket。

        ``pcm_bytes`` 是 base64 解码后的原始 PCM 字节（16kHz/16bit/单声道）。
        在 ``connect`` 之前或 ``finish`` 之后调用会抛 ``_AliyunAPIError``。
        """
        if not self._connected or self._connection is None:
            raise _AliyunAPIError("ASR client 未连接，无法发送音频块")
        if self._completed:
            raise _AliyunAPIError("ASR client 已结束，无法发送音频块")
        if not pcm_bytes:
            return
        try:
            await self._connection.send(pcm_bytes)
        except (TimeoutError, OSError) as exc:
            raise _AliyunTimeoutError(
                f"发送音频块超时: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"发送音频块失败: {type(exc).__name__}"
            ) from exc

    async def finish(self) -> str:
        """发送 ``StopTranscription`` 并等待最终识别文本。

        阻塞直到收到 ``TranscriptionCompleted`` 或 drain task 结束。返回
        本轮对话的最终转写文本（不含原始音频）。
        """
        if not self._connected or self._connection is None:
            raise _AliyunAPIError("ASR client 未连接，无法 finish")
        if self._completed:
            return self._final_text
        # 发送 StopTranscription 控制帧。
        stop_frame = {
            "header": {
                "message_id": uuid.uuid4().hex,
                "task_id": self._task_id,
                "namespace": "SpeechTranscriber",
                "name": _ACTION_STOP_TRANSCRIPTION,
            },
            "payload": {},
        }
        await self._send_json(stop_frame)
        # 等待 drain task 收到最终结果并结束。
        if self._drain_task is not None:
            try:
                await asyncio.wait_for(
                    self._drain_task, timeout=30.0
                )
            except asyncio.TimeoutError as exc:
                raise _AliyunTimeoutError(
                    "等待最终识别结果超时"
                ) from exc
        await self._close()
        self._completed = True
        return self._final_text

    async def partial_results(self) -> Any:
        """AsyncGenerator：yield 部分识别结果（边说边出字）。

        每次 yield 一个 :class:`PartialTranscript`（``is_final=False``）。
        生成器在 ``finish`` 收到最终结果后自然结束。
        """
        while True:
            item = await self._result_queue.get()
            if item is None:
                # 哨兵值：drain task 结束。
                break
            if item.get("is_final"):
                # 最终结果不通过 partial_results yield（由 finish 返回）。
                self._final_text = item.get("text", "")
                continue
            text = item.get("text", "")
            if text:
                yield text
        # drain 结束后确保连接已关闭。
        if self._connected:
            await self._close()

    async def _drain_results(self) -> None:
        """后台持续接收 NLS WebSocket 推送的识别结果，分发到队列。"""
        if self._connection is None:
            return
        try:
            async for raw in self._connection:
                if isinstance(raw, bytes):
                    continue
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.debug(
                        "asr_stream_non_json_frame task_id=%s",
                        self._task_id,
                    )
                    continue
                await self._handle_frame(frame)
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "asr_stream_drain_io_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "asr_stream_drain_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )
        finally:
            # 哨兵：通知 partial_results 生成器结束。
            await self._result_queue.put(None)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        """解析一帧 NLS 协议消息，分发部分/最终结果。"""
        header = frame.get("header", {})
        name = header.get("name")
        payload = frame.get("payload", {})
        if name == _NAME_SENTENCE_RESULT:
            is_final = bool(payload.get("is_sentence_end", False))
            text = str(payload.get("result", "")).strip()
            if text:
                if is_final:
                    # 句子结束也是部分结果，但不是整轮最终文本。
                    await self._result_queue.put(
                        {"text": text, "is_final": False}
                    )
                else:
                    await self._result_queue.put(
                        {"text": text, "is_final": False}
                    )
        elif name == _NAME_TRANSCRIPTION_RESULT:
            text = str(payload.get("result", "")).strip()
            if text:
                # 最终文本：直接设置 _final_text，同时入队供 partial_results
                # 生成器感知结束（is_final=True 的项会被生成器跳过）。
                self._final_text = text
                await self._result_queue.put({"text": text, "is_final": True})
        elif name == _NAME_TASK_FAILED:
            error_msg = payload.get("error_message") or header.get(
                "status_text", "NLS 任务失败"
            )
            logger.warning(
                "asr_stream_task_failed task_id=%s msg=%s",
                self._task_id,
                type(error_msg).__name__,
            )
        elif name == _NAME_TRANSCRIPTION_COMPLETED:
            # 服务端确认结束：drain 循环会在连接关闭后退出。
            logger.info(
                "asr_stream_completed task_id=%s", self._task_id
            )

    async def _send_json(self, frame: dict[str, Any]) -> None:
        """发送一个 JSON 控制帧到 NLS WebSocket。"""
        if self._connection is None:
            raise _AliyunAPIError("NLS WebSocket 未连接")
        try:
            await self._connection.send(json.dumps(frame, ensure_ascii=False))
        except (TimeoutError, OSError) as exc:
            raise _AliyunTimeoutError(
                f"发送控制帧超时: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"发送控制帧失败: {type(exc).__name__}"
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
                "asr_stream_close_error task_id=%s err=%s",
                self._task_id,
                type(exc).__name__,
            )


async def _default_ws_connect(
    url: str, additional_headers: dict[str, str] | None = None
) -> ClientConnection:
    """默认 WebSocket 连接工厂：用 ``websockets`` v16 asyncio client。

    与 :class:`AliyunStreamASRClient` 解耦，便于测试注入 mock 连接工厂。
    """
    import websockets

    # websockets v16 的 connect 接受 additional_headers 传递鉴权 header。
    return await websockets.connect(url, additional_headers=additional_headers)


def _sign_nls_request(
    params: dict[str, str],
    access_key_secret: str,
) -> str:
    """计算阿里云 POP API 的 HMAC-SHA1 签名。

    阿里云签名流程：
    1. 构造 canonicalized query string（参数按字典序排列，URL 编码）。
    2. 构造 string-to-sign: GET&<percent-encoded-resource>&<percent-encoded-query>。
    3. HMAC-SHA1(string-to-sign, access_key_secret + "&") → Base64。
    """
    import hashlib
    import hmac
    from urllib.parse import quote, urlencode

    # 1. 参数按 key 排序，构造规范化 query string。
    sorted_params = sorted(params.items())
    canonicalized = urlencode(sorted_params, quote_via=quote)

    # 2. 构造 string-to-sign。
    string_to_sign = "GET&%2F&" + quote(canonicalized, safe="")

    # 3. HMAC-SHA1 签名。
    key = (access_key_secret + "&").encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    import base64

    return base64.b64encode(digest).decode("utf-8")


async def _fetch_nls_token(
    *,
    access_key_id: str,
    access_key_secret: str,
    region: str,
    http_client: Any | None = None,
) -> tuple[str, int]:
    """调用阿里云 NLS Token API 换取临时 Token。

    用 httpx + HMAC-SHA1 签名（阿里云 POP API 协议）。返回 (token, expires_in_seconds)。
    测试通过 ``http_client`` 注入 mock 覆盖。
    """
    import httpx

    url = _TOKEN_API_URL.format(region=region)
    client = http_client or httpx.AsyncClient(timeout=30.0)
    try:
        params = {
            "AccessKeyId": access_key_id,
            "Action": "CreateToken",
            "Format": "JSON",
            "RegionId": region,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "Version": "2019-02-28",
        }
        # 计算 HMAC-SHA1 签名并追加到请求参数。
        signature = _sign_nls_request(params, access_key_secret)
        params["Signature"] = signature

        response = await client.get(url, params=params)
        status_code = response.status_code
        if status_code == 429:
            raise _AliyunRateLimitError("NLS Token API 限流", status_code)
        if status_code in (401, 403):
            raise _AliyunAuthError(
                f"NLS Token API 鉴权失败: {status_code}", status_code
            )
        if status_code >= 500:
            raise _AliyunAPIError(
                f"NLS Token API 服务端错误: {status_code}", status_code
            )
        if status_code != 200:
            raise _AliyunAPIError(
                f"NLS Token API 异常响应: {status_code}", status_code
            )
        payload = response.json()
        token = str(payload.get("Token", {}).get("Id", ""))
        expire_time = int(payload.get("Token", {}).get("ExpireTime", 0))
        if expire_time > 0:
            # ExpireTime 是绝对 Unix 时间戳（秒），换算为相对 TTL。
            expire_seconds = max(60, expire_time - int(time.time()))
        else:
            expire_seconds = _TOKEN_TTL_SECONDS
        if not token:
            raise _AliyunAPIError("NLS Token API 返回空 Token")
        return token, expire_seconds
    except _AliyunVoiceError:
        raise
    except (TimeoutError, OSError) as exc:
        raise _AliyunTimeoutError(
            f"NLS Token API 超时: {type(exc).__name__}"
        ) from exc
    except Exception as exc:
        raise _AliyunConnectionError(
            f"NLS Token API 连接失败: {type(exc).__name__}"
        ) from exc
    finally:
        if http_client is None and hasattr(client, "aclose"):
            await client.aclose()


def _secret_value(value: Any) -> str | None:
    """从 SecretStr 或裸值提取字符串，None 透传。"""
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value)


__all__ = ["AliyunStreamASRClient"]
