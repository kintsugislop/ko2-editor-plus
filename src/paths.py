"""Paths shared by the station and its command-line tools."""
from pathlib import Path
import os
import pwd

SOURCE_DIR = Path(__file__).resolve().parent
DATA_DIR = SOURCE_DIR.parent / "data"
LIBRARY_DIR = DATA_DIR / "library" / pwd.getpwuid(os.getuid()).pw_name
