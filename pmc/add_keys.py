from __future__ import annotations

import argparse
import getpass
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

DEFAULT_SECRETS = Path("~/.config/poormans-code/secrets.env").expanduser()
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE
)
_REQUESTS = (
    ("MISTRAL_API_KEY", "Mistral key", 3, 1),
    ("GROQ_API_KEY", "Additional Groq key", 1, 2),
    ("NVIDIA_API_KEY", "NVIDIA key", 1, 1),
    ("JULES_API_KEY", "Additional Jules key", 2, 2),
)


def _next_names(
    existing: set[str], base: str, count: int, first_number: int
) -> list[str]:
    used = {
        int(match.group(1))
        for name in existing
        if (match := re.fullmatch(re.escape(base) + r"_(\d+)", name))
    }
    # The established unnumbered credential is conventionally slot 1.
    if base in existing:
        used.add(1)
    names: list[str] = []
    number = first_number
    while len(names) < count:
        if number not in used:
            names.append(f"{base}_{number}")
            used.add(number)
        number += 1
    return names


def planned_names(
    contents: str,
    requests: tuple[tuple[str, str, int, int], ...] = _REQUESTS,
) -> list[tuple[str, str]]:
    existing = set(_ASSIGNMENT.findall(contents))
    planned: list[tuple[str, str]] = []
    for base, label, count, first_number in requests:
        names = _next_names(existing, base, count, first_number)
        for index, name in enumerate(names, 1):
            prompt = label if count == 1 else f"{label} {index}"
            planned.append((name, prompt))
        existing.update(names)
    return planned


def add_provider_keys(
    path: Path = DEFAULT_SECRETS,
    *,
    reader: Callable[[str], str] = getpass.getpass,
    output: TextIO | None = None,
    requests: tuple[tuple[str, str, int, int], ...] = _REQUESTS,
) -> list[str]:
    import sys

    output = output or sys.stdout
    path = path.expanduser()
    original = path.read_text() if path.exists() else ""
    additions = planned_names(original, requests)

    print("Adding PMC provider credentials.", file=output)
    print("Values will not be displayed.\n", file=output)
    values: list[tuple[str, str]] = []
    for name, prompt in additions:
        value = reader(f"{prompt}: ")
        if not value:
            raise ValueError(f"{name} cannot be empty")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} cannot contain a newline")
        values.append((name, value))

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    separator = "" if not original or original.endswith("\n") else "\n"
    appended = "".join(f"{name}={value}\n" for name, value in values)
    fd, temporary = tempfile.mkstemp(prefix=".secrets.env.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(original)
            stream.write(separator)
            stream.write(appended)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

    added = [name for name, _ in values]
    print("\nAdded:", file=output)
    for name in added:
        print(f"  {name}", file=output)
    print("\nSecrets file permissions: 600", file=output)
    return added


def main() -> int:
    try:
        add_provider_keys()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No credentials were changed.")
        return 130
    except ValueError as exc:
        print(f"Credential collection failed: {exc}")
        return 2
    return 0


def nvidia_main() -> int:
    try:
        add_provider_keys(
            requests=(("NVIDIA_API_KEY", "Additional NVIDIA key", 1, 1),)
        )
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No credentials were changed.")
        return 130
    except ValueError as exc:
        print(f"Credential collection failed: {exc}")
        return 2
    return 0


def gemini_main() -> int:
    try:
        add_provider_keys(
            requests=(("GEMINI_API_KEY", "Additional Gemini key", 2, 2),)
        )
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No credentials were changed.")
        return 130
    except ValueError as exc:
        print(f"Credential collection failed: {exc}")
        return 2
    return 0


def groq_main() -> int:
    try:
        add_provider_keys(
            requests=(("GROQ_API_KEY", "Additional Groq key", 1, 2),)
        )
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No credentials were changed.")
        return 130
    except ValueError as exc:
        print(f"Credential collection failed: {exc}")
        return 2
    return 0


def batch_main() -> int:
    """Collect an explicitly requested number of additional provider keys."""
    parser = argparse.ArgumentParser(description="Add PMC provider credentials safely")
    parser.add_argument("--mistral", type=int, default=0)
    parser.add_argument("--groq", type=int, default=0)
    parser.add_argument("--jules", type=int, default=0)
    parser.add_argument("--nvidia", type=int, default=0)
    args = parser.parse_args()
    counts = {
        "mistral": args.mistral,
        "groq": args.groq,
        "jules": args.jules,
        "nvidia": args.nvidia,
    }
    if any(value < 0 for value in counts.values()) or not any(counts.values()):
        parser.error("provide at least one non-negative provider count")
    requests = tuple(
        (base, label, count, first)
        for key, base, label, first in (
            ("mistral", "MISTRAL_API_KEY", "Mistral key", 1),
            ("groq", "GROQ_API_KEY", "Additional Groq key", 2),
            ("jules", "JULES_API_KEY", "Additional Jules key", 2),
            ("nvidia", "NVIDIA_API_KEY", "Additional NVIDIA key", 1),
        )
        for count in (counts[key],)
        if count
    )
    try:
        add_provider_keys(requests=requests)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No credentials were changed.")
        return 130
    except ValueError as exc:
        print(f"Credential collection failed: {exc}")
        return 2
    return 0
