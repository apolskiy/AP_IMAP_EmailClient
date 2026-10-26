# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Deliver composed emails into the shared mailbox by SMTP or IMAP APPEND (design §6.4).

Messages are always serialized with CRLF line endings (``policy.SMTP``): ``smtplib`` dot-stuffs bytes
without fixing bare LF, and Greenmail then stores a doubled dot that corrupts links (Spike Evidence).
"""

import imaplib
import smtplib
import socket
import ssl
import time
from collections.abc import Callable
from email import policy
from typing import Protocol

from emi_sender.compose import ComposedEmail
from emi_sender.errors import DeliveryError
from emi_sender.keywords import SenderKeyword
from emi_sender.settings import SenderSettings, Security, Transport

# Server is down or unreachable: no retry helps; the CLI exits with code 3.
UNREACHABLE_KEYWORDS = frozenset({
    SenderKeyword.SMTP_SERVER_UNREACHABLE,
    SenderKeyword.SMTP_CONNECTION_REFUSED,
    SenderKeyword.SMTP_CONNECTION_TIMEOUT,
})


def serialize(composed: ComposedEmail) -> bytes:
    """Return the message bytes with CRLF line endings.

    Args:
        composed: The email.

    Returns:
        Bytes safe for SMTP DATA and IMAP APPEND.
    """
    return composed.message.as_bytes(policy=policy.SMTP)


def _root(error: BaseException) -> BaseException:
    """The original socket error hidden inside smtplib/imaplib wrappers, if any."""
    cause = error.__cause__ or error.__context__
    return cause if isinstance(cause, OSError) else error


def _fatal(keyword: SenderKeyword, error: BaseException, endpoint: str) -> DeliveryError:
    """A failure no retry can heal."""
    return DeliveryError(f"{keyword}: {type(error).__name__}: {error}", keyword, infrastructure=True,
                         details={"endpoint": endpoint, "error_type": type(error).__name__})


def _healable(keyword: SenderKeyword, error: BaseException, endpoint: str) -> DeliveryError:
    """A mid-session failure a reconnect may heal."""
    return DeliveryError(f"{keyword}: {type(error).__name__}: {error}", keyword, retryable=True,
                         infrastructure=True, details={"endpoint": endpoint, "error_type": type(error).__name__})


_CONNECT_FATAL: tuple[tuple[type[BaseException], SenderKeyword], ...] = (
    (socket.gaierror, SenderKeyword.SMTP_SERVER_UNREACHABLE),
    (ConnectionRefusedError, SenderKeyword.SMTP_CONNECTION_REFUSED),
    (TimeoutError, SenderKeyword.SMTP_CONNECTION_TIMEOUT),
    (ssl.SSLError, SenderKeyword.SMTP_TLS_HANDSHAKE_FAILED),
    (smtplib.SMTPAuthenticationError, SenderKeyword.SMTP_AUTH_FAILED),
    (smtplib.SMTPConnectError, SenderKeyword.SMTP_SERVER_UNREACHABLE),
    (OSError, SenderKeyword.SMTP_SERVER_UNREACHABLE),
)


def classify_connect(error: BaseException, endpoint: str) -> DeliveryError:
    """Classify a failure while connecting or logging in (never retried).

    Args:
        error: What was raised.
        endpoint: ``host:port``.

    Returns:
        The keyworded error.
    """
    root = _root(error)
    for error_type, keyword in _CONNECT_FATAL:
        if isinstance(error, error_type) or isinstance(root, error_type):
            return _fatal(keyword, root, endpoint)
    return _fatal(SenderKeyword.SMTP_SERVER_UNREACHABLE, error, endpoint)


def classify_send(error: BaseException, endpoint: str) -> DeliveryError:
    """Classify a failure while sending one message.

    Args:
        error: What was raised.
        endpoint: ``host:port``.

    Returns:
        The keyworded error; mid-session network faults are retryable.
    """
    root = _root(error)
    result: DeliveryError
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        result = DeliveryError(f"recipient rejected: {error.recipients}", SenderKeyword.SMTP_RECIPIENT_REJECTED,
                               details={"endpoint": endpoint})
    elif isinstance(error, smtplib.SMTPResponseException) and 400 <= error.smtp_code < 500:
        result = _healable(SenderKeyword.SMTP_MESSAGE_REJECTED, error, endpoint)  # 4xx: try again later
    elif isinstance(error, (smtplib.SMTPDataError, smtplib.SMTPSenderRefused)):
        result = DeliveryError(f"message rejected: {error}", SenderKeyword.SMTP_MESSAGE_REJECTED,
                               details={"endpoint": endpoint})
    elif isinstance(root, TimeoutError):
        result = _healable(SenderKeyword.SMTP_READ_TIMEOUT, root, endpoint)
    elif isinstance(root, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)) and root is not error:
        result = _healable(SenderKeyword.SMTP_SOCKET_WRITE_FAILED, root, endpoint)
    else:
        result = _healable(SenderKeyword.SMTP_CONNECTION_DROPPED, root, endpoint)
    return result


class Delivery(Protocol):
    """One connection that delivers messages."""

    def deliver(self, composed: ComposedEmail) -> None:
        """Deliver one message or raise :class:`DeliveryError`."""

    def close(self) -> None:
        """Close the connection; never raises."""


class SmtpDelivery:
    """SMTP delivery: envelope to the shared base mailbox, header ``To`` = the test address."""

    def __init__(self, settings: SenderSettings,
                 factory: Callable[[SenderSettings], smtplib.SMTP] | None = None) -> None:
        """Create an unconnected delivery.

        Args:
            settings: Delivery settings.
            factory: Builds a connected ``smtplib.SMTP`` (a simulated one in unit tests).
        """
        self._settings = settings
        self._factory = factory or _connect_smtp
        self._client: smtplib.SMTP | None = None

    def _connection(self) -> smtplib.SMTP:
        """Return the open connection, connecting first if needed."""
        if self._client is None:
            try:
                self._client = self._factory(self._settings)
            except (OSError, smtplib.SMTPException) as error:
                raise classify_connect(error, self._settings.endpoint) from error
        return self._client

    def deliver(self, composed: ComposedEmail) -> None:
        """Send one message.

        Args:
            composed: The email.

        Raises:
            DeliveryError: Keyworded failure.
        """
        client = self._connection()
        try:
            client.sendmail(self._settings.from_address, [self._settings.base_mailbox], serialize(composed))
        except (OSError, smtplib.SMTPException) as error:
            self.close()
            raise classify_send(error, self._settings.endpoint) from error

    def close(self) -> None:
        """Close the connection; never raises."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()


