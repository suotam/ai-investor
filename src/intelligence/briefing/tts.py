"""Optional local TTS for the audio brief. No cloud dependency, disabled by default.

Providers:
  * DisabledTTS - default; the TTS-ready text file remains the deliverable.
  * WindowsSapiTTS - uses the built-in Windows System.Speech synthesizer via PowerShell
    (fully local, no install). Output: output/briefs/YYYY-MM-DD-daily.wav.
Failure of TTS never fails a brief or pipeline.
"""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from src.config import Settings
from src.logging_setup import get_logger

log = get_logger("briefing.tts")


class TTSProvider(ABC):
    name = "abstract"

    @abstractmethod
    def synthesize(self, text: str, out_path: Path) -> Path | None:
        """Return the written audio path, or None when unavailable."""


class DisabledTTS(TTSProvider):
    name = "disabled"

    def synthesize(self, text: str, out_path: Path) -> Path | None:
        return None


class WindowsSapiTTS(TTSProvider):
    """Local Windows speech synthesis (System.Speech). Rate ~ 0 (normal), WAV output."""

    name = "windows_sapi"

    def __init__(self, rate: int = 0, timeout: float = 600.0):
        self.rate = rate
        self.timeout = timeout

    def synthesize(self, text: str, out_path: Path) -> Path | None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        script_path = out_path.with_suffix(".tts.txt")
        script_path.write_text(text, encoding="utf-8")
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {self.rate}; "
            f"$s.SetOutputToWaveFile('{out_path}'); "
            f"$t = Get-Content -Raw -Encoding UTF8 '{script_path}'; "
            "$s.Speak($t); $s.Dispose()"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                check=True, capture_output=True, timeout=self.timeout,
            )
            script_path.unlink(missing_ok=True)
            return out_path if out_path.exists() else None
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("TTS synthesis failed (text version remains available): %s", exc)
            script_path.unlink(missing_ok=True)
            return None


def get_tts_provider(settings: Settings) -> TTSProvider:
    if getattr(settings, "tts_enabled", False) and getattr(settings, "tts_provider", "windows_sapi") == "windows_sapi":
        return WindowsSapiTTS(rate=int(getattr(settings, "tts_rate", 0)))
    return DisabledTTS()


def synthesize_latest_brief(settings: Settings, brief_type: str = "daily") -> Path | None:
    """Synthesize the most recent audio-text brief to WAV. Never raises."""
    provider = get_tts_provider(settings)
    if isinstance(provider, DisabledTTS):
        return None
    candidates = sorted(settings.brief_output_dir.glob(f"*-{brief_type}-audio.txt"), reverse=True)
    if not candidates:
        return None
    text = candidates[0].read_text(encoding="utf-8")
    out = candidates[0].with_name(candidates[0].name.replace("-audio.txt", ".wav"))
    return provider.synthesize(text, out)
