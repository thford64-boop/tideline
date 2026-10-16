# Changelog

## 0.0.1 — first release (2026-10-16)

The first public release of Tideline, a non-linear video editor for Linux on
MLT, FFmpeg and GTK 4. See README.md for the full feature list.

New in this release:

- **First-run setup**: choose your layout (Editing, Effects or Audio, shown as
  previews), a playback quality and your first project (name, folder, format).
- **Project manager** on later starts: recent projects with filter, New
  Project, Open Project and an unsaved empty project. Also under
  File ▸ Project Manager (Ctrl+Alt+O).
- **Preferences** (Edit ▸ Preferences, Ctrl+,): Performance / Balanced /
  Quality playback presets, preview resolution down to 1/8, preview scaling,
  render threads, frame dropping, interface update rate during playback and
  live Effect Controls values. Start-up layout, full screen and project manager.
- Smoother playback: panels that follow the playhead are redrawn at a capped
  rate while the picture plays at full frame rate.
- App icon in every size, used by the windows even when run from source.
- `install.sh` for distributions other than Arch.
