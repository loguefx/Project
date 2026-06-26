#!/usr/bin/env python3
"""ShowTVDownloader — unified entrypoint / Windows service host.

This single module is the PyInstaller entry point. The produced
``ShowTVDownloader.exe`` is multi-purpose and dispatches on its arguments:

    ShowTVDownloader.exe install            install the Windows service
    ShowTVDownloader.exe start | stop       start / stop the service
    ShowTVDownloader.exe remove             uninstall the service
    ShowTVDownloader.exe restart            stop then start
    ShowTVDownloader.exe serve [--port N]   run the web server in the foreground
    ShowTVDownloader.exe --run-now          run a downloader campaign (subcommand)
    ShowTVDownloader.exe --rss-grab         RSS grab subcommand
    ShowTVDownloader.exe --watch-downloads  download-watcher subcommand
    ShowTVDownloader.exe --validate-paths   path validator subcommand
    (no args, launched by the SCM)          run as the service

The downloader subcommands exist so the web server can spawn campaign / watcher
work as separate processes of *itself* when frozen (there's no python.exe or
downloader.py to call). See runtime_paths.child_argv().
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from runtime_paths import DATA_DIR, IS_FROZEN
from version import __version__

SERVICE_NAME = "ShowTVDownloader"
SERVICE_DISPLAY = "ShowTVDownloader Web Service"
SERVICE_DESC = (
    "Hosts the ShowTVDownloader web dashboard and background download/scan "
    "workers, and supports in-app updates from GitHub Releases."
)

# Args that mean "act as the downloader CLI" rather than the service/web host.
_DOWNLOADER_PREFIXES = ("--",)


def _configure_file_logging(logfile_name: str) -> None:
    """When running headless (service / detached subprocess) there's no console,
    so log to a file under DATA_DIR."""
    logfile = DATA_DIR / logfile_name
    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# Web host (foreground + service share this)
# ---------------------------------------------------------------------------

def _serve_blocking(host: str, port: int, on_server_ready=None):
    """Build a werkzeug server for the Flask app and serve forever (blocking).
    Returns the server object via on_server_ready so a caller (the service) can
    shut it down from another thread."""
    import web
    from werkzeug.serving import make_server

    web.bootstrap()
    httpd = make_server(host, port, web.app, threaded=True)
    if on_server_ready is not None:
        on_server_ready(httpd)
    logging.getLogger("service").info(
        "ShowTVDownloader %s serving on http://%s:%d (data: %s)",
        __version__, host, port, DATA_DIR,
    )
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# Windows service definition
# ---------------------------------------------------------------------------

def _build_service_class():
    """Import pywin32 lazily so the downloader subcommands and `serve` mode work
    even on machines without pywin32 (e.g. dev/source runs)."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class ShowTVDownloaderService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_ = SERVICE_DESC
        # Frozen: tell the SCM to launch THIS exe directly (not PythonService.exe).
        if IS_FROZEN:
            _exe_name_ = sys.executable

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self.httpd = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            try:
                if self.httpd is not None:
                    self.httpd.shutdown()
            except Exception:  # pylint: disable=broad-except
                pass
            win32event.SetEvent(self.hWaitStop)

        def SvcDoRun(self):
            _configure_file_logging("service.log")
            try:
                os.chdir(str(DATA_DIR))
            except OSError:
                pass
            servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting (v{__version__})")
            try:
                port = int(os.environ.get("STVD_PORT", "5000"))
                self._serve(port)
            except Exception as exc:  # pylint: disable=broad-except
                servicemanager.LogErrorMsg(f"{SERVICE_NAME} crashed: {exc}")
                logging.getLogger("service").exception("Service crashed")
                raise

        def _serve(self, port: int):
            def _ready(httpd):
                self.httpd = httpd
            _serve_blocking("0.0.0.0", port, on_server_ready=_ready)

    return ShowTVDownloaderService


# ---------------------------------------------------------------------------
# Entry dispatch
# ---------------------------------------------------------------------------

def _run_downloader(argv):
    import downloader
    # downloader.main() reads sys.argv via argparse; make it see our flags.
    sys.argv = [sys.argv[0], *argv]
    downloader.main()


def _run_service_dispatch():
    """No-arg launch by the Service Control Manager."""
    import servicemanager
    svc_cls = _build_service_class()
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(svc_cls)
    servicemanager.StartServiceCtrlDispatcher()


def _handle_service_command():
    import win32serviceutil
    svc_cls = _build_service_class()
    win32serviceutil.HandleCommandLine(svc_cls)


def main():
    argv = sys.argv[1:]

    # 1) Downloader subcommands (web server spawns these as separate processes).
    if argv and argv[0].startswith(_DOWNLOADER_PREFIXES):
        _configure_file_logging("subprocess.log")
        _run_downloader(argv)
        return

    # 2) Foreground web server (handy for testing the exe without installing it).
    if argv and argv[0] in ("serve", "web", "run"):
        import argparse
        p = argparse.ArgumentParser(prog="ShowTVDownloader serve")
        p.add_argument("--host", default="0.0.0.0")
        p.add_argument("--port", type=int, default=int(os.environ.get("STVD_PORT", "5000")))
        ns = p.parse_args(argv[1:])
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        _serve_blocking(ns.host, ns.port)
        return

    # 3) No args → launched by the SCM as the service itself.
    if not argv:
        _run_service_dispatch()
        return

    # 4) Anything else (install/start/stop/remove/restart/…) → service control.
    _handle_service_command()


if __name__ == "__main__":
    main()
