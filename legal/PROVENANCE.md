# Legal Text Provenance

The Qt texts are byte-for-byte copies from the Qt 6.11.2 release repositories:

- `licenses/qt/LGPL-3.0-only.txt` is
  `qtbase/LICENSES/LGPL-3.0-only.txt` at commit
  `ef55f427f2c8b410d34f8a7681020a3000cf6866` (tag `v6.11.2`), retrieved from
  https://raw.githubusercontent.com/qt/qtbase/ef55f427f2c8b410d34f8a7681020a3000cf6866/LICENSES/LGPL-3.0-only.txt
  (SHA-256 `da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768`).
- `licenses/qt/GPL-3.0-only.txt` is generated from the repository's top-level
  `LICENSE`, itself a byte-for-byte copy of `qtbase/LICENSES/GPL-3.0-only.txt`
  at the same commit and tag, retrieved from
  https://raw.githubusercontent.com/qt/qtbase/ef55f427f2c8b410d34f8a7681020a3000cf6866/LICENSES/GPL-3.0-only.txt
  (SHA-256 `8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903`).
- `licenses/qt/Qt-GPL-exception-1.0.txt` is
  `pyside-setup/LICENSES/Qt-GPL-exception-1.0.txt` at commit
  `24627cd36e1593adf22eb1f2950e4248e7bcc1ec` (tag `v6.11.2`), retrieved from
  https://code.qt.io/cgit/pyside/pyside-setup.git/plain/LICENSES/Qt-GPL-exception-1.0.txt?id=24627cd36e1593adf22eb1f2950e4248e7bcc1ec
  (SHA-256 `40678d338ce53cd93f8b22b281a2ecbcaa3ee65ce60b25ffb0c462b0530846b2`).

The Python text is copied at build time from `LICENSE.txt` in the installed
CPython distribution. Distribution-specific files are copied at build time
from each installed wheel's declared license files. Their exact source paths
and associations are recorded in `DEPENDENCY_INVENTORY.json`.

`licenses/openssl/Apache-2.0.txt` is the byte-for-byte `LICENSE.txt` from
OpenSSL 3.0.16 commit `fa1e5dfb142bb1c26c3c38a10aafa7a095df52e5`
(tag `openssl-3.0.16`), retrieved from
https://raw.githubusercontent.com/openssl/openssl/fa1e5dfb142bb1c26c3c38a10aafa7a095df52e5/LICENSE.txt
(SHA-256 `7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a`).
This Apache-2.0 text applies to OpenSSL 3.x.

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
