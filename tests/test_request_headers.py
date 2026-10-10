"""Regression checks for repeated HTTP request header values and their lifetime."""

import gc
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import tracemalloc
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VALUES = [letter * 2048 for letter in (b"a", b"b", b"c")]
EXPECTED = b", ".join(VALUES).decode("latin1")


def serve_headers_test(fd, count):
    from meinheld import server

    completed = 0

    def app(environ, start_response):
        nonlocal completed
        correct = environ.get("HTTP_X_REPEAT") == EXPECTED
        completed += 1
        if completed == count:
            server.schedule_call(0, server.stop)
        start_response("200 OK" if correct else "500 Error", [("Content-Length", "2")])
        return [b"ok"]

    server.set_access_logger(None)
    server.set_error_logger(None)
    server.set_keepalive(0)
    server.set_listen_socket([fd])
    server.schedule_call(0, server.schedule_call, 8, server.stop)
    # These sizes identify the intermediate Unicode values from the first
    # and second duplicate-header joins. No request value should outlive run().
    intermediate_sizes = {
        sys.getsizeof(b", ".join(VALUES[:n]).decode("latin1") + ", ") for n in (1, 2)
    }
    tracemalloc.start()
    server.run(app)
    gc.collect()
    retained = sum(
        1
        for trace in tracemalloc.take_snapshot().traces
        if trace.size in intermediate_sizes
        and any(frame.filename == __file__ for frame in trace.traceback)
    )
    tracemalloc.stop()
    print(json.dumps({"completed": completed, "retained": retained}))


class RepeatedHeaderTests(unittest.TestCase):
    def test_duplicate_header_values_are_joined_and_released(self):
        count = 100
        with socket.socket() as listener, tempfile.TemporaryFile(
            mode="w+"
        ) as error_log:
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
                    "--headers-server",
                    str(listener.fileno()),
                    str(count),
                ],
                pass_fds=(listener.fileno(),),
                env=env,
                stdout=subprocess.PIPE,
                stderr=error_log,
                text=True,
            )
            try:
                request = b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                request += b"".join(b"X-Repeat: " + value + b"\r\n" for value in VALUES)
                request += b"\r\n"
                for _ in range(count):
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=5
                    ) as client:
                        client.sendall(request)
                        response = http.client.HTTPResponse(client)
                        response.begin()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"ok")
                        response.close()
                output, _ = process.communicate(timeout=12)
                error_log.seek(0)
                self.assertEqual(process.returncode, 0, error_log.read())
                stats = json.loads(output)
                self.assertEqual(stats["completed"], count, stats)
                self.assertEqual(stats["retained"], 0, stats)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--headers-server":
        serve_headers_test(int(sys.argv[2]), int(sys.argv[3]))
    else:
        unittest.main()
