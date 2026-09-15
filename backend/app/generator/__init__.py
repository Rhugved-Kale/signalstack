"""Synthetic data generation for SignalStack.

This package builds a self-consistent fake world (`world.py`) and then exposes
it through fake HTTP-like APIs that fail and corrupt data the way real ones do
(`fake_apis.py`).

It deliberately has **no dependency on `app.db`** and never touches a database:
it produces plain Python dicts shaped like JSON API responses. Persisting them
is the ingestion layer's job.
"""
