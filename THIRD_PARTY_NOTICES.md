# Third-Party Notices

SDC Testing Toolbox is licensed separately under the GNU General Public License
version 3 only (`GPL-3.0-only`); see `LICENSE`. This file summarizes important
third-party licensing information for the pinned project dependencies and
packaged application. It does not replace the dependencies' license texts or
constitute legal advice.

Distribution of toolbox executables is governed by GPLv3 section 6, including
its requirements for conveying the machine-readable Corresponding Source.
Keep the toolbox's `LICENSE` with every distribution and follow the complete
license text rather than relying on this summary.

## Runtime Components

### sdc11073 3.0.0

`sdc11073` is licensed under the MIT License. Its required notice is reproduced
below from the authoritative
[sdc11073 3.0.0 license](https://github.com/Draegerwerk/sdc11073/blob/v3.0.0/LICENSE):

> MIT License
>
> Copyright (c) 2026 Draeger
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

### PySide6 and Qt 6.11.2

PySide6 is Qt's official Python binding. The installed PySide6 metadata offers
LGPLv3, GPLv2, GPLv3, and commercial licensing options; individual Qt modules
and third-party code shipped with Qt can have additional or different terms.
The toolbox's GPL-3.0-only license does not erase or replace those terms.

Anyone distributing a build under an open-source Qt option must identify the
license that actually applies to every included module and comply with it. For
LGPL-covered Qt libraries, this generally includes providing the applicable
license and prominent notice, providing or offering the corresponding Qt
library source (including modifications), permitting replacement/relinking and
reverse engineering for debugging modifications, and providing installation
information where LGPLv3 requires it. Do not use GPL-only Qt modules unless the
combined distribution is compatible with their GPL terms. A commercial Qt
license is governed by its agreement instead.

Authoritative references:

- [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html)
- [Qt open-source obligations](https://www.qt.io/development/open-source-lgpl-obligations)
- [Licenses used in Qt](https://doc.qt.io/qt-6/licenses-used-in-qt.html)

### Python

PyInstaller bundles the Python interpreter and standard library. Python is
licensed under the Python Software Foundation License Version 2 and includes
components under other licenses and acknowledgements. A binary distributor
must retain the notices and license materials required by the bundled Python
version and its incorporated components. See Python's authoritative
[license and acknowledgements](https://docs.python.org/3/license.html).

Python Software Foundation License Version 2:

> 1. This LICENSE AGREEMENT is between the Python Software Foundation ("PSF"),
> and the Individual or Organization ("Licensee") accessing and otherwise using
> this software ("Python") in source or binary form and its associated
> documentation.
>
> 2. Subject to the terms and conditions of this License Agreement, PSF hereby
> grants Licensee a nonexclusive, royalty-free, world-wide license to reproduce,
> analyze, test, perform and/or display publicly, prepare derivative works,
> distribute, and otherwise use Python alone or in any derivative version,
> provided, however, that PSF's License Agreement and PSF's notice of copyright,
> i.e., "Copyright © 2001 Python Software Foundation; All Rights Reserved" are
> retained in Python alone or in any derivative version prepared by Licensee.
>
> 3. In the event Licensee prepares a derivative work that is based on or
> incorporates Python or any part thereof, and wants to make the derivative work
> available to others as provided herein, then Licensee hereby agrees to include
> in any such work a brief summary of the changes made to Python.
>
> 4. PSF is making Python available to Licensee on an "AS IS" basis. PSF MAKES
> NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR IMPLIED. BY WAY OF EXAMPLE, BUT
> NOT LIMITATION, PSF MAKES NO AND DISCLAIMS ANY REPRESENTATION OR WARRANTY OF
> MERCHANTABILITY OR FITNESS FOR ANY PARTICULAR PURPOSE OR THAT THE USE OF
> PYTHON WILL NOT INFRINGE ANY THIRD PARTY RIGHTS.
>
> 5. PSF SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON FOR ANY
> INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS A RESULT OF
> MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON, OR ANY DERIVATIVE THEREOF,
> EVEN IF ADVISED OF THE POSSIBILITY THEREOF.
>
> 6. This License Agreement will automatically terminate upon a material breach
> of its terms and conditions.
>
> 7. Nothing in this License Agreement shall be deemed to create any relationship
> of agency, partnership, or joint venture between PSF and Licensee. This License
> Agreement does not grant permission to use PSF trademarks or trade name in a
> trademark sense to endorse or promote products or services of Licensee, or any
> third party.
>
> 8. By copying, installing or otherwise using Python, Licensee agrees to be
> bound by the terms and conditions of this License Agreement.

## Build Tooling

PyInstaller is build tooling and also embeds its bootloader and loader files in
generated applications. It is licensed primarily under GPLv2-or-later with a
Bootloader Exception; some files use Apache-2.0 or MIT. The exception permits
distribution of generated bundles without imposing PyInstaller's GPL on the
application, but does not change dependency licenses. Review the
[PyInstaller licensing terms](https://pyinstaller.org/en/stable/license.html).
`pyinstaller-hooks-contrib` is also build tooling; review its installed version
and license if any part is copied into a distributed artifact.

## Distribution Checklist

The tracked PyInstaller specification places this file, `LICENSE`, and the
installed runtime distributions' metadata (including license files supplied by
their wheels) in the application bundle. Release artifacts must keep the two
top-level files available to recipients. Before distributing any artifact,
inventory its actual contents and versions, review every dependency's installed
license and notice files, include all required texts and attributions, and
satisfy source-code, relinking, and installation-information obligations. This
maintained summary is not a complete bill of materials and cannot establish
compliance for a particular build.
