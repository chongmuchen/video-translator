"""Backward-compatible all-at-once pipeline orchestration."""

from __future__ import annotations

import logging

from ..models import JobManifest
from .stepwise import StepwiseVideoTranslationPipeline
from .synthesizer import SpeechSynthesizer
from .transcriber import FasterWhisperTranscriber
from .translator import SegmentTranslator


class VideoTranslationPipeline(StepwiseVideoTranslationPipeline):
    """Run all resumable stages.

    The factory methods deliberately reference this module's adapter classes so
    tests and deployments can replace individual model backends.
    """

    def _make_transcriber(
        self,
        logger: logging.Logger,
    ) -> FasterWhisperTranscriber:
        return FasterWhisperTranscriber(self.settings, logger)

    def _make_translator(self, logger: logging.Logger) -> SegmentTranslator:
        return SegmentTranslator(self.settings, logger)

    def _make_synthesizer(
        self,
        logger: logging.Logger,
    ) -> SpeechSynthesizer:
        return SpeechSynthesizer(self.settings, logger)

    def run(self, manifest: JobManifest) -> JobManifest:
        return self.run_all(manifest)

