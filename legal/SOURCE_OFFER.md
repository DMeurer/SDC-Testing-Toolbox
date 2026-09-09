# Source, Relinking, and Installation Information

This file accompanies the Windows and Linux one-directory application bundles.
It is an operational distribution notice, not legal advice and not a warranty
that every use or redistribution complies with applicable law.

## Corresponding Source

The toolbox source, build scripts, PyInstaller specification, and dependency
declarations for each release are available from the release's source archive:

https://github.com/DMeurer/SDC-Testing-Toolbox/releases

The exact Python, PySide6, Shiboken6, and Qt versions and versioned source
archive URLs are recorded in `DEPENDENCY_INVENTORY.json`. The Qt list is
derived from the packaged library and plugin paths and names the represented
Qt source modules. Unused PDF and Virtual Keyboard plugins are removed from the
build to avoid claiming or distributing those modules.
Matching unmodified upstream source archives are available from:

- Python: https://www.python.org/downloads/source/
- PySide6 and Shiboken6: https://download.qt.io/official_releases/QtForPython/pyside6/
- Qt module sources: https://download.qt.io/official_releases/qt/

For a public release, the release maintainer must upload the toolbox source
archive and mirror the exact PySide6/Shiboken6 and Qt source archives named by
the inventory next to both binary artifacts. Binary release publication is
blocked until those source assets exist and their SHA-256 hashes are added to
the release notes. Keep those assets available for as long as the binaries are
offered. Merely linking this general upstream index is not the release step.

## Relinking

The application is distributed as a directory, not as a single-file
executable. Qt, PySide6, Shiboken6, and their plugins remain separate shared
libraries under the bundle's `PySide6` and `shiboken6`
directories. Recipients may replace those files with modified,
interface-compatible builds. The distributor imposes no contractual or
technical restriction on reverse engineering needed to debug such changes.

To test a replacement, extract the archive, retain the directory layout,
replace the applicable shared libraries and plugins, and run
`SDC-Testing-Toolbox.exe --smoke-test` on Windows or
`./SDC-Testing-Toolbox --smoke-test` on Linux. No signing key, password, or
installation authorization is required by this project. On platforms that add
independent security controls, users must follow those platform procedures.
