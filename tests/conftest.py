import os
import socket
import threading
from pathlib import Path

import pytest
import yaml
from werkzeug.serving import make_server

from meridian import app as meridian_app

DEMO_USER = "operator1"
DEMO_PASS = "teller!23"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def meridian_url():
    """The sample app on a random port, in a thread, for the whole test session."""
    port = _free_port()
    server = make_server("127.0.0.1", port, meridian_app.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    os.environ["MERIDIAN_URL"] = url
    os.environ.setdefault("MERIDIAN_USER", DEMO_USER)
    os.environ.setdefault("MERIDIAN_PASS", DEMO_PASS)
    yield url
    server.shutdown()


@pytest.fixture
def policy_file(tmp_path: Path, meridian_url: str) -> str:
    base = yaml.safe_load(Path("policy.yaml").read_text(encoding="utf-8"))
    base["allowed_origins"] = [meridian_url]
    base["handoff_timeout_s"] = 20
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return str(p)


@pytest.fixture(autouse=True)
def _reset_sample_app():
    meridian_app.FAULTS.clear()
    meridian_app.MEMBERS.clear()
    import copy
    meridian_app.MEMBERS.update(copy.deepcopy(meridian_app.SEED_MEMBERS))
    yield
