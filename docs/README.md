# The documentation site

This folder is the SDC Testing Toolbox documentation. GitHub Pages builds it with Jekyll and the
[Just the Docs](https://just-the-docs.github.io/just-the-docs/) theme.

## Publishing

GitHub Pages deploys the branch `develop`, folder `/docs` (*Settings > Pages*), to
<https://sdc.d-meurer.com/>. Every push to `develop` updates the site. `CNAME` holds the custom
domain; GitHub writes it when the domain changes in the settings, so leave it in place.

## Previewing locally

With Docker, from the repository root:

```powershell
docker run --rm -it -p 4000:4000 -v "${PWD}/docs:/site" -w /site ruby:3.3 sh -c "bundle install && bundle exec jekyll serve --host 0.0.0.0"
```

Then open <http://localhost:4000>. The first start installs the gems and takes a few minutes.

## Writing pages

- Each page has front matter with its `title`, its `parent` (the section) and a `nav_order`.
- Link to other pages by their relative `.md` path; Jekyll turns them into page links.
- Callouts: put `{: .note }`, `{: .tip }` or `{: .warning }` on the line before a paragraph.
- Diagrams: a fenced code block with the language `mermaid`.

## Screenshots

`assets/screenshots/` is made by the real application. After a GUI change, take them again:

```powershell
.venv\Scripts\python.exe helpers\docs_screenshots.py
```

A window appears for about a minute while it works; do not click it.

`superpowers/` holds notes for coding agents and is not part of the site.
