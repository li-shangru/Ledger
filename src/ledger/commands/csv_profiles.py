"""Private CSV mapping, preview, and the shared staged import workflow."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated

import typer

from ledger.adapters.csv_profile import (
    DATE_FORMATS,
    MAX_CSV_BYTES,
    PREVIEW_NOTICE,
    CsvProfile,
    CsvProfileError,
    MappedRow,
    inspect_csv_headers,
    load_profile,
    parse_csv_bytes,
    profile_digest,
    save_profile,
)
from ledger.commands.import_pdf import (
    import_pdf_cancel,
    import_pdf_finalize,
    import_pdf_review_app,
    import_pdf_status,
)
from ledger.runtime import require_external_runtime_path

csv_profiles_app = typer.Typer(help="Map, preview, stage, review, and finalize CSV exports.")

csv_profiles_app.add_typer(import_pdf_review_app, name="review")
csv_profiles_app.command("status")(import_pdf_status)
csv_profiles_app.command("finalize")(import_pdf_finalize)
csv_profiles_app.command("cancel")(import_pdf_cancel)


def _read_source(path: Path) -> bytes:
    try:
        if ".." in path.parts:
            raise CsvProfileError("profile_unsafe_path")
        path = require_external_runtime_path(path)
        if any(component.is_symlink() for component in (path, *path.parents)):
            raise CsvProfileError("profile_unsafe_path")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
            ):
                raise CsvProfileError("profile_unsafe_path")
            if before.st_size > MAX_CSV_BYTES:
                raise CsvProfileError("csv_too_large")
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                content = handle.read(MAX_CSV_BYTES + 1)
            after = os.fstat(descriptor)
            if len(content) > MAX_CSV_BYTES:
                raise CsvProfileError("csv_too_large")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise CsvProfileError("profile_unsafe_path")
            return content
        finally:
            os.close(descriptor)
    except CsvProfileError:
        raise
    except OSError, ValueError:
        raise CsvProfileError("profile_unsafe_path") from None


def _summary(rows: tuple[MappedRow, ...]) -> None:
    typer.echo(PREVIEW_NOTICE)
    typer.echo(
        f"Valid rows: {len(rows)}; distinct currencies: {len({row.currency for row in rows})}."
    )


def _column(headers: tuple[str, ...], role: str) -> str:
    number = typer.prompt(f"Column number for {role}", type=int)
    if not 1 <= number <= len(headers):
        raise CsvProfileError("profile_invalid")
    return headers[number - 1]


@csv_profiles_app.command("stage")
def stage_csv(
    source: Annotated[Path, typer.Argument(help="Explicit private CSV source.")],
    profile: Annotated[Path, typer.Option("--profile")],
    institution: Annotated[
        str, typer.Option("--institution", help="Explicit stable institution key.")
    ],
    account: Annotated[str, typer.Option("--account", help="Explicit stable account key.")],
    account_type: Annotated[str, typer.Option("--account-type")],
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    base_db: Annotated[Path | None, typer.Option("--base-db")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    backup_dir: Annotated[Path | None, typer.Option("--backup-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Stage parsed rows for review; publication remains a separate finalize step."""
    from ledger.adapters.contracts import AdapterError
    from ledger.commands.common import pdf_args
    from ledger.imports.pdf.workflow import build_paths, stage
    from ledger.runtime import resolve_runtime_path

    try:
        source = require_external_runtime_path(source)
        profile = require_external_runtime_path(profile)
        # Validate the complete input before creating a coordinated staging run.
        _summary(parse_csv_bytes(_read_source(source), load_profile(profile)))
        args = pdf_args(
            pdf_dir=source.parent,
            db_path=resolve_runtime_path(db_path, "ledger.db"),
            base_db=base_db,
            processed_dir=resolve_runtime_path(processed_dir, "processed"),
            backup_dir=backup_dir,
            runs_dir=runs_dir,
        )
        args.csv_source, args.csv_profile = source, profile
        args.institution, args.account, args.account_type = institution, account, account_type
        stage(args, build_paths(args))
    except (CsvProfileError, AdapterError) as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None


