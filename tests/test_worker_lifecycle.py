"""Run with: python -m unittest discover -s tests -p test_worker_lifecycle.py -v.

These tests use isolated server processes and loopback sockets. Build the C
extension first so that the tests exercise the current source tree.
"""

import gc
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STREAM_CHUNK = b"x" * 65536
STREAM_CHUNKS = 3


def child_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def worker_app(environ, start_response):
    from meinheld import server

    if environ["PATH_INFO"] == "/yield":
        import greenlet

        parent = greenlet.getcurrent().parent
        references = sys.getrefcount(parent)
        for _ in range(100):
            server.sleep(0)
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.setblocking(False)
            server.schedule_call(0, writer.send, b"x")
            server.trampoline(reader.fileno(), read=True, timeout=2)
            if reader.recv(1) != b"x":
                raise RuntimeError("socket trampoline did not resume")
        client = environ["meinheld.client"]
        server.schedule_call(0, server._resume_client, client, (), {})
        server._suspend_client(client, 2)
        if sys.getrefcount(parent) != references:
            raise RuntimeError("cooperative switching leaked parent greenlet references")

    headers = [("X-Worker-Pid", str(os.getpid()))]
    if environ["PATH_INFO"] == "/stream":
        headers.append(("Content-Length", str(len(STREAM_CHUNK) * STREAM_CHUNKS)))
        start_response("200 OK", headers)

        def body():
            for _ in range(STREAM_CHUNKS):
                yield STREAM_CHUNK
                server.sleep(1)

        return body()
    body = b"ok"
    headers.append(("Content-Length", str(len(body))))
    start_response("200 OK", headers)
    return [body]


def serve_logging_test(fd, mode, count):
    from meinheld import server

    class Marker:
        live = 0

        def __init__(self):
            Marker.live += 1

        def __del__(self):
            Marker.live -= 1

    class Logger:
        normal = 0
        empty = 0

        def access(self, environ):
            environ["_test_log_marker"] = Marker()
            if "REQUEST_METHOD" in environ:
                self.normal += 1
            else:
                self.empty += 1
            completed = self.normal if mode == "normal" else self.empty
            if completed == count:
                server.schedule_call(0, server.stop)
            if mode == "raising":
                raise RuntimeError("test access logger failure")

        def error(self, *args):
            pass

    logger = Logger()
    server.set_access_logger(logger)
    server.set_error_logger(logger)
    server.set_keepalive(2 if mode == "keepalive" else 0)
    server.set_listen_socket([fd])
    server.schedule_call(0, server.schedule_call, 8, server.stop)
    server.run(worker_app)
    # PyErr_Print keeps the final logger exception in sys.last_*.
    for name in ("last_exc", "last_type", "last_value", "last_traceback"):
        if hasattr(sys, name):
            delattr(sys, name)
    gc.collect()
    print(json.dumps({"normal": logger.normal, "empty": logger.empty, "retained": Marker.live}))


class LoggingLifetimeTests(unittest.TestCase):
    def check_connections(self, mode):
        count = 100
        with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as error_log:
            listener.bind(("127.0.0.1", 0))
            listener.listen(256)
            listener.setblocking(False)
            port = listener.getsockname()[1]
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    __file__,
                    "--logging-server",
                    str(listener.fileno()),
                    mode,
                    str(count),
                ],
                pass_fds=(listener.fileno(),),
                env=child_env(),
                stdout=subprocess.PIPE,
                stderr=error_log,
                text=True,
            )
            try:
                for _ in range(count):
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                        if mode in ("normal", "keepalive"):
                            client.sendall(
                                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: "
                                + (b"keep-alive" if mode == "keepalive" else b"close")
                                + b"\r\n\r\n"
                            )
                            response = http.client.HTTPResponse(client)
                            response.begin()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.read(), b"ok")
                            response.close()
                output, _ = process.communicate(timeout=12)
                error_log.seek(0)
                errors = error_log.read()
                self.assertEqual(process.returncode, 0, errors)
                stats = json.loads(output)
                self.assertEqual(
                    stats["normal"] if mode == "normal" else stats["empty"], count, (stats, errors)
                )
                self.assertEqual(stats["retained"], 0, stats)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_empty_connections_release_log_environment(self):
        self.check_connections("empty")

    def test_keepalive_disconnects_release_log_environment(self):
        self.check_connections("keepalive")

    def test_failing_logger_releases_log_environment(self):
        self.check_connections("raising")

    def test_normal_requests_release_log_environment(self):
        self.check_connections("normal")


