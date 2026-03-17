from pathlib import Path
import sys

from app.ingestion.whatsapp import ingest_whatsapp_export


def main() -> None:
    export_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/whatsapp")
    result = ingest_whatsapp_export(export_path)
    print(result.detail)


if __name__ == "__main__":
    main()
