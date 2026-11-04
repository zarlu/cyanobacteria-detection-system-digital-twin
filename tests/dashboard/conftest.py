"""Start real API servers (subprocess uvicorn, temporary SQLite) for AppTest smoke tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from sim.client import SimClient
from sim.run import generate, parse_args, post_all

REPO = Path(__file__).resolve().parents[2]
KEY = "dash-test-key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiServer:
    def __init__(self, tmp: Path) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = {**os.environ, "DB_URL": f"sqlite:///{tmp / 'api.db'}", "API_KEY": KEY}
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "api.app:create_app",
                "--factory",
                "--port",
                str(self.port),
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.url}/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError("API server did not start")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="module")
def empty_api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ApiServer]:
    server = ApiServer(tmp_path_factory.mktemp("empty"))
    yield server
    server.stop()


@pytest.fixture(scope="module")
def populated_api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ApiServer]:
    """API with 3 days of recent summer_bloom data (ending now) so time ranges hit rows."""
    server = ApiServer(tmp_path_factory.mktemp("populated"))
    out = generate(
        parse_args(["--scenario", "summer_bloom", "--days", "3", "--seed", "1", "--step-min", "30"])
    )
    client = SimClient(server.url, KEY)
    post_all(out, client, mode="backfill", speedup=1.0, sim_start=out.batches[0].sent_at)
    assert client.stats.accepted > 0 and client.stats.failed_requests == 0
    yield server
    server.stop()
