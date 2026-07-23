"""Export a compact, human-readable calibration value sheet.

Usage:
    python export_calibration_values.py
    python export_calibration_values.py path/to/calibration_values.yaml
"""

from mms_shp_detection.calibration_values import main


if __name__ == "__main__":
    main()
