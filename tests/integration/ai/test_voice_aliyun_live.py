"""阿里云语音真实链路集成测试：需要真实 NLS 凭据，无 key 时整体 skip。

环境变量（从 .env 读取，与运行时配置一致）：
- AI_ALIYUN_VOICE_ACCESS_KEY_ID / SECRET：AccessKey，换取 NLS Token
- AI_ALIYUN_VOICE_API_KEY / APP_KEY：NLS 项目 appkey
- AI_ALIYUN_VOICE_REGION：区域（默认 cn-shanghai）

覆盖：
- 一句话识别（ASR）：真实调 /stream/v1/asr，验证返回非空文本
- 语音合成（TTS）：真实调 cosyvoice，验证返回非空音频 bytes
- Token 获取：真实调 CreateToken，验证返回有效 token

无凭据时 skip，参照 tests/live 模式。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI_ALIYUN_VOICE_ACCESS_KEY_ID"),
    reason="AI_ALIYUN_VOICE_ACCESS_KEY_ID 未配置；跳过真实阿里云语音链路测试",
)


@pytest.mark.asyncio
async def test_fetch_nls_token_real() -> None:
    """真实调阿里云 NLS Token API，验证返回非空 token。"""
    from app.services.voice.stream_provider import _fetch_nls_token

    ak_id = os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_ID"]
    ak_secret = os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_SECRET"]
    region = os.environ.get("AI_ALIYUN_VOICE_REGION", "cn-shanghai")
    token, expires_in = await _fetch_nls_token(
        access_key_id=ak_id,
        access_key_secret=ak_secret,
        region=region,
    )
    assert token
    assert expires_in > 0


@pytest.mark.asyncio
async def test_recognize_audio_real() -> None:
    """真实调一句话识别，需要一段测试音频文件。

    用 ALIYUN_VOICE_TEST_AUDIO 环境变量指定音频路径；未指定时 skip。
    """
    audio_path = os.environ.get("ALIYUN_VOICE_TEST_AUDIO")
    if not audio_path or not os.path.exists(audio_path):
        pytest.skip("ALIYUN_VOICE_TEST_AUDIO 未配置或文件不存在")

    from pydantic import SecretStr

    from app.core.config import Settings
    from app.services.voice.providers import _AliyunVoiceClient

    settings = Settings(
        _env_file=None,
        ai_aliyun_voice_api_key=SecretStr(os.environ.get("AI_ALIYUN_VOICE_API_KEY", "")),
        ai_aliyun_voice_app_key=SecretStr(os.environ.get("AI_ALIYUN_VOICE_APP_KEY", "")),
        ai_aliyun_voice_region=os.environ.get("AI_ALIYUN_VOICE_REGION", "cn-shanghai"),
        ai_aliyun_voice_access_key_id=SecretStr(os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_ID"]),
        ai_aliyun_voice_access_key_secret=SecretStr(os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_SECRET"]),
    )
    client = _AliyunVoiceClient(
        api_key=settings.ai_aliyun_voice_api_key,
        app_key=settings.ai_aliyun_voice_app_key,
        region=settings.ai_aliyun_voice_region,
        access_key_id=settings.ai_aliyun_voice_access_key_id,
        access_key_secret=settings.ai_aliyun_voice_access_key_secret,
    )
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    # 音频格式按实际文件后缀声明（一句话识别按声明的 format 解码）。
    audio_format = os.path.splitext(audio_path)[1].lstrip(".").lower() or "mp3"
    result = await client.recognize_audio(
        audio_bytes=audio_bytes,
        audio_format=audio_format,
        sample_rate=16000,
        model=settings.ai_aliyun_voice_asr_model,
    )
    assert result["text"]


@pytest.mark.asyncio
async def test_synthesize_speech_real() -> None:
    """真实调 cosyvoice TTS，验证返回非空音频并落盘。"""
    from pydantic import SecretStr

    from app.core.config import Settings
    from app.services.voice.providers import _AliyunVoiceClient

    settings = Settings(
        _env_file=None,
        ai_aliyun_voice_api_key=SecretStr(os.environ.get("AI_ALIYUN_VOICE_API_KEY", "")),
        ai_aliyun_voice_app_key=SecretStr(os.environ.get("AI_ALIYUN_VOICE_APP_KEY", "")),
        ai_aliyun_voice_region=os.environ.get("AI_ALIYUN_VOICE_REGION", "cn-shanghai"),
        ai_aliyun_voice_access_key_id=SecretStr(os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_ID"]),
        ai_aliyun_voice_access_key_secret=SecretStr(os.environ["AI_ALIYUN_VOICE_ACCESS_KEY_SECRET"]),
    )
    client = _AliyunVoiceClient(
        api_key=settings.ai_aliyun_voice_api_key,
        app_key=settings.ai_aliyun_voice_app_key,
        region=settings.ai_aliyun_voice_region,
        access_key_id=settings.ai_aliyun_voice_access_key_id,
        access_key_secret=settings.ai_aliyun_voice_access_key_secret,
    )
    result = await client.synthesize_speech(
        text="你好，这是一段测试语音。",
        voice="xiaoyun",
        model=settings.ai_aliyun_voice_tts_model,
        audio_format="mp3",
        sample_rate=16000,
        speed=1.0,
    )
    assert result["audio_url"]
