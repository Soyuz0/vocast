"""RSS ingestion — poll feeds, queue articles, turn them into episodes.

This subpackage layers a continuously-running service on top of vocast's
existing article -> audio pipeline. Nothing in here reimplements extraction,
chunking, TTS, or audio encoding; those are reused through
`vocast.ingest.generator`.
"""
