# Local Python 3.12 optimization results

Measured on 2026-10-10 using macOS arm64, Python 3.12.7, greenlet 3.5.1, Gunicorn 26.0.0 and Flask 3.1.3. The baseline is commit `e6e2515`, including the earlier memory leak and worker recycling repairs.

These are local single-process measurements. Linux/Python 3.12.13 with production Gunicorn 25.1.0 and representative cloud-server routes remains to be measured. No production process was modified or restarted.

## Changes in implementation order

1. WSGI uses `PyObject_Vectorcall`, the internal app handler uses `METH_O`, and logging/close/watchdog callbacks use `PyObject_CallOneArg` or `PyObject_CallNoArgs`. Greenlet switch arguments still use tuples required by its capsule API.
2. Server setup owns a ten-field base environ; each request receives a shallow `PyDict_Copy` and its own remote address/port fields. Template references are released at shutdown, and mutated request dictionaries cannot affect subsequent requests.
3. Access-log mappings build and escape only fields requested by the format. Field values are cached only within that log call. Disabled INFO logging skips formatting. Existing time formatting, quote escaping, missing fields, header lookup behavior and custom formats are preserved.
4. The first header fragment retains the simple path. On subsequent fragments, a bounded C buffer grows geometrically and converts to a Python string when the field/value completes. Latin-1 values, duplicate joining and existing header limits are preserved. Partial request cleanup frees native buffers; allocation failures use the matching Python memory allocator.
5. Reuse pools populate on demand while retaining the existing bounded capacities. Duplicate request clearing is removed. Pooled Python objects use public `PyObject_Init` instead of `_Py_NewReference`.

## End-to-end HTTP comparison

ApacheBench: 16 concurrent keepalive connections, 2,000 warmup requests, 100,000 measured requests per sample, five fresh processes per tree, alternating execution order. Every sample reported zero failed requests. Medians:

| Scenario | Baseline QPS | Optimized QPS | Change | CPU us/request before → after | Peak RSS MiB before → after | P99 ms before → after |
|---|---:|---:|---:|---:|---:|---:|
| Tiny WSGI, logs off | 50,307 | 52,339 | +4.0% | 18.73 → 18.48 | 27.16 → 25.81 | 1 → 1 |
| Tiny WSGI, access logs on | 28,492 | 31,503 | +10.6% | 34.07 → 30.91 | 27.28 → 25.73 | 1 → 1 |
| Minimal Flask route, logs off | 21,546 | 22,463 | +4.3% | 45.11 → 44.05 | 38.23 → 36.88 | 2 → 1 |

The 4% changes are modest relative to system variation; they are observed medians, not established production gains. The access-log case consistently improved in the five paired samples. P99 is rounded to milliseconds by ApacheBench. CPU/request includes warmup; RSS includes startup/imports.

## Isolated logging and parser comparison

Five CPU-time samples, median shown. Logging has 30 extra HTTP headers and an in-memory sink; these values exclude actual log writing:

- Access-log formatting: 13.071 → 2.449 us/log (81.3% less CPU in this fixture).

Native parsing of a 4 KiB header, including environ construction and cleanup:

| Fragment size | Baseline us/request | Optimized us/request | CPU reduction |
|---|---:|---:|---:|
| 4141 bytes | 16.963 | 16.580 | 2.3% |
| 32 bytes | 26.940 | 19.132 | 29.0% |
| 1 bytes | 273.466 | 54.346 | 80.1% |

The one-byte case is a controlled extreme fragmentation workload. It does not represent typical network delivery. The 32-byte case also shows reduced cumulative copying. Whole-buffer parsing remains similar.

## Per-step interpretation

- Vectorcall and environ template: compatibility tests pass. Their individual HTTP runs show small, noisy differences; no independent throughput gain is claimed.
- Lazy logging: step 3 versus step 2 improved median access-log QPS by about 10.4%.
- Header buffers: isolated fragment benchmarks provide the clearest evidence. Whole-buffer paths retain the first-fragment fast path.
- Pools: step 5 versus step 4 reduced median peak RSS by about 2.06 MiB in the tiny-server fixture. Pool reuse is retained; capacities are unchanged.

## Verification

- Python 3.12 C extension builds successfully, with existing unused-variable/function warnings.
- Nineteen focused regression tests pass with greenlet enabled.
- Six HTTP compatibility tests pass with `MEINHELD_NOGREEN=1`.
- Tests cover worker recycling, streaming and cooperative switching, prior log and duplicate-header leaks, environment isolation, callable-instance fallback, exception/close callbacks, fragmented headers, Latin-1, empty/duplicate values, limits, disconnects and both upload storage paths.
- New Python benchmark/test files pass Black and flake8 checks.

Raw samples and environment information are in `results/`. Reproduction commands are in [README.md](README.md).

## Remaining deployment verification

Run on Linux/Python 3.12 with the deployed Gunicorn version and CPU/worker limits. Include log on/off, representative Flask routes, uploads, streaming and concurrent I/O. The Flask fixture here has no database or external service calls. Monitor QPS, P99, CPU, error rate and RSS before deployment.
