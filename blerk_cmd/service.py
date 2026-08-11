from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from blerk import config


def _get_blerk_cmd() -> str:
    """Get the full path to the blerk command."""
    cmd = shutil.which("blerk")
    if cmd:
        return cmd
    return f"{sys.executable} -m blerk_cmd.main"


def _load_template(filename: str) -> str:
    """Load a service template from .github/templates/."""
    template_path = Path(__file__).parent.parent / ".github" / "templates" / filename
    return template_path.read_text()


def _substitute_template(template: str, blerk_cmd: str, config_path: str) -> str:
    """Replace placeholders in template."""
    home = str(Path.home())
    user = os.environ.get("USER", os.environ.get("USERNAME", ""))

    # Normalize config path to absolute, native format
    config_path = str(Path(config_path).resolve())

    return template.format(
        BLERK_CMD=f'{blerk_cmd} --config "{config_path}"',
        USER=user,
        HOME=home,
    )


class ServiceManager:
    @staticmethod
    def install(config_path: str) -> None:
        """Install blerk as a system service."""
        if sys.platform == "linux":
            ServiceManager._install_linux(config_path)
        elif sys.platform == "darwin":
            ServiceManager._install_macos(config_path)
        elif sys.platform == "win32":
            ServiceManager._install_windows(config_path)
        else:
            print(f"Unsupported platform: {sys.platform}")
            sys.exit(1)

    @staticmethod
    def _install_linux(config_path: str) -> None:
        """Install systemd service on Linux."""
        if os.geteuid() != 0:
            print("ERROR: Installing systemd service requires root privileges.")
            print("Please run: sudo blerk service install")
            sys.exit(1)

        blerk_cmd = _get_blerk_cmd()
        template = _load_template("blerk.service")
        service_content = _substitute_template(template, blerk_cmd, config_path)

        service_file = Path("/etc/systemd/system/blerk.service")
        service_file.write_text(service_content)
        print(f"Installed: {service_file}")

        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "blerk"], check=True)
        subprocess.run(["systemctl", "start", "blerk"], check=True)
        print("blerk service started and enabled at boot")

    @staticmethod
    def _install_macos(config_path: str) -> None:
        """Install launchd service on macOS."""
        blerk_cmd = _get_blerk_cmd()
        template = _load_template("com.blerk.plist")
        plist_content = _substitute_template(template, blerk_cmd, config_path)

        plist_file = Path.home() / "Library" / "LaunchAgents" / "com.blerk.plist"
        plist_file.parent.mkdir(parents=True, exist_ok=True)
        plist_file.write_text(plist_content)
        print(f"Installed: {plist_file}")

        subprocess.run(["launchctl", "load", str(plist_file)], check=True)
        print("blerk service started and enabled at boot")

    @staticmethod
    def _install_windows(config_path: str) -> None:
        """Install Task Scheduler service on Windows."""
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("ERROR: Installing Task Scheduler entry requires administrator privileges.")
                print("Please run: python -m blerk_cmd.service install")
                print("with 'Run as administrator' or use: runas /user:Administrator blerk service install")
                sys.exit(1)
        except Exception:
            pass

        blerk_cmd = _get_blerk_cmd()
        task_name = "blerk"

        # Create log directory if it doesn't exist
        log_dir = Path.home() / ".blerk"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "blerk.log"

        # Use cmd.exe to handle output redirection
        task_command = f'cmd /c "{blerk_cmd}" --config "{config_path}" >> "{log_file}" 2>&1'

        subprocess.run(
            [
                "schtasks",
                "/create",
                "/tn",
                task_name,
                "/tr",
                task_command,
                "/sc",
                "onstart",
                "/f",
            ],
            check=True,
            capture_output=True,
        )
        print(f"Installed Task Scheduler entry: {task_name}")
        print(f"Logs will be written to: {log_file}")
        print("blerk service will start at next boot")

    @staticmethod
    def uninstall() -> None:
        """Uninstall blerk system service."""
        if sys.platform == "linux":
            ServiceManager._uninstall_linux()
        elif sys.platform == "darwin":
            ServiceManager._uninstall_macos()
        elif sys.platform == "win32":
            ServiceManager._uninstall_windows()
        else:
            print(f"Unsupported platform: {sys.platform}")
            sys.exit(1)

    @staticmethod
    def _uninstall_linux() -> None:
        """Remove systemd service on Linux."""
        if os.geteuid() != 0:
            print("ERROR: Uninstalling systemd service requires root privileges.")
            print("Please run: sudo blerk service uninstall")
            sys.exit(1)

        subprocess.run(["systemctl", "stop", "blerk"], capture_output=True)
        subprocess.run(["systemctl", "disable", "blerk"], capture_output=True)

        service_file = Path("/etc/systemd/system/blerk.service")
        if service_file.exists():
            service_file.unlink()
            print(f"Removed: {service_file}")

        subprocess.run(["systemctl", "daemon-reload"], check=True)
        print("blerk service uninstalled")

    @staticmethod
    def _uninstall_macos() -> None:
        """Remove launchd service on macOS."""
        plist_file = Path.home() / "Library" / "LaunchAgents" / "com.blerk.plist"

        subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True)

        if plist_file.exists():
            plist_file.unlink()
            print(f"Removed: {plist_file}")

        print("blerk service uninstalled")

    @staticmethod
    def _uninstall_windows() -> None:
        """Remove Task Scheduler service on Windows."""
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("ERROR: Uninstalling Task Scheduler entry requires administrator privileges.")
                print("Please run with 'Run as administrator' or use: runas /user:Administrator blerk service uninstall")
                sys.exit(1)
        except Exception:
            pass

        task_name = "blerk"

        subprocess.run(
            ["schtasks", "/delete", "/tn", task_name, "/f"],
            capture_output=True,
        )
        print(f"Removed Task Scheduler entry: {task_name}")
        print("blerk service uninstalled")

    @staticmethod
    def status() -> str:
        """Check service status."""
        if sys.platform == "linux":
            return ServiceManager._status_linux()
        elif sys.platform == "darwin":
            return ServiceManager._status_macos()
        elif sys.platform == "win32":
            return ServiceManager._status_windows()
        else:
            return f"Unsupported platform: {sys.platform}"

    @staticmethod
    def _status_linux() -> str:
        """Check systemd service status."""
        try:
            result = subprocess.run(
                ["systemctl", "status", "blerk"],
                capture_output=True,
                text=True,
            )
            return result.stdout + result.stderr
        except Exception as e:
            return f"Error checking status: {e}"

    @staticmethod
    def _status_macos() -> str:
        """Check launchd service status."""
        try:
            result = subprocess.run(
                ["launchctl", "list"],
                capture_output=True,
                text=True,
            )
            if "com.blerk" in result.stdout:
                return "blerk service is installed and running"
            else:
                return "blerk service is not installed"
        except Exception as e:
            return f"Error checking status: {e}"

    @staticmethod
    def _status_windows() -> str:
        """Check Task Scheduler service status."""
        try:
            result = subprocess.run(
                ["schtasks", "/query", "/tn", "blerk", "/v"],
                capture_output=True,
                text=True,
            )
            if "blerk" in result.stdout:
                return result.stdout
            else:
                return "blerk service is not installed"
        except Exception as e:
            return f"Error checking status: {e}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage blerk as a system service")
    subparsers = parser.add_subparsers(dest="action", required=True)

    install_parser = subparsers.add_parser(
        "install", help="Install blerk as a system service"
    )
    install_parser.add_argument(
        "--config",
        default=config.default_path(),
        help=f"Path to config file (default: {config.default_path()})",
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Remove blerk system service"
    )

    status_parser = subparsers.add_parser("status", help="Show service status")

    args = parser.parse_args(argv)

    try:
        if args.action == "install":
            ServiceManager.install(args.config)
            return 0
        elif args.action == "uninstall":
            ServiceManager.uninstall()
            return 0
        elif args.action == "status":
            print(ServiceManager.status())
            return 0
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        if e.stderr:
            print(e.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
