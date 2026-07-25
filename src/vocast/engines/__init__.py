from .engine import AudioChunk, TTSEngine


def get_engine(name: str) -> TTSEngine:
    if name == "kokoro":
        from .kokoro_engine import KokoroEngine

        return KokoroEngine()
    if name in ("kokoro-onnx", "kokoro_onnx"):
        from .kokoro_onnx_engine import KokoroOnnxEngine

        return KokoroOnnxEngine()
    raise ValueError(f"unknown engine: {name!r} (available: kokoro, kokoro-onnx)")


__all__ = ["AudioChunk", "TTSEngine", "get_engine"]
