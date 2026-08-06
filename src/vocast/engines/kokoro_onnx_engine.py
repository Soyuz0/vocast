"""Kokoro via ONNX Runtime.

Same model and voices as the PyTorch engine, executed by ONNX Runtime instead.
Measured on a Coffee Lake i5, single thread: 1.39x faster than PyTorch, and
model load drops from ~7s to ~1.5s, which every worker pays at startup.

The fp32 weights are used deliberately. Quantized variants are available and
both are worse here: int8 needs VNNI instructions this CPU generation lacks and
runs 2.2x slower than fp32, while fp16 is faster but lossy. fp32 is numerically
equivalent to the PyTorch path, so switching engines cannot change how anything
sounds.
"""

from __future__ import annotations

import os
import re
import urllib.request
from pathlib import Path

import numpy as np

from .engine import AudioChunk, TTSEngine

MODEL_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# The model's context is 510 phoneme tokens, and its per-length voice style
# array has exactly 510 rows, so a batch of precisely 510 tokens indexes off
# the end. Staying under the limit sidesteps that, and the margin costs nothing.
PHONEME_BUDGET = 500

# What kokoro-onnx itself splits phoneme batches on. Anything not followed by
# one of these accumulates into a single batch, however long.
_PHONEME_BREAKS = ".,!?;"


def default_model_dir() -> Path:
    configured = os.environ.get("VOCAST_TTS_MODEL_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".vocast" / "models"


class KokoroOnnxEngine(TTSEngine):
    SAMPLE_RATE = 24000
    DEFAULT_VOICE = "af_heart"
    #: Matched to the PyTorch engine. The runtime could take arbitrary length,
    #: but chunking is what makes a long article interruptible: cancellation is
    #: only checked between chunks.
    MAX_CHARS = 1800

    def __init__(
        self,
        model_dir: Path | str | None = None,
        lang: str = "en-us",
        threads: int | None = None,
    ) -> None:
        try:
            import onnxruntime
            from kokoro_onnx import Kokoro
        except ModuleNotFoundError as exc:  # pragma: no cover - packaging guard
            raise RuntimeError(
                "the kokoro-onnx engine needs `kokoro-onnx` and `onnxruntime`; "
                "install them, or use `tts.engine: kokoro`"
            ) from exc

        directory = Path(model_dir) if model_dir else default_model_dir()
        model = _ensure_file(directory, MODEL_FILE)
        voices = _ensure_file(directory, VOICES_FILE)

        options = onnxruntime.SessionOptions()
        # ONNX Runtime's CPU arena keeps every block it has ever allocated, and
        # an article is synthesized in chunks of varying length, so it grows
        # without bound: measured here at roughly 55 MB per call, nearly a
        # gigabyte across sixteen. Over hours that is what made resident memory
        # climb until the process was killed. Allocating per request instead is
        # flat -- the same measurement moves fifteen megabytes and stays there --
        # and an arena buys little for a model run one chunk at a time.
        options.enable_cpu_mem_arena = False
        # Honour the process-wide thread budget. Left to itself ONNX Runtime
        # takes every core, which with several workers oversubscribes the
        # machine exactly as the PyTorch engine used to.
        resolved = threads or int(os.environ.get("OMP_NUM_THREADS", "0")) or 0
        if resolved:
            options.intra_op_num_threads = resolved
            options.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(
            str(model), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._kokoro = Kokoro.from_session(session, str(voices))
        self._lang = lang

    @property
    def sample_rate(self) -> int:
        return self.SAMPLE_RATE

    @property
    def max_chars(self) -> int:
        return self.MAX_CHARS

    @property
    def default_voice(self) -> str:
        return self.DEFAULT_VOICE

    def synthesize(self, text: str, voice: str | None = None) -> AudioChunk:
        """Synthesize text, batching phonemes ourselves rather than letting
        kokoro-onnx do it.

        Its splitter only breaks on sentence punctuation, so any long stretch
        without any becomes one oversized batch. It then silently truncates that
        batch to the context limit and discards the rest, or crashes outright
        when the truncation lands on exactly 510 tokens. Prose with long URLs,
        code, or dash-joined clauses hits this. Splitting on whitespace as well
        keeps every batch within the limit, so no audio is ever dropped.
        """
        phonemes = self._kokoro.tokenizer.phonemize(text, self._lang)
        parts = phoneme_batches(phonemes, PHONEME_BUDGET)
        rendered = [
            self._kokoro.create(
                part,
                voice=voice or self.DEFAULT_VOICE,
                speed=1.0,
                lang=self._lang,
                is_phonemes=True,
            )[0]
            for part in parts
        ]
        samples = np.concatenate(rendered) if len(rendered) > 1 else rendered[0]
        return AudioChunk(np.asarray(samples, dtype=np.float32), self.SAMPLE_RATE)


def phoneme_batches(phonemes: str, budget: int) -> list[str]:
    """Group phonemes into batches of at most budget characters.

    Breaks at sentence punctuation first so batches fall on natural pauses,
    then at whitespace, and only mid-word when a single word is itself longer
    than the budget, which prose does not produce but pathological input can.
    """
    if not phonemes.strip():
        return [phonemes]
    batches: list[str] = []
    current = ""
    for atom in _atoms(phonemes, budget):
        if not current:
            current = atom
        elif len(current) + 1 + len(atom) <= budget:
            current = current if atom in _PHONEME_BREAKS else current + " "
            current += atom
        else:
            batches.append(current)
            current = atom
    if current:
        batches.append(current)
    return batches


def _atoms(phonemes: str, budget: int) -> list[str]:
    """Split into pieces that each fit the budget, keeping punctuation attached."""
    pieces: list[str] = []
    for clause in re.split(rf"([{re.escape(_PHONEME_BREAKS)}])", phonemes):
        clause = clause.strip()
        if not clause:
            continue
        if len(clause) <= budget:
            pieces.append(clause)
            continue
        for word in clause.split():
            if len(word) <= budget:
                pieces.append(word)
            else:
                pieces.extend(word[i : i + budget] for i in range(0, len(word), budget))
    return pieces


def _ensure_file(directory: Path, name: str) -> Path:
    """Return the model file, downloading it once if absent.

    Mirrors how the PyTorch engine fetches weights on first use, so a fresh
    install needs no manual setup.
    """
    path = directory / name
    if path.exists():
        return path
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".{name}.partial"
    try:
        urllib.request.urlretrieve(f"{MODEL_RELEASE}/{name}", staged)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return path
