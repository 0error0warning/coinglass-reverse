"""All regular tests are offline and isolated from user state."""
import pathlib
import socket
import sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('LIQ_STATE_DIR',str(tmp_path/'liq'))
    monkeypatch.setenv('PREMIUM_STATE_DIR',str(tmp_path/'premium'))
    monkeypatch.delenv('COINALYZE_KEY',raising=False)
    monkeypatch.delenv('MARKET_API_KEY_FILE',raising=False)
    def denied(*args,**kwargs):
        raise AssertionError('Network is forbidden in offline tests; use explicit smoke script')
    monkeypatch.setattr(socket.socket,'connect',denied)
    monkeypatch.setattr(socket.socket,'connect_ex',denied)
