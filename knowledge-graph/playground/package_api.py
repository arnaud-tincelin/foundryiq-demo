"""Package only the Function app and the shared retrieval code."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent
FILES = {
    "host.json": HERE / "api" / "host.json",
    "requirements.txt": HERE / "api" / "requirements.txt",
    "function_app.py": HERE / "api" / "function_app.py",
    "trace_api.py": HERE / "api" / "trace_api.py",
    "records.py": HERE.parent / "records.py",
    "search.py": HERE.parent / "search.py",
    "services.py": HERE.parent / "services.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with ZipFile(args.output, "x", ZIP_DEFLATED) as archive:
        for name, source in FILES.items():
            archive.write(source, name)
    print(args.output)


if __name__ == "__main__":
    main()
