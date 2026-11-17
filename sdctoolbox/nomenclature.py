"""Common IEEE 11073-10101 (MDC) codes, so a metric can say what it is by picking a name.

The list is our own selection: every code the presets use plus the common vital signs,
ventilation, gas, infusion and unit terms, each checked against the standard and its
amendments. The terms are written here in our own words; the standard's descriptions are
copyrighted and are not reproduced. Users who own the standard can add the rest for a session
with their own ``codes.csv`` export (helpers/Coding Systems), see ``load_csv``.

Codes are the decimal context-free codes BICEPS carries: (partition << 16) + term code.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# What a term is for, read from its partition: 4 holds units, 3 events (alerts), and the
# metric partitions hold what a metric measures or sets. The rest only shows as "other".
KINDS = ("measurement", "unit", "alert", "other")
_MEASUREMENT_PARTITIONS = {1, 2, 128, 258, 514}


def kind_of(code: str) -> str:
    """The kind of a context-free code, from its partition."""
    partition = int(code) >> 16
    if partition == 4:  # noqa: PLR2004 - the units partition
        return "unit"
    if partition == 3:  # noqa: PLR2004 - the events partition
        return "alert"
    return "measurement" if partition in _MEASUREMENT_PARTITIONS else "other"


@dataclass(frozen=True)
class Term:
    """One MDC term: its code, reference ID and a short name; units also have a symbol."""

    code: str
    ref_id: str
    term: str
    symbol: str = ""
    shipped: bool = True

    @property
    def kind(self) -> str:
        return kind_of(self.code)

    @property
    def label(self) -> str:
        """The label a coding gets: the symbol of a unit, else the term."""
        return self.symbol or self.term

    @property
    def caption(self) -> str:
        """What a field shows once this is picked, e.g. "bpm · MDC_DIM_BEAT_PER_MIN (264864)"."""
        return f"{self.symbol or self.term or self.ref_id} · {self.ref_id} ({self.code})"

    @property
    def display(self) -> str:
        """The Term column: the name, with the symbol first for a unit."""
        return f"{self.symbol}, {self.term}" if self.symbol else self.term


# (code, reference ID, term) and, for units, the symbol as a fourth item.
_TABLE: tuple[tuple[str, ...], ...] = (
    # -- heart and pulse
    ("147842", "MDC_ECG_HEART_RATE", "Heart rate, from the ECG"),
    ("149514", "MDC_PULS_RATE", "Pulse rate"),
    ("149530", "MDC_PULS_OXIM_PULS_RATE", "Pulse rate, from pulse oximetry"),
    ("149546", "MDC_PULS_RATE_NON_INV", "Pulse rate, from the blood pressure cuff"),
    ("148065", "MDC_ECG_V_P_C_CNT", "Premature ventricular beats, count"),
    ("148066", "MDC_ECG_V_P_C_RATE", "Premature ventricular beats, rate"),
    ("131328", "MDC_ECG_ELEC_POTL", "ECG signal, lead not specified"),
    ("131329", "MDC_ECG_ELEC_POTL_I", "ECG lead I"),
    ("131330", "MDC_ECG_ELEC_POTL_II", "ECG lead II"),
    ("131389", "MDC_ECG_ELEC_POTL_III", "ECG lead III"),
    ("131390", "MDC_ECG_ELEC_POTL_AVR", "ECG lead aVR"),
    ("131391", "MDC_ECG_ELEC_POTL_AVL", "ECG lead aVL"),
    ("131392", "MDC_ECG_ELEC_POTL_AVF", "ECG lead aVF"),
    ("131331", "MDC_ECG_ELEC_POTL_V1", "ECG lead V1"),
    ("131332", "MDC_ECG_ELEC_POTL_V2", "ECG lead V2"),
    ("131333", "MDC_ECG_ELEC_POTL_V3", "ECG lead V3"),
    ("131334", "MDC_ECG_ELEC_POTL_V4", "ECG lead V4"),
    ("131335", "MDC_ECG_ELEC_POTL_V5", "ECG lead V5"),
    ("131336", "MDC_ECG_ELEC_POTL_V6", "ECG lead V6"),
    ("131841", "MDC_ECG_AMPL_ST_I", "ST level, lead I"),
    ("131842", "MDC_ECG_AMPL_ST_II", "ST level, lead II"),
    ("131901", "MDC_ECG_AMPL_ST_III", "ST level, lead III"),
    ("147232", "MDC_ECG_TIME_PD_QT_GL", "QT interval, all leads"),
    ("147228", "MDC_ECG_TIME_PD_QRS_GL", "QRS duration, all leads"),
    ("150276", "MDC_OUTPUT_CARD", "Cardiac output"),
    ("149772", "MDC_OUTPUT_CARD_INDEX", "Cardiac index"),
    # -- pulse oximetry and oxygen
    ("150456", "MDC_PULS_OXIM_SAT_O2", "SpO2, oxygen saturation from pulse oximetry"),
    ("150452", "MDC_PULS_OXIM_PLETH", "Plethysmogram, the pulse oximetry wave"),
    ("150448", "MDC_PULS_OXIM_PERF_REL", "Relative perfusion, from pulse oximetry"),
    ("150488", "MDC_BLD_PERF_INDEX", "Perfusion index"),
    ("150324", "MDC_SAT_O2_ART", "Arterial oxygen saturation"),
    ("150332", "MDC_SAT_O2_VEN", "Venous oxygen saturation"),
    ("160352", "MDC_SAT_O2_VEN_CENT", "Central venous oxygen saturation"),
    ("150688", "MDC_SAT_O2_REGIONAL", "Regional oxygen saturation (tissue oximetry)"),
    # -- blood pressure
    ("150016", "MDC_PRESS_BLD", "Blood pressure, site not specified"),
    ("150017", "MDC_PRESS_BLD_SYS", "Blood pressure, systolic"),
    ("150018", "MDC_PRESS_BLD_DIA", "Blood pressure, diastolic"),
    ("150019", "MDC_PRESS_BLD_MEAN", "Blood pressure, mean"),
    ("150020", "MDC_PRESS_BLD_NONINV", "Non-invasive blood pressure (NIBP)"),
    ("150021", "MDC_PRESS_BLD_NONINV_SYS", "NIBP, systolic"),
    ("150022", "MDC_PRESS_BLD_NONINV_DIA", "NIBP, diastolic"),
    ("150023", "MDC_PRESS_BLD_NONINV_MEAN", "NIBP, mean"),
    ("150032", "MDC_PRESS_BLD_ART", "Arterial blood pressure"),
    ("150033", "MDC_PRESS_BLD_ART_SYS", "Arterial blood pressure, systolic"),
    ("150034", "MDC_PRESS_BLD_ART_DIA", "Arterial blood pressure, diastolic"),
    ("150035", "MDC_PRESS_BLD_ART_MEAN", "Arterial blood pressure, mean"),
    ("150036", "MDC_PRESS_BLD_ART_ABP", "Arterial line pressure (ABP)"),
    ("150037", "MDC_PRESS_BLD_ART_ABP_SYS", "Arterial line pressure, systolic"),
    ("150038", "MDC_PRESS_BLD_ART_ABP_DIA", "Arterial line pressure, diastolic"),
    ("150039", "MDC_PRESS_BLD_ART_ABP_MEAN", "Arterial line pressure, mean"),
    ("150044", "MDC_PRESS_BLD_ART_PULM", "Pulmonary artery pressure"),
    ("150045", "MDC_PRESS_BLD_ART_PULM_SYS", "Pulmonary artery pressure, systolic"),
    ("150046", "MDC_PRESS_BLD_ART_PULM_DIA", "Pulmonary artery pressure, diastolic"),
    ("150047", "MDC_PRESS_BLD_ART_PULM_MEAN", "Pulmonary artery pressure, mean"),
    ("150052", "MDC_PRESS_BLD_ART_PULM_WEDGE", "Pulmonary wedge pressure"),
    ("150084", "MDC_PRESS_BLD_VEN_CENT", "Central venous pressure (CVP)"),
    ("150087", "MDC_PRESS_BLD_VEN_CENT_MEAN", "Central venous pressure, mean"),
    ("150064", "MDC_PRESS_BLD_ATR_LEFT", "Left atrial pressure"),
    ("150068", "MDC_PRESS_BLD_ATR_RIGHT", "Right atrial pressure"),
    ("153608", "MDC_PRESS_INTRA_CRAN", "Intracranial pressure (ICP)"),
    ("153611", "MDC_PRESS_INTRA_CRAN_MEAN", "Intracranial pressure, mean"),
    ("153604", "MDC_PRESS_CEREB_PERF", "Cerebral perfusion pressure"),
    # -- temperature
    ("150344", "MDC_TEMP", "Temperature, site not specified"),
    ("150364", "MDC_TEMP_BODY", "Body temperature"),
    ("150368", "MDC_TEMP_CORE", "Core temperature"),
    ("150388", "MDC_TEMP_SKIN", "Skin temperature"),
    ("150372", "MDC_TEMP_ESOPH", "Oesophageal temperature"),
    ("150380", "MDC_TEMP_NASOPH", "Nasopharyngeal temperature"),
    ("150392", "MDC_TEMP_TYMP", "Eardrum (tympanic) temperature"),
    ("188428", "MDC_TEMP_EAR", "Ear temperature"),
    ("188420", "MDC_TEMP_RECT", "Rectal temperature"),
    ("188424", "MDC_TEMP_ORAL", "Oral temperature"),
    ("150348", "MDC_TEMP_FOLEY", "Bladder temperature, from a urinary catheter"),
    ("188436", "MDC_TEMP_BLD", "Blood temperature"),
    ("150352", "MDC_TEMP_ART", "Arterial blood temperature"),
    ("150396", "MDC_TEMP_VEN", "Venous blood temperature"),
    ("150356", "MDC_TEMP_AWAY", "Airway gas temperature"),
    ("188440", "MDC_TEMP_DIFF", "Temperature difference"),
    ("188508", "MDC_TEMP_ROOM", "Room temperature"),
    ("188416", "MDC_TEMP_DEVICE_INTERNAL", "Temperature inside the device"),
    ("184296", "MDC_TEMP_MICROENV", "Air temperature around the patient, e.g. in an incubator"),
    ("16927604", "MDC_TEMP_SKIN_SETTING", "Skin temperature, setting"),
    # -- breathing
    ("151554", "MDC_RESP_RATE", "Respiration rate"),
    ("151570", "MDC_AWAY_RESP_RATE", "Respiration rate, from airway flow or pressure"),
    ("151594", "MDC_CO2_RESP_RATE", "Respiration rate, from CO2"),
    ("151578", "MDC_TTHOR_RESP_RATE", "Respiration rate, from chest impedance"),
    ("151780", "MDC_IMPED_TTHOR", "Chest impedance (respiration wave)"),
    ("151586", "MDC_VENT_RESP_RATE", "Respiration rate delivered by the ventilator"),
    ("16928802", "MDC_VENT_RESP_RATE_SETTING", "Ventilator respiration rate, setting"),
    # -- airway pressure, flow and volume
    ("151792", "MDC_PRESS_AWAY", "Airway pressure"),
    ("151795", "MDC_PRESS_AWAY_MEAN", "Airway pressure, mean"),
    ("151817", "MDC_PRESS_AWAY_INSP_MAX", "Peak inspiratory pressure"),
    ("151819", "MDC_PRESS_AWAY_INSP_MEAN", "Inspiratory pressure, mean"),
    ("151784", "MDC_PRESS_RESP_PLAT", "Plateau pressure"),
    ("151804", "MDC_PRESS_AWAY_END_EXP_POS", "PEEP, positive end-expiratory pressure"),
    ("151808", "MDC_PRESS_AWAY_END_EXP_POS_INTRINSIC", "Intrinsic PEEP"),
    ("16929020", "MDC_PRESS_AWAY_END_EXP_POS_SETTING", "PEEP, setting"),
    ("151956", "MDC_VENT_PRESS", "Ventilator pressure"),
    ("16929188", "MDC_VENT_PRESS_AWAY_SETTING", "Ventilator airway pressure, setting"),
    ("16929948", "MDC_VENT_PRESS_AWAY_SUPP_SETTING", "Pressure support, setting"),
    ("16929968", "MDC_VENT_PRESS_AWAY_LIMIT_PMAX_SETTING", "Maximum airway pressure limit, setting"),
    ("16929644", "MDC_VENT_PRESS_TRIG_SENS_SETTING", "Pressure trigger sensitivity, setting"),
    ("16930020", "MDC_VENT_FLOW_TRIG_SENS_SETTING", "Flow trigger sensitivity, setting"),
    ("151764", "MDC_FLOW_AWAY", "Airway flow"),
    ("151772", "MDC_FLOW_AWAY_INSP", "Inspiratory flow"),
    ("151773", "MDC_FLOW_AWAY_INSP_MAX", "Peak inspiratory flow"),
    ("151768", "MDC_FLOW_AWAY_EXP", "Expiratory flow"),
    ("151769", "MDC_FLOW_AWAY_EXP_MAX", "Peak expiratory flow"),
    ("16929164", "MDC_VENT_FLOW_INSP_SETTING", "Inspiratory flow, setting"),
    ("152708", "MDC_VOL_AWAY", "Airway volume"),
    ("151868", "MDC_VOL_AWAY_TIDAL", "Tidal volume"),
    ("152660", "MDC_VOL_AWAY_TIDAL_INSP", "Tidal volume, inspired"),
    ("152664", "MDC_VOL_AWAY_TIDAL_EXP", "Tidal volume, expired"),
    ("16929196", "MDC_VENT_VOL_TIDAL_SETTING", "Tidal volume, setting"),
    ("151880", "MDC_VOL_MINUTE_AWAY", "Minute volume"),
    ("151888", "MDC_VOL_MINUTE_AWAY_INSP", "Minute volume, inspired"),
    ("151884", "MDC_VOL_MINUTE_AWAY_EXP", "Minute volume, expired"),
    ("16929224", "MDC_VENT_VOL_MINUTE_AWAY_SETTING", "Minute volume, setting"),
    ("152432", "MDC_VENT_VOL_LEAK", "Leak volume"),
    ("151688", "MDC_COMPL_LUNG", "Lung compliance"),
    ("151692", "MDC_COMPL_LUNG_DYN", "Lung compliance, dynamic"),
    ("151696", "MDC_COMPL_LUNG_STATIC", "Lung compliance, static"),
    ("151840", "MDC_RES_AWAY", "Airway resistance"),
    ("151848", "MDC_RES_AWAY_INSP", "Airway resistance, inspiratory"),
    ("151844", "MDC_RES_AWAY_EXP", "Airway resistance, expiratory"),
    ("151832", "MDC_RATIO_IE", "I:E ratio, inspiration to expiration"),
    ("16929048", "MDC_RATIO_IE_SETTING", "I:E ratio, setting"),
    ("152608", "MDC_TIME_PD_INSP", "Inspiration time"),
    ("152612", "MDC_TIME_PD_EXP", "Expiration time"),
    ("16929632", "MDC_VENT_TIME_PD_INSP_SETTING", "Inspiration time, setting"),
    ("184352", "MDC_VENT_MODE", "Ventilation mode"),
    # -- gases
    ("151724", "MDC_AWAY_CO2", "CO2 in the airway (capnogram)"),
    ("151728", "MDC_AWAY_CO2_ET", "End-tidal CO2"),
    ("151736", "MDC_AWAY_CO2_INSP", "Inspired CO2"),
    ("151700", "MDC_CONC_AWAY_CO2", "CO2 concentration in the airway"),
    ("151708", "MDC_CONC_AWAY_CO2_ET", "End-tidal CO2 concentration"),
    ("151716", "MDC_CONC_AWAY_CO2_INSP", "Inspired CO2 concentration"),
    ("152196", "MDC_CONC_AWAY_O2_INSP", "Inspired oxygen concentration (FiO2)"),
    ("152440", "MDC_CONC_AWAY_O2_ET", "End-tidal oxygen concentration"),
    ("16930360", "MDC_CONC_GASDLV_O2_INSP_SETTING", "Delivered oxygen concentration, setting"),
    ("152108", "MDC_CONC_AWAY_N2O_ET", "End-tidal nitrous oxide"),
    ("152192", "MDC_CONC_AWAY_N2O_INSP", "Inspired nitrous oxide"),
    ("152036", "MDC_CONC_AWAY_SEVOFL", "Sevoflurane concentration"),
    ("152096", "MDC_CONC_AWAY_SEVOFL_ET", "End-tidal sevoflurane"),
    ("152180", "MDC_CONC_AWAY_SEVOFL_INSP", "Inspired sevoflurane"),
    ("16929312", "MDC_CONC_AWAY_SEVOFL_ET_SETTING", "End-tidal sevoflurane, setting"),
    ("152024", "MDC_CONC_AWAY_DESFL", "Desflurane concentration"),
    ("152084", "MDC_CONC_AWAY_DESFL_ET", "End-tidal desflurane"),
    ("152168", "MDC_CONC_AWAY_DESFL_INSP", "Inspired desflurane"),
    ("152040", "MDC_CONC_AWAY_ISOFL", "Isoflurane concentration"),
    ("152100", "MDC_CONC_AWAY_ISOFL_ET", "End-tidal isoflurane"),
    ("152184", "MDC_CONC_AWAY_ISOFL_INSP", "Inspired isoflurane"),
    ("152456", "MDC_CONC_AWAY_AGENT", "Anaesthetic agent concentration"),
    ("152460", "MDC_CONC_AWAY_AGENT_ET", "End-tidal anaesthetic agent"),
    ("152464", "MDC_CONC_AWAY_AGENT_INSP", "Inspired anaesthetic agent"),
    ("152864", "MDC_CONC_MAC_SUM", "MAC, all agents together"),
    # -- infusion
    ("157784", "MDC_FLOW_FLUID_PUMP", "Infusion rate"),
    ("16935000", "MDC_FLOW_FLUID_PUMP_SETTING", "Infusion rate, setting"),
    ("157780", "MDC_FLOW_FLUID_BOLUS", "Bolus rate"),
    ("157860", "MDC_VOL_FLUID_BOLUS", "Bolus volume"),
    ("157884", "MDC_VOL_FLUID_TBI", "Volume to be infused"),
    ("157872", "MDC_VOL_FLUID_TBI_REMAIN", "Volume still to be infused"),
    ("157864", "MDC_VOL_FLUID_DELIV", "Volume infused"),
    ("157993", "MDC_VOL_FLUID_DELIV_TOTAL", "Volume infused, total"),
    ("157916", "MDC_TIME_PD_REMAIN", "Infusion time remaining"),
    ("158120", "MDC_PRESS_FLUID_LINE_DOWNSTREAM", "Line pressure, patient side of the pump"),
    ("157760", "MDC_CONC_DRUG", "Drug concentration"),
    ("157924", "MDC_RATE_DOSE", "Dose rate"),
    ("157816", "MDC_DOSE_DRUG_BOLUS", "Bolus dose"),
    ("157999", "MDC_DOSE_DRUG_TBI", "Drug dose to be infused"),
    ("158001", "MDC_DOSE_DRUG_DELIV_TOTAL", "Drug dose infused, total"),
    ("184514", "MDC_DRUG_NAME_LABEL", "Drug name"),
    ("184515", "MDC_DRUG_ID", "Drug identifier"),
    ("184504", "MDC_PUMP_MODE", "Pump mode"),
    ("184508", "MDC_PUMP_STAT", "Pump status"),
    # -- brain, blood and body
    ("153644", "MDC_EEG_BISPECTRAL_INDEX", "Bispectral index (BIS)"),
    ("153652", "MDC_EEG_ENTROPY_STATE", "State entropy, EEG"),
    ("153648", "MDC_EEG_ENTROPY_RESPONSE", "Response entropy, EEG"),
    ("160020", "MDC_CONC_GLU_GEN", "Glucose concentration"),
    ("160184", "MDC_CONC_GLU_CAPILLARY_WHOLEBLOOD", "Glucose, capillary whole blood"),
    ("160120", "MDC_CONC_HB_GEN", "Haemoglobin concentration"),
    ("159764", "MDC_CONC_HB_ART", "Haemoglobin, arterial"),
    ("159748", "MDC_CONC_PH_ART", "pH, arterial"),
    ("159752", "MDC_CONC_PCO2_ART", "CO2 partial pressure, arterial"),
    ("159756", "MDC_CONC_PO2_ART", "Oxygen partial pressure, arterial"),
    ("188736", "MDC_MASS_BODY_ACTUAL", "Body weight"),
    ("188740", "MDC_LEN_BODY_ACTUAL", "Body height"),
    ("188752", "MDC_RATIO_MASS_BODY_LEN_SQ", "Body mass index (BMI)"),
    ("68063", "MDC_ATTR_PT_WEIGHT", "Patient weight"),
    ("68060", "MDC_ATTR_PT_HEIGHT", "Patient height"),
    ("68056", "MDC_ATTR_PT_AGE", "Patient age"),
    # -- the device itself
    ("68024", "MDC_ATTR_CAPAC_BATT_REMAIN", "Battery charge left"),
    ("67976", "MDC_ATTR_TIME_BATT_REMAIN", "Battery time left"),
    ("68021", "MDC_ATTR_BATT_VOLTAGE", "Battery voltage"),
    # -- units: plain numbers
    ("262656", "MDC_DIM_DIMLESS", "no unit, a plain number", "1"),
    ("262688", "MDC_DIM_PERCENT", "percent", "%"),
    ("262752", "MDC_DIM_PARTS_PER_MILLION", "parts per million", "ppm"),
    ("268384", "MDC_DIM_VOL_PERCENT", "percent by volume", "vol%"),
    ("263136", "MDC_DIM_PH", "pH value", "pH"),
    # -- units: length, angle, area
    ("263424", "MDC_DIM_X_M", "metre", "m"),
    ("263441", "MDC_DIM_CENTI_M", "centimetre", "cm"),
    ("263442", "MDC_DIM_MILLI_M", "millimetre", "mm"),
    ("263443", "MDC_DIM_MICRO_M", "micrometre", "µm"),
    ("262880", "MDC_DIM_ANG_DEG", "degree of angle", "°"),
    ("262912", "MDC_DIM_ANG_RAD", "radian", "rad"),
    # -- units: mass and volume
    ("263872", "MDC_DIM_X_G", "gram", "g"),
    ("263875", "MDC_DIM_KILO_G", "kilogram", "kg"),
    ("263890", "MDC_DIM_MILLI_G", "milligram", "mg"),
    ("263891", "MDC_DIM_MICRO_G", "microgram", "µg"),
    ("263744", "MDC_DIM_X_L", "litre", "L"),
    ("263762", "MDC_DIM_MILLI_L", "millilitre", "mL"),
    ("263763", "MDC_DIM_MICRO_L", "microlitre", "µL"),
    ("265330", "MDC_DIM_MILLI_L_PER_KG", "millilitre per kilogram", "mL/kg"),
    ("269891", "MDC_DIM_KILO_G_PER_M_SQ", "kilogram per square metre", "kg/m²"),
    # -- units: time and rates
    ("264320", "MDC_DIM_X_SEC", "second", "s"),
    ("264338", "MDC_DIM_MILLI_SEC", "millisecond", "ms"),
    ("264339", "MDC_DIM_MICRO_SEC", "microsecond", "µs"),
    ("264352", "MDC_DIM_MIN", "minute", "min"),
    ("264384", "MDC_DIM_HR", "hour", "h"),
    ("264416", "MDC_DIM_DAY", "day", "d"),
    ("264640", "MDC_DIM_X_HZ", "hertz", "Hz"),
    ("264643", "MDC_DIM_KILO_HZ", "kilohertz", "kHz"),
    ("264644", "MDC_DIM_MEGA_HZ", "megahertz", "MHz"),
    ("264672", "MDC_DIM_PER_MIN", "per minute", "1/min"),
    ("264864", "MDC_DIM_BEAT_PER_MIN", "beats per minute", "bpm"),
    ("264928", "MDC_DIM_RESP_PER_MIN", "breaths per minute", "breaths/min"),
    ("270240", "MDC_DIM_X_ROTATIONS_PER_MIN", "revolutions per minute", "rpm"),
    ("264960", "MDC_DIM_X_M_PER_SEC", "metre per second", "m/s"),
    ("264978", "MDC_DIM_MILLI_M_PER_SEC", "millimetre per second", "mm/s"),
    # -- units: flow
    ("265216", "MDC_DIM_X_L_PER_MIN", "litre per minute", "L/min"),
    ("265184", "MDC_DIM_X_L_PER_SEC", "litre per second", "L/s"),
    ("265202", "MDC_DIM_MILLI_L_PER_SEC", "millilitre per second", "mL/s"),
    ("265266", "MDC_DIM_MILLI_L_PER_HR", "millilitre per hour", "mL/h"),
    ("264992", "MDC_DIM_L_PER_MIN_PER_M_SQ", "litre per minute per square metre", "L/min/m²"),
    # -- units: drug doses
    ("267616", "MDC_DIM_X_INTL_UNIT", "international unit", "IU"),
    ("265490", "MDC_DIM_MILLI_G_PER_MIN", "milligram per minute", "mg/min"),
    ("265522", "MDC_DIM_MILLI_G_PER_HR", "milligram per hour", "mg/h"),
    ("265491", "MDC_DIM_MICRO_G_PER_MIN", "microgram per minute", "µg/min"),
    ("265523", "MDC_DIM_MICRO_G_PER_HR", "microgram per hour", "µg/h"),
    ("265650", "MDC_DIM_MILLI_G_PER_KG_PER_HR", "milligram per kilogram per hour", "mg/kg/h"),
    ("265619", "MDC_DIM_MICRO_G_PER_KG_PER_MIN", "microgram per kilogram per minute", "µg/kg/min"),
    # -- units: concentration
    ("264274", "MDC_DIM_MILLI_G_PER_DL", "milligram per decilitre", "mg/dL"),
    ("264256", "MDC_DIM_G_PER_DL", "gram per decilitre", "g/dL"),
    ("266866", "MDC_DIM_MILLI_MOLE_PER_L", "millimole per litre", "mmol/L"),
    ("266994", "MDC_DIM_MILLI_EQUIV_PER_L", "milliequivalent per litre", "mEq/L"),
    # -- units: pressure
    ("266016", "MDC_DIM_MMHG", "millimetre of mercury", "mmHg"),
    ("266048", "MDC_DIM_CM_H2O", "centimetre of water", "cmH2O"),
    ("265984", "MDC_DIM_X_PASCAL", "pascal", "Pa"),
    ("265986", "MDC_DIM_HECTO_PASCAL", "hectopascal", "hPa"),
    ("265987", "MDC_DIM_KILO_PASCAL", "kilopascal", "kPa"),
    ("266080", "MDC_DIM_X_BAR", "bar", "bar"),
    ("266098", "MDC_DIM_MILLI_BAR", "millibar", "mbar"),
    # -- units: lung mechanics
    ("268050", "MDC_DIM_MILLI_L_PER_CM_H2O", "millilitre per centimetre of water", "mL/cmH2O"),
    ("268064", "MDC_DIM_CM_H2O_PER_L_PER_SEC", "centimetre of water per litre per second", "cmH2O/(L/s)"),
    # -- units: temperature
    ("268192", "MDC_DIM_DEGC", "degree Celsius", "°C"),
    ("266560", "MDC_DIM_FAHR", "degree Fahrenheit", "°F"),
    ("266528", "MDC_DIM_KELVIN", "kelvin", "K"),
    # -- units: electrical, force, energy, sound
    ("266400", "MDC_DIM_X_VOLT", "volt", "V"),
    ("266418", "MDC_DIM_MILLI_VOLT", "millivolt", "mV"),
    ("266419", "MDC_DIM_MICRO_VOLT", "microvolt", "µV"),
    ("266304", "MDC_DIM_X_AMPS", "ampere", "A"),
    ("266322", "MDC_DIM_MILLI_AMPS", "milliampere", "mA"),
    ("266432", "MDC_DIM_X_OHM", "ohm", "Ω"),
    ("266435", "MDC_DIM_KILO_OHM", "kilohm", "kΩ"),
    ("266176", "MDC_DIM_X_WATT", "watt", "W"),
    ("266194", "MDC_DIM_MILLI_WATT", "milliwatt", "mW"),
    ("266112", "MDC_DIM_X_JOULES", "joule", "J"),
    ("265920", "MDC_DIM_X_NEWTON", "newton", "N"),
    ("268576", "MDC_DIM_DECIBEL", "decibel", "dB"),
    # -- alerts: limits
    ("196648", "MDC_EVT_HI", "Value is high"),
    ("196670", "MDC_EVT_LO", "Value is low"),
    ("196650", "MDC_EVT_HI_GT_LIM", "Value above its upper limit"),
    ("196672", "MDC_EVT_LO_LT_LIM", "Value below its lower limit"),
    # -- alerts: patient
    ("199680", "MDC_EVT_APNEA", "Apnoea, breathing has stopped"),
    ("199854", "MDC_EVT_DESAT", "Oxygen desaturation"),
    ("199684", "MDC_EVT_ECG_ASYSTOLE", "Asystole"),
    ("199806", "MDC_EVT_ECG_V_FIB", "Ventricular fibrillation"),
    ("199832", "MDC_EVT_ECG_V_TACHY", "Ventricular tachycardia"),
    ("199736", "MDC_EVT_ECG_ATR_FIB", "Atrial fibrillation"),
    ("199728", "MDC_EVT_ECG_TACHY", "Tachycardia, heart too fast"),
    ("199932", "MDC_EVT_ECG_BRADY", "Bradycardia, heart too slow"),
    # -- alerts: technical
    ("196880", "MDC_EVT_LEAD_OFF", "ECG lead off"),
    ("196876", "MDC_EVT_LEAD_DISCONN", "ECG lead disconnected"),
    ("196916", "MDC_EVT_SENSOR_DISCONN", "Sensor disconnected"),
    ("196918", "MDC_EVT_SENSOR_MALF", "Sensor faulty"),
    ("196630", "MDC_EVT_DISCONN", "Disconnected"),
    ("196688", "MDC_EVT_OCCL", "Occlusion, line blocked"),
    ("197200", "MDC_EVT_FLUID_LINE_AIR", "Air in the fluid line"),
    ("196634", "MDC_EVT_EMPTY", "Empty"),
    ("197244", "MDC_EVT_KVO", "Keep-vein-open rate running"),
    ("196668", "MDC_EVT_LEAK", "Leak"),
    ("196950", "MDC_EVT_FLOW_LO", "Flow too low"),
    ("198090", "MDC_EVT_TEMP_HI", "Temperature too high"),
    ("196976", "MDC_EVT_PRESS_HI_GT_LIM", "Pressure above its limit"),
    ("197776", "MDC_EVT_IMPED_HI", "Impedance too high"),
    ("198018", "MDC_EVT_SENSOR_CHK", "Check the sensor"),
    ("197332", "MDC_EVT_VOL_INFUS_COMP", "Volume infused, infusion complete"),
    ("203341", "MDC_EVT_ADVIS_MAINT_NEEDED", "Maintenance needed"),
    ("196682", "MDC_EVT_NOISY", "Signal too noisy"),
    ("196660", "MDC_EVT_INOP", "Measurement not working"),
    ("196678", "MDC_EVT_MALF", "Malfunction"),
    ("196850", "MDC_EVT_EQUIP_MALF", "Equipment malfunction"),
    ("196802", "MDC_EVT_BATT_LO", "Battery low"),
    ("197336", "MDC_EVT_BATT_DEPL", "Battery empty"),
    ("196800", "MDC_EVT_BATT_FAIL", "Battery failed"),
    ("202828", "MDC_EVT_STAT_DOOR_OPEN", "Door open"),
)

SHIPPED: tuple[Term, ...] = tuple(Term(*row) for row in _TABLE)

# Our own pick of the usual units for the shipped measurements, so the unit picker can offer
# just those. A measurement missing here (a mode, a name, an age) offers every unit.
_PRESSURE_BLOOD = ("MDC_DIM_MMHG", "MDC_DIM_KILO_PASCAL")
_PRESSURE_GAS = ("MDC_DIM_CM_H2O", "MDC_DIM_MILLI_BAR", "MDC_DIM_HECTO_PASCAL", "MDC_DIM_KILO_PASCAL")
_GAS_SHARE = ("MDC_DIM_PERCENT", "MDC_DIM_VOL_PERCENT")
_UNIT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ECG_HEART_RATE PULS_RATE PULS_OXIM_PULS_RATE PULS_RATE_NON_INV ECG_V_P_C_RATE",
     ("MDC_DIM_BEAT_PER_MIN", "MDC_DIM_PER_MIN")),
    ("ECG_V_P_C_CNT PULS_OXIM_PLETH RATIO_IE RATIO_IE_SETTING CONC_MAC_SUM EEG_BISPECTRAL_INDEX "
     "EEG_ENTROPY_STATE EEG_ENTROPY_RESPONSE", ("MDC_DIM_DIMLESS",)),
    ("ECG_ELEC_POTL ECG_ELEC_POTL_I ECG_ELEC_POTL_II ECG_ELEC_POTL_III ECG_ELEC_POTL_AVR ECG_ELEC_POTL_AVL "
     "ECG_ELEC_POTL_AVF ECG_ELEC_POTL_V1 ECG_ELEC_POTL_V2 ECG_ELEC_POTL_V3 ECG_ELEC_POTL_V4 ECG_ELEC_POTL_V5 "
     "ECG_ELEC_POTL_V6 ECG_AMPL_ST_I ECG_AMPL_ST_II ECG_AMPL_ST_III", ("MDC_DIM_MILLI_VOLT", "MDC_DIM_MICRO_VOLT")),
    ("ECG_TIME_PD_QT_GL ECG_TIME_PD_QRS_GL", ("MDC_DIM_MILLI_SEC", "MDC_DIM_X_SEC")),
    ("OUTPUT_CARD", ("MDC_DIM_X_L_PER_MIN",)),
    ("OUTPUT_CARD_INDEX", ("MDC_DIM_L_PER_MIN_PER_M_SQ",)),
    ("PULS_OXIM_SAT_O2 SAT_O2_ART SAT_O2_VEN SAT_O2_VEN_CENT SAT_O2_REGIONAL ATTR_CAPAC_BATT_REMAIN",
     ("MDC_DIM_PERCENT",)),
    ("PULS_OXIM_PERF_REL BLD_PERF_INDEX", ("MDC_DIM_PERCENT", "MDC_DIM_DIMLESS")),
    ("PRESS_BLD PRESS_BLD_SYS PRESS_BLD_DIA PRESS_BLD_MEAN PRESS_BLD_NONINV PRESS_BLD_NONINV_SYS "
     "PRESS_BLD_NONINV_DIA PRESS_BLD_NONINV_MEAN PRESS_BLD_ART PRESS_BLD_ART_SYS PRESS_BLD_ART_DIA "
     "PRESS_BLD_ART_MEAN PRESS_BLD_ART_ABP PRESS_BLD_ART_ABP_SYS PRESS_BLD_ART_ABP_DIA PRESS_BLD_ART_ABP_MEAN "
     "PRESS_BLD_ART_PULM PRESS_BLD_ART_PULM_SYS PRESS_BLD_ART_PULM_DIA PRESS_BLD_ART_PULM_MEAN "
     "PRESS_BLD_ART_PULM_WEDGE PRESS_BLD_ATR_LEFT PRESS_BLD_ATR_RIGHT PRESS_CEREB_PERF "
     "AWAY_CO2 AWAY_CO2_ET AWAY_CO2_INSP CONC_PCO2_ART CONC_PO2_ART", _PRESSURE_BLOOD),
    ("PRESS_BLD_VEN_CENT PRESS_BLD_VEN_CENT_MEAN PRESS_INTRA_CRAN PRESS_INTRA_CRAN_MEAN",
     (*_PRESSURE_BLOOD, "MDC_DIM_CM_H2O")),
    ("TEMP TEMP_BODY TEMP_CORE TEMP_SKIN TEMP_ESOPH TEMP_NASOPH TEMP_TYMP TEMP_EAR TEMP_RECT TEMP_ORAL "
     "TEMP_FOLEY TEMP_BLD TEMP_ART TEMP_VEN TEMP_AWAY TEMP_DIFF TEMP_ROOM TEMP_DEVICE_INTERNAL TEMP_MICROENV "
     "TEMP_SKIN_SETTING", ("MDC_DIM_DEGC", "MDC_DIM_FAHR", "MDC_DIM_KELVIN")),
    ("RESP_RATE AWAY_RESP_RATE CO2_RESP_RATE TTHOR_RESP_RATE VENT_RESP_RATE VENT_RESP_RATE_SETTING",
     ("MDC_DIM_RESP_PER_MIN", "MDC_DIM_PER_MIN")),
    ("IMPED_TTHOR", ("MDC_DIM_X_OHM", "MDC_DIM_KILO_OHM")),
    ("PRESS_AWAY PRESS_AWAY_MEAN PRESS_AWAY_INSP_MAX PRESS_AWAY_INSP_MEAN PRESS_RESP_PLAT "
     "PRESS_AWAY_END_EXP_POS PRESS_AWAY_END_EXP_POS_INTRINSIC PRESS_AWAY_END_EXP_POS_SETTING VENT_PRESS "
     "VENT_PRESS_AWAY_SETTING VENT_PRESS_AWAY_SUPP_SETTING VENT_PRESS_AWAY_LIMIT_PMAX_SETTING "
     "VENT_PRESS_TRIG_SENS_SETTING", _PRESSURE_GAS),
    ("FLOW_AWAY FLOW_AWAY_INSP FLOW_AWAY_INSP_MAX FLOW_AWAY_EXP FLOW_AWAY_EXP_MAX VENT_FLOW_INSP_SETTING "
     "VENT_FLOW_TRIG_SENS_SETTING", ("MDC_DIM_X_L_PER_MIN", "MDC_DIM_X_L_PER_SEC", "MDC_DIM_MILLI_L_PER_SEC")),
    ("VOL_AWAY VENT_VOL_LEAK", ("MDC_DIM_MILLI_L", "MDC_DIM_X_L")),
    ("VOL_AWAY_TIDAL VOL_AWAY_TIDAL_INSP VOL_AWAY_TIDAL_EXP VENT_VOL_TIDAL_SETTING",
     ("MDC_DIM_MILLI_L", "MDC_DIM_X_L", "MDC_DIM_MILLI_L_PER_KG")),
    ("VOL_MINUTE_AWAY VOL_MINUTE_AWAY_INSP VOL_MINUTE_AWAY_EXP VENT_VOL_MINUTE_AWAY_SETTING",
     ("MDC_DIM_X_L_PER_MIN",)),
    ("COMPL_LUNG COMPL_LUNG_DYN COMPL_LUNG_STATIC", ("MDC_DIM_MILLI_L_PER_CM_H2O",)),
    ("RES_AWAY RES_AWAY_INSP RES_AWAY_EXP", ("MDC_DIM_CM_H2O_PER_L_PER_SEC",)),
    ("TIME_PD_INSP TIME_PD_EXP VENT_TIME_PD_INSP_SETTING", ("MDC_DIM_X_SEC", "MDC_DIM_MILLI_SEC")),
    ("CONC_AWAY_CO2 CONC_AWAY_CO2_ET CONC_AWAY_CO2_INSP CONC_AWAY_O2_INSP CONC_AWAY_O2_ET "
     "CONC_GASDLV_O2_INSP_SETTING CONC_AWAY_N2O_ET CONC_AWAY_N2O_INSP CONC_AWAY_SEVOFL CONC_AWAY_SEVOFL_ET "
     "CONC_AWAY_SEVOFL_INSP CONC_AWAY_SEVOFL_ET_SETTING CONC_AWAY_DESFL CONC_AWAY_DESFL_ET CONC_AWAY_DESFL_INSP "
     "CONC_AWAY_ISOFL CONC_AWAY_ISOFL_ET CONC_AWAY_ISOFL_INSP CONC_AWAY_AGENT CONC_AWAY_AGENT_ET "
     "CONC_AWAY_AGENT_INSP", _GAS_SHARE),
    ("FLOW_FLUID_PUMP FLOW_FLUID_PUMP_SETTING FLOW_FLUID_BOLUS", ("MDC_DIM_MILLI_L_PER_HR",)),
    ("VOL_FLUID_BOLUS VOL_FLUID_TBI VOL_FLUID_TBI_REMAIN VOL_FLUID_DELIV VOL_FLUID_DELIV_TOTAL",
     ("MDC_DIM_MILLI_L", "MDC_DIM_MICRO_L", "MDC_DIM_X_L")),
    ("TIME_PD_REMAIN ATTR_TIME_BATT_REMAIN", ("MDC_DIM_MIN", "MDC_DIM_HR", "MDC_DIM_X_SEC")),
    ("PRESS_FLUID_LINE_DOWNSTREAM", ("MDC_DIM_MMHG", "MDC_DIM_KILO_PASCAL", "MDC_DIM_MILLI_BAR")),
    ("RATE_DOSE", ("MDC_DIM_MILLI_G_PER_MIN", "MDC_DIM_MILLI_G_PER_HR", "MDC_DIM_MICRO_G_PER_MIN",
                   "MDC_DIM_MICRO_G_PER_HR", "MDC_DIM_MILLI_G_PER_KG_PER_HR", "MDC_DIM_MICRO_G_PER_KG_PER_MIN")),
    ("DOSE_DRUG_BOLUS DOSE_DRUG_TBI DOSE_DRUG_DELIV_TOTAL",
     ("MDC_DIM_MILLI_G", "MDC_DIM_MICRO_G", "MDC_DIM_X_G", "MDC_DIM_X_INTL_UNIT")),
    ("CONC_GLU_GEN CONC_GLU_CAPILLARY_WHOLEBLOOD", ("MDC_DIM_MILLI_G_PER_DL", "MDC_DIM_MILLI_MOLE_PER_L")),
    ("CONC_HB_GEN CONC_HB_ART", ("MDC_DIM_G_PER_DL", "MDC_DIM_MILLI_MOLE_PER_L")),
    ("CONC_PH_ART", ("MDC_DIM_PH",)),
    ("MASS_BODY_ACTUAL ATTR_PT_WEIGHT", ("MDC_DIM_KILO_G", "MDC_DIM_X_G")),
    ("LEN_BODY_ACTUAL ATTR_PT_HEIGHT", ("MDC_DIM_CENTI_M", "MDC_DIM_X_M")),
    ("RATIO_MASS_BODY_LEN_SQ", ("MDC_DIM_KILO_G_PER_M_SQ",)),
    ("ATTR_BATT_VOLTAGE", ("MDC_DIM_X_VOLT", "MDC_DIM_MILLI_VOLT")),
)
_CODE_OF = {term.ref_id: term.code for term in SHIPPED}
# A misspelt reference ID fails here, on import.
_UNITS: dict[str, tuple[str, ...]] = {
    _CODE_OF[f"MDC_{measure}"]: tuple(_CODE_OF[unit] for unit in units)
    for measures, units in _UNIT_GROUPS for measure in measures.split()
}

# Terms from the user's own codes.csv, for this session only.
_loaded: list[Term] = []
loaded_from: Path | None = None


class NomenclatureFileError(ValueError):
    """A codes.csv that cannot be used."""


def terms() -> list[Term]:
    """Every term known: the shipped ones, then the user's own."""
    return [*SHIPPED, *_loaded]