class WorkerLimitTests(unittest.TestCase):
    def make_worker(self, limit, jitter=0):
        from gunicorn.config import Config
        from meinheld import server
        from meinheld.gmeinheld import MeinheldWorker

        cfg = Config()
        cfg.set("max_requests", limit)
        cfg.set("max_requests_jitter", jitter)
        cfg.set("graceful_timeout", 5)
        with patch.object(server, "set_access_logger"), patch.object(server, "set_error_logger"):
            worker = MeinheldWorker(1, os.getpid(), [], None, 10, cfg, Mock())
        self.addCleanup(worker.tmp.close)
        worker.wsgi = Mock(return_value=[b"ok"])
        return worker

    def test_limit_includes_jitter_and_stops_only_once(self):
        from meinheld import server

        with patch("gunicorn.workers.base.randint", return_value=2) as randint:
            worker = self.make_worker(3, jitter=2)
        randint.assert_called_once_with(0, 2)
        self.assertEqual(worker.max_requests, 5)
        environ, start_response = {}, Mock()
        with patch.object(server, "stop") as stop, patch.object(
            server, "set_keepalive"
        ) as keepalive:
            for _ in range(4):
                self.assertEqual(worker.handle_request(environ, start_response), [b"ok"])
            stop.assert_not_called()
            worker.handle_request(environ, start_response)
            stop.assert_called_once_with(5)
            keepalive.assert_called_once_with(0)
            self.assertFalse(worker.alive)
            worker.handle_request(environ, start_response)
            stop.assert_called_once_with(5)
        self.assertEqual(worker.wsgi.call_count, 6)

    def test_zero_limit_runs_original_wsgi_app(self):
        from meinheld import server

        worker = self.make_worker(0)
        with patch.object(server, "set_keepalive"), patch.object(
            server, "set_picoev_max_fd"
        ), patch.object(server, "set_fastwatchdog"), patch.object(
            server, "set_listen_socket"
        ), patch.object(
            server, "run"
        ) as run:
            worker.run()
        run.assert_called_once_with(worker.wsgi)


class GunicornLifecycleTests(unittest.TestCase):
    def request(self, port, path="/", ready=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            pid = int(response.getheader("X-Worker-Pid"))
            if ready is not None:
                ready.set()
            body = response.read()
            self.assertEqual(response.status, 200)
            return pid, body
        finally:
            connection.close()

    def check_worker_restart(self, access_log):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            command = [
                sys.executable,
                "-B",
                "-m",
                "gunicorn",
                "--workers",
                "1",
                "--worker-class",
                "meinheld.gmeinheld.MeinheldWorker",
                "--bind",
                "fd://%s" % listener.fileno(),
                "--max-requests",
                "3",
                "--max-requests-jitter",
                "0",
                "--keep-alive",
                "2",
                "--graceful-timeout",
                "5",
                "--chdir",
                str(ROOT / "tests"),
                "test_worker_lifecycle:worker_app",
            ]
            from gunicorn.config import Config

            if "control_socket_disable" in Config().settings:
                command += ["--no-control-socket"]
            if access_log:
                command += ["--access-logfile", "-"]
            process = subprocess.Popen(
                command,
                pass_fds=(listener.fileno(),),
                env=child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            results, failures = [], []
            ready = threading.Event()

            def stream():
                try:
                    results.append(self.request(port, "/stream", ready))
                except Exception as exc:
                    failures.append(exc)
                    ready.set()

            thread = threading.Thread(target=stream, daemon=True)
            try:
                first_pid, body = self.request(port, "/yield")
                self.assertEqual(body, b"ok")
                thread.start()
                self.assertTrue(ready.wait(5), "stream did not start")
                last_pid, body = self.request(port)
                self.assertEqual(last_pid, first_pid)
                self.assertEqual(body, b"ok")
                thread.join(8)
                self.assertFalse(thread.is_alive(), "stream was not drained")
                self.assertFalse(failures, failures)
                self.assertEqual(results, [(first_pid, STREAM_CHUNK * STREAM_CHUNKS)])
                new_pid, body = self.request(port)
                self.assertNotEqual(new_pid, first_pid, "worker was not recycled after 3 requests")
                self.assertEqual(body, b"ok")
            finally:
                if thread.ident is not None:
                    thread.join(10)
                process.terminate()
                try:
                    output, errors = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, errors = process.communicate()
                if process.returncode not in (0, -15):
                    self.fail(
                        "gunicorn exited with %s:\n%s\n%s" % (process.returncode, output, errors)
                    )
                if sys.exc_info()[0] is not None:
                    print(errors, file=sys.stderr)

    def test_restart_and_drain_with_access_log(self):
        self.check_worker_restart(True)

    def test_restart_and_drain_without_access_log(self):
        self.check_worker_restart(False)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--logging-server":
        serve_logging_test(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    else:
        unittest.main()
