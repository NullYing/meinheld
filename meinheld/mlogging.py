import datetime
import logging
logging.Logger.manager.emittedNoHandlerWarning = 1
import os
import sys
import traceback

import fcntl

def _close_on_exec(fd):
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    flags |= fcntl.FD_CLOEXEC
    fcntl.fcntl(fd, fcntl.F_SETFD, flags)

class SafeAtoms(dict):

    def __init__(self, atoms):
        dict.__init__(self)
        for key, value in atoms.items():
            self[key] = value.replace('"', '\\"')

    def __getitem__(self, k):
        if k.startswith("{"):
            kl = k.lower()
            if kl in self:
                return super(SafeAtoms, self).__getitem__(kl)
            else:
                return "-"
        if k in self:
            return super(SafeAtoms, self).__getitem__(k)
        else:
            return '-'

class AccessAtoms(dict):
    """Build and escape only fields requested by the access-log format.

    Values belong to one request. No request headers are cached globally.
    """

    _fields = {
        "h": "REMOTE_ADDR", "f": "HTTP_REFERER", "a": "HTTP_USER_AGENT",
    }
    _numbers = {"s": "STATUS_CODE", "b": "SEND_BYTES", "D": "REQUEST_TIME"}

    def __init__(self, environ, bracket_time=False):
        super().__init__()
        self.environ = environ
        self.bracket_time = bracket_time

    def __missing__(self, key):
        env = self.environ
        if key in self._fields:
            value = env.get(self._fields[key], "-")
        elif key in self._numbers:
            default = 1 if key == "D" else "-"
            value = str(env.get(self._numbers[key], default))
        elif key in ("l", "u"):
            value = "-"
        elif key == "t":
            value = env.get("LOCAL_TIME", "-")
            if self.bracket_time:
                value = "[%s]" % value
        elif key == "r":
            value = "%s %s %s" % (
                env.get("REQUEST_METHOD", "-"), env.get("PATH_INFO", "-"),
                env.get("SERVER_PROTOCOL", "HTTP/1.0"),
            )
        elif key == "T":
            value = str(env.get("REQUEST_TIME", 1) / 100)
        elif key == "p":
            value = "<%s>" % os.getpid()
        elif key.startswith("{") and key.lower().endswith("}i"):
            value = env.get("HTTP_" + key[1:-2].upper(), "-")
        else:
            value = "-"
        value = value.replace('"', '\\"')
        self[key] = value
        return value


class Logger(object):

    LOG_LEVELS = {
        "critical": logging.CRITICAL,
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG
    }

    error_fmt = r"%(asctime)s [%(process)d] [%(levelname)s] %(message)s"
    datefmt = r"%Y-%m-%d %H:%M:%S"

    access_log_format = '"%(h)s %(l)s %(u)s [%(t)s] "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
    access_fmt = "%(message)s"

    def __init__(self, cfg=None):
        self.error_log = logging.getLogger("meinheld.error")
        self.access_log = logging.getLogger("meinheld.access")
        self.error_handlers = []
        self.access_handlers = []
        self.setup(cfg)

    def setup(self, cfg):
        self.error_log.setLevel(logging.ERROR)
        self.access_log.setLevel(logging.INFO)

        self._set_handler(self.error_log, logging.Formatter(self.error_fmt, self.datefmt))
        self._set_handler(self.access_log, fmt=logging.Formatter(self.access_fmt))

    def critical(self, msg, *args, **kwargs):
        self.error_log.critical(msg, *args, **kwargs)

    def error(self, *args, **kwargs):
        self.error_log.error(*args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.error_log.warning(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.error_log.info(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self.error_log.debug(msg, *args, **kwargs)

    def exception(self, msg, *args):
        self.error_log.exception(msg, *args)

    def log(self, lvl, msg, *args, **kwargs):
        if isinstance(lvl, str):
            lvl = self.LOG_LEVELS.get(lvl.lower(), logging.INFO)
        self.error_log.log(lvl, msg, *args, **kwargs)

    def access(self, environ):
        if not self.access_log.isEnabledFor(logging.INFO):
            return
        try:
            atoms = AccessAtoms(environ)
            self.access_log.info(self.access_log_format % atoms)
        except Exception:
            self.error(*sys.exc_info())

    def reopen_files(self):
        for log in (self.error_log, self.access_log):
            for handler in log.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.acquire()
                    try:
                        if handler.stream:
                            handler.stream.close()
                            handler.stream = open(handler.baseFilename,
                                    handler.mode)
                    finally:
                        handler.release()

    def close_on_exec(self):
        for log in (self.error_log, self.access_log):
            for handler in log.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.acquire()
                    try:
                        if handler.stream:
                            _close_on_exec(handler.stream.fileno())
                    finally:
                        handler.release()


    def _get_handler(self, log):
        for h in log.handlers:
            if getattr(h, "_meinheld", False) == True:
                return h

    def _set_handler(self, log, fmt):
        h = self._get_handler(log)
        if h:
            log.handlers.remove(h)

        h = logging.StreamHandler()
        h._meinheld = True
        h.setFormatter(fmt)
        log.addHandler(h)

def _error(self, *args, **kwargs):
    self.error_log.error(*args, **kwargs)

def _access(self, environ):
    if not self.access_log.isEnabledFor(logging.INFO):
        return
    try:
        atoms = AccessAtoms(environ, bracket_time=True)
        self.access_log.info(self.cfg.access_log_format % atoms)
    except Exception:
        self.error(*sys.exc_info())

logger = Logger()
from meinheld import server
server.set_access_logger(logger)
server.set_error_logger(logger)