def lookup(code: str | int) -> Term | None:
    """The term for a context-free code (or a reference ID), shipped ones first."""
    wanted = str(code).strip()
    return next((t for t in terms() if wanted in (t.code, t.ref_id)), None)


def units_for(code: str | int) -> tuple[str, ...]:
    """The codes of the usual units of a measurement code, best first; empty if not known."""
    return _UNITS.get(str(code).strip(), ())


def _score(term: Term, query: str, words: list[str]) -> int | None:
    """How well a term matches, lower is better; None if it does not."""
    names = [term.code, term.ref_id.casefold(), term.symbol.casefold(), term.term.casefold()]
    haystack = " ".join([*names, term.ref_id.replace("_", " ").casefold()])
    if not all(word in haystack for word in words):
        return None
    if query in names:
        return 0
    if any(name.startswith(query) or name.removeprefix("mdc_").startswith(query) for name in names if name):
        return 1
    if all(re.search(r"(^|[\s_,(])" + re.escape(word), haystack) for word in words):
        return 2
    return 3


def search(text: str, kind: str | None = None, limit: int | None = None) -> list[Term]:
    """Terms matching every word of `text` in their code, reference ID or term; best first.

    `kind` narrows to one of KINDS. Exact matches come first, then prefixes, then word starts,
    then anything containing the words; within a rank shipped terms and shorter reference IDs
    (the more general term) first.
    """
    if kind is not None and kind not in KINDS:
        msg = f"unknown kind {kind!r}; use one of {', '.join(KINDS)}"
        raise ValueError(msg)
    query = " ".join(text.casefold().split())
    words = query.split()
    ranked = []
    for index, term in enumerate(terms()):
        if kind is not None and term.kind != kind:
            continue
        score = _score(term, query, words) if words else 0
        if score is not None:
            ranked.append((score, not term.shipped, len(term.ref_id) if words else index, index, term))
    ranked.sort(key=lambda entry: entry[:4])
    return [entry[-1] for entry in ranked[:limit]]