def _connect_smtp(settings: SenderSettings) -> smtplib.SMTP:
    """Open an SMTP connection per the settings."""
    context = ssl.create_default_context()
    if not settings.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if settings.security is Security.SSL:
        client: smtplib.SMTP = smtplib.SMTP_SSL(settings.host, settings.effective_smtp_port,
                                                timeout=settings.connect_timeout, context=context)
    else:
        client = smtplib.SMTP(settings.host, settings.effective_smtp_port, timeout=settings.connect_timeout)
        if settings.security is Security.STARTTLS:
            client.starttls(context=context)
    if client.sock is not None:
        client.sock.settimeout(settings.read_timeout)
    if settings.smtp_login:
        client.login(settings.username, settings.password)
    return client


class AppendDelivery:
    """IMAP ``APPEND`` straight into the shared mailbox (exact timing for pre-arrived cases)."""

    def __init__(self, settings: SenderSettings) -> None:
        """Create an unconnected delivery.

        Args:
            settings: Delivery settings.
        """
        self._settings = settings
        self._client: imaplib.IMAP4 | None = None

    def _connection(self) -> imaplib.IMAP4:
        """Return the open, logged-in connection."""
        if self._client is None:
            settings = self._settings
            try:
                if settings.security is Security.SSL:
                    client: imaplib.IMAP4 = imaplib.IMAP4_SSL(settings.host, settings.effective_imap_port,
                                                              timeout=settings.connect_timeout)
                else:
                    client = imaplib.IMAP4(settings.host, settings.effective_imap_port,
                                           timeout=settings.connect_timeout)
                client.login(settings.username, settings.password)
            except imaplib.IMAP4.error as error:  # login rejected: no retry helps
                raise _fatal(SenderKeyword.IMAP_APPEND_FAILED, error, settings.endpoint) from error
            except OSError as error:
                raise classify_connect(error, settings.endpoint) from error
            self._client = client
        return self._client

    def deliver(self, composed: ComposedEmail) -> None:
        """Append one message to the folder.

        Args:
            composed: The email.

        Raises:
            DeliveryError: Keyworded failure.
        """
        client = self._connection()
        try:
            result, data = client.append(self._settings.folder, None, None, serialize(composed))
        except (OSError, imaplib.IMAP4.abort) as error:
            self.close()
            raise _healable(SenderKeyword.IMAP_APPEND_FAILED, _root(error), self._settings.endpoint) from error
        if result != "OK":
            raise DeliveryError(f"APPEND returned {result}: {data!r}", SenderKeyword.IMAP_APPEND_FAILED,
                                details={"endpoint": self._settings.endpoint, "folder": self._settings.folder})

    def close(self) -> None:
        """Log out; never raises."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def make_delivery(settings: SenderSettings) -> Delivery:
    """Build the delivery for the configured transport.

    Args:
        settings: Delivery settings.

    Returns:
        A new, unconnected delivery.
    """
    return SmtpDelivery(settings) if settings.transport is Transport.SMTP else AppendDelivery(settings)


def deliver_with_retry(
    delivery: Delivery,
    composed: ComposedEmail,
    *,
    budget_sec: float,
    backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0),
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Deliver one message, retrying healable faults inside the send budget.

    Args:
        delivery: The connection.
        composed: The email.
        budget_sec: Time allowed for this email, retries included.
        backoff: Delays between attempts; the last value repeats.
        clock: Monotonic clock.
        sleeper: Sleep function.

    Returns:
        The number of attempts used.

    Raises:
        DeliveryError: A fatal fault, or a healable one still failing when the budget ran out.
    """
    deadline = clock() + budget_sec
    attempt = 0
    while True:
        attempt += 1
        try:
            delivery.deliver(composed)
            return attempt
        except DeliveryError as error:
            delay = backoff[min(attempt - 1, len(backoff) - 1)]
            if not error.retryable or clock() + delay > deadline:
                raise
            sleeper(delay)
