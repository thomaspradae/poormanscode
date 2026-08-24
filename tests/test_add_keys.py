from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from pmc.add_keys import add_provider_keys, planned_names


def test_default_additional_names_follow_requested_numbering():
    assert [name for name, _ in planned_names("")] == [
        "MISTRAL_API_KEY_1",
        "MISTRAL_API_KEY_2",
        "MISTRAL_API_KEY_3",
        "GROQ_API_KEY_2",
        "NVIDIA_API_KEY_1",
        "JULES_API_KEY_2",
        "JULES_API_KEY_3",
    ]


def test_add_keys_preserves_existing_values_and_avoids_collisions(tmp_path: Path):
    path = tmp_path / "secrets.env"
    original = "# keep this\nGROQ_API_KEY=existing-groq\nJULES_API_KEY_2=existing-jules\n"
    path.write_text(original)
    path.chmod(0o600)
    supplied = iter(f"new-secret-{number}" for number in range(7))
    output = io.StringIO()

    added = add_provider_keys(
        path, reader=lambda _: next(supplied), output=output
    )

    assert path.read_text().startswith(original)
    assert added == [
        "MISTRAL_API_KEY_1",
        "MISTRAL_API_KEY_2",
        "MISTRAL_API_KEY_3",
        "GROQ_API_KEY_2",
        "NVIDIA_API_KEY_1",
        "JULES_API_KEY_3",
        "JULES_API_KEY_4",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "existing-groq" not in output.getvalue()
    assert "new-secret" not in output.getvalue()


def test_add_keys_rerun_allocates_new_names(tmp_path: Path):
    path = tmp_path / "secrets.env"
    first_values = iter(f"first-{number}" for number in range(7))
    first = add_provider_keys(path, reader=lambda _: next(first_values), output=io.StringIO())
    second_values = iter(f"second-{number}" for number in range(7))
    second = add_provider_keys(path, reader=lambda _: next(second_values), output=io.StringIO())

    assert not set(first).intersection(second)
    assert all(path.read_text().count(f"{name}=") == 1 for name in first + second)


def test_empty_value_leaves_file_unchanged(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text("EXISTING=untouched\n")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="cannot be empty"):
        add_provider_keys(path, reader=lambda _: "", output=io.StringIO())

    assert path.read_text() == "EXISTING=untouched\n"


def test_single_nvidia_key_uses_next_available_number(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text("NVIDIA_API_KEY_1=preserved\n")
    path.chmod(0o600)
    output = io.StringIO()

    added = add_provider_keys(
        path,
        reader=lambda _: "new-nvidia-secret",
        output=output,
        requests=(("NVIDIA_API_KEY", "Additional NVIDIA key", 1, 1),),
    )

    assert added == ["NVIDIA_API_KEY_2"]
    assert path.read_text().startswith("NVIDIA_API_KEY_1=preserved\n")
    assert "new-nvidia-secret" not in output.getvalue()


def test_two_gemini_keys_start_at_two_and_skip_collisions(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text("GEMINI_API_KEY_2=preserved\n")
    path.chmod(0o600)
    supplied = iter(("gemini-secret-a", "gemini-secret-b"))
    output = io.StringIO()

    added = add_provider_keys(
        path,
        reader=lambda _: next(supplied),
        output=output,
        requests=(("GEMINI_API_KEY", "Additional Gemini key", 2, 2),),
    )

    assert added == ["GEMINI_API_KEY_3", "GEMINI_API_KEY_4"]
    assert "gemini-secret" not in output.getvalue()


def test_single_groq_key_skips_existing_second_key(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text("GROQ_API_KEY_2=preserved\n")
    path.chmod(0o600)
    output = io.StringIO()

    added = add_provider_keys(
        path,
        reader=lambda _: "new-groq-secret",
        output=output,
        requests=(("GROQ_API_KEY", "Additional Groq key", 1, 2),),
    )

    assert added == ["GROQ_API_KEY_3"]
    assert "new-groq-secret" not in output.getvalue()
