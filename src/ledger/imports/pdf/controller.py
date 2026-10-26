"""In-process controller for the Streamlit PDF import workflow."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ledger.imports.pdf.manifest import read_manifest
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import (
    FINALIZABLE_STATUSES,
    OpenImportCoordinationError,
    build_import_readiness,
    open_runs_for_target,
    validate_publication_protocol_state,
)
from ledger.utils.private_files import pin_existing_directory, pin_private_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None  # type: ignore[assignment]


_UPLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+&'-]{0,199}\.pdf", re.IGNORECASE)
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_INBOX_ENTRIES = 4096


class UploadRejectedError(ValueError):
    """The browser upload is not an allowlisted PDF."""


class UploadConflictError(FileExistsError):
    """The requested inbox name is already occupied by different content."""


class ActionUnavailableError(RuntimeError):
    """The latest protocol/readiness state does not admit the requested action."""


class ImportCenterOperationError(RuntimeError):
    """An existing workflow operation failed after the controller dispatched it."""

    def __init__(self, message: str, *, output: str = ""):
        super().__init__(message)
        self.output = output


@dataclass(frozen=True)
class UploadReceipt:
    """Result of accepting one browser-uploaded statement."""

    path: Path
    sha256: str
    status: Literal["stored", "already_present"]


@dataclass(frozen=True)
class ImportCenterSnapshot:
    """Current run state and the actions admitted by its live gates."""

    status: str
    run_dir: Path | None
    inbox_pdf_count: int
    actions: tuple[str, ...]
    manifest: dict[str, Any] | None
    readiness: dict[str, Any] | None
    blockers: tuple[str, ...]
    diagnostics: tuple[str, ...]
    protocol_valid: bool
    publication_phase: str | None
    publication_completed: bool


@dataclass(frozen=True)
class ImportOperationResult:
    """Observable result of one controller mutation."""

    action: str
    changed: bool
    message: str
    output: str
    snapshot: ImportCenterSnapshot


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _validate_upload(filename: str, content: bytes) -> None:
    if not isinstance(filename, str) or not _UPLOAD_NAME.fullmatch(filename):
        raise UploadRejectedError(
            "Use a plain PDF filename containing only letters, numbers, spaces, and safe punctuation."
        )
    if len(content) > _MAX_UPLOAD_BYTES:
        raise UploadRejectedError("The uploaded PDF exceeds the 200 MiB limit.")
    if not content.startswith(b"%PDF-"):
        raise UploadRejectedError("The uploaded file does not have a PDF header.")


@contextlib.contextmanager
def _upload_lock(directory_descriptor: int) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(".upload.lock", flags, 0o600, dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UploadConflictError("The inbox upload lock is not a regular file.")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _child_digest(directory_descriptor: int, name: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UploadConflictError(f"Inbox destination already exists and is not a file: {name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if expected != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or expected != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise OSError(f"Inbox file changed while hashing: {name}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _existing_target_digest(directory_descriptor: int, filename: str) -> str | None:
    try:
        metadata = os.stat(filename, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise UploadConflictError(f"Inbox destination already exists: {filename}")
    try:
        return _child_digest(directory_descriptor, filename)
    except OSError as exc:
        raise UploadConflictError(f"Inbox destination already exists: {filename}") from exc


def _matching_upload(
    directory_descriptor: int,
    *,
    filename: str,
    digest: str,
) -> str | None:
    existing_target = _existing_target_digest(directory_descriptor, filename)
    if existing_target is not None:
        if existing_target == digest:
            return filename
        raise UploadConflictError(
            f"Inbox destination already exists with different content: {filename}"
        )

    names = os.listdir(directory_descriptor)
    if len(names) > _MAX_INBOX_ENTRIES:
        raise UploadRejectedError("The PDF inbox contains too many entries to scan safely.")
    for name in sorted(names):
        if name == filename or Path(name).suffix != ".pdf":
            continue
        try:
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if (
                stat.S_ISREG(metadata.st_mode)
                and _child_digest(directory_descriptor, name) == digest
            ):
                return name
        except OSError:
            # A changed or non-readable unrelated entry cannot authorize an idempotent
            # result. The O_EXCL publication below remains the no-clobber boundary.
            continue
    return None


def accept_uploaded_pdf(inbox: Path, filename: str, content: bytes) -> UploadReceipt:
    """Securely accept one PDF with no-clobber, content-hash idempotency."""
    content = bytes(content)
    _validate_upload(filename, content)
    filename = f"{filename[:-4]}.pdf"
    digest = hashlib.sha256(content).hexdigest()
    with pin_private_directory(inbox) as pinned, _upload_lock(pinned.descriptor):
        existing_name = _matching_upload(
            pinned.descriptor,
            filename=filename,
            digest=digest,
        )
        if existing_name is not None:
            pinned.validate()
            return UploadReceipt(inbox / existing_name, digest, "already_present")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(filename, flags, 0o600, dir_fd=pinned.descriptor)
        except FileExistsError:
            # An external writer may not honor our lock. Resolve that race through
            # the same hash/no-clobber policy instead of replacing its file.
            existing_digest = _existing_target_digest(pinned.descriptor, filename)
            if existing_digest == digest:
                pinned.validate()
                return UploadReceipt(inbox / filename, digest, "already_present")
            raise UploadConflictError(
                f"Inbox destination already exists with different content: {filename}"
            ) from None

        created_identity = (0, 0)
        try:
            metadata = os.fstat(descriptor)
            created_identity = (metadata.st_dev, metadata.st_ino)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(pinned.descriptor)
            pinned.validate()
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(OSError):
                current = os.stat(
                    filename,
                    dir_fd=pinned.descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == created_identity:
                    os.unlink(filename, dir_fd=pinned.descriptor)
            raise
    return UploadReceipt(inbox / filename, digest, "stored")


class ImportCenterController:
    """Thin state/action adapter over the existing assisted PDF workflow."""

    def __init__(self, paths: Paths):
        self.paths = Paths(
            pdf_dir=paths.pdf_dir.expanduser().resolve(),
            processed_dir=paths.processed_dir.expanduser().resolve(),
            db_path=paths.db_path.expanduser().resolve(),
            base_db_path=(
                paths.base_db_path.expanduser().resolve() if paths.base_db_path else None
            ),
            backup_dir=paths.backup_dir.expanduser().resolve(),
            ledger_config=paths.ledger_config.expanduser().resolve(),
            merchant_rules=paths.merchant_rules.expanduser().resolve(),
            categories=paths.categories.expanduser().resolve(),
            runs_dir=paths.runs_dir.expanduser().resolve(),
        )

    def upload(self, filename: str, content: bytes) -> UploadReceipt:
        return accept_uploaded_pdf(self.paths.pdf_dir, filename, content)

    def _inbox_pdf_count(self) -> tuple[int, str | None]:
        inbox = self.paths.pdf_dir
        if not inbox.exists():
            return 0, None
        if inbox.is_symlink() or not inbox.is_dir():
            return 0, "The configured PDF inbox is not a regular directory."
        try:
            with pin_existing_directory(inbox) as pinned:
                names = os.listdir(pinned.descriptor)
                if len(names) > _MAX_INBOX_ENTRIES:
                    return 0, "The PDF inbox contains too many entries to inspect safely."
                count = 0
                for name in names:
                    if Path(name).suffix != ".pdf":
                        continue
                    metadata = os.stat(name, dir_fd=pinned.descriptor, follow_symlinks=False)
                    count += int(stat.S_ISREG(metadata.st_mode))
                pinned.validate()
                return count, None
        except OSError as exc:
            return 0, f"Could not inspect the PDF inbox: {exc}"

    def _contained_run(self, run_dir: Path) -> Path | None:
        runs_root = self.paths.runs_dir
        lexical = _absolute_lexical(run_dir)
        if lexical.parent != runs_root or lexical.is_symlink() or not lexical.is_dir():
            return None
        try:
            resolved = lexical.resolve(strict=True)
        except OSError:
            return None
        if resolved != lexical:
            return None
        return lexical

    def _latest_recorded_run(self) -> tuple[Path | None, list[str]]:
        diagnostics: list[str] = []
        try:
            open_runs = open_runs_for_target(self.paths.db_path, self.paths.runs_dir)
        except OpenImportCoordinationError as exc:
            return None, [f"Import-run coordination is unreadable: {exc}"]
        contained_open = [run for run in open_runs if self._contained_run(run) is not None]
        if len(contained_open) > 1:
            return None, [
                "More than one import run can still promote; resolve them outside the UI."
            ]
        if len(contained_open) == 1:
            return contained_open[0], diagnostics
        if open_runs:
            return None, ["An open import run is outside the configured import-runs directory."]

        latest = self.paths.runs_dir / "latest-run.txt"
        if latest.is_file() and not latest.is_symlink():
            try:
                candidate = self._contained_run(Path(latest.read_text(encoding="utf-8").strip()))
            except OSError as exc:
                diagnostics.append(f"Could not read the latest import-run pointer: {exc}")
            else:
                if candidate is not None:
                    return candidate, diagnostics
                diagnostics.append("The latest import-run pointer is outside its configured root.")

        root = self.paths.runs_dir
        if not root.is_dir() or root.is_symlink():
            return None, diagnostics
        try:
            candidates = sorted(
                (
                    path
                    for path in root.iterdir()
                    if path.is_dir() and not path.is_symlink() and (path / "run.json").is_file()
                ),
                reverse=True,
            )
        except OSError as exc:
            diagnostics.append(f"Could not inspect import-run history: {exc}")
            return None, diagnostics
        return (candidates[0] if candidates else None), diagnostics

    @staticmethod
    def _actions_for(
        *,
        status: str,
        inbox_pdf_count: int,
        readiness: dict[str, Any] | None,
        run_state: dict[str, Any] | None,
        protocol_valid: bool,
    ) -> tuple[str, ...]:
        actions = ["upload"]
        if not protocol_valid:
            return tuple(actions)

        if run_state is None:
            if inbox_pdf_count:
                actions.append("stage")
            return tuple(actions)

        recovery = status in {"finalizing", "finalize_failed"} or (
            status == "failed" and run_state.get("failure_reason") == "finalize_failed"
        )
        if recovery:
            actions.append("resume_finalize")
            return tuple(actions)

        if status == "staging":
            actions.append("cancel")
            return tuple(actions)

        if status in FINALIZABLE_STATUSES:
            pending = int(((readiness or {}).get("counts") or {}).get("pending_review") or 0)
            if pending:
                actions.append("review")
            if bool((readiness or {}).get("can_finalize")):
                actions.append("finalize")
            actions.append("cancel")
            return tuple(actions)

        if status == "failed":
            actions.append("cancel")
            return tuple(actions)

        if status in {"finalized", "cancelled"} and inbox_pdf_count:
            actions.append("stage")
        return tuple(actions)

    def snapshot(self, *, run_dir: Path | None = None) -> ImportCenterSnapshot:
        inbox_pdf_count, inbox_diagnostic = self._inbox_pdf_count()
        diagnostics = [inbox_diagnostic] if inbox_diagnostic else []
        if run_dir is not None:
            selected_run = self._contained_run(run_dir)
            if selected_run is None:
                diagnostics.append("The selected import run is outside its configured root.")
        else:
            selected_run, discovery_diagnostics = self._latest_recorded_run()
            diagnostics.extend(discovery_diagnostics)

        if selected_run is None:
            actions = self._actions_for(
                status="idle",
                inbox_pdf_count=inbox_pdf_count,
                readiness=None,
                run_state=None,
                protocol_valid=not diagnostics,
            )
            return ImportCenterSnapshot(
                status="idle",
                run_dir=None,
                inbox_pdf_count=inbox_pdf_count,
                actions=actions,
                manifest=None,
                readiness=None,
                blockers=(),
                diagnostics=tuple(diagnostics),
                protocol_valid=not diagnostics,
                publication_phase=None,
                publication_completed=False,
            )

        try:
            run_state = json.loads((selected_run / "run.json").read_text(encoding="utf-8"))
            if not isinstance(run_state, dict):
                raise ValueError("run.json is not an object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            diagnostics.append(f"Could not read import run state: {exc}")
            return ImportCenterSnapshot(
                status="unknown",
                run_dir=selected_run,
                inbox_pdf_count=inbox_pdf_count,
                actions=("upload",),
                manifest=None,
                readiness=None,
                blockers=(),
                diagnostics=tuple(diagnostics),
                protocol_valid=False,
                publication_phase=None,
                publication_completed=False,
            )

        protocol_valid = True
        try:
            validate_publication_protocol_state(run_state)
        except ValueError:
            protocol_valid = False
            diagnostics.append(
                "The import publication protocol state is invalid; UI mutations are disabled."
            )

        recorded_target = run_state.get("target_db_path")
        if not isinstance(recorded_target, str) or (
            Path(recorded_target).expanduser().resolve() != self.paths.db_path
        ):
            protocol_valid = False
            diagnostics.append(
                "The import run targets a different database; UI mutations are disabled."
            )

        staging_value = run_state.get("staging_db_path")
        expected_staging = selected_run / "staging.db"
        if (
            not isinstance(staging_value, str)
            or _absolute_lexical(staging_value) != expected_staging
            or expected_staging.is_symlink()
        ):
            protocol_valid = False
            diagnostics.append(
                "The import run's staging database is outside its run; UI mutations are disabled."
            )

        csv_value = run_state.get("csv_root")
        expected_csv = selected_run / "csv"
        if (
            not isinstance(csv_value, str)
            or _absolute_lexical(csv_value) != expected_csv
            or expected_csv.is_symlink()
        ):
            protocol_valid = False
            diagnostics.append(
                "The import run's staged CSV root is outside its run; UI mutations are disabled."
            )

        manifest: dict[str, Any] | None = None
        readiness: dict[str, Any] | None = None
        if protocol_valid:
            try:
                manifest = read_manifest(selected_run)
                readiness = build_import_readiness(selected_run)
                manifest = {**manifest, "readiness": readiness}
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                protocol_valid = False
                diagnostics.append(f"Could not build live import readiness: {exc}")
        else:
            with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
                manifest = read_manifest(selected_run)

        status = str(run_state.get("status", "unknown"))
        blockers = tuple(str(item) for item in (readiness or {}).get("blockers", ()))
        actions = self._actions_for(
            status=status,
            inbox_pdf_count=inbox_pdf_count,
            readiness=readiness,
            run_state=run_state,
            protocol_valid=protocol_valid,
        )
        return ImportCenterSnapshot(
            status=status,
            run_dir=selected_run,
            inbox_pdf_count=inbox_pdf_count,
            actions=actions,
            manifest=manifest,
            readiness=readiness,
            blockers=blockers,
            diagnostics=tuple(diagnostics),
            protocol_valid=protocol_valid,
            publication_phase=(
                str(run_state["publication_phase"])
                if run_state.get("publication_phase") is not None
                else None
            ),
            publication_completed=run_state.get("publication_completed") is True,
        )

    @staticmethod
    def _invoke(action: str, callback: Any) -> str:
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                callback()
        except SystemExit as exc:
            message = str(exc.code) if exc.code else f"{action} did not complete."
            raise ImportCenterOperationError(message, output=output.getvalue()) from None
        except Exception as exc:
            raise ImportCenterOperationError(str(exc), output=output.getvalue()) from exc
        return output.getvalue()

    @staticmethod
    def _no_op(action: str, message: str, snapshot: ImportCenterSnapshot) -> ImportOperationResult:
        return ImportOperationResult(
            action=action,
            changed=False,
            message=message,
            output="",
            snapshot=snapshot,
        )

    def stage(self) -> ImportOperationResult:
        """Stage the current inbox through ``workflow.stage`` in this process."""
        from ledger.imports.pdf import workflow

        before = self.snapshot()
        if "stage" not in before.actions:
            if before.run_dir is not None and before.protocol_valid:
                return self._no_op(
                    "stage",
                    "An import run is already recorded; no duplicate stage was started.",
                    before,
                )
            reason = " ".join((*before.blockers, *before.diagnostics))
            raise ActionUnavailableError(reason or "There are no uploaded PDFs to stage.")

        output = self._invoke("Stage", lambda: workflow.stage(argparse.Namespace(), self.paths))
        after = self.snapshot()
        return ImportOperationResult(
            action="stage",
            changed=True,
            message=f"Stage completed with status {after.status}.",
            output=output,
            snapshot=after,
        )

    def _selected_snapshot(self, run_dir: Path | None) -> ImportCenterSnapshot:
        selected = self.snapshot(run_dir=run_dir) if run_dir is not None else self.snapshot()
        if selected.run_dir is None:
            raise ActionUnavailableError("No import run is available.")
        return selected

    def review_summary(self, *, run_dir: Path | None = None) -> dict[str, Any]:
        """Read one run's queue through the workflow's staging-only review API."""
        from ledger.imports.pdf import workflow

        selected = self._selected_snapshot(run_dir)
        if "review" not in selected.actions:
            raise ActionUnavailableError("This import run has no pending review decisions.")
        output = self._invoke(
            "Review list",
            lambda: workflow.review_list(
                selected.run_dir,  # type: ignore[arg-type]
                status="all",
                json_output=True,
            ),
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:  # pragma: no cover - fixed workflow contract
            raise ImportCenterOperationError("The review service returned invalid JSON.") from exc
        if not isinstance(payload, dict):  # pragma: no cover - fixed workflow contract
            raise ImportCenterOperationError("The review service returned an invalid result.")
        return payload

    def finalize(self, *, run_dir: Path | None = None) -> ImportOperationResult:
        """Finalize or resume recovery through the existing publication state machine."""
        from ledger.imports.pdf import workflow

        before = self._selected_snapshot(run_dir)
        if before.status == "finalized":
            return self._no_op(
                "finalize",
                "This run is already finalized; no second publication was attempted.",
                before,
            )
        admitted = "resume_finalize" if "resume_finalize" in before.actions else "finalize"
        if admitted not in before.actions:
            reason = " ".join((*before.blockers, *before.diagnostics))
            raise ActionUnavailableError(reason or "This import run is not ready to finalize.")

        output = self._invoke(
            "Finalize",
            lambda: workflow.finalize(argparse.Namespace(run_dir=before.run_dir), self.paths),
        )
        after = self.snapshot(run_dir=before.run_dir)
        return ImportOperationResult(
            action=admitted,
            changed=True,
            message=f"Finalize completed with status {after.status}.",
            output=output,
            snapshot=after,
        )

    def cancel(
        self,
        *,
        run_dir: Path | None = None,
        reason: str = "ui_cancel",
    ) -> ImportOperationResult:
        """Cancel a discardable staged run through the existing locked workflow."""
        from ledger.imports.pdf import workflow

        before = self._selected_snapshot(run_dir)
        if before.status == "cancelled":
            return self._no_op(
                "cancel",
                "This run is already cancelled.",
                before,
            )
        if "cancel" not in before.actions:
            reason_text = " ".join((*before.blockers, *before.diagnostics))
            raise ActionUnavailableError(
                reason_text or "This import run cannot be cancelled in its current state."
            )
        output = self._invoke(
            "Cancel",
            lambda: workflow.cancel(
                argparse.Namespace(run_dir=before.run_dir, reason=reason),
                self.paths,
            ),
        )
        after = self.snapshot(run_dir=before.run_dir)
        return ImportOperationResult(
            action="cancel",
            changed=True,
            message="Import run cancelled.",
            output=output,
            snapshot=after,
        )
