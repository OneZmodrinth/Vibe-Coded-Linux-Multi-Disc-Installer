#!/usr/bin/env python3
"""
proton-disc-gui.py — Qt6 (PySide6) front end for managing multi-disc
game installs under Proton, one profile per game.

Shares the exact same config format as proton-disc-tool.sh
(~/.config/proton-disc-tool/<name>.conf), so you can mix and match the
CLI and this GUI on the same machine without conflicts.

Install deps:  pip install PySide6   (or: pacman -S pyside6)
Run:           python3 proton_disc_gui.py
"""

import os
import re
import sys
import time
import shutil
import zlib
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
    QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QFileDialog, QDialog, QDialogButtonBox, QRadioButton,
    QButtonGroup, QPlainTextEdit, QSplitter, QMessageBox, QGroupBox,
)

CONFIG_ROOT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "proton-disc-tool"
MOUNT_BASE = Path(f"/run/user/{os.getuid()}/proton-disc-tool")

PROTON_SCAN_DIRS = [
    Path.home() / ".local/share/Steam/steamapps/common",
    Path.home() / ".steam/steam/steamapps/common",
    Path.home() / ".local/share/Steam/compatibilitytools.d",
    Path.home() / ".steam/root/compatibilitytools.d",
    Path.home() / ".steam/steam/compatibilitytools.d",
]
STEAM_CLIENT_DIRS = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/root",
]

CONF_KEYS = ["PROTON", "PREFIX", "LETTER", "SRCTYPE", "SRC", "INSTALLER_EXE", "GAME_EXE"]

COMPATDATA_ALIAS_BASE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "proton-disc-tool" / "compatdata"


def compat_data_path(prefix: Path, log) -> Path:
    """
    GE-Proton's protonfixes hook (protonfixes/fix.py: get_game_id) does
    `re.findall(r'\\d+', os.environ['STEAM_COMPAT_DATA_PATH'])[-1]` to guess
    the Steam AppID, and crashes with an IndexError if the path has no
    digits in it at all — which a hand-picked prefix folder usually won't.
    Real Steam games always land in .../compatdata/<numeric appid>/.
    We fake that up: alias the real prefix behind a numeric-looking
    symlink and point STEAM_COMPAT_DATA_PATH at the alias instead. The
    actual prefix contents (pfx/, dosdevices/, drive_c/...) still live
    exactly where you put them; this is just a name Proton is happier with.
    """
    COMPATDATA_ALIAS_BASE.mkdir(parents=True, exist_ok=True)
    real = prefix.resolve()
    fake_appid = str(1000000 + (zlib.crc32(str(real).encode()) % 9000000))
    link = COMPATDATA_ALIAS_BASE / fake_appid

    if link.is_symlink():
        if Path(os.readlink(link)) != real:
            log(f"compat-data alias {link} pointed somewhere else, refreshing it")
            link.unlink()
    elif link.exists():
        raise RuntimeError(f"{link} exists and isn't a symlink — remove it manually")

    if not link.exists():
        link.symlink_to(real, target_is_directory=True)

    log(f"using compat-data alias {link} -> {real} (works around a GE-Proton protonfixes crash "
        f"on prefix paths with no digits in them)")
    return link


# ---------------------------------------------------------------- config --

def parse_conf(path: Path) -> dict:
    data = {k: "" for k in CONF_KEYS}
    for line in path.read_text().splitlines():
        m = re.match(r'^([A-Z_]+)="(.*)"$', line)
        if m:
            data[m.group(1)] = m.group(2)
    return data


def write_conf(path: Path, data: dict) -> None:
    ordered = {k: data.get(k, "") for k in CONF_KEYS}
    path.write_text("".join(f'{k}="{v}"\n' for k, v in ordered.items()))


def list_games() -> list[str]:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in CONFIG_ROOT.glob("*.conf"))


# --------------------------------------------------------------- discovery --

def find_proton_installs() -> list[Path]:
    found = []
    for d in PROTON_SCAN_DIRS:
        if not d.is_dir():
            continue
        for sub in d.iterdir():
            if sub.is_dir() and (sub / "proton").is_file() and os.access(sub / "proton", os.X_OK):
                found.append(sub)
    return sorted(set(found))


