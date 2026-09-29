# DFET Annotation tool

A Windows desktop app for reviewing and editing COCO / Label Studio image annotations.
It runs completely offline.

## Download and run

1. Go to [**Releases**](https://github.com/VachuVac/DFET_annotation_tool/releases/latest) and download
   `DFET_Annotation_tool_1.0.exe`.
2. Double-click the downloaded `.exe`. No installation or Python is needed.

The first start can take a few seconds while the app unpacks itself.

Windows may show a blue **"Windows protected your PC"** warning because the exe isn't signed.
Click **More info → Run anyway**.

Settings (class colors, shortcuts) are stored in `%LOCALAPPDATA%\DFETAnnotationTool\`.

## Run from source

Requires Python with `pyqt6`, `opencv-python` and `numpy`:

```
python review.py
```

## License

[MIT](LICENSE)
