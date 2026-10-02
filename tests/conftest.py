import os
import tempfile

# Settings читаются при импорте app.config — заполняем до него.
os.environ.setdefault("PACHCA_TOKEN", "test")
os.environ.setdefault("PACHCA_CHANNEL_ID", "1")
os.environ.setdefault("EMAIL_ACCOUNT", "test")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.sqlite3")
