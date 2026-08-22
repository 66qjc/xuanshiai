"""Voice transcribe task handler for the AI worker.

Handles ``voice_transcribe`` tasks.  Transcription already happened synchronously
in the route layer (Aliyun one-sentence ASR, ≤60 s, returns immediately), so the
task's ``payload_summary`` carries the finished ``transcript`` / ``confidence`` /
``duration_ms`` / ``detected_language``.  This handler only persists them to the
dedicated ``voice_transcript`` table.

Rationale for separate table: ``complete_task`` unconditionally sets
``payload_summary = NULL`` on success, which would wipe the transcript.
Writing directly to ``ai_task`` from the handler session would also deadlock
``complete_task`` (handler_db holds an X-lock; complete_task's SELECT FOR
UPDATE in finalize_db blocks).  All existing handlers follow the pattern of
writing only to business tables; this handler does the same.  ``result_ref``
stores the task_id as a reference key so the route can join to
``voice_transcript``.

Handler signature follows the worker contract:
``async def handler(db, task, worker_id) -> (result_ref, revisions) | None``.
Returning ``None`` records a retryable failure.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.tasks import AiTaskRecord, fail_task

logger = logging.getLogger(__name__)

VOICE_TRANSCRIBE_TASK_TYPE = "voice_transcribe"


async def voice_transcribe_handler(
    db: AsyncSession,
    task: AiTaskRecord,
    worker_id: str,
) -> tuple[str, None] | None:
    """Persist the already-transcribed result for one ``voice_transcribe`` task.

    The route layer called Aliyun one-sentence ASR synchronously before
    enqueuing, so ``payload_summary`` holds the finished transcript.  This
    handler writes it to ``voice_transcript`` and returns ``task_id`` as the
    ``result_ref`` so ``GET /ai/tasks/{task_id}`` can join to the transcript.

    Returns ``(result_ref, None)`` on success (no revision vector for voice
    tasks) or ``None`` on failure (the worker records a retryable failure).
    """
    payload = task.payload_summary or {}
    transcript = str(payload.get("transcript") or "")
    if not transcript:
        logger.warning(
            "voice_transcribe_missing_transcript task_id=%s", task.task_id
        )
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    confidence = payload.get("confidence")
    duration_ms = payload.get("duration_ms")
    detected_language = payload.get("detected_language")

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
            "confidence": confidence,
            "duration_ms": duration_ms,
            "detected_language": detected_language,
        },
    )

    # result_ref 存 task_id 作为引用键，路由通过它关联 voice_transcript 表。
    return task.task_id, None


__all__ = ["voice_transcribe_handler", "VOICE_TRANSCRIBE_TASK_TYPE"]
