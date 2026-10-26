"""
Custom exceptions for the ledger system.

Structured exception hierarchy for proper error handling.
"""


class LedgerError(Exception):
    """Base exception for all ledger errors."""

    pass


class ConfigError(LedgerError):
    """Configuration loading or validation error."""

    pass


class ParseError(LedgerError):
    """Error during file parsing."""

    def __init__(
        self, message: str, file_path: str = "", row: int = 0, details: dict | None = None
    ):
        self.file_path = file_path
        self.row = row
        self.details = details or {}
        super().__init__(f"{message} (file={file_path}, row={row})")


class ValidationError(LedgerError):
    """Data validation error."""

    pass


class DuplicateError(LedgerError):
    """Duplicate transaction detected."""

    def __init__(self, txn_id: str, existing_path: str, new_path: str):
        self.txn_id = txn_id
        self.existing_path = existing_path
        self.new_path = new_path
        super().__init__(f"Duplicate txn_id={txn_id}: {existing_path} vs {new_path}")


class CollisionError(LedgerError):
    """Hash collision detected (same ID, different content)."""

    def __init__(self, txn_id: str, existing_fp: str, new_fp: str):
        self.txn_id = txn_id
        self.existing_fp = existing_fp
        self.new_fp = new_fp
        super().__init__(f"Collision for txn_id={txn_id}: fingerprints differ")


class UnknownFormatError(ParseError):
    """CSV format not recognized."""

    def __init__(self, file_path: str, headers: list[str]):
        self.headers = headers
        super().__init__("Unknown CSV format", file_path=file_path, details={"headers": headers})
