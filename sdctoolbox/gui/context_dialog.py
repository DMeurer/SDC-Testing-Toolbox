"""The "Patient and location" dialog.

Contexts are the part of BICEPS that says *who* and *where*, as opposed to what the device
is measuring. They are worth showing because they behave unlike everything else in the MDIB:

* they are multi-state. Setting a patient does not overwrite the previous one, it
  disassociates them and associates a new state, so the MDIB keeps who was attached when.
* the location doubles as a WS-Discovery scope, so changing it re-announces the device and
  a consumer can filter on it before connecting. The patient never leaves the MDIB.

Height and weight are BICEPS Measurements, so each needs a Decimal value and a coded unit.
Race is a BICEPS CodedValue. The dialog does not invent a vocabulary for any of them: enter
an externally chosen code and coding system, using ``mdc``, ``private``, or a full URI.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from sdc11073.xml_types.xml_structure import DateOfBirthProperty

from ..model import (
    Coding,
    LocationInfo,
    PatientInfo,
    PatientMeasurement,
    patient_measurement_wire_value,
)
from .no_wheel import NoWheelComboBox
from .styling import constrain_dynamic_label, mark_as_error, mute

# pm:Sex and pm:PatientType, with the BICEPS code as the data. The empty first entry leaves
# the element out altogether, which is not the same as "Unspecified".
SEX_CAPTIONS = [
    ("not given", ""),
    ("Unspecified", "Unspec"),
    ("Male", "M"),
    ("Female", "F"),
    ("Unknown", "Unkn"),
]

PATIENT_TYPE_CAPTIONS = [
    ("not given", ""),
    ("Unspecified", "Unspec"),
    ("Adult", "Ad"),
    ("Adolescent", "Ado"),
    ("Paediatric", "Ped"),
    ("Infant", "Inf"),
    ("Neonatal", "Neo"),
    ("Other", "Oth"),
]


class ContextDialog(QDialog):
    """Edit the patient and location contexts of our own device."""

    def __init__(
        self,
        location: LocationInfo,
        patient: PatientInfo,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Patient and location")
        self.setMinimumWidth(620)

        self.facility_edit = QLineEdit(location.facility)
        self.building_edit = QLineEdit(location.building)
        self.floor_edit = QLineEdit(location.floor)
        self.poc_edit = QLineEdit(location.point_of_care)
        self.room_edit = QLineEdit(location.room)
        self.bed_edit = QLineEdit(location.bed)

        location_form = QFormLayout()
        # In the hierarchy order SdcLocation uses to build its scope URI.
        location_form.addRow("Facility", self.facility_edit)
        location_form.addRow("Building", self.building_edit)
        location_form.addRow("Floor", self.floor_edit)
        location_form.addRow("Point of care", self.poc_edit)
        location_form.addRow("Room", self.room_edit)
        location_form.addRow("Bed", self.bed_edit)
        location_box = QGroupBox("Location")
        location_box.setLayout(location_form)

        self.given_edit = QLineEdit(patient.given_name)
        self.family_edit = QLineEdit(patient.family_name)
        self.sex_box = NoWheelComboBox()
        for caption, code in SEX_CAPTIONS:
            self.sex_box.addItem(caption, code)
        self._select(self.sex_box, patient.sex)
        self.type_box = NoWheelComboBox()
        for caption, code in PATIENT_TYPE_CAPTIONS:
            self.type_box.addItem(caption, code)
        self._select(self.type_box, patient.patient_type)
        self.birth_edit = QLineEdit(patient.date_of_birth)
        self.birth_edit.setPlaceholderText("1980-04-01, 1980-04 or 1980")

        self.height_value_edit = QLineEdit("" if patient.height is None else str(patient.height.value))
        self.height_unit_code_edit = QLineEdit("" if patient.height is None else patient.height.unit.code)
        self.height_unit_system_edit = QLineEdit("" if patient.height is None else patient.height.unit.system)
        self.height_unit_label_edit = QLineEdit("" if patient.height is None else patient.height.unit.label)
        self.height_widget = self._measurement_widget(
            self.height_value_edit,
            self.height_unit_code_edit,
            self.height_unit_system_edit,
            self.height_unit_label_edit,
        )

        self.weight_value_edit = QLineEdit("" if patient.weight is None else str(patient.weight.value))
        self.weight_unit_code_edit = QLineEdit("" if patient.weight is None else patient.weight.unit.code)
        self.weight_unit_system_edit = QLineEdit("" if patient.weight is None else patient.weight.unit.system)
        self.weight_unit_label_edit = QLineEdit("" if patient.weight is None else patient.weight.unit.label)
        self.weight_widget = self._measurement_widget(
            self.weight_value_edit,
            self.weight_unit_code_edit,
            self.weight_unit_system_edit,
            self.weight_unit_label_edit,
        )

        self.race_code_edit = QLineEdit("" if patient.race is None else patient.race.code)
        self.race_system_edit = QLineEdit("" if patient.race is None else patient.race.system)
        self.race_label_edit = QLineEdit("" if patient.race is None else patient.race.label)
        self.race_widget = self._coding_widget(
            self.race_code_edit,
            self.race_system_edit,
            self.race_label_edit,
            value_name="race code",
        )

        patient_form = QFormLayout()
        patient_form.addRow("Given name", self.given_edit)
        patient_form.addRow("Family name", self.family_edit)
        patient_form.addRow("Sex", self.sex_box)
        patient_form.addRow("Patient type", self.type_box)
        patient_form.addRow("Date of birth", self.birth_edit)
        patient_form.addRow("Height", self.height_widget)
        patient_form.addRow("Weight", self.weight_widget)
        patient_form.addRow("Race", self.race_widget)
        patient_box = QGroupBox("Patient")
        patient_box.setLayout(patient_form)

        self.hint = QLabel(
            "The location is also published as a discovery scope, so changing it "
            "re-announces this device. Clearing every patient field detaches the patient "
            "without attaching another. Height and weight need a value plus coded unit; "
            "race needs a code and coding system.",
        )
        self.hint.setWordWrap(True)
        mute(self.hint)

        self.error_label = QLabel("")
        constrain_dynamic_label(self.error_label, max_lines=3)
        self.error_label.setTextInteractionFlags(Qt.NoTextInteraction)
        mark_as_error(self.error_label)
        self.error_label.hide()

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(location_box)
        layout.addWidget(patient_box)
        layout.addWidget(self.hint)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

    # -- result --------------------------------------------------------------------

    def location(self) -> LocationInfo | None:
        """The location the user entered, or None if the dialog was cancelled."""
        return getattr(self, "_location", None)

    def patient(self) -> PatientInfo | None:
        """The patient the user entered, or None if the dialog was cancelled."""
        return getattr(self, "_patient", None)

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _select(box: QComboBox, code: str) -> None:
        index = box.findData(code)
        box.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _code(box: QComboBox, captions: list[tuple[str, str]]) -> str:
        """The selected BICEPS code.

        Taken from the caption list by index rather than from currentData(), for the same
        reason the alarm dialog does: a value that has been through a QVariant comes back as
        a plain str even when it went in as something else.
        """
        return captions[box.currentIndex()][1]

    @staticmethod
    def _coding_widget(
        code_edit: QLineEdit,
        system_edit: QLineEdit,
        label_edit: QLineEdit,
        *,
        value_name: str,
    ) -> QWidget:
        """Arrange the members of a BICEPS CodedValue without hiding its semantics."""
        code_edit.setPlaceholderText(value_name)
        system_edit.setPlaceholderText("system: mdc, private, or URI")
        label_edit.setPlaceholderText("display label (optional)")
        for edit in (code_edit, system_edit, label_edit):
            edit.setToolTip("Code and coding system are required together; label is for display only.")
        layout = QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(code_edit, 0, 0)
        layout.addWidget(system_edit, 0, 1)
        layout.addWidget(label_edit, 1, 0, 1, 2)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    @staticmethod
    def _measurement_widget(
        value_edit: QLineEdit,
        unit_code_edit: QLineEdit,
        unit_system_edit: QLineEdit,
        unit_label_edit: QLineEdit,
    ) -> QWidget:
        """Arrange a Measurement's value with the coded unit it requires."""
        value_edit.setPlaceholderText("value")
        value_edit.setToolTip("A Decimal value. A measurement is not valid without a coded unit.")
        unit_code_edit.setPlaceholderText("unit code")
        unit_system_edit.setPlaceholderText("unit system: mdc, private, or URI")
        unit_label_edit.setPlaceholderText("unit label (optional)")
        for edit in (unit_code_edit, unit_system_edit, unit_label_edit):
            edit.setToolTip("A measurement unit needs a code and coding system; label is for display only.")
        layout = QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(value_edit, 0, 0)
        layout.addWidget(unit_code_edit, 0, 1)
        layout.addWidget(unit_system_edit, 1, 0)
        layout.addWidget(unit_label_edit, 1, 1)
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    @staticmethod
    def _measurement_from_edits(
        name: str,
        value_edit: QLineEdit,
        code_edit: QLineEdit,
        system_edit: QLineEdit,
        label_edit: QLineEdit,
    ) -> PatientMeasurement | None:
        """Read one complete Measurement, rejecting partial values before an MDIB write."""
        value = value_edit.text().strip()
        code = code_edit.text().strip()
        system = system_edit.text().strip()
        label = label_edit.text().strip()
        if not any((value, code, system, label)):
            return None
        if not value:
            raise ValueError(f"{name} needs a value.")
        if not code:
            raise ValueError(f"{name} needs a unit code.")
        if not system:
            raise ValueError(f"{name} needs a unit coding system.")
        try:
            decimal_value = Decimal(value)
            patient_measurement_wire_value(decimal_value)
            return PatientMeasurement(
                value=decimal_value,
                unit=Coding(code=code, system=system, label=label),
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{name}: {exc}") from exc

    @staticmethod
    def _race_from_edits(
        code_edit: QLineEdit,
        system_edit: QLineEdit,
        label_edit: QLineEdit,
    ) -> Coding | None:
        """Read one complete race CodedValue, rejecting display-only entries."""
        code = code_edit.text().strip()
        system = system_edit.text().strip()
        label = label_edit.text().strip()
        if not any((code, system, label)):
            return None
        if not code:
            raise ValueError("Race needs a code.")
        if not system:
            raise ValueError("Race needs a coding system.")
        try:
            return Coding(code=code, system=system, label=label)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Race: {exc}") from exc

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()

        birth = self.birth_edit.text().strip()
        if birth:
            # Validate here rather than letting it fail inside a context transaction, where
            # the message would name an XSD type rather than the field the user typed in.
            try:
                DateOfBirthProperty.mk_value_object(birth)
            except (ValueError, TypeError):
                self._fail(f"{birth!r} is not a date. Use 1980-04-01, 1980-04 or 1980.")
                return

        try:
            height = self._measurement_from_edits(
                "Height",
                self.height_value_edit,
                self.height_unit_code_edit,
                self.height_unit_system_edit,
                self.height_unit_label_edit,
            )
            weight = self._measurement_from_edits(
                "Weight",
                self.weight_value_edit,
                self.weight_unit_code_edit,
                self.weight_unit_system_edit,
                self.weight_unit_label_edit,
            )
            race = self._race_from_edits(
                self.race_code_edit,
                self.race_system_edit,
                self.race_label_edit,
            )
            patient = PatientInfo(
                given_name=self.given_edit.text().strip(),
                family_name=self.family_edit.text().strip(),
                sex=self._code(self.sex_box, SEX_CAPTIONS),
                patient_type=self._code(self.type_box, PATIENT_TYPE_CAPTIONS),
                date_of_birth=birth,
                height=height,
                weight=weight,
                race=race,
            )
        except ValueError as exc:
            self._fail(str(exc))
            return

        self._location = LocationInfo(
            facility=self.facility_edit.text().strip(),
            building=self.building_edit.text().strip(),
            floor=self.floor_edit.text().strip(),
            point_of_care=self.poc_edit.text().strip(),
            room=self.room_edit.text().strip(),
            bed=self.bed_edit.text().strip(),
        )
        self._patient = patient
        self.accept()
