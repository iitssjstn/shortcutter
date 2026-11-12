"""Fouttypes van de pipeline, met een duidelijke Nederlandstalige melding voor de UI."""


class PipelineError(Exception):
    kind = "onbekend"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DownloadBlocked(PipelineError):
    kind = "download_blocked"


class DownloadFailed(PipelineError):
    kind = "download_failed"


class WhisperError(PipelineError):
    kind = "whisper"


class RenderError(PipelineError):
    kind = "render"


class SourceMissing(PipelineError):
    kind = "source_missing"


class LLMError(Exception):
    """Fout bij de LLM-API. Wordt nooit fataal: de pipeline valt terug op de heuristiek."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # error | invalid | rate_limit | no_key
