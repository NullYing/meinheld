"""HTTP regressions for event waits, file lifetimes and bounded input reads."""

from contextlib import closing, contextmanager
import gc
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STREAM = b"x" * (1024 * 1024)
IGNORED_VALUE = "x" * 4096


def rss_bytes():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("VmRSS is unavailable")


def serve(fd, body_buffer_size):
    from meinheld import server

    retained_inputs = []
    file_closed = []

    class TrackedFile:
        def __init__(self, content):
            self.file = tempfile.TemporaryFile()
            self.file.write(content)
            self.file.seek(0)

        def fileno(self):
            return self.file.fileno()

        def close(self):
            self.file.close()
            file_closed.append(True)

    class Logger:
        def error(self, *args):
            pass

    def small_send_buffer(environ):
        peer = socket.socket(fileno=environ["meinheld.client"].get_fd())
        try:
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        finally:
            peer.detach()

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        if path == "/stop":
            server.schedule_call(0, server.stop)
        elif path == "/stream":
            small_send_buffer(environ)
            start_response("200 OK", [("Content-Length", str(len(STREAM)))])
            return (b"x" * 8192 for _ in range(len(STREAM) // 8192))
        elif path in ("/stream-first", "/stream-chunked"):
            small_send_buffer(environ)
            headers = (
                []
                if path == "/stream-chunked"
                else [("Content-Length", str(512 * 1024 + 16))]
            )
            start_response("200 OK", headers)

            def body():
                # Fresh bytes must stay alive when header/body writev suspends.
                yield bytes(range(256)) * 1024
                yield b"b" * (256 * 1024)
                yield b"tail" * 4

            return body()
        elif path.startswith("/file/"):
            content = STREAM if path == "/file/large-limited" else b"12345678"
            length = {
                "/file/short": 16,
                "/file/limited": 4,
                "/file/large-limited": 512 * 1024 + 12345,
                "/file/exact": 8,
            }.get(path)
            headers = (
                [("Content-Length", str(length))] if length is not None else []
            )
            start_response("200 OK", headers)
            file = TrackedFile(content)
            if path == "/file/offset":
                file.file.seek(3)
            if path == "/file/large-limited":
                small_send_buffer(environ)
            return environ["wsgi.file_wrapper"](file)
        elif path == "/upload":
            content = environ["wsgi.input"].read()
            payload = hashlib.sha256(content).hexdigest().encode("ascii")
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/retain-upload":
            retained_inputs.append(environ["wsgi.input"])
        elif path == "/read-retained":
            payload = retained_inputs.pop(0).read()
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/rss":
            gc.collect()
            payload = json.dumps({"rss": rss_bytes()}).encode("ascii")
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/closed":
            payload = json.dumps(file_closed).encode("ascii")
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/trace-start":
            tracemalloc.start()
        elif path == "/ignored-headers":
            start_response(
                "200 OK",
                [
                    ("Date", IGNORED_VALUE),
                    ("Server", IGNORED_VALUE),
                    ("Content-Length", "2"),
                ],
            )
            return [b"ok"]
        elif path == "/trace-check":
            gc.collect()
            allocation_size = sys.getsizeof(IGNORED_VALUE.encode("ascii"))
            retained = sum(
                trace.size == allocation_size
                for trace in tracemalloc.take_snapshot().traces
            )
            tracemalloc.stop()
            payload = json.dumps({"retained": retained}).encode("ascii")
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/readline-one":
            input_stream = environ["wsgi.input"]
            count = 0
            while input_stream.readline(1):
                count += 1
            payload = str(count).encode("ascii")
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/readline-semantics":
            input_stream = environ["wsgi.input"]
            values = [
                input_stream.readline(size) for size in (0, 1, 2, -1, 100)
            ]
            values += list(input_stream)
            values += [input_stream.readline(), input_stream.readline(0)]
            payload = json.dumps(
                [value.decode("latin1") for value in values]
            ).encode()
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        elif path == "/readlines":
            values = environ["wsgi.input"].readlines(4)
            values += environ["wsgi.input"].readlines()
            payload = json.dumps(
                [value.decode("latin1") for value in values]
            ).encode()
            start_response("200 OK", [("Content-Length", str(len(payload)))])
            return [payload]
        start_response("200 OK", [("Content-Length", "2")])
        return [b"ok"]

    server.set_access_logger(None)
    server.set_error_logger(Logger())
    server.set_keepalive(5)
    server.set_client_body_buffer_size(body_buffer_size)
    server.set_listen_socket([fd])
    server.schedule_call(0, lambda: print("ready", flush=True))
    server.run(app)


@contextmanager
def running_server(body_buffer_size=500 * 1024):
    with socket.socket() as listener, tempfile.TemporaryFile(
        mode="w+"
    ) as errors:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                __file__,
                "--serve",
                str(listener.fileno()),
                str(body_buffer_size),
            ],
            pass_fds=(listener.fileno(),),
            env=env,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        try:
            if not select.select([process.stdout], [], [], 5)[0]:
                raise AssertionError("server startup timed out")
            if process.stdout.readline().strip() != "ready":
                errors.seek(0)
                raise AssertionError(errors.read())
            yield port
            status, _, _ = request(port, "/stop")
            if status != 200:
                raise AssertionError("server did not stop cleanly")
            process.wait(timeout=5)
            if process.returncode:
                errors.seek(0)
                raise AssertionError(errors.read())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def request(port, path, body=None, connection=None):
    owned = connection is None
    connection = connection or http.client.HTTPConnection(
        "127.0.0.1", port, timeout=3
    )
    try:
        connection.request(
            "POST" if body is not None else "GET", path, body=body
        )
        response = connection.getresponse()
        return response.status, response.read(), response.getheaders()
    finally:
        if owned:
            connection.close()


class PerformanceRegressionTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Linux epoll regression")
    @unittest.skipIf(
        os.environ.get("MEINHELD_NOGREEN") == "1", "requires greenlet"
    )
    def test_fragmented_request_can_switch_from_read_to_write(self):
        with running_server() as port:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=3
            ) as client:
                client.sendall(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n")
                time.sleep(0.1)
                client.sendall(b"\r\n")
                # Delay reading so the server must wait for a writable socket.
                time.sleep(0.1)
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), STREAM)
            self.assertEqual(request(port, "/")[1], b"ok")

    def test_sendfile_early_eof_closes_connection_without_blocking_worker(
        self,
    ):
        with running_server() as port:
            connection = http.client.HTTPConnection(
                "127.0.0.1", port, timeout=3
            )
            try:
                connection.request("GET", "/file/short")
                response = connection.getresponse()
                with self.assertRaises(http.client.IncompleteRead) as raised:
                    response.read()
                self.assertEqual(raised.exception.partial, b"12345678")
            finally:
                connection.close()
            self.assertEqual(request(port, "/")[1], b"ok")
            self.assertEqual(json.loads(request(port, "/closed")[1]), [True])

    def test_sendfile_respects_declared_length_and_keepalive(self):
        self.check_file_length("/file/limited", b"1234")

    @unittest.skipIf(
        os.environ.get("MEINHELD_NOGREEN") == "1", "requires greenlet"
    )
    def test_sendfile_resumes_with_only_the_remaining_declared_length(self):
        self.check_file_length(
            "/file/large-limited", STREAM[: 512 * 1024 + 12345]
        )

    def check_file_length(self, path, expected):
        with running_server() as port:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=3
            ) as client:
                client.sendall(
                    (
                        "GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n" % path
                    ).encode("ascii")
                )
                if path == "/file/large-limited":
                    time.sleep(0.1)
                wire = b""
                while b"\r\n\r\n" not in wire or len(
                    wire.split(b"\r\n\r\n", 1)[1]
                ) < len(expected):
                    block = client.recv(65536)
                    self.assertTrue(block, "file response ended early")
                    wire += block
                if select.select([client], [], [], 0.1)[0]:
                    wire += client.recv(65536)
                self.assertEqual(wire.split(b"\r\n\r\n", 1)[1], expected)
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.read(), b"ok")

    def test_sendfile_infers_remaining_file_length_from_current_position(self):
        with running_server() as port:
            status, payload, headers = request(port, "/file/offset")
            self.assertEqual((status, payload), (200, b"45678"))
            self.assertEqual(dict(headers)["Content-Length"], "5")

    def test_sendfile_exact_length_closes_file(self):
        with running_server() as port:
            self.assertEqual(request(port, "/file/exact")[1], b"12345678")
            self.assertEqual(json.loads(request(port, "/closed")[1]), [True])

    @unittest.skipIf(
        os.environ.get("MEINHELD_NOGREEN") == "1", "requires greenlet"
    )
    def test_first_body_bytes_survive_suspended_header_write(self):
        self.check_suspended_body("/stream-first")

    @unittest.skipIf(
        os.environ.get("MEINHELD_NOGREEN") == "1", "requires greenlet"
    )
    def test_chunked_body_survives_suspended_header_write(self):
        self.check_suspended_body("/stream-chunked")

    def check_suspended_body(self, path):
        with running_server() as port:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=3
            ) as client:
                client.sendall(
                    (
                        "GET %s HTTP/1.1\r\nHost: localhost\r\n\r\n" % path
                    ).encode("ascii")
                )
                time.sleep(0.1)
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.read(),
                    bytes(range(256)) * 1024
                    + b"b" * (256 * 1024)
                    + b"tail" * 4,
                )
            self.assertEqual(request(port, "/")[1], b"ok")

    @unittest.skipUnless(sys.platform == "linux", "Linux RSS measurement")
    def test_repeated_file_uploads_release_native_stream_buffers(self):
        with running_server(body_buffer_size=1024) as port:
            content = b"x" * 2048
            expected = hashlib.sha256(content).hexdigest().encode("ascii")
            with closing(
                http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            ) as connection:
                for _ in range(32):
                    self.assertEqual(
                        request(port, "/upload", content, connection)[1],
                        expected,
                    )
                before = json.loads(
                    request(port, "/rss", connection=connection)[1]
                )["rss"]
                for _ in range(2048):
                    self.assertEqual(
                        request(port, "/upload", content, connection)[1],
                        expected,
                    )
                after = json.loads(
                    request(port, "/rss", connection=connection)[1]
                )["rss"]
                self.assertLess(after - before, 3 * 1024 * 1024)

    def test_upload_input_can_outlive_its_request(self):
        with running_server(body_buffer_size=1024) as port:
            content = b"retained upload" * 200
            self.assertEqual(
                request(port, "/retain-upload", content)[1], b"ok"
            )
            self.assertEqual(request(port, "/read-retained")[1], content)

    def test_ignored_response_headers_release_encoded_values(self):
        with running_server() as port:
            request(port, "/trace-start")
            for _ in range(30):
                status, payload, headers = request(port, "/ignored-headers")
                self.assertEqual((status, payload), (200, b"ok"))
                header_dict = dict(headers)
                self.assertNotEqual(header_dict["Date"], IGNORED_VALUE)
                self.assertNotEqual(header_dict["Server"], IGNORED_VALUE)
            self.assertEqual(
                json.loads(request(port, "/trace-check")[1])["retained"], 0
            )

    def test_small_readline_does_not_scan_the_entire_remaining_line(self):
        with running_server() as port:
            # Below the native-memory body threshold. The old quadratic scan
            # exceeds the 3-second request deadline; bounded reads finish fast.
            content = b"x" * (256 * 1024) + b"\n"
            self.assertEqual(
                request(port, "/readline-one", content)[1], b"262145"
            )

    def test_readline_sizes_newlines_and_iteration_match_bytesio(self):
        content = b"abc\ndefgh\n\xfftail\nlast"
        reference = io.BytesIO(content)
        expected = [reference.readline(size) for size in (0, 1, 2, -1, 100)]
        expected += list(reference)
        expected += [reference.readline(), reference.readline(0)]
        with running_server() as port:
            values = json.loads(
                request(port, "/readline-semantics", content)[1]
            )
            self.assertEqual(
                values, [value.decode("latin1") for value in expected]
            )
            values = json.loads(request(port, "/readlines", content)[1])
            self.assertEqual(
                values,
                [
                    value.decode("latin1")
                    for value in content.splitlines(keepends=True)
                ],
            )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), int(sys.argv[3]))
    else:
        unittest.main()
