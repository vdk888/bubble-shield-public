# Changelog

## 1.18.17

### Fixed
- **Installer online fallback pulled unpinned dependencies.** `install-app.sh`
  has two dependency-install paths: the default offline path (vendored wheels in
  `vendor/wheels/`, versions pinned by the exact wheel files) and an online
  fallback taken on the rare Mac with no ABI-matching interpreter. The online
  fallback ran `pip install fastapi uvicorn pywebview jinja2 pypdf
  python-multipart` with no version constraints, so it fetched the LATEST
  releases from PyPI — e.g. pywebview 6.x against launcher code written and
  tested for pywebview 3.4. That is the same pywebview 3.x/4.x API-drift crash
  class (`window.events` AttributeError) fixed in 1.18.16; the unpinned fallback
  simply relocated it. Added `constraints.txt` at the repo root pinning the exact
  versions vendored in `vendor/wheels/`, and applied `-c constraints.txt` to
  BOTH the offline and online pip installs so they resolve to identical versions.
  pywebview now stays 3.4 on every install path.
