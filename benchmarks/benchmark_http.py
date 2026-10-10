"""Compare locally built source trees with ApacheBench and Python 3.12.

Example: python benchmarks/benchmark_http.py /tmp/before /tmp/after --repeat 5
The trees need an in-place extension build; no production traffic is generated.
"""

import argparse
import http.client
import json
import logging
import os
from pathlib import Path
import re
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
from types import SimpleNamespace


def serve(root, fd, app_kind, log_kind):
    sys.path.insert(0, root)
    from meinheld import server

    requests = 0
    cpu_start = 0

    def tiny(environ, start_response):
        nonlocal requests
        if environ["PATH_INFO"] == "/__stop__":
            server.schedule_call(0, server.stop)
        else:
            requests += 1
        start_response(
            "200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")]
        )
        return [b"ok"]

    app = tiny
    if app_kind == "flask":
        from flask import Flask

        flask_app = Flask(__name__)

        @flask_app.route("/")
        def index():
            nonlocal requests
            requests += 1
            return "ok"

        @flask_app.route("/__stop__")
        def stop():
            server.schedule_call(0, server.stop)
            return "ok"

        app = flask_app

    if log_kind == "access":
        from meinheld.mlogging import _access

        class AccessLogger:
            access = _access

            def error(self, *args):
                raise RuntimeError(args)

        access_logger = AccessLogger()
        access_logger.cfg = SimpleNamespace(
            access_log_format='%(h)s %(t)s "%(r)s" %(s)s %(b)s "%(a)s"'
        )
        access_logger.access_log = logging.getLogger("benchmark.access")
        access_logger.access_log.setLevel(logging.INFO)
        access_logger.access_log.propagate = False
        access_logger.access_log.addHandler(
            logging.StreamHandler(open(os.devnull, "w"))
        )
        server.set_access_logger(access_logger)
    else:
        server.set_access_logger(None)
    server.set_error_logger(None)
    server.set_keepalive(5)
    server.set_listen_socket([fd])

    def ready():
        nonlocal cpu_start
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_start = usage.ru_utime + usage.ru_stime
        print(json.dumps({"ready": True}), flush=True)

    server.schedule_call(0, ready)
    server.run(app)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(
        json.dumps(
            {
                "requests": requests,
                "cpu_us_per_request": (usage.ru_utime + usage.ru_stime - cpu_start)
                * 1e6
                / requests,
                "peak_rss_mib": usage.ru_maxrss
                / (1024**2 if sys.platform == "darwin" else 1024),
            }
        ),
        flush=True,
    )


def run_once(root, args):
    with socket.socket() as listener, tempfile.TemporaryFile(mode="w+") as errors:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1024)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                __file__,
                "--serve",
                str(root),
                str(listener.fileno()),
                args.app,
                args.log,
            ],
            pass_fds=(listener.fileno(),),
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
        )
        try:
            if not process.stdout.readline():
                raise RuntimeError("benchmark server failed to start")
            command = [args.ab, "-k", "-r", "-c", str(args.concurrency)]
            if args.long_header:
                command += ["-H", "X-Long: " + "x" * 4096]
            command += ["-H", "User-Agent: meinheld-benchmark"]
            url = "http://127.0.0.1:%d/" % port
            subprocess.run(
                command + ["-n", "2000", url],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            result = subprocess.run(
                command + ["-n", str(args.requests), url],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if not re.search(r"Failed requests:\s+0\b", result.stdout):
                raise RuntimeError(result.stdout)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/__stop__")
            connection.getresponse().read()
            connection.close()
            output, _ = process.communicate(timeout=10)
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read())
            stats = json.loads(output)
            stats["qps"] = float(
                re.search(r"Requests per second:\s+([\d.]+)", result.stdout)[1]
            )
            stats["p99_ms"] = int(re.search(r"99%\s+(\d+)", result.stdout)[1])
            return stats
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--app", choices=("tiny", "flask"), default="tiny")
    parser.add_argument("--log", choices=("none", "access"), default="none")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--requests", type=int, default=40000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--ab", default="ab")
    parser.add_argument("--long-header", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {str(root.resolve()): [] for root in args.roots}
    for run in range(args.repeat):
        # Reverse every other round to reduce effects from heat and system drift.
        roots = args.roots if run % 2 == 0 else list(reversed(args.roots))
        for root in roots:
            sample = run_once(root.resolve(), args)
            results[str(root.resolve())].append(sample)
            print(json.dumps({"root": root.name, "run": run + 1, **sample}), flush=True)
    report = {
        "app": args.app,
        "log": args.log,
        "long_header": args.long_header,
        "samples": results,
        "medians": {
            root: {
                key: statistics.median(s[key] for s in samples)
                for key in ("qps", "p99_ms", "cpu_us_per_request", "peak_rss_mib")
            }
            for root, samples in results.items()
        },
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["medians"], indent=2))


if __name__ == "__main__":
    main()
