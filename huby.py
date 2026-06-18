#!/usr/bin/env python3
"""Interactive Linux USB topology viewer.

The app reads /sys/bus/usb/devices, builds a root-hub/port tree, and refreshes
it in a curses TUI. It does not need root privileges for normal viewing.
"""

from __future__ import annotations

import argparse
import curses
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Iterable


DEFAULT_SYSFS = Path("/sys/bus/usb/devices")
ROOT_RE = re.compile(r"^usb(?P<bus>\d+)$")
DEVICE_RE = re.compile(r"^(?P<bus>\d+)-(?P<ports>\d+(?:\.\d+)*)$")

USB_CLASS_NAMES = {
    "00": "Per-interface",
    "01": "Audio",
    "02": "Communications",
    "03": "HID",
    "05": "Physical",
    "06": "Still Imaging",
    "07": "Printer",
    "08": "Mass Storage",
    "09": "Hub",
    "0a": "CDC Data",
    "0b": "Smart Card",
    "0d": "Content Security",
    "0e": "Video",
    "0f": "Personal Healthcare",
    "dc": "Diagnostic",
    "e0": "Wireless",
    "ef": "Miscellaneous",
    "fe": "Application Specific",
    "ff": "Vendor Specific",
}


@dataclass(frozen=True)
class UsbInterface:
    name: str
    number: str | None
    interface: str | None
    class_code: str | None
    subclass: str | None
    protocol: str | None
    driver: str | None


@dataclass(frozen=True)
class UsbDevice:
    name: str
    path: Path
    parent_name: str | None
    bus: int
    port_path: tuple[int, ...] = ()
    product: str | None = None
    manufacturer: str | None = None
    serial: str | None = None
    id_vendor: str | None = None
    id_product: str | None = None
    speed: str | None = None
    version: str | None = None
    max_power: str | None = None
    maxchild: int = 0
    device_class: str | None = None
    device_subclass: str | None = None
    device_protocol: str | None = None
    busnum: str | None = None
    devnum: str | None = None
    devpath: str | None = None
    dev_node: str | None = None
    configuration: str | None = None
    authorized: str | None = None
    removable: str | None = None
    driver: str | None = None
    physical_location: str | None = None
    interfaces: tuple[UsbInterface, ...] = ()

    @property
    def is_root(self) -> bool:
        return self.name.startswith("usb")

    @property
    def is_hub(self) -> bool:
        return self.maxchild > 0 or normalized_code(self.device_class) == "09"

    @property
    def port(self) -> int | None:
        if not self.port_path:
            return None
        return self.port_path[-1]


@dataclass(frozen=True)
class Snapshot:
    devices: dict[str, UsbDevice]
    children: dict[str, list[str]]
    scanned_at: float
    errors: tuple[str, ...] = ()

    @property
    def roots(self) -> list[str]:
        return sorted(
            (name for name, dev in self.devices.items() if dev.parent_name is None),
            key=lambda name: self.devices[name].bus,
        )


@dataclass(frozen=True)
class ViewRow:
    kind: str
    depth: int
    text: str
    device_name: str | None = None
    parent_name: str | None = None
    port: int | None = None
    state: str = "normal"


@dataclass(frozen=True)
class UsbEvent:
    timestamp: float
    action: str
    name: str
    label: str
    location: str


@dataclass
class AppState:
    sysfs: Path
    interval: float
    show_empty_ports: bool
    snapshot: Snapshot = field(default_factory=lambda: Snapshot({}, {}, time.time()))
    rows: list[ViewRow] = field(default_factory=list)
    selected: int = 0
    top: int = 0
    events: Deque[UsbEvent] = field(default_factory=lambda: deque(maxlen=8))
    highlighted_until: dict[str, float] = field(default_factory=dict)
    last_scan: float = 0.0


def read_text(path: Path) -> str | None:
    try:
        value = path.read_text(errors="replace").strip()
    except (FileNotFoundError, PermissionError, IsADirectoryError, OSError):
        return None
    return value or None


