"""Measure native HTTP parsing, controlling fragment sizes without TCP timing.

Requires a C compiler and an in-place meinheld extension build.
"""

import argparse
import ctypes
import importlib.util
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import sysconfig
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    from meinheld import server

    # Make the server's public C symbols available to this separate helper.
    ctypes.PyDLL(server.__file__, mode=ctypes.RTLD_GLOBAL)
    with tempfile.TemporaryDirectory(prefix="meinheld-parser-bench-") as temporary:
        extension = Path(temporary) / (
            "_parser_bench" + sysconfig.get_config_var("EXT_SUFFIX")
        )
        command = shlex.split(sysconfig.get_config_var("LDSHARED")) + [
            "-O3",
            "-fPIC",
            "-DPY_SSIZE_T_CLEAN",
            "-DWITH_GREENLET",
            "-I" + sysconfig.get_path("include"),
            "-I" + str(root / "meinheld/server"),
            str(Path(__file__).with_name("parser_bench.c")),
            "-o",
            str(extension),
        ]
        subprocess.run(command, check=True)
        spec = importlib.util.spec_from_file_location("_parser_bench", extension)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        request = (
            b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Long: " + b"x" * 4096 + b"\r\n\r\n"
        )
        for chunk, iterations in ((0, 50000), (32, 10000), (1, 2000)):
            samples = [helper.bench(request, iterations, chunk) for _ in range(5)]
            print(
                json.dumps(
                    {
                        "root": str(root),
                        "fragment_bytes": chunk or len(request),
                        "cpu_us_per_request": statistics.median(samples),
                        "samples": samples,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
