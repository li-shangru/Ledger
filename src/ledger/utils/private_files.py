"""Owner-only filesystem helpers for local financial artifacts."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO, cast

from ledger.runtime import RuntimePathError, require_external_runtime_path

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def _canonical_platform_temp_path(path: Path) -> Path:
    """Resolve only conventional root-owned macOS temp aliases.

    Arbitrary symlinks remain forbidden. macOS exposes ``/tmp`` and ``/var`` as
    root-owned aliases into ``/private``; accepting those conventional aliases
    keeps direct service destinations consistent with shell-script defaults.
    """
    path = Path(path)
    if not path.is_absolute():
        return path
    for alias in (Path("/tmp"), Path("/var")):
        if not alias.is_symlink():
            continue
        try:
            relative = path.relative_to(alias)
        except ValueError:
            continue
        return alias.resolve(strict=True) / relative
    return path


def _reject_symlink_components(path: Path) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError(f"Private destination path contains a symlink: {current}")


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _absolute_private_path(path: Path) -> Path:
    expanded = Path(path).expanduser()
    lexical = Path(os.path.abspath(os.fspath(expanded)))
    try:
        require_external_runtime_path(lexical)
    except RuntimePathError as exc:
        raise OSError(str(exc)) from None
    return _canonical_platform_temp_path(lexical)


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _child_name(name: str) -> str:
    child = Path(name)
    if not name or child.name != name or name in {".", ".."}:
        raise ValueError(f"Private child name must be a basename: {name!r}")
    return name


@dataclass
class PinnedPrivateFile:
    """An owner-only regular file pinned relative to its pinned parent."""

    path: Path
    descriptor: int
    identity: tuple[int, int, int, int]
    parent: PinnedPrivateDirectory
    mutable: bool = False

    def validate(self) -> None:
        self.parent.validate()
        try:
            current = os.stat(
                self.path.name,
                dir_fd=self.parent.descriptor,
                follow_symlinks=False,
            )
            pinned = os.fstat(self.descriptor)
        except OSError as exc:
            raise OSError("Private file changed during operation") from exc
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        pinned_identity = (
            pinned.st_dev,
            pinned.st_ino,
            pinned.st_size,
            pinned.st_mtime_ns,
        )
        identity_matches = (
            current_identity[:2] == self.identity[:2] and pinned_identity[:2] == self.identity[:2]
            if self.mutable
            else current_identity == self.identity and pinned_identity == self.identity
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(pinned.st_mode)
            or not identity_matches
        ):
            raise OSError("Private file changed during operation")


@dataclass
class PinnedPrivateDirectory:
    """An owner-only directory held by descriptor for one filesystem operation."""

    path: Path
    descriptor: int
    identity: tuple[int, int]

    def validate(self) -> None:
        _reject_symlink_components(self.path)
        try:
            lexical = self.path.stat(follow_symlinks=False)
            pinned = os.fstat(self.descriptor)
        except OSError as exc:
            raise OSError(f"Private directory changed during operation: {self.path}") from exc
        if (
            not stat.S_ISDIR(lexical.st_mode)
            or not stat.S_ISDIR(pinned.st_mode)
            or _identity(lexical) != self.identity
            or _identity(pinned) != self.identity
        ):
            raise OSError(f"Private directory changed during operation: {self.path}")

    def require_owner_only(self) -> None:
        """Reject permission drift instead of silently tightening on a read path."""
        self.validate()
        if os.name == "posix" and stat.S_IMODE(os.fstat(self.descriptor).st_mode) != 0o700:
            raise PermissionError(f"Private directory permissions are not owner-only: {self.path}")

    def child_names(self, *, suffix: str, max_entries: int) -> list[str]:
        """List a bounded set of child basenames through the pinned descriptor."""
        self.require_owner_only()
        names = sorted(name for name in os.listdir(self.descriptor) if name.endswith(suffix))
        if len(names) > max_entries:
            raise OSError(f"Private directory contains too many records: {self.path}")
        return [_child_name(name) for name in names]

    @contextlib.contextmanager
    def pin_child_directory(self, name: str) -> Iterator[PinnedPrivateDirectory]:
        """Pin one existing child without releasing the pinned parent.

        Keeping both descriptors open closes the ancestor-swap gap that would exist
        if callers converted the child back to an absolute path and reopened it.
        """
        name = _child_name(name)
        self.require_owner_only()
        descriptor = os.open(name, _directory_flags(), dir_fd=self.descriptor)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise NotADirectoryError("Private child is not a directory")
            child = PinnedPrivateDirectory(
                self.path / name,
                descriptor,
                _identity(metadata),
            )
            child.require_owner_only()
            self.validate()
            yield child
            child.validate()
            self.validate()
        finally:
            with contextlib.suppress(BaseException):
                os.close(descriptor)

    @contextlib.contextmanager
    def pin_private_file(
        self,
        name: str,
        *,
        require_parent_owner_only: bool = True,
        mutable: bool = False,
    ) -> Iterator[PinnedPrivateFile]:
        """Pin one owner-only regular child file without following symlinks."""
        name = _child_name(name)
        if require_parent_owner_only:
            self.require_owner_only()
        else:
            self.validate()
        flags = os.O_RDWR if mutable else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = os.open(name, flags, dir_fd=self.descriptor)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("Private child is not a regular file")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("Private child permissions are not owner-only")
            pinned = PinnedPrivateFile(
                self.path / name,
                descriptor,
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ),
                self,
                mutable,
            )
            pinned.validate()
            yield pinned
            pinned.validate()
        finally:
            with contextlib.suppress(BaseException):
                os.close(descriptor)

    @contextlib.contextmanager
    def atomic_binary(self, name: str) -> Iterator[BinaryIO]:
        child = self.path / _child_name(name)
        with _private_atomic_handle(child, binary=True, directory=self) as handle:
            yield cast(BinaryIO, handle)

    @contextlib.contextmanager
    def atomic_text(
        self,
        name: str,
        *,
        encoding: str = "utf-8",
        newline: str | None = None,
    ) -> Iterator[TextIO]:
        child = self.path / _child_name(name)
        with _private_atomic_handle(
            child,
            binary=False,
            encoding=encoding,
            newline=newline,
            directory=self,
        ) as handle:
            yield cast(TextIO, handle)

    def read_text_if_exists(self, name: str, *, encoding: str = "utf-8") -> str | None:
        name = _child_name(name)
        self.validate()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, dir_fd=self.descriptor)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"Private child is not a regular file: {self.path / name}")
            with os.fdopen(descriptor, "r", encoding=encoding) as handle:
                descriptor = -1
                content = handle.read()
            self.validate()
            return content
        finally:
            if descriptor >= 0:
                with contextlib.suppress(BaseException):
                    os.close(descriptor)

    def read_private_text(
        self,
        name: str,
        *,
        max_bytes: int,
        encoding: str = "utf-8",
    ) -> str:
        """Read one bounded owner-only regular file through the pinned directory."""
        name = _child_name(name)
        self.require_owner_only()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        descriptor = os.open(name, flags, dir_fd=self.descriptor)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"Private child is not a regular file: {self.path / name}")
            if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError(
                    f"Private child permissions are not owner-only: {self.path / name}"
                )
            if metadata.st_size > max_bytes:
                raise OSError(f"Private child exceeds the read limit: {self.path / name}")
            content = os.read(descriptor, max_bytes + 1)
            if len(content) > max_bytes:
                raise OSError(f"Private child exceeds the read limit: {self.path / name}")
            self.validate()
            return content.decode(encoding)
        finally:
            with contextlib.suppress(BaseException):
                os.close(descriptor)

    def unlink(self, name: str, *, missing_ok: bool = False) -> None:
        name = _child_name(name)
        self.validate()
        try:
            metadata = os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"Private child is a symlink: {self.path / name}")
        os.unlink(name, dir_fd=self.descriptor)
        self.validate()


@contextlib.contextmanager
def _pin_directory(
    path: Path,
    *,
    create: bool,
    tighten_final: bool,
) -> Iterator[PinnedPrivateDirectory]:
    path = _absolute_private_path(path)
    descriptor = os.open(Path(path.anchor), _directory_flags())
    current = Path(path.anchor)
    try:
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            child_path = current / part
            created = False
            try:
                child_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, _PRIVATE_DIR_MODE, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                child_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                if child_path.is_symlink():
                    raise OSError(f"Private directory path is a symlink: {child_path}") from exc
                raise

            try:
                metadata = os.fstat(child_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NotADirectoryError(
                        f"Private directory path is not a directory: {child_path}"
                    )
                is_final = index == len(parts) - 1
                if os.name == "posix" and (created or (is_final and tighten_final)):
                    os.fchmod(child_descriptor, _PRIVATE_DIR_MODE)
                if created:
                    os.fsync(child_descriptor)
                    os.fsync(descriptor)
            except BaseException:
                with contextlib.suppress(BaseException):
                    os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
            current = child_path

        metadata = os.fstat(descriptor)
        pinned = PinnedPrivateDirectory(path, descriptor, _identity(metadata))
        pinned.validate()
        yield pinned
    finally:
        with contextlib.suppress(BaseException):
            os.close(descriptor)


@contextlib.contextmanager
def pin_private_directory(path: Path) -> Iterator[PinnedPrivateDirectory]:
    """Create/tighten and pin a private directory for a multi-step operation."""
    with _pin_directory(path, create=True, tighten_final=True) as pinned:
        yield pinned


@contextlib.contextmanager
def pin_existing_directory(path: Path) -> Iterator[PinnedPrivateDirectory]:
    """Pin an existing directory without creating it or changing its mode."""
    with _pin_directory(path, create=False, tighten_final=False) as pinned:
        yield pinned


def ensure_private_dir(path: Path) -> Path:
    """Create *path* privately and tighten its final directory to ``0700``."""
    with pin_private_directory(path) as pinned:
        return pinned.path


@dataclass
class _PrivateCandidate:
    path: Path
    parent_descriptor: int
    parent_identity: tuple[int, int]
    temp_name: str
    descriptor: int


def _unlink_candidate(candidate: _PrivateCandidate) -> None:
    if candidate.parent_descriptor < 0:
        return
    with contextlib.suppress(BaseException):
        os.unlink(candidate.temp_name, dir_fd=candidate.parent_descriptor)


def _close_candidate(candidate: _PrivateCandidate) -> None:
    if candidate.descriptor >= 0:
        with contextlib.suppress(BaseException):
            os.close(candidate.descriptor)
        candidate.descriptor = -1
    _unlink_candidate(candidate)
    if candidate.parent_descriptor >= 0:
        with contextlib.suppress(BaseException):
            os.close(candidate.parent_descriptor)
        candidate.parent_descriptor = -1


def _prepare_candidate(
    path: Path,
    *,
    directory: PinnedPrivateDirectory | None = None,
) -> _PrivateCandidate:
    path = _absolute_private_path(Path(path))
    if directory is None:
        with pin_private_directory(path.parent) as pinned:
            return _prepare_candidate(path, directory=pinned)
    if path.parent != directory.path:
        raise ValueError(f"Private destination is outside pinned directory: {path}")
    directory.validate()
    parent_descriptor = -1
    descriptor = -1
    temp_name = ""
    try:
        expected_parent_identity = directory.identity
        parent_descriptor = os.dup(directory.descriptor)
        parent_metadata = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise NotADirectoryError(
                f"Private destination parent is not a directory: {directory.path}"
            )
        if _identity(parent_metadata) != expected_parent_identity:
            raise OSError(
                f"Private destination parent changed during publication: {directory.path}"
            )
        _validate_parent_identity(directory.path, expected_parent_identity)

        create_flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        for _ in range(100):
            temp_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    temp_name,
                    create_flags,
                    _PRIVATE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError(f"Could not allocate private temporary file for: {path}")

        if os.name == "posix":
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        return _PrivateCandidate(
            path=path,
            parent_descriptor=parent_descriptor,
            parent_identity=expected_parent_identity,
            temp_name=temp_name,
            descriptor=descriptor,
        )
    except BaseException:
        if descriptor >= 0:
            with contextlib.suppress(BaseException):
                os.close(descriptor)
        if temp_name and parent_descriptor >= 0:
            with contextlib.suppress(BaseException):
                os.unlink(temp_name, dir_fd=parent_descriptor)
        if parent_descriptor >= 0:
            with contextlib.suppress(BaseException):
                os.close(parent_descriptor)
        raise


def _validate_parent_identity(parent: Path, expected: tuple[int, int]) -> None:
    _reject_symlink_components(parent)
    try:
        current = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"Private destination parent changed during publication: {parent}") from exc
    if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected:
        raise OSError(f"Private destination parent changed during publication: {parent}")


def _publish_candidate(candidate: _PrivateCandidate) -> None:
    _validate_parent_identity(candidate.path.parent, candidate.parent_identity)
    backup_name = ""
    try:
        destination = os.stat(
            candidate.path.name,
            dir_fd=candidate.parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        destination_identity: tuple[int, int] | None = None
    else:
        if stat.S_ISLNK(destination.st_mode):
            raise OSError(f"Private destination path is a symlink: {candidate.path}")
        if not stat.S_ISREG(destination.st_mode):
            raise OSError(f"Private destination is not a regular file: {candidate.path}")
        destination_identity = _identity(destination)

    published_identity = _identity(
        os.stat(
            candidate.temp_name,
            dir_fd=candidate.parent_descriptor,
            follow_symlinks=False,
        )
    )
    published = False
    publication_durable = False
    try:
        if destination_identity is not None:
            for _ in range(100):
                backup_name = f".{candidate.path.name}.{secrets.token_hex(12)}.rollback"
                try:
                    os.link(
                        candidate.path.name,
                        backup_name,
                        src_dir_fd=candidate.parent_descriptor,
                        dst_dir_fd=candidate.parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                break
            else:
                raise FileExistsError(
                    f"Could not preserve private destination before publication: {candidate.path}"
                )
            preserved = os.stat(
                backup_name,
                dir_fd=candidate.parent_descriptor,
                follow_symlinks=False,
            )
            current_destination = os.stat(
                candidate.path.name,
                dir_fd=candidate.parent_descriptor,
                follow_symlinks=False,
            )
            if (
                _identity(preserved) != destination_identity
                or _identity(current_destination) != destination_identity
            ):
                raise OSError(f"Private destination changed before publication: {candidate.path}")
            # The rollback link must itself survive a crash before the old name is
            # atomically replaced. Otherwise a failed rename barrier could leave no
            # durable prior state to restore.
            os.fsync(candidate.parent_descriptor)

        os.replace(
            candidate.temp_name,
            candidate.path.name,
            src_dir_fd=candidate.parent_descriptor,
            dst_dir_fd=candidate.parent_descriptor,
        )
        published = True
        _validate_parent_identity(candidate.path.parent, candidate.parent_identity)
        os.fsync(candidate.parent_descriptor)
        publication_durable = True
        if backup_name:
            os.unlink(backup_name, dir_fd=candidate.parent_descriptor)
            backup_name = ""
            os.fsync(candidate.parent_descriptor)
    except BaseException:
        if published and not publication_durable:
            try:
                current = os.stat(
                    candidate.path.name,
                    dir_fd=candidate.parent_descriptor,
                    follow_symlinks=False,
                )
                if _identity(current) != published_identity:
                    raise OSError(f"Private destination changed before rollback: {candidate.path}")
                if backup_name:
                    os.replace(
                        backup_name,
                        candidate.path.name,
                        src_dir_fd=candidate.parent_descriptor,
                        dst_dir_fd=candidate.parent_descriptor,
                    )
                    backup_name = ""
                else:
                    os.unlink(candidate.path.name, dir_fd=candidate.parent_descriptor)
                os.fsync(candidate.parent_descriptor)
            except BaseException as rollback_error:
                recovery_name = backup_name
                backup_name = ""
                raise RuntimeError(
                    f"Private publication failed and rollback was incomplete: {candidate.path}; "
                    f"anchored recovery file: {recovery_name or '<none>'}"
                ) from rollback_error
        raise
    finally:
        if backup_name:
            os.unlink(backup_name, dir_fd=candidate.parent_descriptor)
            os.fsync(candidate.parent_descriptor)


@contextlib.contextmanager
def _private_atomic_handle(
    path: Path,
    *,
    binary: bool,
    encoding: str = "utf-8",
    newline: str | None = None,
    directory: PinnedPrivateDirectory | None = None,
) -> Iterator[BinaryIO | TextIO]:
    candidate = _prepare_candidate(path, directory=directory)
    handle: BinaryIO | TextIO | None = None
    try:
        if binary:
            handle = os.fdopen(candidate.descriptor, "w+b")
        else:
            handle = os.fdopen(candidate.descriptor, "w", encoding=encoding, newline=newline)
        candidate.descriptor = -1
        yield handle
        handle.flush()
        if os.name == "posix":
            os.fchmod(handle.fileno(), _PRIVATE_FILE_MODE)
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        _publish_candidate(candidate)
    finally:
        if handle is not None:
            with contextlib.suppress(BaseException):
                handle.close()
        _close_candidate(candidate)


@contextlib.contextmanager
def private_atomic_binary(path: Path) -> Iterator[BinaryIO]:
    """Yield an already-open private binary file and publish it atomically."""
    with _private_atomic_handle(path, binary=True) as handle:
        yield cast(BinaryIO, handle)


@contextlib.contextmanager
def private_atomic_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Iterator[TextIO]:
    """Yield an already-open private text file and publish it atomically."""
    with _private_atomic_handle(
        path,
        binary=False,
        encoding=encoding,
        newline=newline,
    ) as handle:
        yield cast(TextIO, handle)


def write_private_text_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> Path:
    """Atomically publish owner-only text while preserving an old target on failure."""
    path = Path(path)
    with private_atomic_text(path, encoding=encoding, newline=newline) as handle:
        handle.write(text)
    return path


def tighten_private_file(path: Path) -> Path:
    """Restrict an existing regular file to owner-only access without following links."""
    path = _absolute_private_path(Path(path))
    with _pin_directory(path.parent, create=False, tighten_final=False) as parent:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent.descriptor)
        except OSError as exc:
            if path.is_symlink():
                raise OSError(f"Private file path is a symlink: {path}") from exc
            raise
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise FileNotFoundError(f"Private file not found: {path}")
            file_identity = _identity(metadata)
            if os.name == "posix":
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            parent.validate()
            current = os.stat(path.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or _identity(current) != file_identity:
                raise OSError(f"Private file changed during permission update: {path}")
        finally:
            with contextlib.suppress(BaseException):
                os.close(descriptor)
    return path


def copy_private_file(source: Path, target: Path) -> Path:
    """Atomically copy *source* to an owner-only *target* without copying metadata."""
    source = Path(source)
    target = Path(target)
    with source.open("rb") as source_handle, private_atomic_binary(target) as target_handle:
        shutil.copyfileobj(source_handle, target_handle)
    return target
