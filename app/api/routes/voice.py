"""语音（STT/TTS）路由（P-04 / Phase 4）。

前缀 ``/api/v1/voice``（由 ``app/api/router.py`` 注册），共 2 个路径：

- ``POST /voice/transcribe``：202 上传音频 → 异步转写任务 → 轮询 /ai/tasks/{id}
- ``POST /voice/synthesize``：200 文本 → 同步语音合成 → 返回音频 URL

STT 走异步任务，复用 ``ai_task`` 状态机与 ``GET /ai/tasks/{task_id}`` 轮询：
转写结果通过 ``payload_summary`` JSON 字段返回（``result_ref`` 存短引用键），
避免 varchar(128) 的长度限制。

TTS 走同步：短文本合成延迟低（几百毫秒），用户等待播放，异步轮询有体验延迟。

错误统一为 ``AiErrorDetail`` 形状并携带 request_id。普通响应不携带音频原文、
provider trace 或密钥。所有写操作要求 ``Idempotency-Key`` header。
"""

from __future__ import annotations

import json
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import AiErrorResponse, AiTaskStatus
from app.services.ai.base import AITaskContext
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.ai.tasks import TaskError, enqueue_task
from app.services.voice.base import (
    MAX_TTS_TEXT_LENGTH,
    SynthesizeRequest,
    TranscribeRequest,
)
from app.services.voice.gateway import VoiceGateway

router = APIRouter()

# Idempotency-Key 契约（与 ai_profile 路由一致）。
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# 音频上传约束（与 media.py 对齐，但 voice 用独立常量便于后期调整）。
VOICE_AUDIO_MAX_BYTES = 5 * 1024 * 1024
VOICE_ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/x-m4a",
    "audio/m4a",
}


# ----------------------------------------------------------------------
# 响应 schema
# ----------------------------------------------------------------------


class TranscribeAccepted(BaseModel):
    """202 转写任务接受响应。复用 /ai/tasks/{task_id} 轮询。"""

    task_id: str
    status: AiTaskStatus = AiTaskStatus.QUEUED
    poll_after_ms: int = Field(default=1000, ge=0)


class SynthesizeRequestSchema(BaseModel):
    """TTS 合成请求体。"""

    text: str = Field(..., min_length=1, max_length=MAX_TTS_TEXT_LENGTH)
    locale: str | None = None
    voice: str = "xiaoyun"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class SynthesizeResponse(BaseModel):
    """200 TTS 合成响应。"""

    audio_url: str
    audio_format: str = "mp3"
    duration_ms: int = Field(default=0, ge=0)
    expires_at: str | None = None


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------


def _request_id() -> str:
    supplied = request_id_context.get()
    if supplied and supplied != "-":
        return supplied
    return uuid.uuid4().hex


def _error_response(
    code: str, message: str, status_code: int, *, retryable: bool = False
) -> HTTPException:
    detail = AiErrorResponse(
        code=code,
        message=message,
        request_id=_request_id(),
        retryable=retryable,
        retry_after_ms=0,
    )
    return HTTPException(status_code=status_code, detail=detail.model_dump())


