# Python 3.12 performance checks

Build each source tree with the same interpreter and compiler settings:

```sh
cd /path/to/tree
python -m pip install -e .
```

Run the checks from the optimized tree. The HTTP runner requires ApacheBench
(`ab`), opens loopback ports and launches isolated server processes. It warms
each server with 2,000 requests, alternates the order of the two trees and
records all samples plus medians. A nonzero failed-request count fails the run.

```sh
python benchmarks/benchmark_http.py /path/to/before /path/to/after --repeat 5 --requests 100000 --output tiny.json
python benchmarks/benchmark_http.py /path/to/before /path/to/after --log access --repeat 5 --requests 100000 --output access.json
python benchmarks/benchmark_http.py /path/to/before /path/to/after --app flask --repeat 5 --requests 100000 --output flask.json
python benchmarks/benchmark_http.py /path/to/before /path/to/after --long-header --repeat 5 --requests 100000 --output headers.json
```

The default HTTP concurrency is 16 with keepalive enabled. The Flask case needs
Flask installed and serves a minimal route without databases or business logic.
Access logs use a real logging StreamHandler writing to `/dev/null`. The runner
measures a single meinheld process; Gunicorn worker lifecycle is tested separately.

`cpu_us_per_request` includes the warmup and shutdown interval, while QPS and
ApacheBench P99 describe the measured load only. RSS is the process lifetime peak,
including imports and startup. ApacheBench reports percentiles in whole milliseconds;
small differences at this resolution should not be interpreted as precise tail gains.

Two additional benchmarks isolate logging and native HTTP parsing:

```sh
python benchmarks/benchmark_logging.py /path/to/before
python benchmarks/benchmark_logging.py /path/to/after
python benchmarks/benchmark_parser.py /path/to/before
python benchmarks/benchmark_parser.py /path/to/after
```

The logging benchmark uses 30 extra request headers, a fixed log format and an
in-memory sink. The parser benchmark compiles a temporary helper outside the
server extension and feeds a 4 KiB header in whole, 32-byte and 1-byte fragments.
It measures parsing, environ construction and cleanup without application calls
or TCP. It requires the server extension's C symbols to be exported. Both checks
report five CPU-time samples and their median.

Run the focused regression suite after building the extension:

```sh
PYTHONPATH=tests python -m unittest -v \
  test_worker_lifecycle test_request_headers test_wsgi_optimizations \
  test_access_format test_performance_regressions test_epoll_backend
```

The suite covers request/log lifetimes, Gunicorn recycling and concurrent streaming,
cooperative switching, environment isolation, callable applications, exception and
close callbacks, fragmented/duplicate headers, Latin-1, limits, partial disconnects,
memory and temporary-file upload bodies, and access-log formatting. The performance
regressions also cover backpressured responses, sendfile EOF and remaining length,
upload stream lifetimes, bounded readline scans, and ignored response headers.
The Linux epoll checks compile a native helper with assertions disabled and exercise
both immediate and deferred deletion modes; they require a C compiler and Python
development headers.

For deployment decisions, repeat HTTP tests on Linux/Python 3.12 with the actual
Gunicorn configuration and representative application routes, including database
and outbound HTTP traffic. Local benchmark results do not establish production gains.
