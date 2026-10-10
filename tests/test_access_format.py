import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from meinheld.mlogging import Logger, SafeAtoms, _access


def legacy_atoms(environ, bracket_time):
    atoms = {
        "h": environ.get("REMOTE_ADDR", "-"),
        "l": "-",
        "u": "-",
        "t": (
            ("[%s]" % environ.get("LOCAL_TIME", "-"))
            if bracket_time
            else environ.get("LOCAL_TIME", "-")
        ),
        "r": "%s %s %s"
        % (
            environ.get("REQUEST_METHOD", "-"),
            environ.get("PATH_INFO", "-"),
            environ.get("SERVER_PROTOCOL", "HTTP/1.0"),
        ),
        "s": str(environ.get("STATUS_CODE", "-")),
        "b": str(environ.get("SEND_BYTES", "-")),
        "f": environ.get("HTTP_REFERER", "-"),
        "a": environ.get("HTTP_USER_AGENT", "-"),
        "T": str(environ.get("REQUEST_TIME", 1) / 100),
        "D": str(environ.get("REQUEST_TIME", 1)),
        "p": "<%s>" % os.getpid(),
    }
    atoms.update(
        {
            "{%s}i" % key[5:].lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
    )
    return SafeAtoms(atoms)


class AccessFormatTests(unittest.TestCase):
    def logger(self, format):
        logger = SimpleNamespace(
            access_log=Mock(),
            error=Mock(),
            access_log_format=format,
            cfg=SimpleNamespace(access_log_format=format),
        )
        logger.access_log.isEnabledFor.return_value = True
        return logger

    def test_output_matches_previous_formatter(self):
        formats = [
            '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(T)s %(D)s %(p)s',
            "%({X_CUSTOM}i)s %({x_custom}I)s %({unknown}i)s %(unknown)s %({x-custom}i)s %({x}o)s",
            "%(s)5s %(r).3s %% %(h)s %(h)s",
        ]
        environments = [
            {},
            {
                "REMOTE_ADDR": "127.0.0.1",
                "REQUEST_METHOD": "GET",
                "PATH_INFO": '/with"quotes',
                "SERVER_PROTOCOL": "HTTP/1.1",
                "LOCAL_TIME": "now",
                "STATUS_CODE": 200,
                "SEND_BYTES": 12,
                "REQUEST_TIME": 123,
                "HTTP_USER_AGENT": 'agent"quoted',
                "HTTP_REFERER": "ref",
                "HTTP_X_CUSTOM": 'custom"quoted',
            },
        ]
        for environ in environments:
            for format in formats:
                for callback, bracket in ((Logger.access, False), (_access, True)):
                    with self.subTest(
                        environ=environ, format=format, callback=callback.__name__
                    ):
                        logger = self.logger(format)
                        callback(logger, environ)
                        logger.error.assert_not_called()
                        logger.access_log.info.assert_called_once_with(
                            format % legacy_atoms(environ, bracket)
                        )

    def test_unused_headers_are_not_read(self):
        class Environ(dict):
            def items(self):
                raise AssertionError("all headers were scanned")

        logger = self.logger("%(s)s")
        _access(logger, Environ(STATUS_CODE=200, HTTP_UNUSED=object()))
        logger.error.assert_not_called()
        logger.access_log.info.assert_called_once_with("200")

    def test_disabled_logging_skips_formatting(self):
        logger = self.logger("%(T)s")
        logger.access_log.isEnabledFor.return_value = False
        _access(logger, {"REQUEST_TIME": object()})
        logger.access_log.isEnabledFor.assert_called_once_with(logging.INFO)
        logger.access_log.info.assert_not_called()
        logger.error.assert_not_called()

    def test_invalid_format_still_reports_error(self):
        logger = self.logger("%(s)d")
        _access(logger, {"STATUS_CODE": 200})
        logger.error.assert_called_once()
        logger.access_log.info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
