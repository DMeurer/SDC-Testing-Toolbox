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

The additional native-component texts were retrieved on 2026-09-03:

- `licenses/common/Zlib.txt` is Qt 6.11.2's unmodified
  `qtbase/src/3rdparty/zlib/LICENSE` (SHA-256
  `d7e3139972874dc8abdbbfce27ec1e6f942b28279dd7d27453630a8b4610e080`).
- `licenses/iconv/LGPL-2.1-only.txt` is the unmodified `COPYING.LIB` shipped
  with GNU libiconv 1.17 in Git for Windows (SHA-256
  `20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95`),
  equivalent to https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt.
- `licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx` is Microsoft's exact
  `Visual-C-Runtime-2015-2022-License-1.docx` linked from
  https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/ (SHA-256
  `f1e3d56ceb2ad68aae0711b910375009e651ac5530fa0760f0dea6e81e54fae1`).
  The adjacent `.txt` is a Pandoc 3.10.1 plain-text rendering for direct
  readability (SHA-256
  `6b103d18342f404b1d7f4b7fda9f4e183137c8138f4b1dd4d076d2006d787726`,
  with line endings normalized to LF).
- `licenses/qt-third-party/libjpeg-turbo/{LICENSE.txt,ijg-license.txt,COPYRIGHT.txt}`
  are unmodified Qt 6.11.2 `qtbase/src/3rdparty/libjpeg` files (SHA-256,
  respectively, `ba6bceebcba0fdd35488477c2cca8c4632ce82c74dbfbc87d886ce6fc4433579`,
  `1cb448f5e2e6d3aca60943f4eee1a04675628dc0266a074a42bc1560ebc28d61`,
  and `e5b349c6a98d997a8d0f5fcfe5cc900b1f76bbc32743674c52a70981498748c0`).
- `licenses/qt-third-party/libtiff/COPYRIGHT` and
  `licenses/qt-third-party/libwebp/COPYING` are unmodified Qt 6.11.2
  `qtimageformats/src/3rdparty` files (SHA-256
  `fbd6fed7938541d2c809c0826225fc85e551fdbfa8732b10f0c87e0847acafd7`
  and `5aec868f669e384a22372a4e8a1a6cd7d44c64cd451f960ca69cc170d1e13acf`).

Generated inventories also hash every native notice as copied into the final
artifact. The validator independently fixes each maintained native component's
expected notice path and identifying phrases, so inventory edits cannot
reassign an unrelated existing legal file.
