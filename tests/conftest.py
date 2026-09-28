import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from supervisor.state.sqlite import SQLiteStore  # noqa: E402
from supervisor.state.store import MemoryStore  # noqa: E402

STORE_KINDS = ["memory", "sqlite", pytest.param("firestore", marks=pytest.mark.firestore)]


def make_store(kind: str, tmp_path: Path):
    if kind == "memory":
        return MemoryStore()
    if kind == "sqlite":
        return SQLiteStore(str(tmp_path / "state.sqlite3"))
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("FIRESTORE_EMULATOR_HOST non impostato")
    from google.cloud import firestore

    from supervisor.state.firestore import FirestoreStore

    # Un prefisso per test: i documenti di un test non vedono quelli degli altri.
    return FirestoreStore(firestore.Client(project="demo-gtp-supervisor"), prefix=f"t{uuid.uuid4().hex[:8]}_")


@pytest.fixture(params=STORE_KINDS)
def store(request, tmp_path):
    return make_store(request.param, tmp_path)
