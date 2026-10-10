"""Measure access-log formatting without disk or HTTP client overhead."""

import argparse
import json
import statistics
import sys
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--iterations", type=int, default=100000)
    args = parser.parse_args()
    sys.path.insert(0, args.root)
    from meinheld.mlogging import _access

    class Sink:
        def isEnabledFor(self, level):
            return True

        def info(self, message):
            self.message = message

    logger = SimpleNamespace(
        cfg=SimpleNamespace(
            access_log_format='%(h)s %(t)s "%(r)s" %(s)s %(b)s "%(a)s"'
        ),
        access_log=Sink(),
    )
    environ = {
        "REMOTE_ADDR": "127.0.0.1",
        "LOCAL_TIME": "10/Oct/2026:00:00:00 +0000",
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "STATUS_CODE": 200,
        "SEND_BYTES": 2,
        "REQUEST_TIME": 1,
        "HTTP_USER_AGENT": "bench",
    }
    environ.update({"HTTP_X_HEADER_%d" % i: "value" for i in range(30)})
    samples = []
    for _ in range(5):
        started = time.process_time()
        for _ in range(args.iterations):
            _access(logger, environ)
        samples.append((time.process_time() - started) * 1e6 / args.iterations)
    print(
        json.dumps(
            {
                "root": args.root,
                "cpu_us_per_log": statistics.median(samples),
                "samples": samples,
                "message": logger.access_log.message,
            }
        )
    )


if __name__ == "__main__":
    main()
