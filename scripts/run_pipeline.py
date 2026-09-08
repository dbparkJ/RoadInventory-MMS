from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def main() -> None:
    from mms_shp_detection.infrastructure.process_owner import owned_child_handshake, owned_child_finish
    owned = owned_child_handshake()
    try:
        from mms_shp_detection.pipeline import main as pipeline_main
        pipeline_main()
    finally:
        owned_child_finish(owned)


if __name__ == "__main__":
    main()