def _csv_code(row: dict[str, str]) -> str | None:
    """The context-free code of one codes.csv row, or None if the row is not usable."""
    part_code = (row.get("part_code") or "").strip()
    cf_code = (row.get("cf_code10") or "").strip()
    from_parts = None
    if part_code:
        match = re.fullmatch(r"(\d{1,5})\s*::\s*(\d{1,5})", part_code)
        if match is None or int(match[2]) > 0xFFFF:  # noqa: PLR2004 - a term code is 16 bits
            return None
        from_parts = str((int(match[1]) << 16) + int(match[2]))
    if cf_code and (not cf_code.isdigit() or (from_parts and str(int(cf_code)) != from_parts)):
        return None
    return from_parts or (str(int(cf_code)) if cf_code else None)


def load_csv(path: str | Path) -> int:
    """Add the terms of a codes.csv (from helpers/Coding Systems) for this session.

    Replaces what an earlier file added. Rows without a reference ID or a valid code are
    skipped; a file without the columns, or without a single usable row, is refused with
    NomenclatureFileError and changes nothing. Returns how many terms it added.
    """
    global loaded_from  # noqa: PLW0603 - the session's one extra list
    path = Path(path)
    try:
        with path.open(newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            columns = set(reader.fieldnames or ())
            if "reference_id" not in columns or not columns & {"part_code", "cf_code10"}:
                msg = f"{path.name} has no reference_id and part_code (or cf_code10) columns"
                raise NomenclatureFileError(msg)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        msg = f"{path.name} could not be read: {exc}"
        raise NomenclatureFileError(msg) from exc
    known = {(t.code, t.ref_id) for t in SHIPPED}
    found: dict[tuple[str, str], Term] = {}
    for row in rows:
        ref_id = (row.get("reference_id") or "").strip()
        code = _csv_code(row)
        if code is None or re.fullmatch(r"MDC(_[A-Z0-9]+)+", ref_id) is None or (code, ref_id) in known:
            continue
        term = next((row.get(c, "").strip() for c in ("common_term", "systematic_name") if row.get(c, "").strip()), "")
        found.setdefault((code, ref_id), Term(code, ref_id, term, shipped=False))
    if not found and not any((_csv_code(r), (r.get("reference_id") or "").strip()) in known for r in rows):
        msg = f"{path.name} has no row with a reference ID and a valid code"
        raise NomenclatureFileError(msg)
    _loaded[:] = found.values()
    loaded_from = path
    return len(found)
