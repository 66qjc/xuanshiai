"""Voice provider service domain (STT + TTS).

Parallel to ``app.services.ai`` for text AI capabilities.  Provides
speech-to-text and text-to-speech through a vendor-agnostic Protocol with
mock and Alibaba Cloud implementations.
"""

from app.services.voice.base import (  # noqa: F401
    AITaskContext,
    GatewayCallRecord,
    PartialTranscript,
    ProviderError,
    ProviderErrorKind,
    StreamTranscribeRequest,
    SynthesizeRequest,
    SynthesizeResult,
    TranscribeRequest,
    TranscribeResult,
    VoiceProvider,
)