def list_optical_drives() -> list[str]:
    try:
        out = subprocess.run(["lsblk", "-dpno", "NAME,TYPE,MODEL"],
                              capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return []
    drives = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[1] == "rom":
            drives.append(line.strip())
    return drives


def steam_client_install_path() -> str:
    for d in STEAM_CLIENT_DIRS:
        if d.is_dir():
            return str(d)
    raise RuntimeError("could not find a Steam install")


def find_mountpoint(dev: str) -> str | None:
    """Where a block device is currently mounted on the LINUX side, if at all."""
    r = subprocess.run(["findmnt", "-no", "TARGET", dev], capture_output=True, text=True, check=False)
    target = r.stdout.strip()
    return target or None


# ------------------------------------------------------------- core logic --
# Wine treats a dosdevices symlink named "x::" (trailing double colon) as
# a raw unix device (a real optical drive, auto-detected). A plain "x:"
# symlink points at a mounted directory (used for disc images). Either
# way, the drive LETTER itself never moves when the disc behind it
# changes — that's what stops installers from losing track of discs.
#
# The other half of the fix: whatever exe you launch has to be addressed
# by that drive letter too (e.g. D:\SETUP.EXE), not by its raw Linux
# path. Wine falls back to mapping arbitrary Linux paths through its
# built-in Z: drive, and a game running from Z: no longer looks like
# it's running from the disc at all — that's exactly what breaks
# "insert disc 2" checks. to_windows_path() below does that translation.

def cmdline(cmd: list[str]) -> str:
    return " ".join(cmd)


def init_prefix(proton: Path, prefix: Path, log) -> None:
    pfx = prefix / "pfx"
    if not (pfx / "drive_c").is_dir():
        log(f"prefix at {prefix} looks fresh (no drive_c yet) — booting it once so wine lays out dosdevices")
        env = dict(os.environ,
                    STEAM_COMPAT_CLIENT_INSTALL_PATH=steam_client_install_path(),
                    STEAM_COMPAT_DATA_PATH=str(compat_data_path(prefix, log)))
        cmd = [str(proton / "proton"), "run", "wineboot"]
        log(f"  running: {cmdline(cmd)}")
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
        log(f"  wineboot exited {r.returncode}" + (f" — stderr: {r.stderr.strip()[:400]}" if r.returncode else ""))
    else:
        log(f"prefix at {prefix} already initialized, skipping wineboot")
    (pfx / "dosdevices").mkdir(parents=True, exist_ok=True)


def link_directory(prefix: Path, letter: str, directory: Path, log) -> None:
    """Point the wine drive letter at a real, already-accessible directory.
    This is deliberately used for BOTH images and physical discs: a plain
    directory symlink only needs normal file permissions, whereas the
    raw-device ('x::') trick needs low-level SCSI/ioctl access to
    /dev/srX that a regular user usually doesn't have — Wine fails to
    open the drive at all and the whole launch just silently dies."""
    dd = prefix / "pfx" / "dosdevices"
    for suffix in (":", "::"):
        p = dd / f"{letter}{suffix}"
        if p.exists() or p.is_symlink():
            log(f"removing existing link {p}")
        p.unlink(missing_ok=True)
    (dd / f"{letter}:").symlink_to(directory)
    log(f"linked {letter}: -> {directory}")


def mount_ro(source: str, mountpoint: Path, log, loop: bool) -> None:
    mountpoint.mkdir(parents=True, exist_ok=True)
    log(f"unmounting anything currently at {mountpoint} (ignore errors if nothing was mounted)")
    subprocess.run(["umount", str(mountpoint)], capture_output=True, check=False)

    opts = "loop,ro" if loop else "ro"
    cmd = ["mount", "-o", opts, source, str(mountpoint)]
    log(f"mounting: {cmdline(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        log(f"plain mount failed ({r.stderr.strip()}), retrying with pkexec (you'll get an auth prompt)")
        cmd_pk = ["pkexec"] + cmd
        log(f"  running: {cmdline(cmd_pk)}")
        r = subprocess.run(cmd_pk, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"could not mount {source}: {r.stderr.strip()}")
    log(f"mounted {source} at {mountpoint}")


def link_device(prefix: Path, letter: str, dev: str, log) -> None:
    mp = find_mountpoint(dev)
    if mp:
        log(f"{dev} is already mounted by the OS at {mp} — using that instead of touching the raw device")
        link_directory(prefix, letter, Path(mp), log)
        return
    log(f"{dev} isn't mounted anywhere yet — mounting it ourselves, read-only")
    mountpoint = MOUNT_BASE / letter
    mount_ro(dev, mountpoint, log, loop=False)
    link_directory(prefix, letter, mountpoint, log)


def link_image(prefix: Path, letter: str, img: str, log) -> None:
    mountpoint = MOUNT_BASE / letter
    mount_ro(img, mountpoint, log, loop=True)
    link_directory(prefix, letter, mountpoint, log)


def do_setup(cfg: dict, log) -> None:
    log(f"=== setup: proton={cfg['PROTON']} prefix={cfg['PREFIX']} letter={cfg['LETTER']}: "
        f"source={cfg['SRCTYPE']}:{cfg['SRC']} ===")
    proton = Path(cfg["PROTON"])
    prefix = Path(cfg["PREFIX"])
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "pfx").mkdir(parents=True, exist_ok=True)
    init_prefix(proton, prefix, log)
    if cfg["SRCTYPE"] == "device":
        link_device(prefix, cfg["LETTER"], cfg["SRC"], log)
    else:
        link_image(prefix, cfg["LETTER"], cfg["SRC"], log)
    log("=== setup complete ===")


def do_swap_image(cfg: dict, new_image: str, log) -> None:
    log(f"=== swapping disc image to {new_image} ===")
    prefix = Path(cfg["PREFIX"])
    link_image(prefix, cfg["LETTER"], new_image, log)
    log("=== swap complete ===")


def do_swap_device(cfg: dict, log) -> None:
    log(f"=== refreshing drive mapping for {cfg['SRC']} (pick up whatever disc is in the drive now) ===")
    prefix = Path(cfg["PREFIX"])
    link_device(prefix, cfg["LETTER"], cfg["SRC"], log)
    log("=== refresh complete ===")


def disc_root_for(cfg: dict, log=lambda *_: None) -> str | None:
    """The Linux-side directory that corresponds to the game's disc drive letter, if known."""
    if cfg["SRCTYPE"] == "image":
        root = MOUNT_BASE / cfg["LETTER"]
        return str(root) if root.is_dir() else None
    mp = find_mountpoint(cfg["SRC"])
    if not mp:
        log(f"note: {cfg['SRC']} doesn't look mounted on the Linux side right now "
            f"(only wine's raw-device link exists) — mount it in your file manager first if you need to browse it")
    return mp


def to_windows_path(cfg: dict, linux_path: str, log) -> str:
    """
    Convert an absolute Linux path into the matching wine drive-letter path
    (C:\\... for stuff inside the prefix, <LETTER>:\\... for stuff on the
    disc), instead of letting wine fall back to its generic Z: mapping.
    """
    linux_path = str(Path(linux_path).resolve())
    letter = cfg["LETTER"].upper()

    candidates: list[tuple[str, str]] = []
    drive_c = str((Path(cfg["PREFIX"]) / "pfx" / "drive_c").resolve())
    candidates.append(("C", drive_c))
    disc_root = disc_root_for(cfg, log)
    if disc_root:
        candidates.append((letter, str(Path(disc_root).resolve())))

    log("resolving windows path for: " + linux_path)
    for drv, root in candidates:
        log(f"  checking against {drv}: -> {root}")
        if linux_path == root or linux_path.startswith(root.rstrip("/") + "/"):
            rel = os.path.relpath(linux_path, root)
            win = f"{drv}:\\" + rel.replace("/", "\\")
            log(f"  MATCH: {drv}: root covers this path -> using {win}")
            return win

    win = f"Z:{linux_path}".replace("/", "\\")
    log(f"  no drive root matched this path — falling back to {win} (default Z: mapping). "
        f"if this is on the disc, disc-swap detection in the installer will likely think it's on the wrong drive.")
    return win


def do_run(cfg: dict, exe_linux_path: str, kind: str, log) -> None:
    log(f"=== running {kind}: {exe_linux_path} ===")
    proton = Path(cfg["PROTON"])
    prefix = cfg["PREFIX"]

    win_path = to_windows_path(cfg, exe_linux_path, log)

    env = dict(os.environ,
                STEAM_COMPAT_CLIENT_INSTALL_PATH=steam_client_install_path(),
                STEAM_COMPAT_DATA_PATH=str(compat_data_path(Path(prefix), log)))
    log(f"env STEAM_COMPAT_DATA_PATH={env['STEAM_COMPAT_DATA_PATH']} (real prefix: {prefix})")
    log(f"env STEAM_COMPAT_CLIENT_INSTALL_PATH={env['STEAM_COMPAT_CLIENT_INSTALL_PATH']}")

    # "waitforexitandrun" is the verb Steam itself uses to launch games and
    # has existed since ancient Proton versions — use it unconditionally.
    # (a previous version of this tool tried to auto-detect "run" vs
    # "waitforexitandrun" by calling `proton run --help`, but that actually
    # tries to launch a program literally named "--help" through wine,
    # always fails, and silently forced the wrong branch every time.)
    runner = "waitforexitandrun"
    log(f"using proton subcommand: {runner}")

    log_dir = Path(prefix) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{kind}-{datetime.now():%Y%m%d-%H%M%S}.log"

    cmd = [str(proton / "proton"), runner, win_path]
    log(f"launching: {cmdline(cmd)}")
    log(f"process stdout/stderr redirected to: {log_file}")
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT)

    log(f"{kind} process started, pid {proc.pid} — watching for an immediate crash...")
    time.sleep(2.5)
    ret = proc.poll()
    if ret is not None:
        log(f"WARNING: {kind} process already exited (code {ret}) almost immediately — it probably crashed on launch.")
        log("---- last lines of its log ----")
        try:
            lines = log_file.read_text(errors="replace").splitlines()
            for line in lines[-40:]:
                log("    " + line)
        except OSError as e:
            log(f"    (could not read log file: {e})")
        log("---- end of log ----")
    else:
        log(f"{kind} still running after 2.5s (pid {proc.pid}) — looking good so far. "
            f"full output keeps going to {log_file}")


def do_open_explorer(cfg: dict, log) -> None:
    log("=== opening explorer at drive root ===")
    proton = Path(cfg["PROTON"])
    prefix = cfg["PREFIX"]
    env = dict(os.environ,
                STEAM_COMPAT_CLIENT_INSTALL_PATH=steam_client_install_path(),
                STEAM_COMPAT_DATA_PATH=str(compat_data_path(Path(prefix), log)))
    letter = cfg["LETTER"].upper()
    cmd = [str(proton / "proton"), "waitforexitandrun", "explorer.exe", f"{letter}:\\"]
    log(f"launching: {cmdline(cmd)}")
    subprocess.Popen(cmd, env=env)


# ---------------------------------------------------------------- worker --

class Worker(QThread):
    log_line = Signal(str)
    done = Signal(bool, str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args

    def run(self):
        try:
            self.fn(*self.args, self.log_line.emit)
            self.done.emit(True, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, str(e))


# ------------------------------------------------------------------ UI ----

class NewGameDialog(QDialog):
    def __init__(self, parent=None, existing: dict | None = None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Game profile")
        self.setMinimumWidth(480)

        self.name_edit = QLineEdit(name)
        self.name_edit.setEnabled(not existing)

        self.proton_combo = QComboBox()
        self.protons = find_proton_installs()
        for p in self.protons:
            self.proton_combo.addItem(p.name, str(p))
        if not self.protons:
            self.proton_combo.addItem("no Proton builds found", "")

        self.prefix_edit = QLineEdit(str(Path.home() / ".proton-disc-prefixes" / (name or "game")))
        browse_prefix = QPushButton("Browse…")
        browse_prefix.clicked.connect(self.browse_prefix)

        self.letter_edit = QLineEdit("d")
        self.letter_edit.setMaxLength(1)

        self.radio_device = QRadioButton("Physical optical drive")
        self.radio_image = QRadioButton("Disc image file (.iso/.bin/.img)")
        self.radio_image.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.radio_device)
        group.addButton(self.radio_image)
        self.radio_device.toggled.connect(self.update_source_mode)

        self.device_combo = QComboBox()
        self.device_combo.addItems(list_optical_drives() or ["no optical drives detected"])
        self.device_combo.setEditable(True)

        self.image_edit = QLineEdit()
        browse_image = QPushButton("Browse…")
        browse_image.clicked.connect(self.browse_image)

        if existing:
            self.prefix_edit.setText(existing.get("PREFIX", self.prefix_edit.text()))
            self.letter_edit.setText(existing.get("LETTER", "d"))
            idx = self.proton_combo.findData(existing.get("PROTON", ""))
            if idx >= 0:
                self.proton_combo.setCurrentIndex(idx)
            if existing.get("SRCTYPE") == "device":
                self.radio_device.setChecked(True)
                self.device_combo.setCurrentText(existing.get("SRC", ""))
            else:
                self.radio_image.setChecked(True)
                self.image_edit.setText(existing.get("SRC", ""))

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Proton build", self.proton_combo)
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(self.prefix_edit)
        prefix_row.addWidget(browse_prefix)
        form.addRow("Prefix folder", prefix_row)
        form.addRow("Drive letter", self.letter_edit)

        source_box = QGroupBox("Disc source")
        source_layout = QVBoxLayout()
        source_layout.addWidget(self.radio_image)
        img_row = QHBoxLayout()
        img_row.addWidget(self.image_edit)
        img_row.addWidget(browse_image)
        source_layout.addLayout(img_row)
        source_layout.addWidget(self.radio_device)
        source_layout.addWidget(self.device_combo)
        source_box.setLayout(source_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(source_box)
        layout.addWidget(buttons)
        self.update_source_mode()

    def update_source_mode(self):
        self.device_combo.setEnabled(self.radio_device.isChecked())
        self.image_edit.setEnabled(self.radio_image.isChecked())

    def browse_prefix(self):
        d = QFileDialog.getExistingDirectory(self, "Choose prefix folder", self.prefix_edit.text())
        if d:
            self.prefix_edit.setText(d)

    def browse_image(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose disc image", "",
                                            "Disc images (*.iso *.bin *.img *.mdf *.nrg);;All files (*)")
        if f:
            self.image_edit.setText(f)

    def result_config(self) -> dict:
        proton = self.proton_combo.currentData()
        srctype = "device" if self.radio_device.isChecked() else "image"
        src = self.device_combo.currentText().split()[0] if srctype == "device" else self.image_edit.text()
        return {
            "PROTON": proton or "",
            "PREFIX": self.prefix_edit.text().strip(),
            "LETTER": (self.letter_edit.text().strip() or "d").lower(),
            "SRCTYPE": srctype,
            "SRC": src.strip(),
        }

    def game_name(self) -> str:
        return self.name_edit.text().strip()


class SwapDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Swap disc")
        self.edit = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.edit)
        row.addWidget(browse)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Path to the next disc image:"))
        layout.addLayout(row)
        layout.addWidget(buttons)

    def browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose disc image", "",
                                            "Disc images (*.iso *.bin *.img *.mdf *.nrg);;All files (*)")
        if f:
            self.edit.setText(f)

    def image_path(self) -> str:
        return self.edit.text().strip()


class ExeSlot(QGroupBox):
    """One 'pick + run' unit, used twice: once for the installer, once for the installed game."""

    def __init__(self, title: str, pick_cb, run_cb):
        super().__init__(title)
        self.path_label = QLabel("(not set)")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pick_btn = QPushButton("Choose exe…")
        pick_btn.clicked.connect(pick_cb)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(run_cb)
        self.run_btn.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(pick_btn)
        row.addWidget(self.run_btn)
        layout = QVBoxLayout()
        layout.addWidget(self.path_label)
        layout.addLayout(row)
        self.setLayout(layout)

    def set_path(self, path: str):
        self.path_label.setText(path if path else "(not set)")
        self.run_btn.setEnabled(bool(path))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Proton Disc Tool")
        self.resize(940, 640)
        self.current_worker: Worker | None = None

        self.game_list = QListWidget()
        self.game_list.currentTextChanged.connect(self.on_select)

        new_btn = QPushButton("New game…")
        new_btn.clicked.connect(self.on_new_game)
        edit_btn = QPushButton("Edit / re-run setup")
        edit_btn.clicked.connect(self.on_edit_game)
        del_btn = QPushButton("Remove profile")
        del_btn.clicked.connect(self.on_delete_game)

        left = QVBoxLayout()
        left.addWidget(QLabel("Games"))
        left.addWidget(self.game_list)
        left.addWidget(new_btn)
        left.addWidget(edit_btn)
        left.addWidget(del_btn)
        left_widget = QWidget()
        left_widget.setLayout(left)

        self.detail_label = QLabel("Select or create a game profile.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.installer_slot = ExeSlot("Disc installer", self.on_pick_installer, self.on_run_installer)
        self.game_slot = ExeSlot("Installed game", self.on_pick_game, self.on_run_game)
        slots_row = QHBoxLayout()
        slots_row.addWidget(self.installer_slot)
        slots_row.addWidget(self.game_slot)

        explorer_btn = QPushButton("Open explorer at drive")
        explorer_btn.clicked.connect(self.on_open_explorer)
        swap_btn = QPushButton("Swap disc…")
        swap_btn.clicked.connect(self.on_swap)
        open_folder_btn = QPushButton("Open prefix folder")
        open_folder_btn.clicked.connect(self.on_open_folder)
        misc_row = QHBoxLayout()
        for b in (explorer_btn, swap_btn, open_folder_btn):
            misc_row.addWidget(b)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)

        right = QVBoxLayout()
        right.addWidget(self.detail_label)
        right.addLayout(slots_row)
        right.addLayout(misc_row)
        right.addWidget(QLabel("Log"))
        right.addWidget(self.log)
        right_widget = QWidget()
        right_widget.setLayout(right)

        splitter = QSplitter()
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self.refresh_games()

    # ---- data ----

    def refresh_games(self, select: str | None = None):
        self.game_list.clear()
        for name in list_games():
            self.game_list.addItem(QListWidgetItem(name))
        if select:
            items = self.game_list.findItems(select, Qt.MatchExactly)
            if items:
                self.game_list.setCurrentItem(items[0])

    def current_name(self) -> str | None:
        item = self.game_list.currentItem()
        return item.text() if item else None

    def current_cfg(self) -> dict | None:
        name = self.current_name()
        if not name:
            return None
        return parse_conf(CONFIG_ROOT / f"{name}.conf")

    def save_current_cfg(self, cfg: dict):
        name = self.current_name()
        if name:
            write_conf(CONFIG_ROOT / f"{name}.conf", cfg)

    def on_select(self, name: str):
        if not name:
            self.detail_label.setText("Select or create a game profile.")
            self.installer_slot.set_path("")
            self.game_slot.set_path("")
            return
        cfg = parse_conf(CONFIG_ROOT / f"{name}.conf")
        self.detail_label.setText(
            f"<b>{name}</b><br>"
            f"Proton: {cfg.get('PROTON', '?')}<br>"
            f"Prefix: {cfg.get('PREFIX', '?')}<br>"
            f"Drive: {cfg.get('LETTER', '?')}:<br>"
            f"Source ({cfg.get('SRCTYPE', '?')}): {cfg.get('SRC', '?')}"
        )
        self.installer_slot.set_path(cfg.get("INSTALLER_EXE", ""))
        self.game_slot.set_path(cfg.get("GAME_EXE", ""))

    def append_log(self, line: str):
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {line}")

    def run_worker(self, fn, *args, on_success=None):
        self.current_worker = Worker(fn, *args)
        self.current_worker.log_line.connect(self.append_log)

        def finished(ok, msg):
            if ok:
                self.append_log("-- finished OK --")
                if on_success:
                    on_success()
            else:
                self.append_log(f"-- FAILED: {msg} --")
                QMessageBox.warning(self, "Error", msg)

        self.current_worker.done.connect(finished)
        self.current_worker.start()

    # ---- profile actions ----

    def on_new_game(self):
        dlg = NewGameDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        name = dlg.game_name()
        if not name:
            QMessageBox.warning(self, "Missing name", "Give this profile a name.")
            return
        cfg = dlg.result_config()
        if not cfg["PROTON"]:
            QMessageBox.warning(self, "No Proton", "No Proton build selected/found.")
            return
        write_conf(CONFIG_ROOT / f"{name}.conf", cfg)
        self.append_log(f"running setup for '{name}'...")
        self.run_worker(do_setup, cfg, on_success=lambda: self.refresh_games(select=name))
        self.refresh_games(select=name)

    def on_edit_game(self):
        name = self.current_name()
        if not name:
            return
        existing = parse_conf(CONFIG_ROOT / f"{name}.conf")
        dlg = NewGameDialog(self, existing=existing, name=name)
        if dlg.exec() != QDialog.Accepted:
            return
        cfg = dlg.result_config()
        # keep the exe slots, only the source/proton/prefix/letter are edited here
        cfg["INSTALLER_EXE"] = existing.get("INSTALLER_EXE", "")
        cfg["GAME_EXE"] = existing.get("GAME_EXE", "")
        write_conf(CONFIG_ROOT / f"{name}.conf", cfg)
        self.append_log(f"re-running setup for '{name}'...")
        self.run_worker(do_setup, cfg, on_success=lambda: self.on_select(name))

    def on_delete_game(self):
        name = self.current_name()
        if not name:
            return
        if QMessageBox.question(self, "Remove profile",
                                 f"Remove the profile '{name}'? (prefix files are kept on disk)") != QMessageBox.Yes:
            return
        (CONFIG_ROOT / f"{name}.conf").unlink(missing_ok=True)
        self.refresh_games()

    # ---- installer / game exe slots ----

    def on_pick_installer(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        start_dir = disc_root_for(cfg, self.append_log) or cfg.get("PREFIX", "")
        f, _ = QFileDialog.getOpenFileName(self, "Choose the disc installer .exe", start_dir,
                                            "Executables (*.exe *.EXE);;All files (*)")
        if not f:
            return
        cfg["INSTALLER_EXE"] = f
        self.save_current_cfg(cfg)
        self.installer_slot.set_path(f)
        self.append_log(f"installer exe set to: {f}")

    def on_run_installer(self):
        cfg = self.current_cfg()
        if not cfg or not cfg.get("INSTALLER_EXE"):
            return
        self.run_worker(do_run, cfg, cfg["INSTALLER_EXE"], "installer")

    def on_pick_game(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        drive_c = Path(cfg["PREFIX"]) / "pfx" / "drive_c"
        prog_files = drive_c / "Program Files (x86)"
        start_dir = str(prog_files if prog_files.is_dir() else drive_c)
        f, _ = QFileDialog.getOpenFileName(self, "Choose the installed game's .exe", start_dir,
                                            "Executables (*.exe *.EXE);;All files (*)")
        if not f:
            return
        cfg["GAME_EXE"] = f
        self.save_current_cfg(cfg)
        self.game_slot.set_path(f)
        self.append_log(f"game exe set to: {f}")

    def on_run_game(self):
        cfg = self.current_cfg()
        if not cfg or not cfg.get("GAME_EXE"):
            return
        self.run_worker(do_run, cfg, cfg["GAME_EXE"], "game")

    # ---- disc actions ----

    def on_open_explorer(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        self.run_worker(do_open_explorer, cfg)

    def on_swap(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        if cfg["SRCTYPE"] != "image":
            if QMessageBox.question(
                self, "Refresh drive mapping",
                "Make sure the next disc is already inserted, then I'll refresh the drive "
                "letter to point at wherever the OS mounts it. Continue?"
            ) != QMessageBox.Yes:
                return
            self.run_worker(do_swap_device, cfg)
            return

        dlg = SwapDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        img = dlg.image_path()
        if not img:
            return
        name = self.current_name()

        def on_success():
            cfg["SRC"] = img
            write_conf(CONFIG_ROOT / f"{name}.conf", cfg)
            self.on_select(name)

        self.run_worker(do_swap_image, cfg, img, on_success=on_success)

    def on_open_folder(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        path = cfg.get("PREFIX", "")
        opener = "xdg-open" if shutil.which("xdg-open") else None
        if opener and path:
            subprocess.Popen([opener, path])
        else:
            QMessageBox.information(self, "Prefix path", path)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
