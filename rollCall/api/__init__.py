"""
REST API package.

The FastAPI app is constructed in `api.main:app`. It's opt-in: the runner
only mounts it when `REST_API_ENABLED=true` is set. See `runner.py` for
the wiring.

Routes are thin controllers that translate Pydantic request models into
primitive args for the `services/` layer, then format service results
into Pydantic response models. The API has no business logic of its own
— that's all in `services/`.
"""
