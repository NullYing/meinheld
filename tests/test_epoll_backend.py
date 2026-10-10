"""Exercise picoev's real Linux kernel operations with NDEBUG enabled."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "linux", "requires Linux epoll")
class EpollBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shlex.split(
            os.environ.get("CC") or sysconfig.get_config_var("CC") or "cc"
        )
        if not compiler or not shutil.which(compiler[0]):
            raise unittest.SkipTest("a C compiler is required")
        include = sysconfig.get_path("include")
        if not (Path(include) / "Python.h").exists():
            raise unittest.SkipTest("Python development headers are required")
        cls.temporary = tempfile.TemporaryDirectory(prefix="meinheld-epoll-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.executables = []
        source = Path(__file__).with_name("epoll_backend_checks.c")
        for deferred in (0, 1):
            executable = Path(cls.temporary.name) / (
                "epoll-check-%d" % deferred
            )
            command = compiler + [
                "-std=c99",
                "-D_GNU_SOURCE",
                "-DNDEBUG",
                "-O2",
                "-DPICOEV_EPOLL_DEFER_DELETES=%d" % deferred,
                "-I" + include,
                str(source),
                "-o",
                str(executable),
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30
            )
            if result.returncode:
                raise AssertionError(
                    "native epoll check build failed:\n" + result.stderr
                )
            cls.executables.append((deferred, executable))

    def run_check(self, name):
        for deferred, executable in self.executables:
            with self.subTest(deferred_deletes=deferred):
                result = subprocess.run(
                    [str(executable), name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_registration_can_switch_to_write(self):
        self.run_check("read-write")

    def test_read_registration_can_be_deleted_and_readded_for_write(self):
        self.run_check("delete-readd")

    def test_exclusive_listener_is_removed_before_shutdown_timeout_add(self):
        self.run_check("exclusive-delete")

    def test_closed_descriptor_add_reports_kernel_error(self):
        self.run_check("closed-add")

    def test_closed_descriptor_update_reports_error_and_preserves_events(
        self,
    ):
        self.run_check("closed-update")

    def test_exclusive_registration_can_change_events_without_mod(self):
        self.run_check("exclusive-update")

    def test_unsupported_descriptor_preserves_kernel_error(self):
        self.run_check("kernel-rejection")


if __name__ == "__main__":
    unittest.main()
