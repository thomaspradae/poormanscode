# Telegram Codex operator

`pmc-telegram-control` is a durable, single-threaded Telegram bridge for a
resumable Codex conversation. It stores incoming Telegram update IDs, the
selected conversation, and memorable aliases in SQLite. Restarting its
systemd service neither loses nor replays commands.

It does **not** attempt to mirror `.codex` authentication/session databases
between machines. The durable recovery boundary is the Git repository, PMC
state backup, operator SQLite database, and documented work state. A failed
runner can therefore recreate an operator without corrupting a live session.

## Bootstrap on a Codex runner

First install and authenticate Codex on that runner. Then register an existing
thread, once:

```bash
scripts/pmc-telegram-control register \
  01a027d7-cfc3-7d82-b9d5-9fdf6d73daa5 \
  --title 'Football PMC watchdog' \
  --workspace /home/uace/football-game
```

Run it under a user systemd service:

```bash
systemd-run --user --unit=pmc-telegram-control --property=Restart=always \
  /home/uace/poormans-hpc/poormanscode/scripts/pmc-telegram-control run
```

For a durable installation, copy `systemd/pmc-telegram-control.service` to
`~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable
the service. Enable lingering (`loginctl enable-linger uace`) so it survives
logout/reboot without an interactive desktop session.

## Telegram commands

* `/convos` lists available conversations and their three-word alias.
* `/use amber-river-forge` selects one conversation.
* `/status` shows the selected conversation.
* Any ordinary message is delivered to that selected thread using
  `codex exec resume <thread-id> <message>`.

The alias is only a friendly handle. SQLite retains the actual UUID.

## Primary / standby runners

Run exactly one active controller. The primary is `uace-ofi-01`; install
`pmc-telegram-failover.service` only on `uace-ofi-02`. It waits for three
failed primary checks before starting its local controller and steps aside
again when the primary is healthy.

Before a planned handoff, copy the SQLite database from the active runner to
the standby while its controller is stopped. The database contains processed
Telegram update IDs and the selected conversation.

This is an *automatic cold standby*, not a consensus cluster: a severe network
partition could briefly make both hosts think the other is down. For strict
split-brain prevention, add a third shared lease authority (for example a
small always-on database). Do not run both controller services intentionally.
