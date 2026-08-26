"""The window that asks how to start, when nothing was said on the command line.

Same four things `run_toolbox.py` takes as arguments, with the same defaults, all editable.

The interface address gets a list rather than a bare field. WS-Discovery binds to a single
IPv4 address and a normal machine has eight or nine, most of them holding link-local
addresses that go nowhere. Picking from a named list is the difference between working
first time and half an hour of confusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import config, constants
from .styling import mark_as_error, mute

CONFIG_FILE_FILTER = "SDC toolbox config (*.json);;All files (*)"

# Addresses in this range mean "no DHCP answered". They are never the right choice.
LINK_LOCAL_PREFIX = "169.254."


@dataclass(frozen=True)
class StartupSettings:
    """What the user chose. Mirrors the command line arguments exactly."""

    name: str
    ip: str
    config_path: str | None
    verbose: bool


def available_ipv4() -> list[tuple[str, str]]:
    """Every IPv4 address on this machine as (address, adapter name).

    Usable addresses come first, link-local ones after, because on a laptop with a VPN and
    a couple of virtual adapters the useless ones outnumber the real one.
    """
    try:
        import ifaddr
    except ImportError:  # pragma: no cover - ifaddr ships with sdc11073
        return [(constants.DEFAULT_IP, "loopback")]

    found: list[tuple[str, str]] = []
    for adapter in ifaddr.get_adapters():
        for address in adapter.ips:
            if address.is_IPv4:
                found.append((str(address.ip), adapter.nice_name))

    found.sort(key=lambda entry: (entry[0].startswith(LINK_LOCAL_PREFIX), entry[0]))
    return found or [(constants.DEFAULT_IP, "loopback")]


class StartupDialog(QDialog):
    """Ask for the same four things the command line takes."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        name: str = constants.DEFAULT_INSTANCE_NAME,
        ip: str = constants.DEFAULT_IP,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start the SDC testing toolbox")
        self.setMinimumWidth(520)

        self.name_edit = QLineEdit(name)
        self.name_edit.setToolTip(
            "Decides the EPR, so restarting under the same name keeps this device's\n"
            "identity on the network. Two instances must not share a name.",
        )

        self.ip_box = QComboBox()
        self.ip_box.setEditable(True)
        for address, adapter in available_ipv4():
            suffix = "  (link-local, probably not what you want)" if address.startswith(LINK_LOCAL_PREFIX) else ""
            self.ip_box.addItem(f"{address}  \u2014  {adapter}{suffix}", address)
        self._select_ip(ip)
        self.ip_box.setToolTip(
            "Discovery binds to one address only. Loopback lets two instances on this\n"
            "machine talk without involving the network.",
        )

        self.config_box = QComboBox()
        self.config_box.setEditable(True)
        self.config_box.lineEdit().setPlaceholderText("optional")
        # Same shape as the address row above: the choices worth having are listed, and
        # anything else can still be typed or browsed for.
        self.config_box.addItem("", "")
        for preset in config.list_presets():
            caption = f"{preset.name}  \u2014  {preset.summary()}"
            self.config_box.addItem(caption, str(preset.path))
        self.config_box.setCurrentIndex(0)
        self.config_box.setToolTip(
            "A preset that ships with the tool, or any exported config file.\n"
            "Loading one replaces whatever the device would otherwise start with.",
        )
        browse = QPushButton("Browse\u2026")
        browse.clicked.connect(self._on_browse)
        config_row = QHBoxLayout()
        config_row.setContentsMargins(0, 0, 0, 0)
        config_row.addWidget(self.config_box, 1)
        config_row.addWidget(browse)
        config_widget = QWidget()
        config_widget.setLayout(config_row)

        self.verbose_box = QCheckBox("Verbose logging")
        self.verbose_box.setToolTip("Show what sdc11073 is doing. Useful when discovery misbehaves.")

        self.hint = QLabel(
            "Start a second copy with a different name to have two devices find each other.",
        )
        self.hint.setWordWrap(True)
        mute(self.hint)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        mark_as_error(self.error_label)
        self.error_label.hide()

        form = QFormLayout()
        form.addRow("Device name", self.name_edit)
        form.addRow("Bind to", self.ip_box)
        form.addRow("Config file", config_widget)
        form.addRow("", self.verbose_box)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Start")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

    # -- result --------------------------------------------------------------------

    def settings(self) -> StartupSettings | None:
        """What the user chose, or None if they cancelled."""
        return getattr(self, "_settings", None)

    # -- internals -----------------------------------------------------------------

    def _select_ip(self, ip: str) -> None:
        for index in range(self.ip_box.count()):
            if self.ip_box.itemData(index) == ip:
                self.ip_box.setCurrentIndex(index)
                return
        self.ip_box.setCurrentText(ip)

    def chosen_ip(self) -> str:
        """The address, without the adapter name the list shows beside it."""
        index = self.ip_box.currentIndex()
        # An index only matches when the text was not edited by hand.
        if index >= 0 and self.ip_box.currentText() == self.ip_box.itemText(index):
            return self.ip_box.itemData(index)
        return self.ip_box.currentText().split("\u2014")[0].strip()

    def chosen_config(self) -> str | None:
        """The config file path, or None when the user picked nothing.

        A listed preset carries its full path as item data; anything typed or browsed for is
        the text itself. Same reasoning as chosen_ip.
        """
        index = self.config_box.currentIndex()
        if index >= 0 and self.config_box.currentText() == self.config_box.itemText(index):
            return self.config_box.itemData(index) or None
        return self.config_box.currentText().strip() or None

    def _on_browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Choose a config file", "", CONFIG_FILE_FILTER)
        if filename:
            self.config_box.setCurrentText(filename)

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()

        name = self.name_edit.text().strip()
        if not name:
            self._fail("Give the device a name. It decides the EPR other devices see.")
            return

        ip = self.chosen_ip()
        if not ip:
            self._fail("Choose an address to bind discovery to.")
            return

        config_path = self.chosen_config()
        if config_path is not None:
            # Check it now rather than after a window has appeared and half a device exists.
            if not Path(config_path).exists():
                self._fail(f"No such file: {config_path}")
                return
            try:
                config.load_file(config_path)
            except config.ConfigError as exc:
                self._fail(str(exc))
                return

        self._settings = StartupSettings(
            name=name,
            ip=ip,
            config_path=config_path,
            verbose=self.verbose_box.isChecked(),
        )
        self.accept()
