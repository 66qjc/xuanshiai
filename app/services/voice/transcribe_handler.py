"""Voice transcribe task handler for the AI worker.

Handles ``voice_transcribe`` tasks: reads the audio reference from the
task's ``payload_summary``, calls the VoiceGateway, and writes the transcript
to a dedicated ``voice_transcript`` table (NOT ``ai_task.payload_summary``).

Rationale for separate table: ``complete_task`` unconditionally sets
``payload_summary = NULL`` on success, which would wipe the transcript.
Writing directly to ``ai_task`` from the handler session would also deadlock
``complete_task`` (handler_db holds an X-lock; complete_task's SELECT FOR
UPDATE in finalize_db blocks). All existing handlers follow the pattern of
writing only to business tables; this handler does the same. ``result_ref``
stores the task_id as a reference key so the route can join to
``voice_transcript``.

Handler signature follows the worker contract:
``async def handler(db, task, worker_id) -> (result_ref, revisions) | None``.
Returning ``None`` records a retryable failure.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.ai.base import AITaskContext
from app.services.ai.tasks import AiTaskRecord, fail_task
from app.services.voice.base import TranscribeRequest
from app.services.voice.gateway import VoiceGateway

logger = logging.getLogger(__name__)

VOICE_TRANSCRIBE_TASK_TYPE = "voice_transcribe"
_VOICE_SCHEMA_VERSION = "voice-transcribe-v1"


async def voice_transcribe_handler(
    db: AsyncSession,
    task: AiTaskRecord,
    worker_id: str,
) -> tuple[str, None] | None:
    """Run speech-to-text for one claimed ``voice_transcribe`` task.

    Reads ``audio_ref`` and ``question_field_key`` from ``payload_summary``,
    invokes the VoiceGateway, then writes the transcript to the
    ``voice_transcript`` table.  ``result_ref`` stores the task_id so
    ``GET /ai/tasks/{task_id}`` can join to the transcript.

    Returns ``(result_ref, None)`` on success (no revision vector for voice
    tasks) or ``None`` on failure (the worker records a retryable failure).
    """
    payload = task.payload_summary or {}
    audio_ref = str(payload.get("audio_ref") or "")
    if not audio_ref:
        logger.warning(
            "voice_transcribe_missing_audio_ref task_id=%s", task.task_id
        )
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    question_field_key = str(payload.get("question_field_key") or "")

    context = AITaskContext(
        task_id=task.task_id,
        request_id=uuid.uuid4().hex,
        scene="voice_transcribe",
        provider=settings.ai_voice_provider,
        model=settings.ai_voice_model_name,
        schema_version=_VOICE_SCHEMA_VERSION,
    )
    request = TranscribeRequest(
        audio_ref=audio_ref,
        audio_format="mp3",
        sample_rate=16000,
        max_duration_seconds=settings.ai_asr_max_duration_seconds,
        question_field_key=question_field_key,
    )

    gateway = VoiceGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.transcribe(context, request)

    if outcome.result is None:
        logger.warning(
            "voice_transcribe_failed task_id=%s error_code=%s",
            task.task_id,
            outcome.error_code,
        )
        await fail_task(
            db, task.task_id, worker_id,
            error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            retryable=outcome.retryable,
        )
        return None

    transcript = outcome.result.text

    # 写入独立的 voice_transcript 表（业务表，非 ai_task）。
    # handler 在 handler_db 中写入，complete_task 在 finalize_db 中操作 ai_task，
    # 两表无行锁冲突。finalize_handler(True) 提交 handler_db 后本行可见。
    await db.execute(
        text(
            "INSERT INTO voice_transcript "
            "(task_id, owner_user_id, transcript, confidence, duration_ms, "
            "detected_language) "
            "VALUES (:task_id, :owner_user_id, :transcript, :confidence, "
            ":duration_ms, :detected_language) "
            "ON DUPLICATE KEY UPDATE "
            "transcript = VALUES(transcript), confidence = VALUES(confidence), "
            "duration_ms = VALUES(duration_ms), "
            "detected_language = VALUES(detected_language)"
        ),
        {
            "task_id": task.task_id,
            "owner_user_id": task.owner_user_id,
            "transcript": transcript,
            "confidence": outcome.result.confidence,
            "duration_ms": outcome.result.duration_ms,
            "detected_language": outcome.result.detected_language,
        },
    )

    # result_ref 存 task_id 作为引用键，路由通过它关联 voice_transcript 表。
    return task.task_id, None


__all__ = ["voice_transcribe_handler", "VOICE_TRANSCRIBE_TASK_TYPE"]
