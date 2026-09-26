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
import shlex
import shutil
import zlib
import base64
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap
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

CONF_KEYS = ["PROTON", "PREFIX", "LETTER", "SRCTYPE", "SRC", "INSTALLER_EXE", "GAME_EXE", "LAUNCH_OPTIONS"]

STYLE_SHEET = """
QWidget {
    background-color: #1c1e26;
    color: #e4e6ef;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #1c1e26;
}
QListWidget {
    background-color: #14151b;
    border: 1px solid #2c2f3a;
    border-radius: 6px;
    padding: 4px;
}
QListWidget::item {
    padding: 6px 8px;
    border-radius: 4px;
}
QListWidget::item:selected {
    background-color: #3a6fb5;
    color: #ffffff;
}
QListWidget::item:hover:!selected {
    background-color: #262936;
}
QGroupBox {
    border: 1px solid #2c2f3a;
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #8fb2e8;
}
QPushButton {
    background-color: #2c3040;
    border: 1px solid #3a3f52;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #3a4257;
    border-color: #5a86c9;
}
QPushButton:pressed {
    background-color: #24283a;
}
QPushButton:disabled {
    color: #6a6d7a;
    background-color: #23252f;
}
QLineEdit, QComboBox {
    background-color: #14151b;
    border: 1px solid #2c2f3a;
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: #3a6fb5;
}
QLineEdit:focus, QComboBox:focus {
    border-color: #5a86c9;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QRadioButton {
    padding: 3px;
}
QLabel {
    color: #cfd2e0;
}
QSplitter::handle {
    background-color: #2c2f3a;
    width: 3px;
}
QPlainTextEdit#logView {
    background-color: #101116;
    border: 1px solid #2c2f3a;
    border-radius: 6px;
    font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    color: #9dd1a0;
    padding: 6px;
}
QScrollBar:vertical {
    background: #1c1e26;
    width: 10px;
}
QScrollBar::handle:vertical {
    background: #3a3f52;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #5a86c9;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""

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


def apply_launch_options(cfg: dict, env: dict, cmd: list[str], log) -> tuple[dict, list[str]]:
    """
    Steam-style launch options: leading KEY=VALUE tokens become env vars,
    '%command%' marks where the real proton invocation goes (so you can
    wrap it, e.g. 'gamemoderun %command%'), and anything else just gets
    appended after it (e.g. '%command% -windowed', or no %command% at all
    which behaves the same as putting it at the front).
    """
    opts = (cfg.get("LAUNCH_OPTIONS") or "").strip()
    if not opts:
        return env, cmd

    log(f"applying launch options: {opts}")
    tokens = shlex.split(opts)
    i = 0
    while i < len(tokens) and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tokens[i]):
        k, v = tokens[i].split("=", 1)
        env[k] = v
        log(f"  launch option env: {k}={v}")
        i += 1
    remaining = tokens[i:]

    if "%command%" in remaining:
        idx = remaining.index("%command%")
        new_cmd = remaining[:idx] + cmd + remaining[idx + 1:]
    elif remaining:
        new_cmd = cmd + remaining
    else:
        new_cmd = cmd
    if new_cmd != cmd:
        log(f"  final command becomes: {cmdline(new_cmd)}")
    return env, new_cmd


def find_wine_binary(proton: Path) -> Path | None:
    for candidate in ("files/bin/wine64", "files/bin/wine", "dist/bin/wine64", "dist/bin/wine"):
        p = proton / candidate
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def do_open_winetricks(cfg: dict, log) -> None:
    log("=== opening winetricks ===")
    if not shutil.which("winetricks"):
        raise RuntimeError("winetricks isn't installed — install it first (e.g. `sudo pacman -S winetricks`)")

    proton = Path(cfg["PROTON"])
    wine_bin = find_wine_binary(proton)
    if not wine_bin:
        raise RuntimeError(f"couldn't find a wine binary inside {proton} — unexpected Proton layout")

    prefix_pfx = Path(cfg["PREFIX"]) / "pfx"
    env = dict(os.environ, WINEPREFIX=str(prefix_pfx), WINE=str(wine_bin))
    wineserver = wine_bin.parent / "wineserver"
    if wineserver.is_file():
        env["WINESERVER"] = str(wineserver)

    log(f"WINEPREFIX={env['WINEPREFIX']}")
    log(f"WINE={env['WINE']}")
    cmd = ["winetricks"]
    log(f"launching: {cmdline(cmd)}")
    subprocess.Popen(cmd, env=env)
    log("winetricks launched — its own window will open shortly")


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
    env, cmd = apply_launch_options(cfg, env, cmd, log)
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
        left.setSpacing(8)
        left.addWidget(QLabel("Games"))
        left.addWidget(self.game_list)
        left.addWidget(new_btn)
        left.addWidget(edit_btn)
        left.addWidget(del_btn)
        left_widget = QWidget()
        left.setContentsMargins(10, 10, 10, 10)
        left_widget.setLayout(left)

        self.detail_label = QLabel("Select or create a game profile.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.launch_options_edit = QLineEdit()
        self.launch_options_edit.setPlaceholderText("e.g. PROTON_LOG=1 %command% -windowed  (Steam-style, optional)")
        self.launch_options_edit.editingFinished.connect(self.on_launch_options_changed)
        launch_row = QHBoxLayout()
        launch_row.addWidget(QLabel("Launch options:"))
        launch_row.addWidget(self.launch_options_edit)

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

        winetricks_btn = QPushButton("Open winetricks")
        winetricks_btn.clicked.connect(self.on_open_winetricks)
        run_other_btn = QPushButton("Run other exe…")
        run_other_btn.clicked.connect(self.on_run_other)
        tools_row = QHBoxLayout()
        tools_row.addWidget(winetricks_btn)
        tools_row.addWidget(run_other_btn)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setObjectName("logView")

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self.detail_label)
        right.addLayout(launch_row)
        right.addLayout(slots_row)
        right.addLayout(misc_row)
        right.addLayout(tools_row)
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
            self.launch_options_edit.setText("")
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
        self.launch_options_edit.setText(cfg.get("LAUNCH_OPTIONS", ""))

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

    def on_launch_options_changed(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        cfg["LAUNCH_OPTIONS"] = self.launch_options_edit.text().strip()
        self.save_current_cfg(cfg)
        self.append_log(f"launch options set to: {cfg['LAUNCH_OPTIONS'] or '(none)'}")

    def on_open_winetricks(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        self.run_worker(do_open_winetricks, cfg)

    def on_run_other(self):
        cfg = self.current_cfg()
        if not cfg:
            return
        f, _ = QFileDialog.getOpenFileName(self, "Choose any .exe to run in this prefix", cfg.get("PREFIX", ""),
                                            "Executables (*.exe *.EXE);;All files (*)")
        if not f:
            return
        self.run_worker(do_run, cfg, f, "custom")

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


APP_ICON_B64 = (
    "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"
)


def load_app_icon() -> QIcon:
    pixmap = QPixmap()
    pixmap.loadFromData(base64.b64decode(APP_ICON_B64), "PNG")
    return QIcon(pixmap)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE_SHEET)
    icon = load_app_icon()
    app.setWindowIcon(icon)
    win = MainWindow()
    win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
