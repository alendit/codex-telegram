"""Speech-to-text adapters."""

from .client import (
    CodexSpeechToTextClient,
    OpenAISpeechToTextClient,
    SpeechToTextClient,
    SpeechToTextError,
)

__all__ = [
    "CodexSpeechToTextClient",
    "OpenAISpeechToTextClient",
    "SpeechToTextClient",
    "SpeechToTextError",
]
