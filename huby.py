#!/usr/bin/env python3
"""Interactive Linux USB topology viewer.

The app reads /sys/bus/usb/devices, builds a root-hub/port tree, and refreshes
it in a curses TUI. It does not need root privileges for normal viewing.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Iterable


DEFAULT_SYSFS = Path("/sys/bus/usb/devices")
DEFAULT_META_DIR = Path("/home/.huby/meta")
ROOT_RE = re.compile(r"^usb(?P<bus>\d+)$")
DEVICE_RE = re.compile(r"^(?P<bus>\d+)-(?P<ports>\d+(?:\.\d+)*)$")
PORT_DIR_RE = re.compile(r".+-port(?P<port>\d+)$")
META_FIELDS = ("name", "role", "notes")

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
    real_path: Path | None
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
    dev_nodes: tuple[str, ...] = ()
    serial_by_path: tuple[str, ...] = ()
    connected_at: float | None = None
    connected_time_source: str | None = None
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
class UsbPortStatus:
    hub_name: str
    port: int
    path: Path
    real_path: Path | None
    state: str | None = None
    disabled: bool | None = None
    connect_type: str | None = None
    location: str | None = None
    over_current_count: str | None = None
    peer: str | None = None


@dataclass(frozen=True)
class Snapshot:
    devices: dict[str, UsbDevice]
    children: dict[str, list[str]]
    scanned_at: float
    errors: tuple[str, ...] = ()
    port_statuses: dict[tuple[str, int], UsbPortStatus] = field(default_factory=dict)

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
class PowerTarget:
    hub_location: str
    port: int
    label: str


@dataclass(frozen=True)
class PendingPowerAction:
    action: str
    target: PowerTarget


@dataclass(frozen=True)
class PortMeta:
    hub_location: str
    port: int
    name: str = ""
    role: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class MetaEditorState:
    target: PowerTarget
    values: dict[str, str]
    field_index: int = 0
    cursor: int = 0


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
    auto_refresh: bool
    auto_refresh_power_state: bool
    show_empty_ports: bool
    uhubctl_path: str
    force_uhubctl: bool
    dry_run_power: bool
    meta_dir: Path
    snapshot: Snapshot = field(default_factory=lambda: Snapshot({}, {}, time.time()))
    rows: list[ViewRow] = field(default_factory=list)
    port_meta: dict[tuple[str, int], PortMeta] = field(default_factory=dict)
    selected: int = 0
    top: int = 0
    events: Deque[UsbEvent] = field(default_factory=lambda: deque(maxlen=8))
    highlighted_until: dict[str, float] = field(default_factory=dict)
    last_scan: float = 0.0
    pending_power: PendingPowerAction | None = None
    meta_edit: MetaEditorState | None = None
    status_message: str | None = None
    status_until: float = 0.0


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


def read_bool_flag(path: Path) -> bool | None:
    value = read_text(path)
    if value is None:
        return None
    return value not in {"0", "false", "False", "no", "No"}


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


def uhubctl_location(device: UsbDevice) -> str | None:
    if device.is_root:
        return str(device.bus)
    return device.name


def power_target_for_row(snapshot: Snapshot, row: ViewRow) -> PowerTarget | None:
    parent = snapshot.devices.get(row.parent_name or "")
    if not parent or row.port is None:
        return None

    hub_location = uhubctl_location(parent)
    if not hub_location:
        return None

    if row.device_name and row.device_name in snapshot.devices:
        label = device_label(snapshot.devices[row.device_name])
    else:
        label = f"{device_label(parent)} port {row.port}"
    return PowerTarget(hub_location=hub_location, port=row.port, label=label)


def meta_key(target: PowerTarget) -> tuple[str, int]:
    return target.hub_location, target.port


def meta_filename(hub_location: str, port: int) -> str:
    safe_location = re.sub(r"[^A-Za-z0-9_.-]+", "_", hub_location)
    return f"{safe_location}__p{port}.json"


def meta_path(meta_dir: Path, hub_location: str, port: int) -> Path:
    return meta_dir / meta_filename(hub_location, port)


def timestamp_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def load_port_meta(meta_dir: Path) -> dict[tuple[str, int], PortMeta]:
    metadata: dict[tuple[str, int], PortMeta] = {}
    if not meta_dir.is_dir():
        return metadata

    try:
        files = sorted(meta_dir.glob("*.json"), key=lambda path: path.name)
    except OSError:
        return metadata

    for path in files:
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        hub_location = str(data.get("hub_location", "")).strip()
        try:
            port = int(data.get("port"))
        except (TypeError, ValueError):
            continue
        if not hub_location or port < 1:
            continue
        metadata[(hub_location, port)] = PortMeta(
            hub_location=hub_location,
            port=port,
            name=str(data.get("name", "")),
            role=str(data.get("role", "")),
            notes=str(data.get("notes", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
    return metadata


def save_port_meta(meta_dir: Path, target: PowerTarget, values: dict[str, str], existing: PortMeta | None) -> PortMeta:
    now = timestamp_now()
    metadata = PortMeta(
        hub_location=target.hub_location,
        port=target.port,
        name=values.get("name", "").strip(),
        role=values.get("role", "").strip(),
        notes=values.get("notes", "").strip(),
        created_at=existing.created_at if existing and existing.created_at else now,
        updated_at=now,
    )
    meta_dir.mkdir(parents=True, exist_ok=True)
    path = meta_path(meta_dir, target.hub_location, target.port)
    payload = {
        "hub_location": metadata.hub_location,
        "port": metadata.port,
        "name": metadata.name,
        "role": metadata.role,
        "notes": metadata.notes,
        "created_at": metadata.created_at,
        "updated_at": metadata.updated_at,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return metadata


def row_port_status(snapshot: Snapshot, row: ViewRow) -> UsbPortStatus | None:
    if row.parent_name is None or row.port is None:
        return None
    return snapshot.port_statuses.get((row.parent_name, row.port))


def port_status_tags(status: UsbPortStatus | None) -> list[str]:
    if not status:
        return []

    tags: list[str] = []
    if status.disabled is True:
        tags.append("disabled")
    if status.state:
        tags.append(status.state)
    return tags


def format_port_status_suffix(status: UsbPortStatus | None) -> str:
    tags = port_status_tags(status)
    if not tags:
        return ""
    return " " + " ".join(f"[{tag}]" for tag in tags)


def format_meta_suffix(meta: PortMeta | None) -> str:
    if not meta:
        return ""
    if meta.name:
        return f" <{meta.name}>"
    if meta.role:
        return f" <{meta.role}>"
    return " <meta>"


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


def devname_to_path(devname: str | None) -> str | None:
    if not devname:
        return None
    devname = devname.strip()
    if not devname:
        return None
    if devname.startswith("/dev/"):
        return devname
    return f"/dev/{devname}"


def add_dev_node(nodes: list[str], devname: str | None) -> None:
    dev_path = devname_to_path(devname)
    if dev_path and dev_path not in nodes:
        nodes.append(dev_path)


def collect_dev_nodes_from_tree(path: Path, nodes: list[str], max_depth: int = 8) -> None:
    try:
        root = path.resolve(strict=True)
    except OSError:
        return

    stack: list[tuple[Path, int]] = [(root, 0)]
    seen: set[Path] = set()
    skip_dirs = {"driver", "subsystem", "firmware_node"}

    while stack:
        current, depth = stack.pop()
        if current in seen:
            continue
        seen.add(current)

        uevent = read_uevent(current)
        add_dev_node(nodes, uevent.get("DEVNAME"))

        if depth >= max_depth:
            continue
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
        except OSError:
            continue

        for child in children:
            if child.name in skip_dirs or child.is_symlink():
                continue
            try:
                if child.is_dir():
                    stack.append((child, depth + 1))
            except OSError:
                continue


def read_dev_nodes(sysfs: Path, device_name: str, root_devname: str | None) -> tuple[str, ...]:
    nodes: list[str] = []
    add_dev_node(nodes, root_devname)

    try:
        interfaces = sorted(sysfs.glob(f"{device_name}:*"), key=lambda path: path.name)
    except OSError:
        return tuple(nodes)

    for interface_path in interfaces:
        if interface_path.is_dir():
            collect_dev_nodes_from_tree(interface_path, nodes)
    return tuple(nodes)


def read_dev_aliases(alias_dir: Path, dev_nodes: tuple[str, ...]) -> tuple[str, ...]:
    if not dev_nodes or not alias_dir.is_dir():
        return ()

    targets: set[Path] = set()
    for dev_node in dev_nodes:
        try:
            targets.add(Path(dev_node).resolve(strict=True))
        except OSError:
            continue

    aliases: list[str] = []
    try:
        candidates = sorted(alias_dir.iterdir(), key=lambda path: path.name)
    except OSError:
        return ()

    for candidate in candidates:
        if not candidate.is_symlink():
            continue
        try:
            target = candidate.resolve(strict=True)
        except OSError:
            continue
        if target in targets:
            aliases.append(str(candidate))
    return tuple(aliases)


def read_connection_time(path: Path) -> tuple[float | None, str | None]:
    try:
        stat = path.stat()
    except OSError:
        return None, None

    if stat.st_ctime > 0:
        return stat.st_ctime, "sysfs ctime"
    if stat.st_mtime > 0:
        return stat.st_mtime, "sysfs mtime"
    return None, None


def read_device(sysfs: Path, path: Path, bus: int, ports: tuple[int, ...], parent: str | None) -> UsbDevice:
    uevent = read_uevent(path)
    dev_name = uevent.get("DEVNAME")
    try:
        real_path = path.resolve(strict=False)
    except OSError:
        real_path = None
    dev_nodes = read_dev_nodes(sysfs, path.name, dev_name)
    connected_at, connected_time_source = read_connection_time(path)
    return UsbDevice(
        name=path.name,
        path=path,
        real_path=real_path,
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
        dev_nodes=dev_nodes,
        serial_by_path=read_dev_aliases(Path("/dev/serial/by-path"), dev_nodes),
        connected_at=connected_at,
        connected_time_source=connected_time_source,
        configuration=read_text(path / "configuration"),
        authorized=read_text(path / "authorized"),
        removable=read_text(path / "removable"),
        driver=read_driver(path),
        physical_location=read_physical_location(path),
        interfaces=read_interfaces(sysfs, path.name),
    )


def hub_interface_paths(sysfs: Path, device: UsbDevice) -> list[Path]:
    pattern = f"{device.bus}-0:*" if device.is_root else f"{device.name}:*"
    try:
        return sorted((path for path in sysfs.glob(pattern) if path.is_dir()), key=lambda path: path.name)
    except OSError:
        return []


def read_peer_path(path: Path) -> str | None:
    peer = path / "peer"
    try:
        if peer.is_symlink():
            return str(peer.resolve(strict=False))
    except OSError:
        return None
    return None


def read_port_status(hub_name: str, path: Path, port: int) -> UsbPortStatus:
    try:
        real_path = path.resolve(strict=False)
    except OSError:
        real_path = None
    return UsbPortStatus(
        hub_name=hub_name,
        port=port,
        path=path,
        real_path=real_path,
        state=read_text(path / "state"),
        disabled=read_bool_flag(path / "disable"),
        connect_type=read_text(path / "connect_type"),
        location=read_text(path / "location"),
        over_current_count=read_text(path / "over_current_count"),
        peer=read_peer_path(path),
    )


def read_port_statuses(sysfs: Path, devices: dict[str, UsbDevice]) -> dict[tuple[str, int], UsbPortStatus]:
    statuses: dict[tuple[str, int], UsbPortStatus] = {}

    for device in devices.values():
        if not device.is_hub:
            continue
        for interface_path in hub_interface_paths(sysfs, device):
            try:
                children = sorted(interface_path.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for child in children:
                match = PORT_DIR_RE.match(child.name)
                if not match:
                    continue
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                port = int(match.group("port"))
                statuses[(device.name, port)] = read_port_status(device.name, child, port)

    return statuses


def scan_usb(
    sysfs: Path = DEFAULT_SYSFS,
    include_port_statuses: bool = True,
    previous_port_statuses: dict[tuple[str, int], UsbPortStatus] | None = None,
) -> Snapshot:
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

    if include_port_statuses:
        port_statuses = read_port_statuses(sysfs, devices)
    else:
        port_statuses = previous_port_statuses or {}
    return Snapshot(devices, children, time.time(), tuple(errors), port_statuses)


def build_rows(
    snapshot: Snapshot,
    show_empty_ports: bool,
    highlighted: dict[str, float] | None = None,
    port_meta: dict[tuple[str, int], PortMeta] | None = None,
) -> list[ViewRow]:
    highlighted = highlighted or {}
    port_meta = port_meta or {}
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
            port_status = snapshot.port_statuses.get((device.name, port))
            status_suffix = format_port_status_suffix(port_status)
            target = PowerTarget(uhubctl_location(device) or device.name, port, "")
            meta_suffix = format_meta_suffix(port_meta.get(meta_key(target)))
            if child_name:
                child = snapshot.devices[child_name]
                prefix = "port %s [plugged]" % port
                if child.is_hub:
                    prefix = f"{prefix} [hub]"
                rows.append(
                    ViewRow(
                        kind="device",
                        depth=depth,
                        text=f"{prefix}{status_suffix}{meta_suffix} {device_label(child)}",
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
                        text=f"port {port} [empty]{status_suffix}{meta_suffix}",
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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def format_connected_time(device: UsbDevice) -> str | None:
    if device.connected_at is None:
        return None
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(device.connected_at))
    elapsed = format_duration(time.time() - device.connected_at)
    source = f", {device.connected_time_source}" if device.connected_time_source else ""
    return f"{timestamp} ({elapsed} ago{source})"


def build_uhubctl_command(path: str, action: str, target: PowerTarget, force: bool) -> list[str]:
    command = [path]
    if force:
        command.append("-f")
    command.extend(["-l", target.hub_location, "-p", str(target.port), "-a", action])
    return command


def format_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


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


def power_target_lines(target: PowerTarget | None) -> list[str]:
    if not target:
        return ["Power target: unavailable"]
    return [
        f"Power target: uhubctl -l {target.hub_location} -p {target.port}",
        "Power keys: o off, i on, c cycle",
    ]


def port_status_lines(status: UsbPortStatus | None) -> list[str]:
    if not status:
        return ["Port status: unavailable from sysfs"]

    lines = ["Port status:"]
    if status.state:
        lines.append(f"  state: {status.state}")
    if status.disabled is not None:
        disabled = "yes" if status.disabled else "no"
        lines.append(f"  disabled: {disabled}")
    if status.connect_type:
        lines.append(f"  connect type: {status.connect_type}")
    if status.location:
        lines.append(f"  location: {status.location}")
    if status.over_current_count:
        lines.append(f"  over-current count: {status.over_current_count}")
    lines.append(f"  sysfs: {status.path}")
    if status.real_path and status.real_path != status.path:
        lines.append(f"  real path: {status.real_path}")
    if status.peer:
        lines.append(f"  peer: {status.peer}")
    return lines


def meta_lines(meta: PortMeta | None, target: PowerTarget | None, meta_dir: Path) -> list[str]:
    if not target:
        return ["Meta: unavailable"]
    lines = ["Meta:"]
    if meta:
        if meta.name:
            lines.append(f"  name: {meta.name}")
        if meta.role:
            lines.append(f"  role: {meta.role}")
        if meta.notes:
            lines.append(f"  notes: {meta.notes}")
        if meta.updated_at:
            lines.append(f"  updated: {meta.updated_at}")
    else:
        lines.append("  none")
    lines.append(f"  file: {meta_path(meta_dir, target.hub_location, target.port)}")
    lines.append("  edit: m")
    return lines


def field_lines(
    device: UsbDevice,
    target: PowerTarget | None = None,
    port_status: UsbPortStatus | None = None,
    meta: PortMeta | None = None,
    meta_dir: Path = DEFAULT_META_DIR,
) -> list[str]:
    connected_time = format_connected_time(device)
    lines = [
        f"Name: {device.name}",
        f"Status: plugged",
        f"Location: {location_label(device)}",
    ]
    if connected_time:
        lines.append(f"Connected: {connected_time}")
    lines.append(f"Sysfs: {device.path}")
    if device.real_path and device.real_path != device.path:
        lines.append(f"Real path: {device.real_path}")
    lines.extend(power_target_lines(target))
    lines.extend(meta_lines(meta, target, meta_dir))
    lines.extend(port_status_lines(port_status))
    if device.dev_nodes:
        lines.append("Dev nodes:")
        for dev_node in device.dev_nodes:
            lines.append(f"  {dev_node}")
    if device.serial_by_path:
        lines.append("Serial by-path:")
        for alias in device.serial_by_path:
            lines.append(f"  {alias}")

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


def empty_port_lines(
    snapshot: Snapshot,
    row: ViewRow,
    target: PowerTarget | None = None,
    port_status: UsbPortStatus | None = None,
    meta: PortMeta | None = None,
    meta_dir: Path = DEFAULT_META_DIR,
) -> list[str]:
    parent = snapshot.devices.get(row.parent_name or "")
    if not parent:
        return ["Empty USB port", "Status: empty"]

    location = f"{parent.name} port {row.port}"
    if parent.busnum:
        location = f"bus {parent.busnum}, {location}"
    lines = [
        "Empty USB port",
        "Status: empty",
        f"Location: {location}",
        f"Parent hub: {device_label(parent)}",
        f"Parent sysfs: {parent.path}",
    ]
    if parent.real_path and parent.real_path != parent.path:
        lines.append(f"Parent real path: {parent.real_path}")
    lines.extend(power_target_lines(target))
    lines.extend(meta_lines(meta, target, meta_dir))
    lines.extend(port_status_lines(port_status))
    return lines


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

        self.state.port_meta = load_port_meta(self.state.meta_dir)
        self.refresh_snapshot(force=True, include_port_statuses=True)
        while True:
            now = time.monotonic()
            if self.state.auto_refresh and now - self.state.last_scan >= self.state.interval:
                self.refresh_snapshot(include_port_statuses=self.state.auto_refresh_power_state)
            self.draw(stdscr)
            key = stdscr.getch()
            if key == -1:
                continue
            if self.handle_key(key):
                break

    def refresh_snapshot(self, force: bool = False, include_port_statuses: bool = False) -> None:
        old_snapshot = self.state.snapshot
        new_snapshot = scan_usb(
            self.state.sysfs,
            include_port_statuses=include_port_statuses,
            previous_port_statuses=old_snapshot.port_statuses,
        )

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
        self.state.rows = build_rows(
            new_snapshot,
            self.state.show_empty_ports,
            self.state.highlighted_until,
            self.state.port_meta,
        )
        self.state.last_scan = time.monotonic()
        if self.state.rows:
            self.state.selected = min(self.state.selected, len(self.state.rows) - 1)
        else:
            self.state.selected = 0

    def handle_key(self, key: int) -> bool:
        if key in (ord("q"), ord("Q"), 27):
            if self.state.meta_edit:
                self.cancel_meta_edit()
                return False
            if self.state.pending_power:
                self.cancel_power_action()
                return False
            return True
        if self.state.meta_edit:
            self.handle_meta_key(key)
            return False
        if self.state.pending_power:
            if key in (ord("y"), ord("Y")):
                self.execute_pending_power_action()
            elif key in (ord("n"), ord("N")):
                self.cancel_power_action()
            return False
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
        elif key in (ord("r"), ord("R"), curses.KEY_F5):
            self.refresh_snapshot(force=False, include_port_statuses=False)
            self.set_status("Rescanned USB topology", seconds=2.0)
        elif key in (ord("s"), ord("S")):
            self.refresh_snapshot(force=True, include_port_statuses=True)
            self.set_status("Refreshed sysfs port power state", seconds=2.0)
        elif key in (ord("e"), ord("E")):
            self.state.show_empty_ports = not self.state.show_empty_ports
            self.state.rows = build_rows(
                self.state.snapshot,
                self.state.show_empty_ports,
                self.state.highlighted_until,
                self.state.port_meta,
            )
            self.state.selected = min(self.state.selected, max(0, len(self.state.rows) - 1))
        elif key in (ord("o"), ord("O")):
            self.prepare_power_action("off")
        elif key in (ord("i"), ord("I")):
            self.prepare_power_action("on")
        elif key in (ord("c"), ord("C")):
            self.prepare_power_action("cycle")
        elif key in (ord("m"), ord("M")):
            self.open_meta_editor()
        return False

    def move_selection(self, delta: int) -> None:
        if not self.state.rows:
            return
        self.state.selected = max(0, min(len(self.state.rows) - 1, self.state.selected + delta))

    def selected_row(self) -> ViewRow | None:
        if not self.state.rows:
            return None
        if self.state.selected >= len(self.state.rows):
            return None
        return self.state.rows[self.state.selected]

    def set_status(self, message: str, seconds: float = 5.0) -> None:
        self.state.status_message = message
        self.state.status_until = time.monotonic() + seconds

    def rebuild_rows(self) -> None:
        self.state.rows = build_rows(
            self.state.snapshot,
            self.state.show_empty_ports,
            self.state.highlighted_until,
            self.state.port_meta,
        )
        self.state.selected = min(self.state.selected, max(0, len(self.state.rows) - 1))

    def open_meta_editor(self) -> None:
        row = self.selected_row()
        target = power_target_for_row(self.state.snapshot, row) if row else None
        if not target:
            self.set_status("No port selected for metadata")
            return

        existing = self.state.port_meta.get(meta_key(target))
        values = {field_name: getattr(existing, field_name, "") if existing else "" for field_name in META_FIELDS}
        self.state.meta_edit = MetaEditorState(target=target, values=values)
        self.set_status(f"Editing metadata for {target.hub_location}:{target.port}", seconds=3600.0)

    def cancel_meta_edit(self) -> None:
        self.state.meta_edit = None
        self.set_status("Metadata edit cancelled", seconds=2.0)

    def save_meta_edit(self) -> None:
        editor = self.state.meta_edit
        if not editor:
            return
        key = meta_key(editor.target)
        existing = self.state.port_meta.get(key)
        try:
            metadata = save_port_meta(self.state.meta_dir, editor.target, editor.values, existing)
        except OSError as exc:
            self.set_status(f"Cannot save metadata: {exc}", seconds=8.0)
            return

        self.state.port_meta[key] = metadata
        self.state.meta_edit = None
        self.rebuild_rows()
        self.set_status(f"Saved metadata: {meta_path(self.state.meta_dir, metadata.hub_location, metadata.port)}")

    def handle_meta_key(self, key: int) -> None:
        editor = self.state.meta_edit
        if not editor:
            return
        field_name = META_FIELDS[editor.field_index]
        value = editor.values.get(field_name, "")

        if key in (10, 13, curses.KEY_ENTER, curses.KEY_F2, 19):  # Enter, F2, or Ctrl-S if flow control is disabled.
            self.save_meta_edit()
            return
        if key in (9, curses.KEY_DOWN):
            editor.field_index = (editor.field_index + 1) % len(META_FIELDS)
            editor.cursor = min(editor.cursor, len(editor.values.get(META_FIELDS[editor.field_index], "")))
            return
        if key == curses.KEY_UP:
            editor.field_index = (editor.field_index - 1) % len(META_FIELDS)
            editor.cursor = min(editor.cursor, len(editor.values.get(META_FIELDS[editor.field_index], "")))
            return
        if key == curses.KEY_LEFT:
            editor.cursor = max(0, editor.cursor - 1)
            return
        if key == curses.KEY_RIGHT:
            editor.cursor = min(len(value), editor.cursor + 1)
            return
        if key == curses.KEY_HOME:
            editor.cursor = 0
            return
        if key == curses.KEY_END:
            editor.cursor = len(value)
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if editor.cursor > 0:
                editor.values[field_name] = value[: editor.cursor - 1] + value[editor.cursor :]
                editor.cursor -= 1
            return
        if key == curses.KEY_DC:
            if editor.cursor < len(value):
                editor.values[field_name] = value[: editor.cursor] + value[editor.cursor + 1 :]
            return
        if 32 <= key <= 126:
            character = chr(key)
            editor.values[field_name] = value[: editor.cursor] + character + value[editor.cursor :]
            editor.cursor += 1

    def prepare_power_action(self, action: str) -> None:
        row = self.selected_row()
        target = power_target_for_row(self.state.snapshot, row) if row else None
        if not target:
            self.set_status("No controllable upstream hub/port for selected row")
            return
        self.state.pending_power = PendingPowerAction(action=action, target=target)
        command = build_uhubctl_command(self.state.uhubctl_path, action, target, self.state.force_uhubctl)
        self.set_status(f"Confirm {action} for {target.label}: {format_command(command)}")

    def cancel_power_action(self) -> None:
        self.state.pending_power = None
        self.set_status("Power action cancelled", seconds=2.0)

    def execute_pending_power_action(self) -> None:
        pending = self.state.pending_power
        if not pending:
            return
        self.state.pending_power = None
        command = build_uhubctl_command(
            self.state.uhubctl_path,
            pending.action,
            pending.target,
            self.state.force_uhubctl,
        )
        command_text = format_command(command)

        if self.state.dry_run_power:
            self.state.events.appendleft(
                UsbEvent(time.time(), "dry-run", "", pending.target.label, command_text)
            )
            self.set_status(f"Dry run: {command_text}")
            return

        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except FileNotFoundError:
            self.state.events.appendleft(
                UsbEvent(time.time(), "power failed", "", pending.target.label, f"{self.state.uhubctl_path} not found")
            )
            self.set_status(f"{self.state.uhubctl_path} not found")
            return
        except subprocess.TimeoutExpired:
            self.state.events.appendleft(
                UsbEvent(time.time(), "power failed", "", pending.target.label, "uhubctl timed out")
            )
            self.set_status("uhubctl timed out")
            return

        output = " ".join((result.stdout or result.stderr or "").split())
        if result.returncode == 0:
            self.state.events.appendleft(
                UsbEvent(time.time(), f"power {pending.action}", "", pending.target.label, command_text)
            )
            self.set_status(f"Ran: {command_text}; press s to refresh port power state", seconds=6.0)
            return

        detail = output[:160] if output else f"exit {result.returncode}"
        self.state.events.appendleft(
            UsbEvent(time.time(), "power failed", "", pending.target.label, detail)
        )
        self.set_status(f"uhubctl failed: {detail}", seconds=8.0)

    def draw(self, stdscr: curses.window) -> None:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        if not self.state.meta_edit:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
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
        help_text = "q quit | arrows/jk select | r/F5 rescan | s port state | e empty | m meta | o off | i on | c cycle"
        if self.state.pending_power:
            help_text = "Confirm power action: y run | n cancel | Esc cancel"
        if self.state.meta_edit:
            help_text = "Metadata: Tab move | Enter/F2 save | Esc cancel"
        if not details_enabled:
            help_text += " | widen terminal for details"
        addstr(stdscr, footer_y, 0, help_text, width, curses.A_REVERSE)
        if self.state.meta_edit:
            self.draw_meta_editor(stdscr)
        stdscr.refresh()

    def status_line(self) -> str:
        snapshot = self.state.snapshot
        plugged = sum(1 for dev in snapshot.devices.values() if not dev.is_root)
        hubs = sum(1 for dev in snapshot.devices.values() if dev.is_hub)
        empty = "shown" if self.state.show_empty_ports else "hidden"
        scanned = time.strftime("%H:%M:%S", time.localtime(snapshot.scanned_at))
        refresh_mode = f"auto {self.state.interval:g}s" if self.state.auto_refresh else "manual refresh"
        power_state_mode = "power state auto" if self.state.auto_refresh_power_state else "power state manual"
        status = (
            f"{plugged} devices | {hubs} hubs | empty ports {empty} | "
            f"{refresh_mode} | {power_state_mode} | scanned {scanned}"
        )
        if snapshot.errors:
            status += f" | {len(snapshot.errors)} read errors"
        if self.state.dry_run_power:
            status += " | power dry-run"
        if self.state.pending_power:
            pending = self.state.pending_power
            status = f"Confirm {pending.action}: {pending.target.label} at {pending.target.hub_location}:{pending.target.port}"
        elif self.state.status_message and self.state.status_until > time.monotonic():
            status = self.state.status_message
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
        target = power_target_for_row(self.state.snapshot, row)
        port_status = row_port_status(self.state.snapshot, row)
        meta = self.state.port_meta.get(meta_key(target)) if target else None
        if row.device_name:
            device = self.state.snapshot.devices.get(row.device_name)
            lines = (
                field_lines(device, target, port_status, meta, self.state.meta_dir)
                if device
                else ["Device disappeared; refresh pending."]
            )
        else:
            lines = empty_port_lines(self.state.snapshot, row, target, port_status, meta, self.state.meta_dir)

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

    def draw_meta_editor(self, stdscr: curses.window) -> None:
        editor = self.state.meta_edit
        if not editor:
            return

        height, width = stdscr.getmaxyx()
        box_width = min(max(64, width - 8), width - 2)
        box_height = 10
        top = max(1, (height - box_height) // 2)
        left = max(1, (width - box_width) // 2)

        for y in range(top, min(top + box_height, height - 1)):
            addstr(stdscr, y, left, " " * box_width, box_width, curses.color_pair(2))

        title = f" Edit Port Metadata: {editor.target.hub_location}:{editor.target.port} "
        addstr(stdscr, top, left, title, box_width, curses.color_pair(1) | curses.A_BOLD)
        addstr(
            stdscr,
            top + 1,
            left + 2,
            f"File: {meta_path(self.state.meta_dir, editor.target.hub_location, editor.target.port)}",
            box_width - 4,
            curses.color_pair(2),
        )

        for index, field_name in enumerate(META_FIELDS):
            y = top + 3 + index
            label = f"{field_name}:"
            value = editor.values.get(field_name, "")
            attr = curses.color_pair(2)
            if index == editor.field_index:
                attr |= curses.A_BOLD
            addstr(stdscr, y, left + 2, label, 10, attr)
            addstr(stdscr, y, left + 12, value, box_width - 14, attr)

        addstr(stdscr, top + box_height - 2, left + 2, "Tab/Up/Down move  Enter/F2 save  Esc cancel", box_width - 4, curses.color_pair(2))

        cursor_y = top + 3 + editor.field_index
        cursor_x = left + 12 + min(editor.cursor, box_width - 15)
        try:
            stdscr.move(cursor_y, cursor_x)
            curses.curs_set(1)
        except curses.error:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive Linux USB hub and port TUI")
    parser.add_argument("--sysfs", type=Path, default=DEFAULT_SYSFS, help="USB sysfs directory")
    parser.add_argument("--interval", type=float, default=1.0, help="auto-refresh interval in seconds")
    parser.add_argument("--auto-refresh", action="store_true", help="poll USB topology instead of using manual refresh")
    parser.add_argument(
        "--auto-refresh-power-state",
        action="store_true",
        help="also refresh sysfs port power state during auto-refresh",
    )
    parser.add_argument("--hide-empty", action="store_true", help="start with empty hub ports hidden")
    parser.add_argument("--once", action="store_true", help="print one USB tree snapshot and exit")
    parser.add_argument(
        "--uhubctl",
        default=shutil.which("uhubctl") or "uhubctl",
        help="uhubctl executable path",
    )
    parser.add_argument("--no-force", action="store_true", help="do not pass -f to uhubctl")
    parser.add_argument("--dry-run-power", action="store_true", help="show uhubctl commands without running them")
    parser.add_argument("--meta-dir", type=Path, default=DEFAULT_META_DIR, help="directory for per-port metadata JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval < 0.2:
        args.interval = 0.2

    if args.once:
        snapshot = scan_usb(args.sysfs)
        print(format_snapshot(snapshot, show_empty_ports=not args.hide_empty))
        return 1 if snapshot.errors and not snapshot.devices else 0

    state = AppState(
        sysfs=args.sysfs,
        interval=args.interval,
        auto_refresh=args.auto_refresh,
        auto_refresh_power_state=args.auto_refresh_power_state,
        show_empty_ports=not args.hide_empty,
        uhubctl_path=args.uhubctl,
        force_uhubctl=not args.no_force,
        dry_run_power=args.dry_run_power,
        meta_dir=args.meta_dir,
    )
    curses.wrapper(UsbTui(state).run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
