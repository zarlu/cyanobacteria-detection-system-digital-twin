# db/ (owner: A)

SQLAlchemy 2.0 models (`models.py`), engine and session factory with SQLite WAL (`engine.py`),
`init_db()` plus seeding from `contract/channels.yaml` and `config/reach.yaml` (`seed.py`), and
`python -m db.reset` (the migration strategy: drop and recreate all tables).
