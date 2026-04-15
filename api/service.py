"""
Windows Service wrapper for the GOJEP Tender API.

Install:
    python api/service.py install
    python api/service.py start

Remove:
    python api/service.py stop
    python api/service.py remove

Debug (run in console, not as service):
    python api/service.py debug
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PROJECT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_EXE   = sys.executable
LOG_DIR      = os.path.join(PROJECT_DIR, "logs")
LOG_FILE     = os.path.join(LOG_DIR, "api.log")
HOST         = "0.0.0.0"
PORT         = 8000

import servicemanager
import win32event
import win32service
import win32serviceutil


class GojepApiService(win32serviceutil.ServiceFramework):
    _svc_name_        = "GojepTenderAPI"
    _svc_display_name_ = "GOJEP Tender API"
    _svc_description_  = "Public REST API for GOJEP tender listings and analysis."

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.process: subprocess.Popen | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        os.makedirs(LOG_DIR, exist_ok=True)

        # Free the port if a stale uvicorn process is already bound to it
        try:
            import socket
            s = socket.socket()
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                subprocess.run(
                    ["powershell", "-Command",
                     f"Get-NetTCPConnection -LocalPort {PORT} -State Listen |"
                     f" Select-Object -ExpandProperty OwningProcess |"
                     f" ForEach-Object {{ Stop-Process -Id $_ -Force }}"],
                    capture_output=True,
                )
                time.sleep(1)
            s.close()
        except Exception:
            pass

        while True:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as log:
                    self.process = subprocess.Popen(
                        [
                            PYTHON_EXE, "-m", "uvicorn",
                            "api.main:app",
                            "--host", HOST,
                            "--port", str(PORT),
                        ],
                        cwd=PROJECT_DIR,
                        stdout=log,
                        stderr=log,
                    )
                    self.process.wait()

                # If uvicorn exits unexpectedly, check if we were asked to stop
                rc = win32event.WaitForSingleObject(self.stop_event, 0)
                if rc == win32event.WAIT_OBJECT_0:
                    break

                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_WARNING_TYPE,
                    servicemanager.PYS_SERVICE_STARTED,
                    (self._svc_name_, " — uvicorn exited unexpectedly, restarting in 5s"),
                )
                time.sleep(5)

            except Exception as exc:
                servicemanager.LogErrorMsg(f"GojepTenderAPI error: {exc}")
                time.sleep(10)


def _debug():
    """Run uvicorn directly in the console for manual testing."""
    os.makedirs(LOG_DIR, exist_ok=True)
    subprocess.run(
        [PYTHON_EXE, "-m", "uvicorn", "api.main:app",
         "--host", HOST, "--port", str(PORT), "--reload"],
        cwd=PROJECT_DIR,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "debug":
        _debug()
    else:
        win32serviceutil.HandleCommandLine(GojepApiService)
