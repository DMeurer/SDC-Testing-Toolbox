# Legal Text Provenance

The Qt texts in this directory are unmodified canonical files from the Qt 6.11
source repositories:

- `LGPL-3.0-only.txt`: `qtbase/LICENSES/LGPL-3.0-only.txt`, retrieved from
  https://code.qt.io/cgit/qt/qtbase.git/plain/LICENSES/LGPL-3.0-only.txt?h=6.11
- `GPL-3.0-only.txt`: generated from the repository's unmodified top-level
  `LICENSE`, which is the same GPLv3 text as
  https://code.qt.io/cgit/qt/qtbase.git/plain/LICENSES/GPL-3.0-only.txt?h=6.11
- `Qt-GPL-exception-1.0.txt`:
  `pyside-setup/LICENSES/Qt-GPL-exception-1.0.txt`, retrieved from
  https://code.qt.io/cgit/pyside/pyside-setup.git/plain/LICENSES/Qt-GPL-exception-1.0.txt?h=6.11

The Python text is copied at build time from `LICENSE.txt` in the installed
CPython distribution. Distribution-specific files are copied at build time
from each installed wheel's declared license files. Their exact source paths
and associations are recorded in `DEPENDENCY_INVENTORY.json`.

`licenses/openssl/Apache-2.0.txt` is the unmodified Apache License 2.0 text
from https://www.apache.org/licenses/LICENSE-2.0.txt, applicable to OpenSSL
3.x.
