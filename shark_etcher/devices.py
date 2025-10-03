from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence


@dataclass
class BlockDevice:
    name: str
    path: str
    size_bytes: int
    model: str
    removable: bool
    transport: Optional[str]
    description: str
    mountpoints: List[str]

    @property
    def is_writable(self) -> bool:
        return not self.path.startswith("/dev/loop") and not self.path.startswith("/dev/ram")


class DeviceEnumerationError(RuntimeError):
    pass


class UnmountError(RuntimeError):
    """Raised when automatic unmounting fails."""

    def __init__(self, message: str, *, partial: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.partial = partial or []


def find_device_by_path(device_path: str, *, require_removable: bool = False) -> Optional[BlockDevice]:
    """Return the block device matching *device_path* if present."""
    try:
        devices = list_block_devices(require_removable=require_removable)
    except DeviceEnumerationError:
        return None
    for device in devices:
        if device.path == device_path:
            return device
    return None


def unmount_device(device: BlockDevice) -> List[str]:
    """Unmount all mountpoints associated with *device*.

    Returns the list of mountpoints that were successfully unmounted or raises
    :class:`UnmountError` if one or more unmount operations fail.
    """
    targets = sorted({mp for mp in device.mountpoints if mp}, key=len, reverse=True)
    if not targets:
        return []

    unmounted: List[str] = []
    errors: List[str] = []

    for target in targets:
        if _unmount_target(target):
            unmounted.append(target)
        else:
            errors.append(target)

    if errors:
        details = ", ".join(errors)
        raise UnmountError(f"Failed to unmount: {details}", partial=unmounted)

    return unmounted


def _unmount_target(target: str) -> bool:
    candidates = [
        ["umount", target],
    ]

    source = _lookup_mount_source(target)
    if source and shutil.which("udisksctl"):
        candidates.append(["udisksctl", "unmount", "-b", source])

    candidates.append(["umount", "-l", target])

    for cmd in candidates:
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError:
            continue
        else:
            return True
    return False


def _lookup_mount_source(target: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["findmnt", "-no", "SOURCE", "--", target],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = completed.stdout.strip()
    return value or None


def list_block_devices(require_removable: bool = True) -> List[BlockDevice]:
    system = platform.system()
    if system == "Linux":
        devices = _linux_devices()
    elif system == "Darwin":
        devices = _darwin_devices()
    elif system == "Windows":
        devices = _windows_devices()
    else:
        raise DeviceEnumerationError(f"Unsupported platform: {system}")

    if require_removable:
        devices = [d for d in devices if d.removable]

    return devices


def _linux_devices() -> List[BlockDevice]:
    cmd = [
        "lsblk",
        "--bytes",
        "--all",
        "--json",
        "--output",
        "NAME,TYPE,SIZE,RM,MODEL,TRAN,MOUNTPOINT,MOUNTPOINTS",
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DeviceEnumerationError("`lsblk` command not available") from exc
    except subprocess.CalledProcessError as exc:
        raise DeviceEnumerationError(exc.stderr.strip() or "lsblk failed") from exc

    try:
        payload = json.loads(completed.stdout)
        raw_devices = payload.get("blockdevices", [])
    except json.JSONDecodeError as exc:
        raise DeviceEnumerationError("Failed to parse lsblk output") from exc

    devices: List[BlockDevice] = []
    for raw in raw_devices:
        if raw.get("type") != "disk":
            continue
        name = raw.get("name")
        if not name:
            continue
        path = os.path.join("/dev", name)
        size_bytes = int(raw.get("size") or 0)
        removable = bool(int(raw.get("rm") or 0))
        model = (raw.get("model") or "").strip()
        transport = raw.get("tran") or None
        mountpoints = sorted(_collect_mountpoints(raw))
        description = _format_description(name, size_bytes, model, transport)
        devices.append(
            BlockDevice(
                name=name,
                path=path,
                size_bytes=size_bytes,
                model=model,
                removable=removable,
                transport=transport,
                description=description,
                mountpoints=mountpoints,
            )
        )
    return devices


def _collect_mountpoints(node: dict) -> Iterable[str]:
    mountpoints: List[str] = []
    mp = node.get("mountpoint")
    if mp:
        mountpoints.append(mp)
    list_mp = node.get("mountpoints") or []
    for item in list_mp:
        if item:
            mountpoints.append(item)
    for child in node.get("children", []) or []:
        mountpoints.extend(_collect_mountpoints(child))
    return {m for m in mountpoints if m}


def _format_description(name: str, size_bytes: int, model: str, transport: Optional[str]) -> str:
    size_text = _format_size(size_bytes)
    label = model or "Generic Device"
    if transport:
        label = f"{label} ({transport})"
    return f"{name} - {size_text} - {label}"


def _format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "Unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size_bytes)
    unit = units[0]
    for unit in units:
        if value < 1024.0:
            break
        value /= 1024.0
    return f"{value:.1f} {unit}"


def _darwin_devices() -> List[BlockDevice]:
    raise DeviceEnumerationError("macOS support not implemented yet")


def _windows_devices() -> List[BlockDevice]:
    raise DeviceEnumerationError("Windows support not implemented yet")
