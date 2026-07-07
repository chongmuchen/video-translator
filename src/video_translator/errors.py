"""Application-specific errors."""


class VideoTranslatorError(RuntimeError):
    """Base error shown to CLI and API users."""


class ConfigurationError(VideoTranslatorError):
    """A required service or executable is not configured."""


class PipelineError(VideoTranslatorError):
    """A pipeline stage failed."""


class PipelineCanceled(VideoTranslatorError):
    """A pipeline stage was canceled by the user."""


class InvalidSourceError(VideoTranslatorError):
    """The requested input source is not allowed or not readable."""
