from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from storage import backup_database


timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
path = backup_database(Path("backups") / f"faq-chatbot-{timestamp}.db")
print(path.resolve())
