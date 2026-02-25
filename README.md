# Revolut Card Draw (Qt + ADB)

Interactive desktop tool to preview an Android screen, crop an allowed drawing area, and replay SVG/CSV gestures via ADB touches.

## What It Does

- Streams live Android screen frames in a Qt window.
- Lets you define a crop region (the allowed drawing area).
- Supports **draw** and **crop** modes.
- Loads SVG/CSV tracks and plays them back as touch gestures.
- Shows hover preview overlays before dropping/drawing.
- Supports manual freehand drawing in Draw mode.
- Supports `Ctrl+V` image overlay as a visual tracing guide.

## Requirements

- Python 3.10+
- ADB installed and available in `PATH`
- Android device with USB debugging / wireless debugging enabled

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python pp_qt.py
```

## ADB Notes

- Connect device over USB or Wi-Fi first.
- Typical Wi-Fi connect:

```bash
adb connect <phone-ip>:<port>
```

- App auto-detects best touch backend (`sendevent`, `motionevent`, fallback `swipe`).

## Controls

- **Crop mode**
  - Left drag: set crop
  - Double click or right click: reset crop
- **Draw mode**
  - Move cursor: position preview
  - Left click: place selected SVG/CSV at cursor
  - Left drag: freehand draw
- **Drag & drop**
  - Drag SVG/CSV from sidebar to canvas to preview and place
- **Overlay**
  - `Ctrl+V`: paste clipboard image as visual overlay guide

## Files

- `pp_qt.py` — main Qt application
- `requirements.txt` — Python dependencies
