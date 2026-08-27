"""Central configuration.

No network address, handle or coding-system literal belongs anywhere else in the package.
"""

from __future__ import annotations

import pathlib
import uuid

# --------------------------------------------------------------------------------------
# MDIB scaffolding
# --------------------------------------------------------------------------------------

BOOTSTRAP_MDIB_PATH = pathlib.Path(__file__).with_name("mdib_bootstrap.xml")

# Ready-made configs shipped with the tool. The File menu builds its preset list from here.
PRESET_DIR = pathlib.Path(__file__).resolve().parent.parent / "presets"

# Handles that exist in the bootstrap MDIB and are referenced by the services.
MDS_HANDLE = "mds0"
SCO_HANDLE = "sco.mds0"
ALERT_SYSTEM_HANDLE = "alerts.mds0"
VMD_HANDLE = "vmd0"
CHANNEL_HANDLE = "ch0.vmd0"
SYSTEM_CONTEXT_HANDLE = "SC.mds0"
PATIENT_CONTEXT_HANDLE = "PC.mds0"
LOCATION_CONTEXT_HANDLE = "LC.mds0"

# Prefixes for handles generated at runtime. A metric created from the label "Zoom level"
# becomes "m.zoom_level" and its set operation becomes "op.zoom_level"; collisions get a
# numeric suffix.
METRIC_HANDLE_PREFIX = "m."
OPERATION_HANDLE_PREFIX = "op."
# ActivateOperations: something the device does, rather than a value it holds.
ACTION_HANDLE_PREFIX = "act."

# Subsystems created on demand, one Vmd and one Channel per named section, so a device
# published from a preset has a containment tree with parts rather than one flat channel.
VMD_HANDLE_PREFIX = "vmd."
CHANNEL_HANDLE_PREFIX = "ch."

# An alert condition and the signals that announce it. One condition can drive several
# signals, which is the distinction BICEPS draws and the reason they are separate objects.
ALERT_HANDLE_PREFIX = "al."
SIGNAL_HANDLE_PREFIX = "sig."

# --------------------------------------------------------------------------------------
# Coded values
# --------------------------------------------------------------------------------------

# The default nomenclature of IEEE 11073-10101.
CODING_SYSTEM_MDC = "urn:oid:1.2.840.10004.1.1.1.0.0.1"

# Our own coding system. Everything we invent lives here rather than pretending to be MDC.
# This is the "semantic gap" the thesis comparison table lists as an SDC weakness: without
# device specialisations there is no agreed vocabulary for what we model.
CODING_SYSTEM_PRIVATE = "urn:sdc-testing-toolbox:private"

# MDC_DIM_DIMLESS - the real 11073-10101 code for "no unit".
CODE_DIMENSIONLESS = "262656"

# --------------------------------------------------------------------------------------
# Discovery and identity
# --------------------------------------------------------------------------------------

# Default interface. Loopback keeps two instances on one machine talking without involving
# the network. We bind by IP rather than by adapter name because the loopback adapter's
# display name is not portable - ifaddr reports it as "Software Loopback Interface 1" on
# Windows, while the sdc11073 tutorial hard-codes "Loopback Pseudo-Interface 1".
DEFAULT_IP = "127.0.0.1"

# EPRs are derived from this namespace plus an instance name, so restarting an instance
# with the same name keeps its identity.
EPR_NAMESPACE = uuid.UUID("{7c2e4a10-6b3d-4f8e-9c21-0d5a8e3f1b47}")

DEFAULT_INSTANCE_NAME = "toolbox"

# Location context published by every provider, so consumers can filter by it later.
# Keys are LocationInfo field names, not the abbreviations SdcLocation takes.
DEFAULT_LOCATION = {"facility": "HOSP", "point_of_care": "CU1", "bed": "Toolbox"}

# --------------------------------------------------------------------------------------
# DPWS device metadata
# --------------------------------------------------------------------------------------

MANUFACTURER = "SDC Testing Toolbox"
MANUFACTURER_URL = "https://d-meurer.com"
MODEL_NAME = "Fictional SDC Device"
MODEL_NUMBER = "0.1"
FIRMWARE_VERSION = "0.1"


def epr_for(instance_name: str) -> uuid.UUID:
    """Return the stable EPR UUID for a named instance."""
    return uuid.uuid5(EPR_NAMESPACE, instance_name)
