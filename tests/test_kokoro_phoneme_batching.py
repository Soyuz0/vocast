import numpy as np

from vocast.engines.kokoro_onnx_engine import (
    PHONEME_BUDGET,
    KokoroOnnxEngine,
    phoneme_batches,
)

BUDGET = 40


def test_short_input_is_left_as_one_batch():
    assert phoneme_batches("hˈɛlˌoʊ wˈɜːld.", BUDGET) == ["hˈɛlˌoʊ wˈɜːld."]


def test_a_long_run_without_punctuation_is_still_split():
    """The bug this exists for: kokoro-onnx only breaks on sentence punctuation,
    so an unpunctuated run became one oversized batch and got truncated."""
    run = " ".join(["wˈɜːd"] * 60)

    batches = phoneme_batches(run, BUDGET)

    assert len(batches) > 1
    assert all(len(b) <= BUDGET for b in batches)


def test_no_batch_ever_exceeds_the_budget():
    text = "ðɪs ɪz ə klˈɔːz, ænd ənˈʌðɚ wˈʌn; " + "lˈɔŋ " * 200 + "ˈɛnd."

    assert all(len(b) <= BUDGET for b in phoneme_batches(text, BUDGET))


def test_a_single_word_longer_than_the_budget_is_sliced():
    """Nothing may be dropped, even for input prose would never produce."""
    word = "x" * 95

    batches = phoneme_batches(word, BUDGET)

    assert "".join(batches) == word
    assert all(len(b) <= BUDGET for b in batches)


def test_every_phoneme_survives_batching():
    text = "fˈɜːst klˈɔːz, sˈɛkənd klˈɔːz. " + "mˈoːɹ wˈɜːdz " * 40

    joined = "".join(phoneme_batches(text, BUDGET)).replace(" ", "")

    assert joined == text.replace(" ", "")


def test_punctuation_stays_attached_to_its_clause():
    """Batches should fall on pauses, not orphan a comma onto the next batch."""
    batches = phoneme_batches("ˈeɪ, bˈiː. sˈiː", BUDGET)

    assert batches == ["ˈeɪ, bˈiː. sˈiː"]
    assert " ," not in batches[0]


def test_budget_leaves_room_below_the_model_context():
    """510 tokens indexes off the end of a 510-row style array, so the budget
    must stay strictly under it."""
    assert PHONEME_BUDGET < 510


class FakeTokenizer:
    """Stands in for espeak. Returns phonemes roughly 1.3x the input length,
    which is the ratio the real phonemizer produces for English prose."""

    def phonemize(self, text: str, lang: str) -> str:
        return text.replace("o", "ˈoʊ")


class FakeKokoro:
    """Records what the engine asks it to render.

    The real library caps batches at 510 phoneme tokens and indexes a 510-row
    style array by token count, so anything at or above that limit is a bug.
    """

    STYLE_ROWS = 510

    def __init__(self) -> None:
        self.tokenizer = FakeTokenizer()
        self.batches: list[str] = []
        self.phoneme_flags: list[bool] = []

    def create(self, text, voice, speed, lang, is_phonemes=False):
        if len(text) >= self.STYLE_ROWS:
            raise IndexError(
                f"index {self.STYLE_ROWS} is out of bounds for axis 0 "
                f"with size {self.STYLE_ROWS}"
            )
        self.batches.append(text)
        self.phoneme_flags.append(is_phonemes)
        return np.ones(len(text), dtype=np.float32), 24000


def _engine_with(fake: FakeKokoro) -> KokoroOnnxEngine:
    engine = KokoroOnnxEngine.__new__(KokoroOnnxEngine)
    engine._kokoro = fake
    engine._lang = "en-us"
    return engine


def test_engine_never_hands_the_model_an_oversized_batch():
    """Regression: an unpunctuated run used to arrive as one batch, which the
    library truncated to the context limit, silently discarding the remainder."""
    fake = FakeKokoro()
    unpunctuated = " ".join(["overlong"] * 300)

    _engine_with(fake).synthesize(unpunctuated)

    assert len(fake.batches) > 1
    assert all(len(b) < FakeKokoro.STYLE_ROWS for b in fake.batches)


def test_engine_passes_pre_phonemized_text_through():
    """Phonemizing once ourselves is what lets us measure batch size; the
    library must not redo it."""
    fake = FakeKokoro()

    _engine_with(fake).synthesize("Some ordinary prose.")

    assert fake.phoneme_flags == [True]


def test_engine_returns_all_batches_joined():
    fake = FakeKokoro()
    text = " ".join(["overlong"] * 300)

    result = _engine_with(fake).synthesize(text)

    assert len(result.samples) == sum(len(b) for b in fake.batches)
    assert result.sample_rate == 24000