def read_int(path: Path) -> int:
    value = read_text(path)
    if value is None:
        return 0
    try:
        return int(value, 10)
    except ValueError:
        return 0


def read_driver(path: Path) -> str | None:
    driver_path = path / "driver"
    try:
        if driver_path.exists():
            return Path(os.path.realpath(driver_path)).name
    except OSError:
        return None
    return None


def read_uevent(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    text = read_text(path / "uevent")
    if not text:
        return data
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            data[key] = value
    return data


def read_physical_location(path: Path) -> str | None:
    location = path / "physical_location"
    text = read_text(location)
    if text:
        return text
    if not location.is_dir():
        return None

    parts: list[str] = []
    try:
        children = sorted(location.iterdir(), key=lambda item: item.name)
    except OSError:
        return None

    for child in children:
        if child.is_dir():
            continue
        value = read_text(child)
        if value:
            parts.append(f"{child.name}={value}")
    return ", ".join(parts) if parts else None


def normalized_code(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().lower()
    if code.startswith("0x"):
        code = code[2:]
    if len(code) == 1:
        code = f"0{code}"
    return code


def class_label(code: str | None) -> str | None:
    normalized = normalized_code(code)
    if not normalized:
        return None
    name = USB_CLASS_NAMES.get(normalized)
    if name:
        return f"{name} ({normalized})"
    return normalized


def parse_device_name(name: str) -> tuple[int, tuple[int, ...], str | None] | None:
    root = ROOT_RE.match(name)
    if root:
        return int(root.group("bus")), (), None

    device = DEVICE_RE.match(name)
    if not device:
        return None

    bus = int(device.group("bus"))
    ports = tuple(int(part) for part in device.group("ports").split("."))
    if len(ports) == 1:
        parent = f"usb{bus}"
    else:
        parent = f"{bus}-" + ".".join(str(part) for part in ports[:-1])
    return bus, ports, parent


def sort_device_names(names: Iterable[str], devices: dict[str, UsbDevice]) -> list[str]:
    def key(name: str) -> tuple[int, tuple[int, ...], str]:
        dev = devices[name]
        return dev.bus, dev.port_path, dev.name

    return sorted(names, key=key)


def device_label(device: UsbDevice) -> str:
    product = device.product or "Unknown USB device"
    maker = device.manufacturer
    if maker and maker not in product:
        label = f"{maker} {product}"
    else:
        label = product

    ids = device_id(device)
    if ids:
        label = f"{label} ({ids})"
    return label


def device_id(device: UsbDevice) -> str | None:
    if device.id_vendor and device.id_product:
        return f"{device.id_vendor}:{device.id_product}"
    return None


def location_label(device: UsbDevice) -> str:
    parts: list[str] = []
    if device.busnum and device.devnum:
        parts.append(f"bus {device.busnum} device {device.devnum}")
    elif device.bus:
        parts.append(f"bus {device.bus}")

    if device.name:
        parts.append(f"port path {device.name}")
    if device.devpath:
        parts.append(f"devpath {device.devpath}")
    if device.physical_location:
        parts.append(f"physical {device.physical_location}")
    return ", ".join(parts)


def read_interface(path: Path) -> UsbInterface:
    return UsbInterface(
        name=path.name,
        number=read_text(path / "bInterfaceNumber"),
        interface=read_text(path / "interface"),
        class_code=read_text(path / "bInterfaceClass"),
        subclass=read_text(path / "bInterfaceSubClass"),
        protocol=read_text(path / "bInterfaceProtocol"),
        driver=read_driver(path),
    )


def read_interfaces(sysfs: Path, device_name: str) -> tuple[UsbInterface, ...]:
    interfaces: list[UsbInterface] = []
    try:
        candidates = sorted(sysfs.glob(f"{device_name}:*"), key=lambda path: path.name)
    except OSError:
        return ()
    for path in candidates:
        if path.is_dir():
            interfaces.append(read_interface(path))
    return tuple(interfaces)


def read_device(sysfs: Path, path: Path, bus: int, ports: tuple[int, ...], parent: str | None) -> UsbDevice:
    uevent = read_uevent(path)
    dev_name = uevent.get("DEVNAME")
    return UsbDevice(
        name=path.name,
        path=path,
        parent_name=parent,
        bus=bus,
        port_path=ports,
        product=read_text(path / "product"),
        manufacturer=read_text(path / "manufacturer"),
        serial=read_text(path / "serial"),
        id_vendor=read_text(path / "idVendor"),
        id_product=read_text(path / "idProduct"),
        speed=read_text(path / "speed"),
        version=read_text(path / "version"),
        max_power=read_text(path / "bMaxPower"),
        maxchild=read_int(path / "maxchild"),
        device_class=read_text(path / "bDeviceClass"),
        device_subclass=read_text(path / "bDeviceSubClass"),
        device_protocol=read_text(path / "bDeviceProtocol"),
        busnum=read_text(path / "busnum") or uevent.get("BUSNUM"),
        devnum=read_text(path / "devnum") or uevent.get("DEVNUM"),
        devpath=read_text(path / "devpath"),
        dev_node=f"/dev/{dev_name}" if dev_name else None,
        configuration=read_text(path / "configuration"),
        authorized=read_text(path / "authorized"),
        removable=read_text(path / "removable"),
        driver=read_driver(path),
        physical_location=read_physical_location(path),
        interfaces=read_interfaces(sysfs, path.name),
    )


def scan_usb(sysfs: Path = DEFAULT_SYSFS) -> Snapshot:
    errors: list[str] = []
    devices: dict[str, UsbDevice] = {}

    if not sysfs.exists():
        return Snapshot({}, {}, time.time(), (f"{sysfs} does not exist",))

    try:
        entries = list(sysfs.iterdir())
    except OSError as exc:
        return Snapshot({}, {}, time.time(), (f"Cannot read {sysfs}: {exc}",))

    for path in entries:
        if ":" in path.name or not path.is_dir():
            continue
        parsed = parse_device_name(path.name)
        if parsed is None:
            continue
        bus, ports, parent = parsed
        try:
            devices[path.name] = read_device(sysfs, path, bus, ports, parent)
        except OSError as exc:
            errors.append(f"Cannot read {path.name}: {exc}")

    children: dict[str, list[str]] = {name: [] for name in devices}
    for name, device in devices.items():
        if device.parent_name and device.parent_name in devices:
            children.setdefault(device.parent_name, []).append(name)
    for parent_name, child_names in list(children.items()):
        children[parent_name] = sort_device_names(child_names, devices)

    return Snapshot(devices, children, time.time(), tuple(errors))


def build_rows(snapshot: Snapshot, show_empty_ports: bool, highlighted: dict[str, float] | None = None) -> list[ViewRow]:
    highlighted = highlighted or {}
    rows: list[ViewRow] = []

    def state_for(name: str) -> str:
        return "added" if highlighted.get(name, 0.0) > time.monotonic() else "normal"

    def append_hub_ports(device: UsbDevice, depth: int) -> None:
        child_names = snapshot.children.get(device.name, [])
        children_by_port = {
            snapshot.devices[child_name].port: child_name
            for child_name in child_names
            if snapshot.devices[child_name].port is not None
        }

        if not device.is_hub:
            return

        if show_empty_ports and device.maxchild > 0:
            port_numbers = list(range(1, device.maxchild + 1))
        else:
            port_numbers = sorted(children_by_port)

        for port in port_numbers:
            child_name = children_by_port.get(port)
            if child_name:
                child = snapshot.devices[child_name]
                prefix = "port %s [plugged]" % port
                if child.is_hub:
                    prefix = f"{prefix} [hub]"
                rows.append(
                    ViewRow(
                        kind="device",
                        depth=depth,
                        text=f"{prefix} {device_label(child)}",
                        device_name=child.name,
                        parent_name=device.name,
                        port=port,
                        state=state_for(child.name),
                    )
                )
                append_hub_ports(child, depth + 1)
            elif show_empty_ports:
                rows.append(
                    ViewRow(
                        kind="empty",
                        depth=depth,
                        text=f"port {port} [empty]",
                        parent_name=device.name,
                        port=port,
                        state="empty",
                    )
                )

    for root_name in snapshot.roots:
        root = snapshot.devices[root_name]
        rows.append(
            ViewRow(
                kind="device",
                depth=0,
                text=f"[root hub] {root.name} {device_label(root)} ({root.maxchild} ports)",
                device_name=root.name,
                state=state_for(root.name),
            )
        )
        append_hub_ports(root, 1)

    return rows


def format_snapshot(snapshot: Snapshot, show_empty_ports: bool = True) -> str:
    rows = build_rows(snapshot, show_empty_ports)
    lines: list[str] = []
    for row in rows:
        lines.append(f"{'  ' * row.depth}{row.text}")
    if snapshot.errors:
        lines.append("")
        lines.extend(f"error: {error}" for error in snapshot.errors)
    return "\n".join(lines)


def addstr(win: curses.window, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        height, total_width = win.getmaxyx()
        if y >= height or x >= total_width:
            return
        max_width = min(width, total_width - x)
        safe = text.replace("\n", " ")
        win.addnstr(y, x, safe, max_width, attr)
    except curses.error:
        pass


def clear_line(win: curses.window, y: int, attr: int = 0) -> None:
    try:
        height, width = win.getmaxyx()
        if 0 <= y < height:
            win.move(y, 0)
            win.clrtoeol()
            if attr:
                addstr(win, y, 0, " " * width, width, attr)
    except curses.error:
        pass


def field_lines(device: UsbDevice) -> list[str]:
    lines = [
        f"Name: {device.name}",
        f"Status: plugged",
        f"Location: {location_label(device)}",
        f"Sysfs: {device.path}",
    ]
    if device.dev_node:
        lines.append(f"USB dev node: {device.dev_node}")

    info = [
        ("Product", device.product),
        ("Manufacturer", device.manufacturer),
        ("Serial", device.serial),
        ("USB ID", device_id(device)),
        ("Speed", f"{device.speed} Mbps" if device.speed else None),
        ("Power", device.max_power),
        ("USB version", device.version),
        ("Class", class_label(device.device_class)),
        ("Subclass", device.device_subclass),
        ("Protocol", device.device_protocol),
        ("Configuration", device.configuration),
        ("Authorized", device.authorized),
        ("Removable", device.removable),
        ("Driver", device.driver),
    ]
    lines.extend(f"{label}: {value}" for label, value in info if value)

    if device.interfaces:
        lines.append("")
        lines.append("Interfaces:")
        for item in device.interfaces:
            number = item.number or item.name.rpartition(":")[2]
            label = item.interface or class_label(item.class_code) or "interface"
            driver = f", driver {item.driver}" if item.driver else ""
            code = f", class {class_label(item.class_code)}" if item.class_code else ""
            lines.append(f"  {number}: {label}{code}{driver}")
    return lines


def empty_port_lines(snapshot: Snapshot, row: ViewRow) -> list[str]:
    parent = snapshot.devices.get(row.parent_name or "")
    if not parent:
        return ["Empty USB port", "Status: empty"]

    location = f"{parent.name} port {row.port}"
    if parent.busnum:
        location = f"bus {parent.busnum}, {location}"
    return [
        "Empty USB port",
        "Status: empty",
        f"Location: {location}",
        f"Parent hub: {device_label(parent)}",
        f"Parent sysfs: {parent.path}",
    ]


class UsbTui:
    def __init__(self, state: AppState) -> None:
        self.state = state

    def run(self, stdscr: curses.window) -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(5, curses.COLOR_RED, -1)
        curses.init_pair(6, curses.COLOR_CYAN, -1)
        stdscr.timeout(200)

        self.refresh_snapshot(force=True)
        while True:
            now = time.monotonic()
            if now - self.state.last_scan >= self.state.interval:
                self.refresh_snapshot()
            self.draw(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue
            if self.handle_key(key):
                break

    def refresh_snapshot(self, force: bool = False) -> None:
        old_snapshot = self.state.snapshot
        new_snapshot = scan_usb(self.state.sysfs)

        if not force:
            old_names = set(old_snapshot.devices)
            new_names = set(new_snapshot.devices)
            added = sorted(new_names - old_names)
            removed = sorted(old_names - new_names)
            now = time.monotonic()

            for name in added:
                device = new_snapshot.devices[name]
                self.state.events.appendleft(
                    UsbEvent(time.time(), "plugged", name, device_label(device), location_label(device))
                )
                self.state.highlighted_until[name] = now + 4.0
            for name in removed:
                device = old_snapshot.devices[name]
                self.state.events.appendleft(
                    UsbEvent(time.time(), "unplugged", name, device_label(device), location_label(device))
                )

        self.state.highlighted_until = {
            name: until for name, until in self.state.highlighted_until.items() if until > time.monotonic()
        }
        self.state.snapshot = new_snapshot
        self.state.rows = build_rows(new_snapshot, self.state.show_empty_ports, self.state.highlighted_until)
        self.state.last_scan = time.monotonic()
        if self.state.rows:
            self.state.selected = min(self.state.selected, len(self.state.rows) - 1)
        else:
            self.state.selected = 0

    def handle_key(self, key: int) -> bool:
        if key in (ord("q"), ord("Q"), 27):
            return True
        if key in (curses.KEY_UP, ord("k")):
            self.move_selection(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move_selection(1)
        elif key == curses.KEY_PPAGE:
            self.move_selection(-10)
        elif key == curses.KEY_NPAGE:
            self.move_selection(10)
        elif key == curses.KEY_HOME:
            self.state.selected = 0
        elif key == curses.KEY_END and self.state.rows:
            self.state.selected = len(self.state.rows) - 1
        elif key in (ord("r"), ord("R")):
            self.refresh_snapshot(force=True)
        elif key in (ord("e"), ord("E")):
            self.state.show_empty_ports = not self.state.show_empty_ports
            self.state.rows = build_rows(
                self.state.snapshot, self.state.show_empty_ports, self.state.highlighted_until
            )
            self.state.selected = min(self.state.selected, max(0, len(self.state.rows) - 1))
        return False

    def move_selection(self, delta: int) -> None:
        if not self.state.rows:
            return
        self.state.selected = max(0, min(len(self.state.rows) - 1, self.state.selected + delta))

    def draw(self, stdscr: curses.window) -> None:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        if height < 10 or width < 60:
            addstr(stdscr, 0, 0, "Terminal is too small. Resize to at least 60x10.", width, curses.A_BOLD)
            stdscr.refresh()
            return

        header_attr = curses.color_pair(1) | curses.A_BOLD
        addstr(stdscr, 0, 0, " USB Hub TUI ", width, header_attr)
        addstr(
            stdscr,
            1,
            0,
            self.status_line(),
            width,
            curses.A_DIM,
        )

        footer_y = height - 1
        events_y = max(3, height - 4)
        content_top = 3
        content_bottom = events_y - 1
        content_height = max(1, content_bottom - content_top + 1)

        details_enabled = width >= 95
        split = width if not details_enabled else max(42, min(width - 38, width // 2))
        self.draw_tree(stdscr, content_top, content_height, split)
        if details_enabled:
            self.draw_separator(stdscr, content_top, content_height, split)
            self.draw_details(stdscr, content_top, content_height, split + 2, width - split - 2)

        self.draw_events(stdscr, events_y, footer_y - events_y, width)
        help_text = "q quit | arrows/jk select | r refresh | e toggle empty ports"
        if not details_enabled:
            help_text += " | widen terminal for details"
        addstr(stdscr, footer_y, 0, help_text, width, curses.A_REVERSE)
        stdscr.refresh()

    def status_line(self) -> str:
        snapshot = self.state.snapshot
        plugged = sum(1 for dev in snapshot.devices.values() if not dev.is_root)
        hubs = sum(1 for dev in snapshot.devices.values() if dev.is_hub)
        empty = "shown" if self.state.show_empty_ports else "hidden"
        scanned = time.strftime("%H:%M:%S", time.localtime(snapshot.scanned_at))
        status = f"{plugged} devices | {hubs} hubs | empty ports {empty} | scanned {scanned}"
        if snapshot.errors:
            status += f" | {len(snapshot.errors)} read errors"
        return status

    def draw_tree(self, stdscr: curses.window, top: int, height: int, width: int) -> None:
        rows = self.state.rows
        if not rows:
            addstr(stdscr, top, 0, "No USB devices found.", width, curses.color_pair(5) | curses.A_BOLD)
            for index, error in enumerate(self.state.snapshot.errors[: height - 1], start=1):
                addstr(stdscr, top + index, 0, error, width, curses.color_pair(5))
            return

        if self.state.selected < self.state.top:
            self.state.top = self.state.selected
        if self.state.selected >= self.state.top + height:
            self.state.top = self.state.selected - height + 1
        self.state.top = max(0, min(self.state.top, max(0, len(rows) - height)))

        addstr(stdscr, top - 1, 0, "Topology", width, curses.A_BOLD)
        for screen_line in range(height):
            row_index = self.state.top + screen_line
            if row_index >= len(rows):
                break
            row = rows[row_index]
            prefix = "  " * row.depth
            marker = "> " if row_index == self.state.selected else "  "
            text = marker + prefix + row.text
            attr = 0
            if row_index == self.state.selected:
                attr |= curses.color_pair(2)
            elif row.state == "empty":
                attr |= curses.A_DIM
            elif row.state == "added":
                attr |= curses.color_pair(6) | curses.A_BOLD
            elif "[plugged]" in row.text:
                attr |= curses.color_pair(3)
            addstr(stdscr, top + screen_line, 0, text, width, attr)

    def draw_separator(self, stdscr: curses.window, top: int, height: int, x: int) -> None:
        for offset in range(height):
            addstr(stdscr, top + offset, x, "|", 1, curses.A_DIM)

    def draw_details(self, stdscr: curses.window, top: int, height: int, x: int, width: int) -> None:
        addstr(stdscr, top - 1, x, "Details", width, curses.A_BOLD)
        if not self.state.rows:
            return
        row = self.state.rows[self.state.selected]
        if row.device_name:
            device = self.state.snapshot.devices.get(row.device_name)
            lines = field_lines(device) if device else ["Device disappeared; refresh pending."]
        else:
            lines = empty_port_lines(self.state.snapshot, row)

        for offset, line in enumerate(lines[:height]):
            attr = curses.A_BOLD if offset == 0 else 0
            if line.startswith("Status: plugged"):
                attr |= curses.color_pair(3)
            elif line.startswith("Status: empty"):
                attr |= curses.A_DIM
            addstr(stdscr, top + offset, x, line, width, attr)

    def draw_events(self, stdscr: curses.window, top: int, height: int, width: int) -> None:
        if height <= 0:
            return
        addstr(stdscr, top, 0, "Events", width, curses.A_BOLD)
        for index, event in enumerate(list(self.state.events)[: max(0, height - 1)], start=1):
            stamp = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
            text = f"{stamp} {event.action}: {event.label} at {event.location}"
            attr = curses.color_pair(3) if event.action == "plugged" else curses.color_pair(5)
            addstr(stdscr, top + index, 0, text, width, attr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive Linux USB hub and port TUI")
    parser.add_argument("--sysfs", type=Path, default=DEFAULT_SYSFS, help="USB sysfs directory")
    parser.add_argument("--interval", type=float, default=1.0, help="refresh interval in seconds")
    parser.add_argument("--hide-empty", action="store_true", help="start with empty hub ports hidden")
    parser.add_argument("--once", action="store_true", help="print one USB tree snapshot and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval < 0.2:
        args.interval = 0.2

    if args.once:
        snapshot = scan_usb(args.sysfs)
        print(format_snapshot(snapshot, show_empty_ports=not args.hide_empty))
        return 1 if snapshot.errors and not snapshot.devices else 0

    state = AppState(sysfs=args.sysfs, interval=args.interval, show_empty_ports=not args.hide_empty)
    curses.wrapper(UsbTui(state).run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
