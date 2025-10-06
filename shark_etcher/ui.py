from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional, TextIO

try:
    from .devices import BlockDevice, DeviceEnumerationError, list_block_devices
except ImportError:
    if __package__ in (None, ""):
        package_root = Path(__file__).resolve().parent.parent
        if str(package_root) not in sys.path:
            sys.path.insert(0, str(package_root))
        from shark_etcher.devices import BlockDevice, DeviceEnumerationError, list_block_devices  # type: ignore
    else:
        raise


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


class EtcherApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Shark Etcher")
        self.geometry("780x750")
        self.minsize(680, 670)

        self.palette = {
            "bg": "#f3f4f6",
            "surface": "#ffffff",
            "border": "#d1d5db",
            "text": "#1f2933",
            "muted": "#6b7280",
            "accent": "#2563eb",
            "accent_active": "#1d4ed8",
            "success": "#145a32",
            "warning": "#c0392b",
        }

        self.project_root = PROJECT_ROOT
        self.chunk_size = DEFAULT_CHUNK_SIZE

        self._apply_theme()

        self.image_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Ready")
        self.image_info = tk.StringVar(value="No image selected")
        self.verify_enabled = tk.BooleanVar(value=True)
        self.dry_run_enabled = tk.BooleanVar(value=False)

        self.devices: list[BlockDevice] = []
        self.selected_device: Optional[BlockDevice] = None
        self.progress_total: Optional[int] = None

        self.event_queue: queue.Queue[tuple] = queue.Queue()
        self.flash_thread: Optional[threading.Thread] = None

        self._build_layout()
        self.refresh_devices()
        self.after(100, self._poll_events)

        self.image_path.trace_add("write", self._on_image_path_changed)

    def _build_layout(self) -> None:
        self.option_add("*TCombobox*Listbox.font", ("TkDefaultFont", 10))
        palette = self.palette

        main_frame = ttk.Frame(self, padding=16, style="App.TFrame")
        main_frame.pack(fill=tk.BOTH, expand=True)

        file_frame = ttk.Frame(main_frame, style="Surface.TFrame", padding=12)
        file_frame.pack(fill=tk.X)

        ttk.Label(file_frame, text="Image file:", style="Section.TLabel").pack(anchor=tk.W)
        file_row = ttk.Frame(file_frame, style="Surface.TFrame")
        file_row.pack(fill=tk.X, pady=(8, 8))

        self.file_entry = ttk.Entry(file_row, textvariable=self.image_path, style="App.TEntry")
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.browse_btn = ttk.Button(
            file_row,
            text="Browse...",
            command=self._choose_image,
            style="Secondary.TButton",
        )
        self.browse_btn.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Label(file_frame, textvariable=self.image_info, style="MutedCard.TLabel").pack(anchor=tk.W)

        devices_frame = ttk.LabelFrame(
            main_frame,
            text="Detected drives",
            padding=12,
            style="Card.TLabelframe",
        )
        devices_frame.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        columns = ("size", "model", "transport", "mounts")
        self.devices_tree = ttk.Treeview(
            devices_frame,
            columns=columns,
            show="headings",
            height=4,
            style="Modern.Treeview",
        )
        self.devices_tree.heading("size", text="Size")
        self.devices_tree.heading("model", text="Model")
        self.devices_tree.heading("transport", text="Bus")
        self.devices_tree.heading("mounts", text="Mounted at")
        self.devices_tree.column("size", width=110, anchor=tk.CENTER)
        self.devices_tree.column("model", width=240, anchor=tk.W)
        self.devices_tree.column("transport", width=80, anchor=tk.CENTER)
        self.devices_tree.column("mounts", width=220, anchor=tk.W)
        self.devices_tree.bind("<<TreeviewSelect>>", self._on_device_selected)
        self.devices_tree.tag_configure("internal", foreground=palette["warning"])
        self.devices_tree.tag_configure("removable", foreground=palette["success"])

        tree_scroll = ttk.Scrollbar(
            devices_frame,
            orient=tk.VERTICAL,
            command=self.devices_tree.yview,
        )
        self.devices_tree.configure(yscrollcommand=tree_scroll.set)

        self.devices_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8), pady=6)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=6)

        options_frame = ttk.Frame(main_frame, style="App.TFrame")
        options_frame.pack(fill=tk.X, pady=(4, 4))

        self.verify_check = ttk.Checkbutton(
            options_frame,
            text="Verify after write",
            variable=self.verify_enabled,
            style="Toggle.TCheckbutton",
        )
        self.verify_check.pack(side=tk.LEFT)

        self.dry_run_check = ttk.Checkbutton(
            options_frame,
            text="Dry run (no write)",
            variable=self.dry_run_enabled,
            style="Toggle.TCheckbutton",
        )
        self.dry_run_check.pack(side=tk.LEFT, padx=(16, 0))

        controls_frame = ttk.Frame(main_frame, style="App.TFrame")
        controls_frame.pack(fill=tk.X, pady=8)

        self.refresh_btn = ttk.Button(
            controls_frame,
            text="Refresh",
            command=self.refresh_devices,
            style="Secondary.TButton",
        )
        self.refresh_btn.pack(side=tk.LEFT)

        self.flash_btn = ttk.Button(
            controls_frame,
            text="Write image",
            command=self.start_flash,
            style="Primary.TButton",
        )
        self.flash_btn.pack(side=tk.RIGHT)

        progress_frame = ttk.Frame(main_frame, style="Surface.TFrame", padding=12)
        progress_frame.pack(fill=tk.X)

        self.progress_bar = ttk.Progressbar(
            progress_frame,
            mode="determinate",
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill=tk.X)

        self.status_label = ttk.Label(
            progress_frame,
            textvariable=self.status_text,
            anchor=tk.W,
            style="MutedCard.TLabel",
        )
        self.status_label.pack(fill=tk.X, pady=(6, 0))

        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=12, style="Card.TLabelframe")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(16, 0))

        log_container = ttk.Frame(log_frame, style="Surface.TFrame")
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_widget = tk.Text(
            log_container,
            height=7,
            borderwidth=0,
            highlightthickness=0,
            wrap=tk.NONE,
        )
        log_scrollbar = ttk.Scrollbar(
            log_container,
            orient=tk.VERTICAL,
            command=self.log_widget.yview,
        )
        self.log_widget.configure(yscrollcommand=log_scrollbar.set)
        self.log_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_widget.configure(
            bg=palette["surface"],
            fg=palette["text"],
            insertbackground=palette["accent"],
            selectbackground=palette["accent"],
            selectforeground="white",
        )

        self._busy_widgets = [
            self.refresh_btn,
            self.flash_btn,
            self.devices_tree,
            self.file_entry,
            self.browse_btn,
            self.verify_check,
            self.dry_run_check,
        ]

        self._update_flash_button_state()

    def _apply_theme(self) -> None:
        palette = self.palette
        self.configure(bg=palette["bg"])

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=palette["bg"])
        style.configure("Surface.TFrame", background=palette["surface"])
        style.configure(
            "Card.TLabelframe",
            background=palette["surface"],
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=palette["surface"],
            foreground=palette["muted"],
            font=("TkDefaultFont", 10, "bold"),
        )
        style.configure("App.TLabel", background=palette["bg"], foreground=palette["text"])
        style.configure(
            "Section.TLabel",
            background=palette["surface"],
            foreground=palette["text"],
            font=("TkDefaultFont", 10, "bold"),
        )
        style.configure("Card.TLabel", background=palette["surface"], foreground=palette["text"])
        style.configure("Muted.TLabel", background=palette["bg"], foreground=palette["muted"])
        style.configure("MutedCard.TLabel", background=palette["surface"], foreground=palette["muted"])

        style.configure(
            "App.TEntry",
            foreground=palette["text"],
            fieldbackground=palette["surface"],
            background=palette["surface"],
            borderwidth=1,
        )

        style.configure(
            "Primary.TButton",
            background=palette["accent"],
            foreground="white",
            padding=(14, 8),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("pressed", palette["accent_active"]),
                ("active", palette["accent_active"]),
                ("disabled", palette["border"]),
            ],
            foreground=[("disabled", palette["muted"])],
        )

        style.configure(
            "Secondary.TButton",
            background=palette["surface"],
            foreground=palette["text"],
            padding=(12, 8),
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("pressed", palette["bg"]),
                ("active", palette["bg"]),
                ("disabled", palette["border"]),
            ],
            foreground=[("disabled", palette["muted"])],
        )

        style.configure(
            "Toggle.TCheckbutton",
            background=palette["bg"],
            foreground=palette["text"],
            padding=(6, 4),
        )
        style.map(
            "Toggle.TCheckbutton",
            background=[("active", palette["bg"])],
            foreground=[("disabled", palette["muted"])],
            indicatorcolor=[
                ("selected", palette["accent"]),
                ("!selected", palette["border"]),
            ],
        )

        style.configure(
            "Modern.Treeview",
            background=palette["surface"],
            fieldbackground=palette["surface"],
            foreground=palette["text"],
            borderwidth=1,
            rowheight=28,
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", palette["accent"])],
            foreground=[("selected", "white")],
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=palette["bg"],
            foreground=palette["muted"],
            relief="flat",
            padding=6,
        )
        style.map("Modern.Treeview.Heading", background=[("active", palette["bg"])])

        style.configure(
            "Accent.Horizontal.TProgressbar",
            background=palette["accent"],
            troughcolor=palette["surface"],
            borderwidth=0,
        )
        style.map(
            "Accent.Horizontal.TProgressbar",
            background=[("active", palette["accent_active"])],
        )

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert(tk.END, formatted + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state=tk.DISABLED)

    def refresh_devices(self) -> None:
        try:
            devices = list_block_devices(require_removable=False)
        except DeviceEnumerationError as exc:
            messagebox.showerror("Error", str(exc))
            return

        self.devices = devices
        self.devices_tree.delete(*self.devices_tree.get_children())
        for idx, device in enumerate(devices):
            mounts = ", ".join(device.mountpoints) if device.mountpoints else '--'
            tag = "removable" if device.removable else "internal"
            self.devices_tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    _format_size(device.size_bytes),
                    f"{device.description}",
                    device.transport or "-",
                    mounts,
                ),
                tags=(tag,),
            )

        self.selected_device = None
        self.status_text.set("Select an image and a target drive")
        self._update_flash_button_state()

    def _choose_image(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select image file",
            filetypes=[
                ("Images", "*.img *.iso *.zip *.gz *.xz *.bz2"),
                ("All files", "*"),
            ],
        )
        if file_path:
            self.image_path.set(file_path)
            self._update_image_info()

    def _update_image_info(self) -> None:
        path_value = self.image_path.get().strip()
        if not path_value:
            self.image_info.set("No image selected")
            return
        path = Path(path_value)
        if not path.exists():
            self.image_info.set("Image not found")
            return
        size_text = _format_size(path.stat().st_size)
        self.image_info.set(f"{path.name} - {size_text}")

    def _on_image_path_changed(self, *_args: object) -> None:
        self._update_image_info()
        self._update_flash_button_state()

    def _on_device_selected(self, _event: tk.Event) -> None:
        selection = self.devices_tree.selection()
        if not selection:
            self.selected_device = None
            self._update_flash_button_state()
            return
        index = int(selection[0])
        self.selected_device = self.devices[index]
        self.status_text.set(f"Ready to write to {self.selected_device.path}")
        self._update_flash_button_state()

    def start_flash(self) -> None:
        if self.flash_thread and self.flash_thread.is_alive():
            return

        image_path = self.image_path.get().strip()
        if not image_path:
            messagebox.showwarning("Missing image", "Select an image file first")
            return
        if not os.path.exists(image_path):
            messagebox.showerror("File not found", "The selected image file does not exist")
            return
        if not self.selected_device:
            messagebox.showwarning("Missing drive", "Select a target device")
            return

        device = self.selected_device

        if device.mountpoints and not self.dry_run_enabled.get():
            formatted = "\n".join(device.mountpoints)
            proceed = messagebox.askyesno(
                "Drive mounted",
                f"Drive {device.path} is currently mounted at:\n{formatted}\n\n"
                "It will be unmounted automatically before flashing. Continue?",
            )
            if not proceed:
                return

        if not device.removable:
            proceed = messagebox.askyesno(
                "Potentially unsafe device",
                f"{device.path} does not report as removable. Are you sure you want to continue?",
            )
            if not proceed:
                return

        confirm = messagebox.askyesno(
            "Confirm write",
            f"Write the image to {device.path}?\nAll data on the drive will be lost.",
        )
        if not confirm:
            return

        self._set_busy(True)
        self._reset_progress_bar()
        self.status_text.set("Launching write operation")
        self.log(f"Writing {Path(image_path).name} to {device.path}")

        args = (
            image_path,
            device.path,
            self.verify_enabled.get(),
            self.dry_run_enabled.get(),
        )

        self.flash_thread = threading.Thread(
            target=self._run_worker_process,
            args=args,
            daemon=True,
        )
        self.flash_thread.start()

    def _run_worker_process(
        self,
        image_path: str,
        device_path: str,
        verify: bool,
        dry_run: bool,
    ) -> None:
        command, error_message = self._build_worker_command(
            image_path=image_path,
            device_path=device_path,
            verify=verify,
            dry_run=dry_run,
        )
        if command is None:
            self.event_queue.put(("error", error_message or "Missing pkexec to gain privileges."))
            return

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.project_root),
            )
        except Exception as exc:  # pragma: no cover - launch failure
            self.event_queue.put(("error", f"Failed to launch privileged helper: {exc}"))
            return

        done = False
        errored = False

        def handle_event(event: dict) -> None:
            nonlocal done, errored
            kind = event.get("event")
            if kind == "progress":
                phase = event.get("phase")
                current = int(event.get("current", 0))
                total = event.get("total")
                total_value = int(total) if isinstance(total, int) else None
                if phase == "write":
                    self.event_queue.put(("progress", current, total_value))
                else:
                    self.event_queue.put(("verify", current, total_value))
            elif kind == "status":
                message = event.get("message", "")
                if message:
                    self.event_queue.put(("status", message))
            elif kind == "log":
                message = event.get("message", "")
                if message:
                    self.event_queue.put(("log", message))
            elif kind == "done":
                done = True
                written = int(event.get("bytes_written", 0))
                dry = bool(event.get("dry_run", dry_run))
                self.event_queue.put(("done", written, dry))
            elif kind == "error":
                errored = True
                message = event.get("message", "Unknown error")
                self.event_queue.put(("error", message))

        assert process.stdout is not None
        stdout_thread = threading.Thread(
            target=self._read_worker_stdout,
            args=(process.stdout, handle_event),
            daemon=True,
        )
        stdout_thread.start()

        assert process.stderr is not None
        stderr_thread = threading.Thread(
            target=self._read_worker_stderr,
            args=(process.stderr,),
            daemon=True,
        )
        stderr_thread.start()

        stdout_thread.join()
        return_code = process.wait()
        stderr_thread.join(timeout=0.1)

        if not done and not errored and return_code != 0:
            self.event_queue.put(("error", f"Worker exited with code {return_code}"))

    def _read_worker_stdout(self, stream: TextIO, handler: Callable[[dict], None]) -> None:
        for raw_line in stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.event_queue.put(("log", f"[worker] {line}"))
                continue
            handler(event)
        stream.close()

    def _read_worker_stderr(self, stream: TextIO) -> None:
        for raw_line in stream:
            line = raw_line.rstrip()
            if line:
                self.event_queue.put(("log", f"[worker] {line}"))
        stream.close()

    def _build_worker_command(
        self,
        *,
        image_path: str,
        device_path: str,
        verify: bool,
        dry_run: bool,
    ) -> tuple[Optional[list[str]], Optional[str]]:
        python_executable = sys.executable or "python3"
        command: list[str] = []

        needs_privileges = hasattr(os, "geteuid") and os.geteuid() != 0 and not dry_run
        if needs_privileges:
            pkexec_path = shutil.which("pkexec")
            if not pkexec_path:
                return None, "Root privileges required. Install polkit (pkexec) or run Shark Etcher with sudo."
            command.append(pkexec_path)

        entrypoint = str(self.project_root / "shark_etcher" / "__main__.py")

        command.extend(
            [
                python_executable,
                entrypoint,
                "--worker",
                "--image",
                image_path,
                "--device",
                device_path,
                "--chunk-size",
                str(self.chunk_size),
            ]
        )

        if verify:
            command.append("--verify")
        if dry_run:
            command.append("--dry-run")

        return command, None

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.event_queue.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._handle_progress(event[1], event[2])
                elif kind == "verify":
                    self._handle_verify(event[1], event[2])
                elif kind == "status":
                    message = event[1]
                    self.status_text.set(message)
                    self.log(message)
                elif kind == "done":
                    written, dry_run = event[1], event[2]
                    self._flash_completed(written, dry_run)
                elif kind == "error":
                    self._flash_failed(event[1])
                elif kind == "log":
                    self.log(event[1])
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_events)

    def _handle_progress(self, written: int, total: Optional[int]) -> None:
        self.progress_total = total
        if total is not None and total > 0:
            percent = min(100.0, (written / total) * 100.0)
            if str(self.progress_bar["mode"]) != "determinate":
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate")
            self.progress_bar.configure(value=percent)
            self.status_text.set(f"Writing: {percent:.1f}%")
        else:
            if str(self.progress_bar["mode"]) != "indeterminate":
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start(60)
            self.status_text.set(f"Written {written} bytes")

    def _handle_verify(self, checked: int, total: Optional[int]) -> None:
        if str(self.progress_bar["mode"]) != "determinate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
        if total is not None and total > 0:
            percent = min(100.0, (checked / total) * 100.0)
            self.progress_bar.configure(value=percent)
            self.status_text.set(f"Verifying: {percent:.1f}%")
        else:
            self.status_text.set(f"Verifying: {checked} bytes")

    def _flash_completed(self, written: int, dry_run: bool) -> None:
        self._set_busy(False)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=100)
        if dry_run:
            message = "Dry run completed; no data was written."
        else:
            message = f"Write completed ({_format_size(written)})."
        self.status_text.set(message)
        self.log(message)
        messagebox.showinfo("Completed", message)
        self.after(500, self.refresh_devices)

    def _flash_failed(self, error_message: str) -> None:
        self._set_busy(False)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.status_text.set("Write failed")
        self.log(f"Error: {error_message}")
        messagebox.showerror("Error", error_message)

    def _set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in self._busy_widgets:
            if isinstance(widget, ttk.Treeview):
                if busy:
                    widget.state(["disabled"])
                else:
                    widget.state(["!disabled"])
                continue
            try:
                widget.configure(state=state)
            except tk.TclError:
                # Some widgets (e.g. custom ones) may not expose a configurable state
                continue
        if busy:
            self.progress_bar.configure(mode="determinate", value=0)
        else:
            self._update_flash_button_state()

    def _reset_progress_bar(self) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)

    def _update_flash_button_state(self) -> None:
        image_selected = bool(self.image_path.get().strip())
        device_selected = self.selected_device is not None
        if self.flash_thread and self.flash_thread.is_alive():
            state = tk.DISABLED
        elif image_selected and device_selected:
            state = tk.NORMAL
        else:
            state = tk.DISABLED
        self.flash_btn.configure(state=state)

    def destroy(self) -> None:
        if self.flash_thread and self.flash_thread.is_alive():
            messagebox.showwarning(
                "Write in progress",
                "Wait for the write operation to finish before closing.",
            )
            return
        super().destroy()


def _format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024.0:
            break
        value /= 1024.0
    return f"{value:.1f} {unit}"


def run_gui() -> None:
    app = EtcherApp()
    app.mainloop()
