"""Check project licensing declarations and packaged legal notices."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GPL-3.0-only" in readme
    assert "[Third-Party Notices](THIRD_PARTY_NOTICES.md)" in readme

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for component in ("sdc11073 3.0.0", "PySide6 and Qt 6.11.2", "Python", "PyInstaller"):
        assert component in notices
    assert "Copyright (c) 2026 Draeger" in notices
    assert "Python Software Foundation License Version 2" in notices
    for domain in ("github.com/Draegerwerk/sdc11073", "doc.qt.io", "qt.io", "docs.python.org", "pyinstaller.org"):
        assert domain in notices

    spec = (ROOT / "SDC-Testing-Toolbox.spec").read_text(encoding="utf-8")
    assert '(str(root / "LICENSE"), ".")' in spec
    assert '(str(root / "THIRD_PARTY_NOTICES.md"), ".")' in spec
    assert 'copy_metadata("sdc11073", recursive=True)' in spec
    assert 'copy_metadata("PySide6", recursive=True)' in spec

    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(encoding="utf-8")
    assert "Verify packaged legal notices" in workflow
    assert "dist/LICENSE" in workflow
    assert "dist/THIRD_PARTY_NOTICES.md" in workflow


if __name__ == "__main__":
    main()
    print("Licensing and packaged notices checks passed.")