def _require_voice_feature() -> None:
    """语音功能门禁：与 AI 画像同构的 fail-closed 检查。"""
    if not settings.ai_voice_enabled:
        raise _error_response(
            "AI_FEATURE_DISABLED",
            "语音功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    # 生产环境门禁复用 AI 审批体系（_validate_ai_feature_gates 已覆盖）。
    try:
        require_ai_feature(AiFeature.VOICE, settings)
    except AiFeatureDisabledError as exc:
        raise _error_response(
            exc.code,
            "语音功能当前不可用",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc


def _check_idempotency_key(idempotency_key: str | None) -> None:
    if not idempotency_key or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise _error_response(
            "AI_INPUT_INVALID",
            "Idempotency-Key 必须为 8-128 位 ASCII 字符",
            status.HTTP_400_BAD_REQUEST,
        )


async def _read_limited_audio(file, limit: int) -> bytes:
    """读取上传音频，限制大小（与 media.py 一致）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _error_response(
                "AI_INPUT_INVALID",
                f"音频文件大小不能超过{limit // 1024 // 1024}MB",
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        chunks.append(chunk)
    if total == 0:
        raise _error_response(
            "AI_INPUT_INVALID",
            "音频内容为空",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return b"".join(chunks)


# STT 任务类型常量（与 worker handler 注册对齐）。
VOICE_TRANSCRIBE_TASK_TYPE = "voice_transcribe"


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------


@router.post(
    "/transcribe",
    response_model=TranscribeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传音频进行语音转写（异步）",
)
async def transcribe_audio(
    audio: bytes = File(...),
    question_field_key: str = Form(..., max_length=64),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TranscribeAccepted:
    """上传音频文件，同步转写后创建任务写表。

    音频约束：mp3/wav/m4a，≤5MB，≤60s（前端 ``VoiceRecorder`` 录制参数）。
    转写在路由层同步完成（阿里云一句话识别，≤60s 直接 POST 二进制返回），
    随后入队轻量任务把结果写入 ``voice_transcript`` 表。返回 ``task_id`` 后
    通过 ``GET /ai/tasks/{task_id}`` 轮询；转写文本通过任务的
    ``result_payload.transcript`` 字段返回。

    音频不落盘：一句话识别直接 POST 原始 bytes，无需文件存储引用。
    生产启用需满足 AI 审批门禁（三道审批 + AccessKey fail-closed）。
    """
    _require_voice_feature()
    _check_idempotency_key(idempotency_key)

    # 校验音频（File 已解析为 bytes，此处做大小校验）。
    if not audio:
        raise _error_response(
            "AI_INPUT_INVALID",
            "音频内容为空",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(audio) > VOICE_AUDIO_MAX_BYTES:
        raise _error_response(
            "AI_INPUT_INVALID",
            f"音频文件大小不能超过{VOICE_AUDIO_MAX_BYTES // 1024 // 1024}MB",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )

    # 同步调阿里云一句话识别（≤60s 音频，直接 POST 二进制）。
    context = AITaskContext(
        task_id=uuid.uuid4().hex,
        request_id=_request_id(),
        scene="voice_transcribe",
        provider=settings.ai_voice_provider,
        model=settings.ai_voice_model_name,
        schema_version="voice-transcribe-v1",
    )
    request = TranscribeRequest(
        audio_bytes=audio,
        audio_format="mp3",
        sample_rate=16000,
        max_duration_seconds=settings.ai_asr_max_duration_seconds,
    )
    gateway = VoiceGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.transcribe(context, request)
    if outcome.result is None:
        raise _error_response(
            outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            outcome.error_message or "语音转写失败",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=outcome.retryable,
        )

    # 构造请求摘要 hash（��等校验用）。
    import hashlib

    request_hash = hashlib.sha256(
        f"{current.id}:{question_field_key}:{len(audio)}".encode()
    ).hexdigest()

    # 入队轻量任务：worker handler 只负责写 voice_transcript 表。
    try:
        task = await enqueue_task(
            db=db,
            owner_user_id=current.id,
            task_type=VOICE_TRANSCRIBE_TASK_TYPE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    except TaskError as exc:
        raise _error_response(
            exc.code,
            exc.message,
            exc.status_code,
            retryable=exc.retryable,
        ) from exc

    # payload_summary 存转写结果，worker handler 读取后写 voice_transcript 表。
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "transcript": outcome.result.text,
                    "confidence": outcome.result.confidence,
                    "duration_ms": outcome.result.duration_ms,
                    "detected_language": outcome.result.detected_language,
                    "question_field_key": question_field_key,
                },
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await db.commit()

    return TranscribeAccepted(
        task_id=task.task_id,
        status=task.status,
        poll_after_ms=1000,
    )


@router.post(
    "/synthesize",
    response_model=SynthesizeResponse,
    status_code=status.HTTP_200_OK,
    summary="文本转语音合成（同步）",
)
async def synthesize_speech(
    body: SynthesizeRequestSchema,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SynthesizeResponse:
    """将问题文本合成为语音，返回可播放的音频 URL。

    约束：文本 ≤500 字符（``MAX_TTS_TEXT_LENGTH``）。
    返回 ``audio_url`` 供前端 ``uni.createInnerAudioContext`` 播放。

    开发/测试环境可用 mock provider；生产启用需满足 AI 审批门禁。
    """
    _require_voice_feature()
    _check_idempotency_key(idempotency_key)

    context = AITaskContext(
        task_id=uuid.uuid4().hex,
        request_id=_request_id(),
        scene="voice_tts",
        provider=settings.ai_voice_provider,
        model=settings.ai_voice_model_name,
        schema_version="voice-tts-v1",
    )
    request = SynthesizeRequest(
        text=body.text,
        voice=body.voice,
        locale=body.locale,
        speed=body.speed,
    )

    gateway = VoiceGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.synthesize(context, request)

    if outcome.result is None:
        raise _error_response(
            outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            outcome.error_message or "语音合成失败",
            status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=outcome.retryable,
        )

    return SynthesizeResponse(
        audio_url=outcome.result.audio_url,
        audio_format=outcome.result.audio_format,
        duration_ms=outcome.result.duration_ms,
        expires_at=outcome.result.expires_at,
    )


__all__ = ["router"]
