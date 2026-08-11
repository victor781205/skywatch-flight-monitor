"""Download and build the local global-IATA airport index."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.airports import ensure_airport_data, load_airports


def main():
    ensure_airport_data(force=True)
    data = load_airports()
    print(f"Done: {len(data)} airports")
    print("Example:", data[0] if data else None)


if __name__ == "__main__":
    main()
