from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opencompany.remote import (
    build_remote_password_ref,
    delete_remote_session_password,
    delete_remote_session_config,
    load_remote_session_password,
    load_remote_session_config,
    normalize_remote_session_config,
    parse_ssh_target,
    save_remote_session_password,
    save_remote_session_config,
    session_remote_config_path,
)


class RemoteConfigTests(unittest.TestCase):
    def test_parse_ssh_target_supports_optional_port(self) -> None:
        user, host, port = parse_ssh_target("demo@example.com:2222")
        self.assertEqual(user, "demo")
        self.assertEqual(host, "example.com")
        self.assertEqual(port, 2222)

    def test_parse_ssh_target_rejects_invalid_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "user@host"):
            parse_ssh_target("example.com")

    def test_remote_config_round_trip_does_not_persist_password(self) -> None:
        with TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            config = normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com:22",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "password",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )
            path = save_remote_session_config(session_dir, config)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("password", payload)
            loaded = load_remote_session_config(session_dir)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.ssh_target, "demo@example.com:22")
            self.assertEqual(loaded.remote_dir, "/home/demo/workspace")
            self.assertEqual(loaded.auth_mode, "password")

    def test_remote_config_requires_absolute_remote_dir(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute Linux path"):
            normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com",
                    "remote_dir": "relative/path",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )

    def test_remote_config_rejects_invalid_known_hosts_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "known_hosts_policy"):
            normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "allow_anything",
                    "remote_os": "linux",
                }
            )

    def test_remote_config_rejects_non_linux_remote_os(self) -> None:
        with self.assertRaisesRegex(ValueError, "remote_os must be linux"):
            normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "darwin",
                }
            )

    def test_remote_config_requires_key_path_for_key_auth(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity_file is required"):
            normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )

    def test_remote_config_password_auth_clears_identity_path(self) -> None:
        config = normalize_remote_session_config(
            {
                "kind": "remote_ssh",
                "ssh_target": "demo@example.com",
                "remote_dir": "/home/demo/workspace",
                "auth_mode": "password",
                "identity_file": "~/.ssh/id_should_be_ignored",
                "known_hosts_policy": "accept_new",
                "remote_os": "linux",
            }
        )
        self.assertEqual(config.identity_file, "")

    def test_remote_config_defaults_known_hosts_to_accept_new(self) -> None:
        config = normalize_remote_session_config(
            {
                "kind": "remote_ssh",
                "ssh_target": "demo@example.com",
                "remote_dir": "/home/demo/workspace",
                "auth_mode": "key",
                "identity_file": "~/.ssh/id_ed25519",
                "remote_os": "linux",
            }
        )
        self.assertEqual(config.known_hosts_policy, "accept_new")

    def test_delete_remote_session_config_removes_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir)
            config = normalize_remote_session_config(
                {
                    "kind": "remote_ssh",
                    "ssh_target": "demo@example.com",
                    "remote_dir": "/home/demo/workspace",
                    "auth_mode": "key",
                    "identity_file": "~/.ssh/id_ed25519",
                    "known_hosts_policy": "accept_new",
                    "remote_os": "linux",
                }
            )
            save_remote_session_config(session_dir, config)
            path = session_remote_config_path(session_dir)
            self.assertTrue(path.exists())
            delete_remote_session_config(session_dir)
            self.assertFalse(path.exists())
            self.assertIsNone(load_remote_session_config(session_dir))

    def test_build_remote_password_ref_is_deterministic(self) -> None:
        config = normalize_remote_session_config(
            {
                "kind": "remote_ssh",
                "ssh_target": "demo@example.com:22",
                "remote_dir": "/home/demo/workspace",
                "auth_mode": "password",
                "known_hosts_policy": "accept_new",
                "remote_os": "linux",
            }
        )
        first = build_remote_password_ref("session-1", config)
        second = build_remote_password_ref("session-1", config)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("remote-password-"))

    def test_save_remote_session_password_uses_available_backend(self) -> None:
        with (
            patch("opencompany.remote._keyring_set", return_value=True) as keyring_set,
            patch("opencompany.remote._fallback_store_set", return_value=True) as fallback_set,
        ):
            save_remote_session_password("ref-1", "secret-pass")
        keyring_set.assert_called_once_with("ref-1", "secret-pass")
        fallback_set.assert_called_once_with("ref-1", "secret-pass")

    def test_save_remote_session_password_fails_without_backend(self) -> None:
        with (
            patch("opencompany.remote._keyring_set", return_value=False),
            patch("opencompany.remote._macos_security_set", return_value=False),
            patch("opencompany.remote._linux_secret_tool_set", return_value=False),
            patch("opencompany.remote._fallback_store_set", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "fallback-store"):
                save_remote_session_password("ref-1", "secret-pass")

    def test_load_remote_session_password_falls_back_across_backends(self) -> None:
        with (
            patch("opencompany.remote._keyring_get", return_value=None),
            patch("opencompany.remote._macos_security_get", return_value="secret-pass"),
            patch("opencompany.remote._linux_secret_tool_get", return_value=None),
            patch("opencompany.remote._fallback_store_get", return_value=None),
        ):
            self.assertEqual(load_remote_session_password("ref-1"), "secret-pass")

    def test_delete_remote_session_password_is_best_effort(self) -> None:
        with (
            patch("opencompany.remote._keyring_delete", return_value=False) as keyring_delete,
            patch("opencompany.remote._macos_security_delete", return_value=True) as mac_delete,
            patch("opencompany.remote._linux_secret_tool_delete", return_value=False) as linux_delete,
            patch("opencompany.remote._fallback_store_delete", return_value=False) as fallback_delete,
        ):
            delete_remote_session_password("ref-1")
        keyring_delete.assert_called_once_with("ref-1")
        mac_delete.assert_called_once_with("ref-1")
        linux_delete.assert_called_once_with("ref-1")
        fallback_delete.assert_called_once_with("ref-1")

    def test_save_remote_session_password_uses_encrypted_fallback_store(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with (
                patch.dict(os.environ, {"OPENCOMPANY_HOME": temp_dir}, clear=False),
                patch("opencompany.remote._keyring_set", return_value=False),
                patch("opencompany.remote._macos_security_set", return_value=False),
                patch("opencompany.remote._linux_secret_tool_set", return_value=False),
                patch("opencompany.remote._keyring_get", return_value=None),
                patch("opencompany.remote._macos_security_get", return_value=None),
                patch("opencompany.remote._linux_secret_tool_get", return_value=None),
                patch("opencompany.remote._keyring_delete", return_value=False),
                patch("opencompany.remote._macos_security_delete", return_value=False),
                patch("opencompany.remote._linux_secret_tool_delete", return_value=False),
            ):
                save_remote_session_password("ref-fallback", "secret-pass")
                loaded = load_remote_session_password("ref-fallback")
                self.assertEqual(loaded, "secret-pass")

                store_path = Path(temp_dir) / "secrets" / "remote_passwords.json"
                self.assertTrue(store_path.exists())
                store_text = store_path.read_text(encoding="utf-8")
                self.assertNotIn("secret-pass", store_text)

                delete_remote_session_password("ref-fallback")
                self.assertIsNone(load_remote_session_password("ref-fallback"))
