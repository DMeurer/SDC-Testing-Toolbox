"""The "Patient and location" dialog.

Contexts are the part of BICEPS that says *who* and *where*, as opposed to what the device
is measuring. They are worth showing because they behave unlike everything else in the MDIB:

* they are multi-state. Setting a patient does not overwrite the previous one, it
  disassociates them and associates a new state, so the MDIB keeps who was attached when.
* the location doubles as a WS-Discovery scope, so changing it re-announces the device and
  a consumer can filter on it before connecting. The patient never leaves the MDIB.

The patient fields are a subset of pm:PatientDemographicsCoreData. Height, weight and race
are in the standard and deliberately left out: inviting someone to type a weight into a
learning tool suggests a clinical purpose it has none of.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from sdc11073.xml_types.xml_structure import DateOfBirthProperty

from ..model import LocationInfo, PatientInfo
from .no_wheel import NoWheelComboBox
from .styling import mark_as_error, mute

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
        self.setMinimumWidth(460)

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

        patient_form = QFormLayout()
        patient_form.addRow("Given name", self.given_edit)
        patient_form.addRow("Family name", self.family_edit)
        patient_form.addRow("Sex", self.sex_box)
        patient_form.addRow("Patient type", self.type_box)
        patient_form.addRow("Date of birth", self.birth_edit)
        patient_box = QGroupBox("Patient")
        patient_box.setLayout(patient_form)

        self.hint = QLabel(
            "The location is also published as a discovery scope, so changing it "
            "re-announces this device. Clearing every patient field detaches the patient "
            "without attaching another.",
        )
        self.hint.setWordWrap(True)
        mute(self.hint)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
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

        self._location = LocationInfo(
            facility=self.facility_edit.text().strip(),
            building=self.building_edit.text().strip(),
            floor=self.floor_edit.text().strip(),
            point_of_care=self.poc_edit.text().strip(),
            room=self.room_edit.text().strip(),
            bed=self.bed_edit.text().strip(),
        )
        self._patient = PatientInfo(
            given_name=self.given_edit.text().strip(),
            family_name=self.family_edit.text().strip(),
            sex=self._code(self.sex_box, SEX_CAPTIONS),
            patient_type=self._code(self.type_box, PATIENT_TYPE_CAPTIONS),
            date_of_birth=birth,
        )
        self.accept()
