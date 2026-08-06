from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

AGENT_COMMAND = ("perfpilot-agent.exe", "run")


class PerfPilotAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "PerfPilotAgent"
    _svc_display_name_ = "PerfPilot Android Device Agent"
    _svc_description_ = "Connects local Android devices to the PerfPilot control plane."

    def __init__(self, arguments: list[str]) -> None:
        super().__init__(arguments)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def _command() -> list[str]:
        service_binary = Path(sys.executable).resolve()
        agent_binary = service_binary.with_name(AGENT_COMMAND[0])
        if not agent_binary.is_file():
            raise FileNotFoundError
        return [str(agent_binary), AGENT_COMMAND[1]]

    def _terminate_child(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        self._terminate_child()

    def SvcDoRun(self) -> None:
        environment = dict(os.environ)
        program_data = environment.get("ProgramData", r"C:\ProgramData")
        state_directory = Path(program_data) / "PerfPilot" / "Agent"
        state_directory.mkdir(parents=True, exist_ok=True)
        environment["ANDROID_USER_HOME"] = str(state_directory / ".android")
        self._process = subprocess.Popen(
            self._command(),
            cwd=state_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        while self._process.poll() is None:
            if win32event.WaitForSingleObject(self._stop_event, 500) == win32event.WAIT_OBJECT_0:
                self._terminate_child()
                return
        if self._process.returncode:
            raise RuntimeError("PerfPilot Agent process stopped unexpectedly")


def main() -> None:
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PerfPilotAgentService)
        servicemanager.StartServiceCtrlDispatcher()
        return
    win32serviceutil.HandleCommandLine(PerfPilotAgentService)


if __name__ == "__main__":
    main()
