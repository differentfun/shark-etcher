from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from .devices import (
    DeviceEnumerationError,
    UnmountError,
    find_device_by_path,
    list_block_devices,
    unmount_device,
)
from .imaging import FlashError, VerificationError, flash_image


def _format_size(value: int) -> str:
    if value <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.1f} {unit}"


def _progress_line(prefix: str, current: int, total: Optional[int]) -> str:
    if total:
        percent = (current / total) * 100.0
        return f"{prefix}: {percent:5.1f}% ({_format_size(current)} / {_format_size(total)})"
    return f"{prefix}: {_format_size(current)}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Shark Etcher - flash disk images without telemetry")
    parser.add_argument("--list", action="store_true", help="list detected block devices and exit")
    parser.add_argument("--cli", action="store_true", help="run in console mode (skip GUI)")
    parser.add_argument("--image", "-i", help="path to the disk image")
    parser.add_argument("--device", "-d", help="destination device path (e.g. /dev/sdb)")
    parser.add_argument("--verify", action="store_true", help="verify device contents after writing")
    parser.add_argument("--dry-run", action="store_true", help="simulate the write without touching the device")
    parser.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024, help="chunk size in bytes (default 4 MiB)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if args.worker:
        return _run_worker(args)

    cli_mode = args.cli or args.list or args.image or args.device

    if not cli_mode:
        from .ui import run_gui

        run_gui()
        return 0

    if args.list:
        try:
            devices = list_block_devices(require_removable=False)
        except DeviceEnumerationError as exc:
            print(f"Failed to list devices: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print("No devices detected")
            return 0
        for device in devices:
            removable = "(removable)" if device.removable else ""
            mounts = ", ".join(device.mountpoints) if device.mountpoints else "--"
            print(f"{device.path}\t{_format_size(device.size_bytes)}\t{device.description} {removable}\tMounted: {mounts}")
        return 0

    if not args.image or not args.device:
        parser.error("specify both --image and --device or launch without --cli to use the GUI")

    return _run_cli_flash(args)


def _run_cli_flash(args: argparse.Namespace) -> int:
    if not args.image or not args.device:
        raise ValueError("Missing image or device argument in CLI mode")

    needs_privileges = hasattr(os, "geteuid") and os.geteuid() != 0 and not args.dry_run
    if needs_privileges:
        return _run_cli_via_worker(args)

    device_info = find_device_by_path(args.device, require_removable=False)

    if device_info and device_info.mountpoints and not args.dry_run:
        print(f"Automatically unmounting {args.device} before flashing:")
        try:
            unmounted = unmount_device(device_info)
        except UnmountError as exc:
            print(f"Failed to unmount {args.device}: {exc}", file=sys.stderr)
            return 4
        else:
            for mount in unmounted:
                print(f" - {mount}")
    elif device_info is None:
        print(f"Warning: unable to verify mount status for {args.device}", file=sys.stderr)

    try:
        written = flash_image(
            args.image,
            args.device,
            verify=args.verify,
            chunk_size=args.chunk_size,
            progress_callback=lambda current, total: _print_progress(
                _progress_line("Writing", current, total)
            ),
            verify_progress_callback=lambda current, total: _print_progress(
                _progress_line("Verifying", current, total)
            ),
            status_callback=lambda message: print(message, file=sys.stderr),
            dry_run=args.dry_run,
        )
    except FlashError as exc:
        print(f"Write error: {exc}", file=sys.stderr)
        return 2
    except VerificationError as exc:
        print(f"Verification error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Operation cancelled", file=sys.stderr)
        return 130

    if args.dry_run:
        print("\nDry run completed successfully")
    else:
        print(f"\nWrite completed ({_format_size(written)})")

    return 0


def _run_cli_via_worker(args: argparse.Namespace) -> int:
    if not shutil.which("pkexec"):
        print(
            "Root privileges are required. Install polkit (pkexec) or run the command with sudo.",
            file=sys.stderr,
        )
        return 5

    python_executable = sys.executable or "python3"
    entrypoint = str(PROJECT_ROOT / "shark_etcher" / "__main__.py")
    command = [
        "pkexec",
        python_executable,
        entrypoint,
        "--worker",
        "--image",
        args.image,
        "--device",
        args.device,
        "--chunk-size",
        str(args.chunk_size),
    ]
    if args.verify:
        command.append("--verify")
    if args.dry_run:
        command.append("--dry-run")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
    except FileNotFoundError as exc:
        print(f"Failed to launch privileged helper: {exc}", file=sys.stderr)
        return 6

    error_message: Optional[str] = None
    written_bytes: Optional[int] = None
    dry_run_completed = args.dry_run

    def forward_stderr() -> None:
        assert process.stderr is not None
        for raw_line in process.stderr:
            line = raw_line.rstrip()
            if line:
                print(f"[worker] {line}", file=sys.stderr)

    stderr_thread = threading.Thread(target=forward_stderr, daemon=True)
    stderr_thread.start()

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(f"[worker] {line}", file=sys.stderr)
            continue

        kind = event.get("event")
        if kind == "progress":
            phase = event.get("phase")
            current = int(event.get("current", 0))
            total = event.get("total")
            total_int = int(total) if isinstance(total, int) else None
            label = "Writing" if phase == "write" else "Verifying"
            _print_progress(_progress_line(label, current, total_int))
        elif kind == "status":
            message = event.get("message", "")
            if message:
                print(message, file=sys.stderr)
        elif kind == "log":
            message = event.get("message", "")
            if message:
                print(message, file=sys.stderr)
        elif kind == "done":
            written_bytes = int(event.get("bytes_written", 0))
            dry_run_completed = bool(event.get("dry_run", False))
        elif kind == "error":
            error_message = event.get("message", "Unknown error")

    process.stdout.close()
    return_code = process.wait()
    stderr_thread.join(timeout=0.1)

    # Ensure the progress line does not linger on stdout
    sys.stdout.write("\n")
    sys.stdout.flush()

    if error_message:
        print(error_message, file=sys.stderr)
        return return_code or 1

    if return_code != 0:
        print(f"Privileged helper exited with code {return_code}", file=sys.stderr)
        return return_code

    if written_bytes is None:
        print("Write completed, but no summary was reported.", file=sys.stderr)
        return 0

    if dry_run_completed:
        print("\nDry run completed successfully")
    else:
        print(f"\nWrite completed ({_format_size(written_bytes)})")

    return 0


def _run_worker(args: argparse.Namespace) -> int:
    def emit(event: str, **payload: object) -> None:
        message = {"event": event, **payload}
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    if not args.image or not args.device:
        emit("error", message="Worker missing required arguments")
        return 64

    device_info = find_device_by_path(args.device, require_removable=False)
    if device_info and device_info.mountpoints and not args.dry_run:
        emit("status", message=f"Unmounting {args.device}")
        try:
            unmounted = unmount_device(device_info)
        except UnmountError as exc:
            emit("error", message=str(exc))
            return 10
        else:
            for mount in unmounted:
                emit("log", message=f"Unmounted {mount}")
    elif device_info is None:
        emit("log", message=f"Warning: could not refresh device info for {args.device}")

    try:
        written = flash_image(
            args.image,
            args.device,
            verify=args.verify,
            chunk_size=args.chunk_size,
            progress_callback=lambda current, total: emit(
                "progress", phase="write", current=current, total=total
            ),
            verify_progress_callback=lambda current, total: emit(
                "progress", phase="verify", current=current, total=total
            ),
            status_callback=lambda message: emit("status", message=message),
            dry_run=args.dry_run,
        )
    except FlashError as exc:
        emit("error", message=str(exc))
        return 2
    except VerificationError as exc:
        emit("error", message=str(exc))
        return 3
    except KeyboardInterrupt:
        emit("error", message="Operation cancelled")
        return 130
    except Exception as exc:
        emit("error", message=f"Unexpected error: {exc}")
        return 99

    emit("done", bytes_written=written, dry_run=args.dry_run)
    return 0


def _print_progress(line: str) -> None:
    sys.stdout.write("\r" + line)
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
