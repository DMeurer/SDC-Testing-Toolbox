"""PySide6 user interface of the SDC-testing-toolbox.

sdc11073 invokes its callbacks from its own threads. Never touch a Qt widget from one of
them - marshal everything onto the GUI thread through qt_bridge.
"""
