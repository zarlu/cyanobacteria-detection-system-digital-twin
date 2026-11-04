# api/ (owner: A; `api/routers/ml.py` is owned by B)

FastAPI application. `app.py` holds the app factory `create_app()`, `settings.py` the environment
settings, `deps.py` the shared dependencies (settings, DB session, API key), `routers/` one module
per resource, `qc.py` the server-side quality control driven by `contract/channels.yaml`.
