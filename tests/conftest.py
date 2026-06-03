import sys
import pathlib

# Make the repo root importable so `engine` / `models` resolve when running pytest.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