@csv_profiles_app.command("preview")
def preview(
    source: Annotated[Path, typer.Argument(help="Explicit local CSV path.")],
    profile: Annotated[Path, typer.Option("--profile", help="Explicit private profile JSON path.")],
    show_rows: Annotated[
        bool, typer.Option("--show-rows", help="Print private parsed rows locally.")
    ] = False,
) -> None:
    """Validate all rows against a saved exact layout; default output is counts."""
    try:
        rows = parse_csv_bytes(_read_source(source), load_profile(profile))
        _summary(rows)
        if show_rows:
            typer.echo("PRIVATE OUTPUT: parsed transaction rows follow.")
            # JSON escaping keeps embedded terminal controls inert and dates ISO.
            import json

            for row in rows:
                typer.echo(
                    json.dumps(
                        {
                            "source_row": row.source_row,
                            "transaction_date": row.transaction_date.isoformat(),
                            "description": row.description,
                            "amount": str(row.amount),
                            "currency": row.currency,
                            "reference": row.reference,
                        },
                        ensure_ascii=True,
                    )
                )
    except CsvProfileError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None


@csv_profiles_app.command("map")
def map_csv(
    source: Annotated[Path, typer.Argument(help="Explicit local CSV path to preview.")],
    destination: Annotated[
        Path, typer.Option("--save-to", help="New profile in an existing private directory.")
    ],
) -> None:
    """Ask for every interpretation, validate a preview, then confirm profile save."""
    try:
        content = _read_source(source)
        profile_id = typer.prompt("Profile ID (letters, digits, hyphen or underscore)")
        delimiter_name = typer.prompt("Delimiter (comma, semicolon, tab, pipe)", default="comma")
        delimiters = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
        if delimiter_name not in delimiters:
            raise CsvProfileError("profile_invalid")
        delimiter = delimiters[delimiter_name]
        header_row = typer.prompt("Header record number (1-based)", default=1, type=int)
        headers = inspect_csv_headers(content, delimiter=delimiter, header_row=header_row)
        typer.echo("PRIVATE OUTPUT: exact header choices follow.")
        for index, header in enumerate(headers, start=1):
            typer.echo(f"{index}: {header!r}")
        values: dict[str, object] = {
            "profile_id": profile_id,
            "delimiter": delimiter,
            "header_row": header_row,
            "headers": headers,
            "date_column": _column(headers, "date"),
        }
        typer.echo("Supported exact date formats: " + ", ".join(DATE_FORMATS))
        values["date_format"] = typer.prompt("Exact date format", default="%Y-%m-%d")
        values["description_column"] = _column(headers, "description")
        mode = typer.prompt("Money mode (amount or debit_credit)")
        if mode == "amount":
            values["amount_column"] = _column(headers, "amount")
            values["sign_convention"] = typer.prompt(
                "Sign convention (inflow_positive or outflow_positive)"
            )
        elif mode == "debit_credit":
            values["debit_column"] = _column(headers, "debit/outflow")
            values["credit_column"] = _column(headers, "credit/inflow")
        else:
            raise CsvProfileError("profile_invalid")
        currency_mode = typer.prompt("Currency mode (fixed or column)")
        if currency_mode == "fixed":
            values["currency"] = typer.prompt("Exact uppercase three-letter currency code")
        elif currency_mode == "column":
            values["currency_column"] = _column(headers, "currency")
        else:
            raise CsvProfileError("profile_invalid")
        if typer.confirm("Map a reference column?", default=False):
            values["reference_column"] = _column(headers, "reference")
        profile = CsvProfile(**values)
        _summary(parse_csv_bytes(content, profile))
        typer.echo("Only this format profile will be saved. No account binding is included.")
        if not typer.confirm(
            "Save this validated profile to the explicit private destination?", default=False
        ):
            typer.echo("Profile not saved.")
            return
        save_profile(destination, profile)
        typer.echo(f"Profile saved; format digest: {profile_digest(profile)}")
    except CsvProfileError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None
