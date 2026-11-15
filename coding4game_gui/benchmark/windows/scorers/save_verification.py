"""Save-authoritative scorer with optional visual failure detection."""

from __future__ import annotations


class SaveVerificationScorer:
    """Satisfy the scorer interface without inspecting gameplay frames.

    The Windows environment owns the actual save verifier and converts its
    durable success result into the final score.  Success therefore remains
    save-only.  A scenario may additionally register OCR phases that are
    terminal failures (for example Palworld's ``DEFEAT`` overlay); those end
    the episode without contributing reward.
    """

    def __init__(self, spec: dict, client_size, ocr=None) -> None:
        self.spec = spec or {}
        self.client_size = tuple(client_size)
        self._failure_scorer = None
        failure = self.spec.get("failure_detection")
        if failure:
            from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer

            phases = failure.get("phases") or {}
            if not phases:
                raise ValueError(
                    "scorer.failure_detection.phases must not be empty")
            failure_spec = {
                "calibrated_client_size": self.spec.get(
                    "calibrated_client_size", self.client_size),
                "ocr": self.spec.get("ocr") or {},
                "phases": phases,
                "terminal_phases": (
                    failure.get("terminal_phases") or list(phases)),
                "regions": {},
                "score_phases": [],
            }
            self._failure_scorer = OcrScorer(
                failure_spec, client_size=self.client_size, ocr=ocr)

    def reset(self) -> None:
        if self._failure_scorer is not None:
            self._failure_scorer.reset()

    def delta(self, frame) -> float:
        return 0.0

    def is_terminal(self, frame) -> bool:
        return (self._failure_scorer is not None
                and self._failure_scorer.is_terminal(frame))

    def classify_phase(self, frame) -> str:
        if self._failure_scorer is not None:
            phase = self._failure_scorer.classify_phase(frame)
            if phase is not None:
                return phase
        return "pending_save_verification"

    @property
    def total(self) -> float:
        return 0.0
