# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Which configured folders can the reader open, and is mail being filtered away (design §5.3)?"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from emi_reader.errors import ImapConnectionError
from emi_reader.keywords import ReaderKeyword
from emi_reader.transport import FolderInfo, FolderStatus, ImapTransport

_UNSELECTABLE_FLAGS = frozenset({"\\noselect", "\\nonexistent"})
_JUNK_FLAG = "\\junk"


class Access(StrEnum):
    """Classification of one account/folder pair."""

    ACCESSIBLE = "accessible"
    NOT_LISTED = "not_listed"
    NOSELECT = "noselect"
    SELECT_DENIED = "select_denied"
    LOGIN_FAILED = "login_failed"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class FolderAccess:
    """Probe result for one folder.

    Attributes:
        account: Account label (the login name).
        folder: Folder name.
        state: Classification.
        junk: Whether the server marks it ``\\Junk`` (RFC 6154).
        status: Counters, when the server still answers ``STATUS`` (used to observe filtering).
        detail: Keyword or message explaining an inaccessible state.
    """

    account: str
    folder: str
    state: Access
    junk: bool = False
    status: FolderStatus | None = None
    detail: str = ""

    @property
    def accessible(self) -> bool:
        """Whether the reader can search this folder."""
        return self.state is Access.ACCESSIBLE


def _status_or_none(transport: ImapTransport, folder: str) -> FolderStatus | None:
    """Return STATUS counters, or ``None`` when the server refuses them too."""
    try:
        return transport.status(folder)
    except ImapConnectionError:
        return None


def _probe_folder(
    transport: ImapTransport, account: str, folder: str, listed: dict[str, FolderInfo]
) -> FolderAccess:
    """Classify one folder of an already opened account."""
    info = listed.get(folder.lower())
    if info is None:
        return FolderAccess(account, folder, Access.NOT_LISTED, detail="folder not in LIST")
    junk = _JUNK_FLAG in info.flags
    if info.flags & _UNSELECTABLE_FLAGS:
        return FolderAccess(account, folder, Access.NOSELECT, junk=junk, detail="\\Noselect")
    try:
        status = transport.examine(folder)
    except ImapConnectionError as error:
        if error.primary != ReaderKeyword.IMAP_PROTOCOL_ERROR:
            raise
        return FolderAccess(account, folder, Access.SELECT_DENIED, junk=junk,
                            status=_status_or_none(transport, folder), detail=str(error))
    return FolderAccess(account, folder, Access.ACCESSIBLE, junk=junk, status=status)


def probe_account(
    transport: ImapTransport, account: str, folders: tuple[str, ...], *, raise_retryable: bool = False
) -> list[FolderAccess]:
    """Open the account if needed and classify each configured folder.

    Args:
        transport: The account's connection (opened by this call if it is not yet open).
        account: Account label for the report.
        folders: Folders to classify.
        raise_retryable: Re-raise faults a reconnect may heal instead of recording them, so a
            waiting caller retries rather than declaring the folders inaccessible.

    Returns:
        One entry per folder. Login and network failures mark every folder of the account.

    Raises:
        ImapConnectionError: A retryable fault, when ``raise_retryable`` is set.
    """
    try:
        transport.open()
        listed = {info.name.lower(): info for info in transport.list_folders()}
        return [_probe_folder(transport, account, folder, listed) for folder in folders]
    except ImapConnectionError as error:
        if raise_retryable and error.retryable:
            raise
        login_failed = error.primary == ReaderKeyword.IMAP_AUTH_FAILED
        state = Access.LOGIN_FAILED if login_failed else Access.NETWORK_ERROR
        return [FolderAccess(account, folder, state, detail=error.primary) for folder in folders]


def matrix_rows(entries: list[FolderAccess]) -> list[dict[str, Any]]:
    """Return the accessibility matrix as report-friendly rows.

    Args:
        entries: Probe results.

    Returns:
        One dictionary per folder.
    """
    return [
        {"account": entry.account, "folder": entry.folder, "state": str(entry.state),
         "junk": entry.junk, "detail": entry.detail}
        for entry in entries
    ]


def filter_evidence(
    before: list[FolderAccess], after: list[FolderAccess], risk_folders: tuple[str, ...]
) -> list[str]:
    """List the signals that mail may have gone to a folder the reader cannot open.

    Any one is enough (design §5.3): a declared risk folder that is inaccessible; a denied folder
    whose ``UIDNEXT`` grew during the wait; an inaccessible folder flagged ``\\Junk``.

    Args:
        before: Probe at wait start.
        after: Probe at wait end.
        risk_folders: Folders declared as filter targets (``filter_risk_folders``).

    Returns:
        Human-readable signals; empty when there is no evidence.
    """
    risky = {name.lower() for name in risk_folders}
    start = {(entry.account, entry.folder): entry for entry in before}
    signals: list[str] = []
    for entry in after:
        if entry.accessible:
            continue
        where = f"{entry.account}/{entry.folder}"
        if entry.folder.lower() in risky:
            signals.append(f"declared filter folder {where} is {entry.state}")
        if entry.junk:
            signals.append(f"\\Junk folder {where} is {entry.state}")
        earlier = start.get((entry.account, entry.folder))
        if (earlier is not None and earlier.status is not None and entry.status is not None
                and entry.status.uidnext > earlier.status.uidnext):
            signals.append(f"new mail arrived in unreadable {where} "
                           f"(UIDNEXT {earlier.status.uidnext} -> {entry.status.uidnext})")
    return signals
