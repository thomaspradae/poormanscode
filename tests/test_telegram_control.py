from pathlib import Path

from pmc.telegram_control import Controller, Store, alias_for


class FakeTelegram:
    def __init__(self, updates):
        self.chat_id = "42"
        self._updates = updates
        self.sent = []

    def updates(self, offset):
        return [item for item in self._updates if offset is None or item["update_id"] >= offset]

    def send(self, text):
        self.sent.append(text)


def test_alias_is_stable_and_human_readable():
    alias = alias_for("01a027d7-cfc3-7d82-b9d5-9fdf6d73daa5")
    assert alias == alias_for("01a027d7-cfc3-7d82-b9d5-9fdf6d73daa5")
    assert len(alias.split("-")) == 3


def test_controller_selects_and_delivers_without_replaying_updates(tmp_path: Path):
    store = Store(tmp_path / "control.sqlite3")
    first = store.register("a", "Football", "/repo")
    second = store.register("b", "Website", "/web", active=False)
    telegram = FakeTelegram([
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/convos"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "text": f"/use {second.alias}"}},
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "check the build"}},
    ])
    delivered = []
    controller = Controller(store, telegram, lambda item, text: delivered.append((item, text)) or "done")

    assert controller.poll_once() == 3
    assert controller.poll_once() == 0
    assert store.active().thread_id == "b"
    assert delivered[0][0].thread_id == second.thread_id
    assert delivered[0][0].active is True
    assert delivered[0][1] == "check the build"
    assert any(first.alias in message for message in telegram.sent)
    assert telegram.sent[-1] == "done"
