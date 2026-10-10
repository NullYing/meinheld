"""HTTP-level compatibility checks for the Python 3.12 hot paths."""

from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(fd):
    from meinheld import server

    retained = []
    errors = []
    closed = []

    class Logger:
        def error(self, exception, value, traceback):
            errors.append([exception.__name__, str(value), traceback is not None])

    class Body:
        def __init__(self, value):
            self.value = value

        def __iter__(self):
            yield self.value

        def close(self):
            closed.append(True)

    # A callable instance also exercises vectorcall's fallback to tp_call.
    class App:
        def __call__(self, environ, start_response):
            path = environ["PATH_INFO"]
            if path == "/__stop__":
                server.schedule_call(0, server.stop)
            if path == "/error":
                raise ValueError("expected app error")
            body = {
                key: value
                for key, value in environ.items()
                if isinstance(value, (str, int, bool, tuple))
                and key != "meinheld.client"
            }
            body["_errors"] = errors
            body["_closed"] = len(closed)
            if path == "/upload":
                content = environ["wsgi.input"].read()
                body["_body_size"] = len(content)
                body["_body_sha256"] = hashlib.sha256(content).hexdigest()
            if retained:
                body["_independent"] = environ is not retained[0]
                body["_previous_script"] = retained[0]["SCRIPT_NAME"]
            if path == "/mutate":
                environ["SCRIPT_NAME"] = "/changed"
                environ["wsgi.url_scheme"] = "changed"
                environ["_custom"] = "first request only"
                retained.append(environ)
            payload = json.dumps(body, ensure_ascii=True).encode("ascii")
            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(payload))),
                ],
            )
            return Body(payload)

    server.set_access_logger(None)
    server.set_error_logger(Logger())
    server.set_keepalive(0)
    server.set_listen_socket([fd])
    server.schedule_call(0, server.schedule_call, 15, server.stop)
    server.run(App())


@contextmanager
def running_server():
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as errors:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [sys.executable, "-B", __file__, "--serve", str(listener.fileno())],
            pass_fds=(listener.fileno(),),
            env=env,
            stderr=errors,
            stdout=subprocess.DEVNULL,
        )
        try:
            yield port
            status, _ = request(
                port, b"GET /__stop__ HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            if status != 200:
                raise AssertionError("server did not stop cleanly")
            process.wait(timeout=20)
            errors.seek(0)
            if process.returncode:
                raise AssertionError(errors.read())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def request(port, raw, fragments=None):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if fragments:
            for fragment in fragments:
                client.sendall(fragment)
                time.sleep(0.002)
        else:
            client.sendall(raw)
        response = http.client.HTTPResponse(client)
        response.begin()
        payload = response.read()
        return response.status, payload


class WSGIOptimizationTests(unittest.TestCase):
    def test_body_buffers_and_temporary_files_keep_uploaded_content(self):
        with running_server() as port:
            for size in (64 * 1024, 600 * 1024):
                content = b"x" * size
                raw = (
                    b"POST /upload HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                    + str(size).encode("ascii")
                    + b"\r\n\r\n"
                    + content
                )
                status, payload = request(port, raw)
                self.assertEqual(status, 200)
                environ = json.loads(payload)
                self.assertEqual(environ["_body_size"], size)
                self.assertEqual(
                    environ["_body_sha256"], hashlib.sha256(content).hexdigest()
                )

    def test_fragmented_headers_preserve_names_latin1_and_duplicates(self):
        with running_server() as port:
            fragments = [
                b"GET / HTTP/1.1\r\nHo",
                b"st: localhost\r\nX-Cus",
                b"tom: caf",
                b"\xe9\r\nX-Custom: second\r\nX-Long: ",
            ]
            fragments += [b"x" * 128] * 48
            fragments += [b"\r\nContent-T", b"ype: text/plain\r\nEmpty: \r\n\r\n"]
            status, payload = request(port, None, fragments)
            environ = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(environ["HTTP_HOST"], "localhost")
            self.assertEqual(environ["HTTP_X_CUSTOM"], "caf\xe9, second")
            self.assertEqual(environ["HTTP_X_LONG"], "x" * 6144)
            self.assertEqual(environ["HTTP_EMPTY"], "")
            self.assertEqual(environ["CONTENT_TYPE"], "text/plain")
            self.assertNotIn("HTTP_CONTENT_TYPE", environ)

    def test_header_value_limit_and_cleanup_after_rejection(self):
        with running_server() as port:
            prefix = b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Long: "
            status, payload = request(port, prefix + b"x" * 8192 + b"\r\n\r\n")
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(payload)["HTTP_X_LONG"]), 8192)
            status, _ = request(
                port, None, [prefix, b"x" * 4096, b"x" * 4096, b"x\r\n\r\n"]
            )
            self.assertEqual(status, 400)
            status, _ = request(port, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertEqual(status, 200)

    def test_header_name_limit_and_partial_disconnect(self):
        with running_server() as port:
            prefix = b"GET / HTTP/1.1\r\nHost: localhost\r\n"
            status, _ = request(
                port, None, [prefix, b"X" * 4096, b"X" * 4096, b": value\r\n\r\n"]
            )
            self.assertEqual(status, 400)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(prefix + b"X-Par")
                time.sleep(0.01)
                client.sendall(b"tial")
                time.sleep(0.01)
            status, _ = request(port, prefix + b"\r\n")
            self.assertEqual(status, 200)

    def test_environment_is_independent_and_response_close_is_called(self):
        with running_server() as port:
            request(port, b"GET /mutate HTTP/1.1\r\nHost: localhost\r\n\r\n")
            status, payload = request(
                port, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            environ = json.loads(payload)
            self.assertEqual(status, 200)
            self.assertEqual(environ["SCRIPT_NAME"], "")
            self.assertEqual(environ["wsgi.url_scheme"], "http")
            self.assertEqual(environ["wsgi.version"], [1, 0])
            self.assertNotIn("_custom", environ)
            self.assertTrue(environ["_independent"])
            self.assertEqual(environ["_previous_script"], "/changed")
            self.assertEqual(environ["_closed"], 1)

    def test_application_exception_reaches_three_argument_logger(self):
        with running_server() as port:
            status, _ = request(port, b"GET /error HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertEqual(status, 500)
            _, payload = request(port, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertEqual(
                json.loads(payload)["_errors"],
                [["ValueError", "expected app error", True]],
            )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]))
    else:
        unittest.main()
