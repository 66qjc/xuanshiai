"""POC: 验证基于 ``websockets`` 包手写阿里云 NLS 实时 ASR WebSocket 客户端的协议层可行性。

阿里云 NLS 官方 Python SDK（``nls``）不在 PyPI 上发布，且其 asyncio 兼容性未经验证
（官方文档示例为回调式，非 asyncio-native）。本 POC 用 ``websockets`` 包（v16，随
``uvicorn[standard]`` 安装）手写阿里云实时语音识别 WebSocket 客户端，验证协议层：
StartTranscription → 发送音频二进制帧 → 接收 SentenceResult/TranscriptionResult →
StopTranscription。

验证方式：启动一个 mock WebSocket 服务端模拟阿里云 NLS 协议响应，确认客户端能正确
发送控制帧、二进制音频帧并接收 JSON 结果。不依赖真实阿里云 key。
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.asyncio.server import serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("asr_poc")

# 阿里云 NLS 实时语音识别 WebSocket 接入点（华东2/cn-shanghai）。
ALIYUN_NLS_WSS_URL = "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"

# 协议消息 header.action 枚举。
ACTION_START_TRANSCRIPTION = "StartTranscription"
ACTION_STOP_TRANSCRIPTION = "StopTranscription"
ACTION_TRANSCRIPTION_RESULT = "TranscriptionResult"
ACTION_SENTENCE_RESULT = "SentenceResult"
ACTION_TASK_FAILED = "TaskFailed"

# mock 服务端：模拟阿里云 NLS 实时 ASR 的最小协议响应。
MOCK_PARTIAL_RESULTS = ["我今年", "我今年28岁", "我今年28岁在北京"]
MOCK_FINAL_RESULT = "我今年28岁，在北京工作"


async def _mock_aliyun_server(ws) -> None:
    """模拟阿里云 NLS 服务端：解析控制帧、回放识别结果。"""
    logger.info("mock_server: client connected")
    audio_bytes_received = 0
    started = False
    stopped = False
    async for message in ws:
        if isinstance(message, bytes):
            # 二进制音频帧：阿里云 NLS 接收 PCM 原始字节。
            audio_bytes_received += len(message)
            if not started:
                started = True
                # 收到首个音频块后，逐步推送部分识别结果（SentenceResult）。
                for partial in MOCK_PARTIAL_RESULTS:
                    await ws.send(json.dumps({
                        "header": {"task_id": "mock-task", "event": "result-generated",
                                   "name": ACTION_SENTENCE_RESULT},
                        "payload": {"result": partial, "is_sentence_end": False},
                    }, ensure_ascii=False))
                    await asyncio.sleep(0.05)
            continue
        # 文本控制帧：StartTranscription / StopTranscription。
        try:
            frame = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("mock_server: non-JSON text frame: %s", message[:80])
            continue
        action = frame.get("header", {}).get("name") or frame.get("header", {}).get("action")
        logger.info("mock_server: control frame action=%s", action)
        if action == ACTION_START_TRANSCRIPTION:
            # 阿里云返回 TranscriptionStarted 确认。
            await ws.send(json.dumps({
                "header": {"task_id": "mock-task", "event": "result-generated",
                           "name": "TranscriptionStarted"},
                "payload": {},
            }, ensure_ascii=False))
        elif action == ACTION_STOP_TRANSCRIPTION:
            stopped = True
            # 推送最终结果（TranscriptionResult，is_sentence_end=True）。
            await ws.send(json.dumps({
                "header": {"task_id": "mock-task", "event": "result-generated",
                           "name": ACTION_TRANSCRIPTION_RESULT},
                "payload": {"result": MOCK_FINAL_RESULT, "is_sentence_end": True},
            }, ensure_ascii=False))
            # 确认停止。
            await ws.send(json.dumps({
                "header": {"task_id": "mock-task", "event": "result-generated",
                           "name": "TranscriptionCompleted"},
                "payload": {},
            }, ensure_ascii=False))
            break
    logger.info(
        "mock_server: session ended audio_bytes=%d stopped=%s",
        audio_bytes_received, stopped,
    )


async def _run_client(server_uri: str) -> dict:
    """模拟实时 ASR 客户端流程：connect → send_chunk×N → finish。"""
    partials: list[str] = []
    final_text = ""
    logger.info("client: connecting to %s", server_uri)
    async with websockets.connect(server_uri) as ws:
        # 1. 发送 StartTranscription 配置（阿里云 NLS JSON 控制帧）。
        start_frame = {
            "header": {"message_id": "c1", "task_id": "mock-task",
                       "namespace": "SpeechTranscriber",
                       "name": ACTION_START_TRANSCRIPTION,
                       "appkey": "test-app-key"},
            "payload": {
                "format": "pcm",
                "sample_rate": 16000,
                "enable_intermediate_result": True,
                "enable_punctuation_prediction": True,
            },
            "context": {"sdk": {"name": "nls-python-websocket", "version": "poc"}},
        }
        await ws.send(json.dumps(start_frame, ensure_ascii=False))
        # 2. 发送 PCM 音频二进制帧（模拟 3 个 320 字节块 ≈ 10ms PCM@16k/16bit）。
        for i in range(3):
            await ws.send(b"\x00\x01" * 160)  # 320 bytes per chunk
        # 3. 后台并发接收服务端推送的识别结果。
        async def _drain() -> None:
            nonlocal final_text
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                header = frame.get("header", {})
                name = header.get("name")
                payload = frame.get("payload", {})
                if name == ACTION_SENTENCE_RESULT and not payload.get("is_sentence_end"):
                    partials.append(payload.get("result", ""))
                elif name == ACTION_TRANSCRIPTION_RESULT and payload.get("is_sentence_end"):
                    final_text = payload.get("result", "")
                elif name == "TranscriptionCompleted":
                    break
        drain_task = asyncio.create_task(_drain())
        # 4. 发送 StopTranscription。
        stop_frame = {
            "header": {"message_id": "c2", "task_id": "mock-task",
                       "namespace": "SpeechTranscriber",
                       "name": ACTION_STOP_TRANSCRIPTION, "appkey": "test-app-key"},
            "payload": {},
        }
        await ws.send(json.dumps(stop_frame, ensure_ascii=False))
        await drain_task
    return {"partials": partials, "final_text": final_text}


async def main() -> None:
    """启动 mock 服务端，运行客户端验证全链路。"""
    async with serve(_mock_aliyun_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        server_uri = f"ws://127.0.0.1:{port}"
        result = await _run_client(server_uri)
    logger.info("client: partials=%s", result["partials"])
    logger.info("client: final=%s", result["final_text"])
    assert result["final_text"] == MOCK_FINAL_RESULT, "最终文本不匹配"
    assert len(result["partials"]) == len(MOCK_PARTIAL_RESULTS), "部分结果数量不匹配"
    logger.info("POC 通过：websockets 协议层链路可行")


if __name__ == "__main__":
    asyncio.run(main())
