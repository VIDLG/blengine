"""Actual Appium SDK/process/socket boundaries, isolated from devices and network."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import mobile_appium
from tools.apple import ios_appium


@contextmanager
def automation_sdk(driver=None):
    events: list[str] = []
    process = Mock(pid=424242, returncode=None)
    process.poll.side_effect = lambda: process.returncode

    def wait(*, timeout):
        assert timeout > 0
        events.append("server-reaped")
        process.returncode = 0
        return 0

    process.wait.side_effect = wait
    body = b'{"value":{"ready":true}}'
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )

    def connection(*args, **kwargs):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.recv.side_effect = [response, b""]
        return sock

    created = []

    def remote(url, *, options, client_config):
        nonlocal driver
        events.append("session-created")
        if driver is None:
            driver = SimpleNamespace(session_id="owned-session", quit=Mock())
        driver.capabilities = options.to_capabilities()
        driver.command_executor = SimpleNamespace(client_config=client_config)
        driver.quit.side_effect = lambda: events.append("session-quit")
        created.append(driver)
        return driver

    with (
        patch.object(mobile_appium.socket, "socket") as probe,
        patch.object(mobile_appium.socket, "create_connection", side_effect=connection),
        patch.object(mobile_appium.subprocess, "Popen", return_value=process) as spawn,
        patch.object(
            mobile_appium.os,
            "killpg",
            side_effect=lambda pid, sig: events.append("server-signal"),
        ) as kill,
        patch.object(ios_appium.webdriver, "Remote", side_effect=remote) as sdk,
    ):
        yield SimpleNamespace(
            events=events,
            process=process,
            spawn=spawn,
            kill=kill,
            remote=sdk,
            created=created,
            probe=probe,
        )
