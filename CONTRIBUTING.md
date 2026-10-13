# Contributing

Thanks for helping improve Camera Exploration Lab. Keep changes focused on camera discovery, capture, experiment recording, history, or documented extension interfaces.

## Before submitting a change

- Run the camera-independent test suite:

  ```bash
  PYTHONPATH=src QT_QPA_PLATFORM=offscreen python3 -m unittest discover -v -s tests
  ```

- Add tests with synthetic timings and mocked camera interfaces. Tests should pass without a physical device.
- Keep capture, preview, and processing measurements separate, and document how a new metric is timed.
- Keep requested settings, driver readback, and verified settings as distinct values.
- Preserve source rows and raw timestamps when changing import or measurement behavior.
- Update the architecture and exploration notebook when a design boundary or confirmed finding changes.

## Data handling

Do not add personal camera serial numbers, host paths, credentials, experiment names or notes, raw CSV/JSON, local SQLite databases or backups, captured images, or other private measurements. Use generated fixtures and synthetic camera identities in tests. Review screenshots and logs for device identifiers before attaching them to an issue or change.

## Scope boundaries

The core should remain a small camera workbench. Put specialized computer-vision algorithms and their model/runtime dependencies behind optional extension packages. Keep wardrobe inventory in a separate application. CHDKPTP support, full graphing, and broad camera controls are not part of the current implementation phase.
