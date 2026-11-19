"""Supply exact bundled lane instructions to a fresh worker."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from .common import Halt


MAX_SKILL_BYTES = 16 * 1024
LANES = ("build", "qa", "security", "review")
SCOPE_KEYS = {"source", "head", "base", "task", "config"}
SCHEMAS = ("lane-result.json", "review-result.json", "release-review-result.json")


def _scope(scope):
    if (not isinstance(scope, dict) or set(scope) != SCOPE_KEYS or
            any(not isinstance(value, str) or not value.strip() for value in scope.values())):
        raise Halt("Worker context needs current source/head/base/task/config pins")
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


def _read(package, parts):
    """Read a bounded regular package file through no-follow directory handles."""
    handles = []
    try:
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            return None
        handle = os.open(package, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(handle)
        for directory in parts[:-1]:
            handle = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            handles.append(handle)
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=handle)
        handles.append(handle)
        before = os.fstat(handle)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_SKILL_BYTES:
            return None
        with os.fdopen(handle, "rb", closefd=False) as stream:
            data = stream.read(MAX_SKILL_BYTES + 1)
        after = os.fstat(handle)
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_size,
                               value.st_mtime_ns, value.st_ctime_ns)
        if stamp(before) != stamp(after) or len(data) > MAX_SKILL_BYTES:
            return None
        text = data.decode("utf-8")
        if not text.strip() or "\0" in text:
            return None
        return data
    except (OSError, UnicodeError, NotImplementedError):
        return None
    finally:
        for handle in reversed(handles):
            os.close(handle)


@dataclass(frozen=True)
class WorkerContext:
    package: Path
    lane: str
    content: bytes
    schemas: tuple
    scope_json: str
    skill_parts: tuple

    @property
    def instructions(self):
        skill = self.package.joinpath(*self.skill_parts)
        return (
            "Read this repository's applicable AGENTS.md instructions. "
            "The exact bundled lane skill is supplied below. Apply it and open any "
            f"required relative references against {skill.parent}.\n"
            f"Bundled skill path: {skill}\n"
            f"SHA-256: {hashlib.sha256(self.content).hexdigest()}\n"
            f"BEGIN SFC LANE SKILL\n{self.content.decode('utf-8')}\nEND SFC LANE SKILL"
        )

    def receipt(self):
        return {"version": 1, "lane": self.lane,
                "skill_path": str(self.package.joinpath(*self.skill_parts)),
                "skill_sha256": hashlib.sha256(self.content).hexdigest(),
                "schemas": {name: hashlib.sha256(data).hexdigest() for name, data in self.schemas},
                "scope": json.loads(self.scope_json)}

    def validate(self, scope):
        if (_scope(scope) != self.scope_json or
                _read(self.package, self.skill_parts) != self.content or
                any(_read(self.package, ("schemas", name)) != data for name, data in self.schemas)):
            raise Halt("Worker context changed before dispatch; prepare fresh instructions")


def prepare(package, lane, scope, *, release_qa=False):
    """Pin package bytes with caller scope; source identifies the supplied package."""
    if lane not in LANES or (release_qa and lane != "qa"):
        raise Halt("Unknown worker lane for context preparation")
    pinned_scope = _scope(scope)
    package = Path(package).absolute()
    skill_parts = ("skills", f"sfc-{lane}", "RELEASE.md" if release_qa else "SKILL.md")
    content = _read(package, skill_parts)
    schemas = tuple((name, _read(package, ("schemas", name))) for name in SCHEMAS)
    if content is None or any(data is None for _, data in schemas):
        return None
    return WorkerContext(package, lane, content, schemas, pinned_scope, skill_parts)
