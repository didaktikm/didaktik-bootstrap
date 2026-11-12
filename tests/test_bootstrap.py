"""Host-safe tests: Linux commands and privilege changes are always mocked."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("bootstrap", Path(__file__).resolve().parents[1] / "bootstrap.py")
b = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = b
SPEC.loader.exec_module(b)


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "error" if code else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(b, "DRY_RUN", False))
        self.stack.enter_context(patch.object(b, "DAEMON_JSON", self.root / "daemon.json"))
        self.stack.enter_context(patch.object(b, "log"))
        # Any unmocked command is a test failure, never a host operation.
        self.subprocess = self.stack.enter_context(patch.object(b.subprocess, "run", side_effect=AssertionError("unmocked command")))
        self.stack.enter_context(patch.object(b.os, "execvp", side_effect=AssertionError("unmocked exec")))

    def write(self, path, content, **kwargs):
        return b.write_file(path, content, uid=os.getuid(), gid=os.getgid(), **kwargs)


class CliTests(Base):
    def test_defaults_include_github_cli(self):
        self.assertIn("git", b.BASE_PACKAGES)
        self.assertIn("gh", b.BASE_PACKAGES)

    def test_defaults_and_dependencies(self):
        state = {o.key: o.default for o in b.build_options("alice", "UTC", "/opt/docker")}
        for key in ("sudo_nopasswd", "docker_group", "docker_move", "firewall_off"):
            self.assertFalse(state[key])
        state.update(docker=False, docker_move=True)
        with self.assertRaises(b.BootstrapError):
            b.resolve_dependencies(state)
        self.assertFalse(state["docker"])

    def test_conflicting_cli(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            b.parse_args(["--enable", "docker", "--disable", "docker"])

    def test_root_needs_user(self):
        with patch.object(b.os, "geteuid", return_value=0), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(b.BootstrapError):
                b.default_user()
        with patch.object(b.os, "geteuid", return_value=0), patch.dict(os.environ, {"SUDO_USER": "alice"}):
            self.assertEqual(b.default_user(), "alice")

    def test_sudo_precedes_su_and_preserves_consent(self):
        args = b.parse_args(["alice", "--replace-container-runtime"])
        with patch.object(b.shutil, "which", side_effect=lambda x: "/usr/bin/" + x), patch.object(b.os, "execvp") as execute:
            b.escalate(args, {"docker": True}, "alice")
        argv = execute.call_args.args[1]
        self.assertEqual(argv[0], "/usr/bin/sudo")
        self.assertIn("--replace-container-runtime", argv)
        self.subprocess.assert_not_called()

    def test_su_fallback(self):
        with patch.object(b.shutil, "which", side_effect=lambda x: "/bin/su" if x == "su" else None), patch.object(b.os, "execvp") as execute:
            b.escalate(b.parse_args([]), {}, "alice")
        self.assertEqual(execute.call_args.args[0], "su")

    def test_dry_run_escalation_and_commands(self):
        b.DRY_RUN = True
        b.escalate(b.parse_args([]), {}, "alice")
        b.run(["apt-get", "install", "bad"])
        with patch.object(b, "LOCK_PATH", self.root / "missing" / "lock"):
            self.assertIsNone(b.acquire_lock())
        self.assertEqual(list(self.root.iterdir()), [])
        self.subprocess.assert_not_called()

    def test_run_errors(self):
        for error in (FileNotFoundError("missing"), subprocess.TimeoutExpired("test", 1)):
            with self.subTest(error=error), patch.object(b.subprocess, "run", side_effect=error):
                with self.assertRaises(b.BootstrapError):
                    b.run(["test"], mutating=False)

    def test_supplementary_groups_replaced(self):
        account = b.Account("alice", 1000, 1000, self.root)
        with patch.object(b.os, "geteuid", return_value=0), patch.object(b.os, "getgrouplist", return_value=[1000, 42]), patch.object(b.subprocess, "run", return_value=result()) as command:
            b.run(["true"], acc=account)
        self.assertEqual(command.call_args.kwargs["extra_groups"], [1000, 42])

    def test_dpkg_error_is_not_missing_package(self):
        with patch.object(b, "run", return_value=result(code=2)), self.assertRaises(b.BootstrapError):
            b.package_installed("zsh")


class FileTests(Base):
    def test_backup_unique_and_metadata_repaired(self):
        path = self.root / "file"
        self.write(path, b"one", mode=0o600)
        self.assertFalse(self.write(path, b"one", mode=0o600))
        self.write(path, b"one", mode=0o644)
        self.write(path, b"two")
        self.assertEqual(len(list(self.root.glob("file.bak.*"))), 2)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_validate_failure_cleans_temp_and_preserves_original(self):
        path = self.root / "file"
        path.write_bytes(b"old")
        def fail(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.write(path, b"new", validate=fail)
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_replace_failure_cleans_temp(self):
        path = self.root / "file"
        path.write_bytes(b"old")
        with patch.object(b.os, "replace", side_effect=OSError("failed")), self.assertRaises(b.BootstrapError):
            self.write(path, b"new")
        self.assertFalse(list(self.root.glob(".*.tmp-*")))
        self.assertEqual(path.read_bytes(), b"old")

    def test_symlink_leaf_and_parent_rejected(self):
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real")
        for path in (self.root / "link", self.root / "link" / "file"):
            with self.assertRaises(b.BootstrapError):
                self.write(path, b"new")

    def test_dry_run_no_directories_or_validation(self):
        b.DRY_RUN = True
        with patch.object(b, "validate_daemon") as validate:
            self.write(self.root / "new" / "file", b"new", validate=validate)
            validate.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timezone_link_backup_preserves_target(self):
        source, link = self.root / "zone", self.root / "localtime"
        source.write_bytes(b"old")
        link.symlink_to(source)
        self.write(link, b"new", allow_leaf_symlink=True)
        self.assertEqual(source.read_bytes(), b"old")
        self.assertTrue(next(self.root.glob("localtime.bak.*")).is_symlink())


class DockerTests(Base):
    def test_json_object_required(self):
        for content in ("[]", "null", "42", "", '{"log-opts":[]}'):
            with self.subTest(content=content):
                b.DAEMON_JSON.write_text(content)
                with self.assertRaises(b.BootstrapError):
                    b.daemon_config()

    def test_log_options_merge_and_foreign_driver(self):
        b.DAEMON_JSON.write_text('{"log-driver":"json-file","log-opts":{"max-size":"50m","labels":"app"},"dns":["1.1.1.1"]}')
        cfg = json.loads(b.daemon_config())
        self.assertEqual(cfg["log-opts"], {"max-size": "50m", "max-file": "3", "labels": "app"})
        self.assertEqual(cfg["dns"], ["1.1.1.1"])
        b.DAEMON_JSON.write_text('{"log-driver":"journald"}')
        self.assertNotIn("log-opts", json.loads(b.daemon_config()))

    def test_runtime_consent_before_repository_writes(self):
        with patch.object(b, "package_installed", return_value=True), patch.object(b, "docker_preflight") as preflight:
            with self.assertRaises(b.BootstrapError):
                b.install_docker(b.SystemInfo("debian", "13", "trixie"))
            preflight.assert_not_called()

    def test_missing_candidate_never_removes_runtime(self):
        def command(args, **kwargs):
            return result("Candidate: (none)" if args[0] == "apt-cache" else "")
        with patch.object(b, "package_installed", return_value=True), patch.object(b, "docker_preflight", return_value="amd64"), patch.object(b, "apt_install"), patch.object(b.urllib.request, "urlopen") as fetch, patch.object(b, "verify_docker_key"), patch.object(b, "write_file"), patch.object(b, "run", side_effect=command) as run:
            fetch.return_value.__enter__.return_value.read.return_value = b"key"
            with self.assertRaises(b.BootstrapError):
                b.install_docker(b.SystemInfo("debian", "13", "trixie"), True)
            self.assertFalse(any("remove" in c.args[0] for c in run.call_args_list))

    def test_key_requires_single_exact_primary_fingerprint(self):
        for fingerprint, valid in ((b.DOCKER_FINGERPRINT, True), ("BAD", False)):
            output = "pub:::::::::\nfpr:::::::::" + fingerprint + ":\nsub:::::::::\nfpr:::::::::SUBKEY:\n"
            with self.subTest(fingerprint=fingerprint), patch.object(b, "run", return_value=result(output)):
                if valid:
                    b.verify_docker_key(b"key")
                else:
                    with self.assertRaises(b.BootstrapError):
                        b.verify_docker_key(b"key")

    def test_docker_info_failure_no_fallback(self):
        with patch.object(b, "run", side_effect=b.BootstrapError("daemon unavailable")), self.assertRaises(b.BootstrapError):
            b.docker_root_dir()
        with patch.object(b, "run", return_value=result()), self.assertRaises(b.BootstrapError):
            b.docker_root_dir()

    def test_paths_reject_root_relative_overlap_nonempty(self):
        source = self.root / "source"
        source.mkdir()
        target = self.root / "target"
        target.mkdir()
        (target / "data").touch()
        for value in ("/", "relative", str(source / "child"), str(self.root), str(target)):
            with self.subTest(value=value), self.assertRaises(b.BootstrapError):
                b.migration_paths(str(source), value)

    def test_containerd_snapshotter_refused(self):
        info = {"Driver": "overlayfs", "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]]}
        with patch.object(b, "run", return_value=result(json.dumps(info))), self.assertRaises(b.BootstrapError):
            b.check_storage_layout(self.root)

    def test_dry_migration_without_docker(self):
        b.DRY_RUN = True
        with patch.object(b.shutil, "which", return_value=None):
            b.move_docker(str(self.root / "new"))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_setup_migration_does_not_write_config_first(self):
        with patch.object(b, "install_docker"), patch.object(b, "move_docker") as move, patch.object(b, "configure_daemon_json") as configure:
            b.setup_docker({"docker_group": False, "docker_move": True}, "alice", None, "/opt/docker")
        move.assert_called_once_with("/opt/docker")
        configure.assert_not_called()


class MigrationTests(Base):
    def scenario(self, failure=None, absent=False):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        (source / "data").write_text("original")
        old = b'{"debug":false}\n'
        if not absent:
            b.DAEMON_JSON.write_bytes(old)
        states = {s: True for s in b.DOCKER_SERVICES}
        calls = []
        failed = False
        def command(args, **kwargs):
            nonlocal failed
            calls.append(args)
            stage = "copy" if args[0] == "rsync" else "stop" if args[:2] == ["systemctl", "stop"] else "start" if args[:2] == ["systemctl", "start"] else "other"
            if stage == failure and not failed:
                failed = True
                raise b.BootstrapError(stage)
            if args[0] == "du":
                return result("1 source")
            return result()
        real_write = b.write_file
        def write(path, content, **kwargs):
            nonlocal failed
            if failure in ("write", "interrupt") and not failed:
                failed = True
                if failure == "interrupt":
                    raise KeyboardInterrupt()
                raise b.BootstrapError("write")
            kwargs.update(uid=os.getuid(), gid=os.getgid())
            return real_write(path, content, **kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(b, "run", side_effect=command))
            stack.enter_context(patch.object(b, "check_storage_layout"))
            stack.enter_context(patch.object(b, "validate_daemon"))
            stack.enter_context(patch.object(b, "wait_docker"))
            stack.enter_context(patch.object(b.shutil, "which", return_value="rsync"))
            stack.enter_context(patch.object(b, "service_states", side_effect=[states, {s: False for s in states}, states]))
            stack.enter_context(patch.object(b, "docker_root_dir", side_effect=[str(source), str(source if failure == "verify" else target)]))
            stack.enter_context(patch.object(b, "write_file", side_effect=write))
            if failure:
                with self.assertRaises((b.BootstrapError, KeyboardInterrupt)):
                    b.move_docker(str(target))
                if absent:
                    self.assertFalse(b.DAEMON_JSON.exists())
                else:
                    self.assertEqual(b.DAEMON_JSON.read_bytes(), old)
            else:
                b.move_docker(str(target))
                self.assertEqual(json.loads(b.DAEMON_JSON.read_text())["data-root"], str(target))
        self.assertEqual((source / "data").read_text(), "original")
        self.assertFalse(any(c[0] == "mv" for c in calls))

    def test_success_keeps_source(self):
        self.scenario()

    def test_rollback_copy_failure(self):
        self.scenario("copy")

    def test_rollback_restarts_all_previously_active_services(self):
        states = {s: True for s in b.DOCKER_SERVICES}
        with patch.object(b, "run", return_value=result()) as command, patch.object(b, "wait_docker"), patch.object(b, "service_states", return_value=states):
            b.rollback_docker(None, states)
        self.assertEqual(command.call_args_list[0].args[0][1], "stop")
        self.assertEqual(sum(c.args[0][1] == "start" for c in command.call_args_list), 3)

    def test_rollback_write_failure(self):
        self.scenario("write")

    def test_rollback_start_failure(self):
        self.scenario("start")

    def test_rollback_verification_failure(self):
        self.scenario("verify")

    def test_rollback_interrupt(self):
        self.scenario("interrupt")

    def test_rollback_restores_absent_config(self):
        self.scenario("verify", absent=True)

    def test_rollback_reports_failure_and_still_restores_config(self):
        b.DAEMON_JSON.write_text("changed")
        states = {s: False for s in b.DOCKER_SERVICES}
        with patch.object(b, "run", side_effect=b.BootstrapError("cannot stop")), patch.object(b, "service_states", return_value=states):
            with self.assertRaisesRegex(b.BootstrapError, "rollback НЕ завершён"):
                b.rollback_docker(None, states)
        self.assertFalse(b.DAEMON_JSON.exists())


class ZshTests(Base):
    def test_unexpected_origin_rejected(self):
        dest = self.root / "repo"
        (dest / ".git").mkdir(parents=True)
        with patch.object(b, "run", return_value=result("https://unexpected")) as command:
            with self.assertRaises(b.BootstrapError):
                b.ensure_repo(b.Account("alice", 1000, 1000, self.root), b.OMZ_URL, dest, b.OMZ_REV)
        self.assertEqual(command.call_count, 1)

    def test_dirty_repo_rejected_without_force(self):
        dest = self.root / "repo"
        (dest / ".git").mkdir(parents=True)
        with patch.object(b, "run", side_effect=[result(b.OMZ_URL), result(" M file")]) as command:
            with self.assertRaises(b.BootstrapError):
                b.ensure_repo(b.Account("alice", 1000, 1000, self.root), b.OMZ_URL, dest, b.OMZ_REV)
        self.assertFalse(any("checkout" in c.args[0] for c in command.call_args_list))

    def test_nonroot_dry_run_does_not_inspect_root_home(self):
        b.DRY_RUN = True
        with patch.object(b.os, "geteuid", return_value=1000), patch.object(b, "ensure_repo") as repo:
            b.setup_zsh(b.Account("root", 0, 0, Path("/root")))
        repo.assert_not_called()

    def test_smoke_failure_propagates(self):
        with patch.object(b, "run", side_effect=b.BootstrapError("bad zsh")), self.assertRaises(b.BootstrapError):
            b.smoke_test(b.Account("alice", 1000, 1000, self.root))


if __name__ == "__main__":
    unittest.main()
