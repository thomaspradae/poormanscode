"""Durable Telegram control plane for a resumable local Codex operator.

The controller intentionally owns the queue and conversation registry instead
of treating Telegram as a terminal.  It can be restarted at any point without
replaying an update or losing the selected Codex thread.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pmc-telegram-control"
DEFAULT_SECRETS = Path.home() / ".config" / "poormans-code" / "telegram.env"
DEFAULT_WORKSPACE = Path.home() / "football-game"
WORDS_A = ("amber", "cedar", "copper", "ember", "harbor", "maple", "river", "solar")
WORDS_B = ("bird", "field", "hill", "lake", "north", "orchid", "stone", "valley")
WORDS_C = ("atlas", "bridge", "canyon", "delta", "forge", "grove", "harvest", "signal")


@dataclass(frozen=True)
class Conversation:
    thread_id: str
    alias: str
    title: str
    workspace: str
    active: bool


def alias_for(thread_id: str) -> str:
    """Create a memorable deterministic label; IDs remain the authority."""
    digest = hashlib.sha256(thread_id.encode("utf-8")).digest()
    return "-".join((
        WORDS_A[digest[0] % len(WORDS_A)],
        WORDS_B[digest[1] % len(WORDS_B)],
        WORDS_C[digest[2] % len(WORDS_C)],
    ))


class Store:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(database)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS conversations (
              thread_id TEXT PRIMARY KEY,
              alias TEXT UNIQUE NOT NULL,
              title TEXT NOT NULL,
              workspace TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 0,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telegram_updates (
              update_id INTEGER PRIMARY KEY,
              received_at INTEGER NOT NULL,
              text TEXT NOT NULL,
              handled_at INTEGER,
              result TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.conn.commit()

    def register(self, thread_id: str, title: str, workspace: str, active: bool = True) -> Conversation:
        alias = alias_for(thread_id)
        now = int(time.time())
        with self.conn:
            if active:
                self.conn.execute("UPDATE conversations SET active = 0")
            self.conn.execute(
                """INSERT INTO conversations(thread_id, alias, title, workspace, active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET title=excluded.title, workspace=excluded.workspace,
                  active=excluded.active, updated_at=excluded.updated_at""",
                (thread_id, alias, title, workspace, int(active), now),
            )
        return self.get(thread_id)

    def get(self, selector: str) -> Conversation:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE thread_id = ? OR alias = ?", (selector, selector)
        ).fetchone()
        if row is None:
            raise KeyError(selector)
        return Conversation(row["thread_id"], row["alias"], row["title"], row["workspace"], bool(row["active"]))

    def active(self) -> Conversation | None:
        row = self.conn.execute("SELECT * FROM conversations WHERE active = 1 ORDER BY updated_at DESC LIMIT 1").fetchone()
        return None if row is None else Conversation(row["thread_id"], row["alias"], row["title"], row["workspace"], True)

    def select(self, selector: str) -> Conversation:
        item = self.get(selector)
        with self.conn:
            self.conn.execute("UPDATE conversations SET active = 0")
            self.conn.execute("UPDATE conversations SET active = 1, updated_at = ? WHERE thread_id = ?", (int(time.time()), item.thread_id))
        return self.get(item.thread_id)

    def conversations(self) -> list[Conversation]:
        rows = self.conn.execute("SELECT * FROM conversations ORDER BY active DESC, updated_at DESC").fetchall()
        return [Conversation(row["thread_id"], row["alias"], row["title"], row["workspace"], bool(row["active"])) for row in rows]

    def seen(self, update_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM telegram_updates WHERE update_id = ?", (update_id,)).fetchone() is not None

    def record_update(self, update_id: int, text: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO telegram_updates(update_id, received_at, text) VALUES (?, ?, ?)", (update_id, int(time.time()), text))

    def finish_update(self, update_id: int, result: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE telegram_updates SET handled_at = ?, result = ? WHERE update_id = ?", (int(time.time()), result, update_id))

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def request(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        data = None if payload is None else urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310: fixed Telegram API origin
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} failed")
        return result

    def updates(self, offset: int | None) -> list[dict[str, object]]:
        payload: dict[str, object] = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            payload["offset"] = offset
        return list(self.request("getUpdates", payload).get("result", []))

    def send(self, text: str) -> None:
        # Telegram's message limit is 4096; preserve the useful tail if an agent is verbose.
        safe = text[-4000:]
        self.request("sendMessage", {"chat_id": self.chat_id, "text": safe})


def load_secrets(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" in raw:
            key, value = raw.split("=", 1)
            values[key] = value.strip().strip("'\"")
    if not values.get("TELEGRAM_BOT_TOKEN") or not values.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError(f"Missing Telegram credentials in {path}")
    return values


def compact(text: str, limit: int = 3600) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"…{text[-limit:]}"


class Controller:
    def __init__(self, store: Store, telegram: Telegram, run_agent: Callable[[Conversation, str], str]) -> None:
        self.store = store
        self.telegram = telegram
        self.run_agent = run_agent

    def commands(self) -> str:
        return (
            "Commands:\n"
            "/status — selected conversation\n"
            "/convos — known Codex conversations\n"
            "/use <three-word-alias> — switch conversation\n"
            "/help — this message\n\n"
            "Any normal message is delivered to the selected Codex operator."
        )

    def handle(self, update_id: int, text: str) -> str:
        text = text.strip()
        if text in {"/help", "/start"}:
            return self.commands()
        if text == "/convos":
            items = self.store.conversations()
            if not items:
                return "No registered Codex conversation. Bootstrap one with pmc-telegram-control register."
            return "Codex conversations:\n" + "\n".join(
                f"{'●' if item.active else '○'} {item.alias}\n  {item.title}" for item in items
            )
        if text == "/status":
            item = self.store.active()
            return "No selected conversation." if item is None else f"Selected: {item.title}\nAlias: {item.alias}\nWorkspace: {item.workspace}"
        if text.startswith("/use "):
            selector = text[5:].strip()
            try:
                item = self.store.select(selector)
            except KeyError:
                return f"Unknown conversation: {selector}. Send /convos."
            return f"Selected {item.title} ({item.alias})."
        if text.startswith("/"):
            return "Unknown command. Send /help."
        item = self.store.active()
        if item is None:
            return "No selected Codex conversation. Send /convos, then /use <alias>."
        self.telegram.send(f"Working in {item.alias}: {compact(text, 500)}")
        return compact(self.run_agent(item, text))

    def poll_once(self) -> int:
        last = self.store.get_meta("telegram_offset")
        updates = self.telegram.updates(int(last) if last else None)
        handled = 0
        for update in updates:
            update_id = int(update["update_id"])
            self.store.set_meta("telegram_offset", str(update_id + 1))
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != self.telegram.chat_id:
                continue
            text = str(message.get("text") or "")
            if not text or self.store.seen(update_id):
                continue
            self.store.record_update(update_id, text)
            try:
                result = self.handle(update_id, text)
            except Exception as exc:  # report, but keep the queue moving
                result = f"Operator error: {type(exc).__name__}: {exc}"
            self.store.finish_update(update_id, result)
            self.telegram.send(result)
            handled += 1
        return handled


def codex_runner(model: str) -> Callable[[Conversation, str], str]:
    def run(item: Conversation, message: str) -> str:
        prompt = (
            "You are the Football World Lab operator. This message arrived via the owner's Telegram bot. "
            "Inspect current PMC/repository state before acting. You may repair, test, commit, push, "
            "accept, or resume game work when justified. Preserve work and explain what you did concisely.\n\n"
            f"Telegram message:\n{message}"
        )
        # resume deliberately preserves the named Codex conversation. Approval policy is the
        # configuration equivalent of --approve-for-me, which the resume subcommand does not expose.
        command = [
            "codex", "exec", "resume", "-m", model,
            "-c", 'approval_policy="on-request_auto_review"',
            "-c", 'sandbox_mode="workspace-write"',
            item.thread_id, prompt,
        ]
        result = subprocess.run(command, cwd=item.workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600, check=False)
        if result.returncode:
            return f"Codex stopped (exit {result.returncode}):\n{compact(result.stdout)}"
        return result.stdout or "Codex completed without a text summary."
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    parser.add_argument("--model", default="gpt-5.6-luna")
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register", help="register an existing resumable Codex thread")
    register.add_argument("thread_id")
    register.add_argument("--title", required=True)
    register.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    sub.add_parser("list", help="list registered conversations")
    sub.add_parser("poll-once", help="receive and process pending Telegram messages")
    run = sub.add_parser("run", help="run the durable Telegram control loop")
    run.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args(argv)
    store = Store(args.state_dir / "control.sqlite3")
    if args.command == "register":
        item = store.register(args.thread_id, args.title, args.workspace)
        print(f"registered {item.alias}: {item.title}")
        return 0
    if args.command == "list":
        for item in store.conversations():
            print(f"{'*' if item.active else ' '} {item.alias}\t{item.title}\t{item.thread_id}")
        return 0
    secrets = load_secrets(args.secrets)
    controller = Controller(store, Telegram(secrets["TELEGRAM_BOT_TOKEN"], secrets["TELEGRAM_CHAT_ID"]), codex_runner(args.model))
    if args.command == "poll-once":
        print(f"handled={controller.poll_once()}")
        return 0
    while True:
        controller.poll_once()
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
