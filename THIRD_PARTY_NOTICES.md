# Third-Party Notices

SDC Testing Toolbox is licensed separately under the GNU General Public License
version 3 only (`GPL-3.0-only`); see `LICENSE`. These notices do not establish
legal compliance or constitute legal advice.

`DEPENDENCY_INVENTORY.json` is generated separately for every native build from
installed distribution metadata and PyInstaller's analyzed modules, binaries,
and data files. It is the authoritative artifact-level list of packaged runtime
components and records each component's exact version, declared license or
license expression, project/source URL, bundled text paths, and graph/artifact
evidence. This maintained document explains the release policy and major
licensing choices; it is not a substitute for that inventory or the complete
bundled texts.

## Inventory Policy

The build starts from the direct runtime roots `sdc11073` and `PySide6`, follows
their environment-applicable installed requirements, and associates analyzed
files and modules with installed distributions. Python and Qt are explicit
non-wheel runtime components. PyInstaller is identified as an embedded build
tool because its bootloader and loader enter the result. Resolved build
dependencies that own no artifact file are listed separately as build-only.

The build fails rather than emitting an incomplete package entry when a
packaged distribution has no declared license, project/source URL, or license
text. The result is deterministic for a fixed installed environment and module
graph. Because direct requirements do not lock transitive versions, inventories
are generated at build time and retained in their artifacts, not checked into
source from one developer machine.

On Windows, binaries copied from arbitrary `PATH` locations outside the Python
installation and build environment are excluded, and Windows system libraries
remain host dependencies. Native OpenSSL and Microsoft C/C++ runtime files are
identified by embedded file version. On Linux, copied native libraries are
assigned to installed Debian packages with `dpkg-query`; package versions and
copyright files enter the inventory and payload, while PyInstaller's standard
system-library exclusions remain host dependencies.

## Runtime Components

### sdc11073

The pinned direct dependency is `sdc11073 3.0.0`, licensed under the MIT
License. The build copies the installed wheel's complete license, including
`Copyright (c) 2026 Draeger`, to the inventory's notice path.

Project and source: https://github.com/Draegerwerk/sdc11073

### PySide6, Shiboken6, and Qt

PySide6 is Qt's official Python binding. The wheel metadata declares
`LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; its included
`LicenseRef-Qt-Commercial.txt` is only a reference for holders of a separate
commercial agreement and is not treated as an open-source license grant. These
builds select LGPLv3 for dynamically loaded Qt, PySide6, and Shiboken6 shared
libraries. Individual Qt modules and third-party code can carry additional
terms, which remain applicable.

The release format is a one-directory bundle. The Qt, PySide6, Shiboken6, and
plugin shared libraries remain separate in the bundle, so recipients can
replace them with modified interface-compatible builds. This project imposes
no restriction on reverse engineering needed to debug such modifications.
`legal/SOURCE_OFFER.md` supplies replacement and smoke-test instructions.

The artifact includes canonical LGPLv3 and GPLv3 texts and the Qt GPL
exception, with provenance in `legal/PROVENANCE.md`. Before a binary is
presented as a distributable release, the release maintainer must also publish
the exact corresponding toolbox, PySide6/Shiboken6, and Qt source archives
specified by the generated inventory and record all source and binary SHA-256
hashes in the release notes. If that payload is unavailable, the binary remains
a CI release candidate and must not be published as a distributable release.

Authoritative references:

- https://doc.qt.io/qtforpython-6/licenses.html
- https://www.qt.io/development/open-source-lgpl-obligations
- https://doc.qt.io/qt-6/licenses-used-in-qt.html

### Python

PyInstaller bundles the Python interpreter and standard library. Python is
licensed under the Python Software Foundation License Version 2 and contains
historical licenses and acknowledgements. The build copies the complete
`LICENSE.txt` from the installed Python into
`legal/licenses/python/LICENSE.txt`; the generated inventory records its exact
version and source URL.

License reference: https://docs.python.org/3/license.html

### Transitive Runtime Packages

The actual transitive set can differ as package releases and platform markers
change. Every included package, including `aiohttp`, `lxml`, and their packaged
dependencies, appears in `DEPENDENCY_INVENTORY.json` with its installed license
and notice files copied under `legal/licenses/<normalized-name>/`. No static
list here overrides the artifact inventory.

## Build Tooling

PyInstaller is primarily `GPL-2.0-or-later WITH Bootloader-exception`; its
runtime hooks are Apache-2.0 and isolated files are additionally MIT. The
installed `COPYING.txt`, including the Bootloader Exception, is copied into the
artifact. The exception permits distribution of generated combinations without
applying PyInstaller's GPL restrictions merely through embedded bootloader and
loader files; dependency terms remain separate.

Project and source: https://github.com/pyinstaller/pyinstaller

Other installed build dependencies, such as `pyinstaller-hooks-contrib`, are
classified as build-only unless distribution-owned material is found in the
artifact. This distinction is recorded rather than inferred from source
requirements alone.

## Release Process and Payload

Each Windows ZIP and Linux tarball must contain exactly one complete
`SDC-Testing-Toolbox` directory with the executable, separate shared
libraries, `LICENSE`, this file, `DEPENDENCY_INVENTORY.json`, and the complete
`legal` tree. CI opens each final archive and validates its bytes: every
inventory field, listed notice, expected legal document, inventoried artifact
path, and separately packaged Qt shared library. It does not validate source
strings or only a pre-archive staging directory.

For a release, the maintainer must retain the generated inventory unchanged,
publish the corresponding-source archives defined in `legal/SOURCE_OFFER.md`
beside both binaries, add SHA-256 hashes for every binary and source asset to
the release notes, and keep those downloads together for as long as the
binaries are offered. A candidate missing any part is not a distributable
release. This process resolves the known release prerequisites but does not
replace qualified review of the exact artifact and intended distribution.
