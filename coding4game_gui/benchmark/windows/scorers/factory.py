"""Validate scorer registration and construct the configured Windows scorer."""
from __future__ import annotations


SCORER_IMPLEMENTATION_KINDS = {
    "ocr": "ocr",
    "forza_horizon_5": "ocr",
    "black_myth": "image",
    "kingdom_rush_alliance": "ocr",
    "riders_republic": "ocr",
    "bbt_progress": "image",
    "race": "ocr",
    "kof98_terminal": "image",
    "save_verification": "save",
    "sekiro_boss": "image",
    "overcooked_2": "ocr",
    "nba_2k25": "ocr",
}
VALID_SCORER_KINDS = frozenset(SCORER_IMPLEMENTATION_KINDS.values())
VALID_SCORER_IMPLEMENTATIONS = frozenset(SCORER_IMPLEMENTATION_KINDS)


def validate_scorer_selection(kind: str | None, implementation: str | None,
                              *, context: str = "config") -> None:
    if kind not in VALID_SCORER_KINDS:
        raise ValueError(
            f"{context}: unknown scorer.kind {kind!r}; expected one of "
            f"{sorted(VALID_SCORER_KINDS)}")
    if implementation not in VALID_SCORER_IMPLEMENTATIONS:
        raise ValueError(
            f"{context}: unknown scorer.implementation {implementation!r}; "
            f"expected one of {sorted(VALID_SCORER_IMPLEMENTATIONS)}")
    required_kind = SCORER_IMPLEMENTATION_KINDS[implementation]
    if kind != required_kind:
        raise ValueError(
            f"{context}: scorer implementation {implementation!r} requires "
            f"kind {required_kind!r}, got {kind!r}")


class ScorerFactory:
    """Own the shared OCR engine and construct the configured game scorer."""

    def __init__(self) -> None:
        self._ocr_engine = None

    def _ocr(self, spec: dict):
        if self._ocr_engine is None:
            from coding4game_gui.benchmark.windows.scorers.ocr import PaddleOcrEngine

            cfg = spec.get("ocr") or {}
            self._ocr_engine = PaddleOcrEngine(
                input_size=cfg.get("input_size", (640, 360)),
                min_conf=cfg.get("min_conf", 0.5),
                device=cfg.get("device"),
                config=cfg,
            )
        return self._ocr_engine

    def create_text_reader(self, spec: dict, client_size):
        """Create the OCR-only reader used during cold-start navigation."""
        from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer

        return OcrScorer(spec, client_size=client_size, ocr=self._ocr(spec))

    def create(self, *, spec: dict, client_size, kind: str,
               implementation: str):
        """Create the scorer selected by the driver/scenario configuration."""
        validate_scorer_selection(kind, implementation,
                                  context="Windows scorer")

        if implementation == "ocr":
            from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer

            return OcrScorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "forza_horizon_5":
            from coding4game_gui.benchmark.windows.scorers.forza_horizon_5 import ForzaHorizon5Scorer

            return ForzaHorizon5Scorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "overcooked_2":
            from coding4game_gui.benchmark.windows.scorers.overcooked_2 import Overcooked2Scorer

            return Overcooked2Scorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "nba_2k25":
            from coding4game_gui.benchmark.windows.scorers.nba_2k25 import Nba2k25Scorer

            return Nba2k25Scorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "sekiro_boss":
            from coding4game_gui.benchmark.windows.scorers.sekiro_boss import SekiroBossScorer

            return SekiroBossScorer(spec, client_size=client_size)

        if implementation == "black_myth":
            from coding4game_gui.benchmark.windows.scorers.black_myth import BlackMythScorer

            return BlackMythScorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "save_verification":
            from coding4game_gui.benchmark.windows.scorers.save_verification import SaveVerificationScorer

            ocr = self._ocr(spec) if spec.get("failure_detection") else None
            return SaveVerificationScorer(
                spec, client_size=client_size, ocr=ocr)

        if implementation == "kingdom_rush_alliance":
            from coding4game_gui.benchmark.windows.scorers.kingdom_rush_alliance import KingdomRushAllianceScorer

            return KingdomRushAllianceScorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "riders_republic":
            from coding4game_gui.benchmark.windows.scorers.riders_republic import RidersRepublicScorer

            return RidersRepublicScorer(
                spec, client_size=client_size, ocr=self._ocr(spec))

        if implementation == "bbt_progress":
            from coding4game_gui.benchmark.windows.scorers.bbt_progress import BbtProgressScorer

            return BbtProgressScorer(spec, client_size=client_size)

        if implementation == "kof98_terminal":
            from coding4game_gui.benchmark.windows.scorers.kof98_terminal import Kof98TerminalScorer

            return Kof98TerminalScorer(spec, client_size=client_size)

        if implementation == "race":
            from coding4game_gui.benchmark.windows.scorers.race import RaceScorer

            return RaceScorer(
                spec, client_size=client_size,
                ocr=lambda crop: self._ocr(spec)(crop))

        raise AssertionError(
            f"validated scorer implementation was not dispatched: "
            f"{implementation!r}")
