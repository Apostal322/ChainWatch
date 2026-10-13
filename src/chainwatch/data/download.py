"""Fetch the Elliptic++ transaction files and verify their integrity.

The upstream GitHub repository stores the CSVs with Git LFS. The SHA-256 digests
and sizes below are the LFS object IDs from the repository's pointer files, so a
successful download is byte-identical to what the authors published.

If the download fails (GitHub LFS bandwidth quotas are common for popular
datasets, and corporate networks often block it), fetch the three files manually
from the authors' Google Drive and place them in the raw directory; see README.
"""

from __future__ import annotations

import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from chainwatch.data.schema import CLASSES_FILE, EDGES_FILE, FEATURES_FILE
from chainwatch.utils import get_logger, sha256_file

log = get_logger(__name__)

LFS_BASE_URL = "https://media.githubusercontent.com/media/git-disl/EllipticPlusPlus/main/Transactions%20Dataset"


@dataclass(frozen=True)
class RemoteFile:
    name: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return f"{LFS_BASE_URL}/{self.name}"


REMOTE_FILES = (
    RemoteFile(
        CLASSES_FILE, "013a11742969071a906878ded0319571df0657f9b7133e5c6cdb36217bf0d240", 2_361_914
    ),
    RemoteFile(
        EDGES_FILE, "a35053ba68a98e4382cae2ba65b9d9e36b23b6439e02dff084971b1b72a5156e", 4_470_584
    ),
    RemoteFile(
        FEATURES_FILE,
        "2db326ec8ddb68f1d810c1834e1ff62e0a8300378f0984a1e3b2ca82a439821b",
        694_789_588,
    ),
)


def verify_file(path: Path, expected: RemoteFile) -> bool:
    if not path.exists() or path.stat().st_size != expected.size:
        return False
    return sha256_file(path) == expected.sha256


def verify_raw_dir(raw_dir: Path) -> dict[str, bool]:
    """Return ``{file name: matches published hash}`` for each raw file."""
    return {rf.name: verify_file(raw_dir / rf.name, rf) for rf in REMOTE_FILES}


def download_dataset(raw_dir: Path, force: bool = False) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    for rf in REMOTE_FILES:
        target = raw_dir / rf.name
        if not force and verify_file(target, rf):
            log.info("%s already present and verified, skipping", rf.name)
            continue
        log.info("Downloading %s (%.1f MB) ...", rf.name, rf.size / 1e6)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(rf.url, timeout=60) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
        if not verify_file(tmp, rf):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"{rf.name}: downloaded file does not match the published SHA-256/size. "
                "This usually means GitHub LFS returned an error page (quota exceeded). "
                "Download the file manually (see README) and re-run `chainwatch prepare`."
            )
        tmp.replace(target)
        log.info("%s verified (sha256=%s...)", rf.name, rf.sha256[:12])
