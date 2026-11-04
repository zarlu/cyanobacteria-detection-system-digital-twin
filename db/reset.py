"""``python -m db.reset``: drop and recreate all tables behind ``DB_URL``, then seed.

This is the project's migration strategy (no Alembic). The database file is kept; its tables are
replaced. Safe to run on a missing database.
"""

from __future__ import annotations

import logging
import sys

from db.engine import db_url_from_env, drop_all, init_db, make_engine, make_session_factory
from db.seed import seed_all


def main() -> int:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    url = db_url_from_env()
    engine = make_engine(url)
    drop_all(engine)
    init_db(engine)
    with make_session_factory(engine)() as session:
        seed_all(session)
    print(f"reset {url}: tables recreated and seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
