# AGENTS: Chat agent guidance for this repo

Purpose
-------
This file gives concise, actionable guidance for coding agents to get productive in this repository.

Quick entry points
------------------
- **Run locally**: The main application entry is [review.py](review.py). Open it to inspect CLI options and behavior.
- **Build / bundle**: See [build_command.txt](build_command.txt) and [build_review_app.ps1](build_review_app.ps1) for the project's packaging steps (PyInstaller spec at [review_app.spec](review_app.spec)).

What agents should know (concise)
--------------------------------
- The app is a Python GUI that uses `cv2` (OpenCV) and `numpy` (see imports in [review.py](review.py)).
- Packaging is done with PyInstaller — a `.spec` file is present ([review_app.spec](review_app.spec)) and built artifacts appear under the `build/review_app/` directory.
- Typical development workflow: run the script for quick checks; use `build_review_app.ps1` (Windows PowerShell) or the steps in `build_command.txt` to create a bundled executable.

Common pitfalls and checks
-------------------------
- The app may attempt to open GUI windows (OpenCV/Tkinter). CI or headless runs will need display stubs or skip GUI paths.
- Optional GUI dependencies (Tkinter) are only used for file dialogs/popups; code handles absence by falling back to console messages.

Suggested next agent customizations
----------------------------------
- Create a small `skill` that runs a local lint/test script and reports dependency issues (check for `cv2`/`numpy`).
- Add a `hook` that validates the PyInstaller spec and confirms build artifacts exist after running `build_review_app.ps1`.

If something's missing or you want a different file format for agent instructions, tell me and I will update this file.
