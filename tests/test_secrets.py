import subprocess

from video_translator import secrets as secrets_module
from video_translator.secrets import KeychainSecretStore


def test_keychain_secret_round_trip_commands(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "find-generic-password" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="saved-secret\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(secrets_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        secrets_module.shutil,
        "which",
        lambda _: "/usr/bin/security",
    )
    monkeypatch.setattr(secrets_module.subprocess, "run", fake_run)
    store = KeychainSecretStore()

    reference = store.save(
        "saved-secret",
        reference="a" * 32,
    )
    value = store.read(reference)
    store.delete(reference)

    assert reference == "a" * 32
    assert value == "saved-secret"
    assert "add-generic-password" in calls[0]
    assert "find-generic-password" in calls[1]
    assert "delete-generic-password" in calls[2]
