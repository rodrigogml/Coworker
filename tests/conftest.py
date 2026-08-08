from pathlib import Path
import sys

INSTANCE_ROOT = Path(__file__).resolve().parents[1] / "instance"
if str(INSTANCE_ROOT) not in sys.path:
    sys.path.insert(0, str(INSTANCE_ROOT))
