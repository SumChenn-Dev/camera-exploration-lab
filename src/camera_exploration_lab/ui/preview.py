"""Image conversion shared by the single- and multi-camera live previews."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QImage


def frame_to_image(frame: Any) -> QImage:
    """Copy one OpenCV frame into an owned QImage for GUI-thread rendering."""
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        raise ValueError("Camera returned a frame without image dimensions")
    height, width = int(shape[0]), int(shape[1])
    if height < 1 or width < 1:
        raise ValueError("Camera returned an empty frame")

    flags = getattr(frame, "flags", None)
    if flags is not None and not flags.c_contiguous:
        frame = frame.copy()
    data = frame.data
    strides = getattr(frame, "strides", None)
    bytes_per_line = int(strides[0]) if strides else width * (int(shape[2]) if len(shape) > 2 else 1)

    if len(shape) == 2:
        image_format = QImage.Format.Format_Grayscale8
    else:
        channels = int(shape[2])
        if channels == 3:
            image_format = QImage.Format.Format_BGR888
        elif channels == 4:
            image_format = QImage.Format.Format_BGRA8888
        else:
            raise ValueError(f"Unsupported camera frame with {channels} channels")
    image = QImage(data, width, height, bytes_per_line, image_format)
    if image.isNull():
        raise ValueError("Could not convert the camera frame for preview")
    return image.copy()
