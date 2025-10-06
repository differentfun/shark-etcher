from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional


ProgressCallback = Callable[[int, Optional[int]], None]
StatusCallback = Callable[[str], None]


class FlashError(RuntimeError):
    pass


class VerificationError(RuntimeError):
    pass


@dataclass
class ImageSource:
    open_stream: Callable[[], BinaryIO]
    size: Optional[int]
    display_name: str
    cleanup: Callable[[], None]


def prepare_image_source(image_path: str) -> ImageSource:
    path = Path(image_path)
    if not path.exists():
        raise FlashError(f"Image file not found: {path}")
    if not path.is_file():
        raise FlashError(f"Image path is not a file: {path}")

    suffixes = [s.lower() for s in path.suffixes]
    cleanup_callbacks = []

    def make_stream() -> BinaryIO:
        return open(path, "rb")

    size: Optional[int] = path.stat().st_size
    display_name = path.name

    if suffixes:
        if suffixes[-1] in {".gz", ".gzip"}:
            def make_stream() -> BinaryIO:  # type: ignore[misc]
                return gzip.open(path, "rb")
            size = None
        elif suffixes[-1] in {".xz", ".lzma"}:
            def make_stream() -> BinaryIO:  # type: ignore[misc]
                return lzma.open(path, "rb")
            size = None
        elif suffixes[-1] in {".bz2", ".bzip2"}:
            def make_stream() -> BinaryIO:  # type: ignore[misc]
                return bz2.open(path, "rb")
            size = None
        elif suffixes[-1] == ".zip":
            with zipfile.ZipFile(path) as archive:
                entries = [info for info in archive.infolist() if not info.is_dir()]
                if len(entries) != 1:
                    raise FlashError("ZIP archives must contain exactly one image file")
                entry = entries[0]
            temp_dir = Path(tempfile.mkdtemp(prefix="shark_etcher_zip_"))
            target_path = temp_dir / Path(entry.filename).name

            def ensure_extracted() -> Path:
                if target_path.exists():
                    return target_path
                with zipfile.ZipFile(path) as archive:
                    with archive.open(entry) as source, open(target_path, "wb") as dest:
                        shutil.copyfileobj(source, dest, length=4 * 1024 * 1024)
                return target_path

            def make_stream() -> BinaryIO:  # type: ignore[misc]
                extracted = ensure_extracted()
                return open(extracted, "rb")

            def cleanup() -> None:
                try:
                    if target_path.exists():
                        target_path.unlink()
                    temp_dir.rmdir()
                except OSError:
                    pass

            cleanup_callbacks.append(cleanup)
            size = entry.file_size
            display_name = f"{path.name} -> {entry.filename}"

    def run_cleanup() -> None:
        for func in cleanup_callbacks:
            try:
                func()
            except Exception:
                pass

    return ImageSource(
        open_stream=make_stream,
        size=size,
        display_name=display_name,
        cleanup=run_cleanup,
    )


def stream_image_to_device(
    image_source: ImageSource,
    device_path: str,
    *,
    chunk_size: int = 4 * 1024 * 1024,
    progress_callback: Optional[ProgressCallback] = None,
    status_callback: Optional[StatusCallback] = None,
    dry_run: bool = False,
) -> int:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    total_bytes = image_source.size
    bytes_written = 0

    if status_callback:
        status_callback("Starting write")

    try:
        source = image_source.open_stream()
    except OSError as exc:
        raise FlashError(f"Unable to open image: {exc}") from exc

    if dry_run:
        destination: BinaryIO = io.BytesIO()
        sync_required = False
    else:
        try:
            fd = os.open(device_path, os.O_RDWR | getattr(os, "O_SYNC", 0))
        except PermissionError as exc:
            raise FlashError(
                f"Permission denied when opening {device_path}. Try running as root."
            ) from exc
        except FileNotFoundError as exc:
            raise FlashError(f"Device not found: {device_path}") from exc
        except OSError as exc:
            raise FlashError(f"Unable to open device {device_path}: {exc.strerror}") from exc
        destination = os.fdopen(fd, "wb", buffering=0)
        sync_required = True

    with source, destination:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            destination.write(chunk)
            if sync_required:
                destination.flush()
                try:
                    os.fsync(destination.fileno())
                except OSError:
                    pass
            bytes_written += len(chunk)
            if progress_callback:
                progress_callback(bytes_written, total_bytes)

    if status_callback:
        status_callback("Write completed")

    return bytes_written


def verify_device_contents(
    image_source: ImageSource,
    device_path: str,
    *,
    chunk_size: int = 4 * 1024 * 1024,
    progress_callback: Optional[ProgressCallback] = None,
    status_callback: Optional[StatusCallback] = None,
) -> None:
    if status_callback:
        status_callback("Starting verification")

    total = image_source.size
    bytes_checked = 0

    try:
        source = image_source.open_stream()
    except OSError as exc:
        raise VerificationError(f"Unable to reopen image: {exc}") from exc

    try:
        fd = os.open(device_path, os.O_RDONLY)
    except PermissionError as exc:
        raise VerificationError(
            f"Permission denied when reading {device_path}. Try running as root."
        ) from exc
    except FileNotFoundError as exc:
        raise VerificationError(f"Device not found: {device_path}") from exc
    except OSError as exc:
        raise VerificationError(f"Unable to read {device_path}: {exc.strerror}") from exc

    device = os.fdopen(fd, "rb", buffering=0)

    with source, device:
        while True:
            image_chunk = source.read(chunk_size)
            if not image_chunk:
                break
            device_chunk = device.read(len(image_chunk))
            if image_chunk != device_chunk:
                raise VerificationError(
                    f"Verification failed at offset {bytes_checked}"
                )
            bytes_checked += len(image_chunk)
            if progress_callback:
                progress_callback(bytes_checked, total)

    if status_callback:
        status_callback("Verification completed")


def flash_image(
    image_path: str,
    device_path: str,
    *,
    verify: bool = False,
    chunk_size: int = 4 * 1024 * 1024,
    progress_callback: Optional[ProgressCallback] = None,
    verify_progress_callback: Optional[ProgressCallback] = None,
    status_callback: Optional[StatusCallback] = None,
    dry_run: bool = False,
) -> int:
    image_source = prepare_image_source(image_path)
    try:
        written = stream_image_to_device(
            image_source,
            device_path,
            chunk_size=chunk_size,
            progress_callback=progress_callback,
            status_callback=status_callback,
            dry_run=dry_run,
        )
        if verify and not dry_run:
            verify_device_contents(
                image_source,
                device_path,
                chunk_size=chunk_size,
                progress_callback=verify_progress_callback or progress_callback,
                status_callback=status_callback,
            )
        return written
    finally:
        image_source.cleanup()
