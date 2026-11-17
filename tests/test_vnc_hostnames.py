import socket
from types import SimpleNamespace

import pytest
from twisted.internet.address import IPv4Address, IPv6Address
from twisted.internet.testing import MemoryReactorClock

from ai_desktop_agent.vm import vnc_transport
from ai_desktop_agent.vm.vnc_client import VNCClient


class Resolver:
    def __init__(self, addresses=None, delayed=False):
        self.names = []
        self.addresses = addresses
        self.delayed = delayed
        self.receiver = None

    def resolveHostName(self, receiver, hostName, portNumber=0, **kwargs):  # noqa: N802, N803
        self.names.append(hostName)
        self.receiver = receiver
        receiver.resolutionBegan(SimpleNamespace(name=hostName))
        if not self.delayed:
            self.complete(portNumber)

    def complete(self, port=5900):
        addresses = (
            self.addresses
            if self.addresses is not None
            else [IPv4Address("TCP", "127.0.0.1", port)]
        )
        for address in addresses:
            self.receiver.addressResolved(address)
        self.receiver.resolutionComplete()


@pytest.fixture
def runtime(monkeypatch):
    clock = MemoryReactorClock()
    clock.callFromThread = lambda fn, *args: fn(*args)
    clock.nameResolver = Resolver()
    monkeypatch.setattr(vnc_transport, "reactor", clock)
    proxies = []

    def connect(host, timeout=0.02):
        proxy = vnc_transport.ManagedVNCProxy(vnc_transport.ManagedVNCFactory(), timeout=timeout)
        proxies.append(proxy)
        proxy.connect(host, family=socket.AF_UNSPEC)
        return proxy

    yield clock, connect
    for proxy in proxies:
        proxy.disconnect()


@pytest.mark.parametrize(
    "host", ["review_desktop", "project_vm_1", b"review_desktop", "desk_one.internal"]
)
def test_docker_names_reach_the_resolver_without_being_renamed(runtime, host):
    clock, connect = runtime
    proxy = connect(host)
    expected = host.decode("ascii") if isinstance(host, bytes) else host
    assert clock.nameResolver.names == [expected]
    assert not proxy.closed
    assert [(call[0], call[1]) for call in clock.tcpClients] == [("127.0.0.1", 5900)]


@pytest.mark.parametrize(
    "host", ["host_name/escape", "host_name\nother", "host_name\x00", "_leading", "名前_付き"]
)
def test_the_docker_exception_does_not_allow_invalid_or_non_ascii_names(runtime, host):
    clock, connect = runtime
    proxy = connect(host)
    assert proxy.closed
    assert not clock.nameResolver.names
    assert isinstance(proxy._ready.exception(), ValueError)


@pytest.mark.parametrize("host", ["desktop", "127.0.0.1", "::1", "例え.テスト"])
def test_existing_hostname_and_ip_forms_still_use_the_same_resolver(runtime, host):
    clock, connect = runtime
    proxy = connect(host)
    assert clock.nameResolver.names == [host]
    assert not proxy.closed and clock.tcpClients


def test_docker_resolution_keeps_ipv6_and_ipv4_connection_attempts(runtime):
    clock, connect = runtime
    clock.nameResolver.addresses = [
        IPv6Address("TCP", "::1", 5900),
        IPv4Address("TCP", "127.0.0.1", 5900),
    ]
    proxy = connect("dual_stack")
    clock.advance(0.3)
    assert [call[0] for call in clock.tcpClients] == ["::1", "127.0.0.1"]
    assert not proxy.closed
    proxy.disconnect()
    assert all(connector.stoppedConnecting for connector in clock.connectors)
    assert not clock.getDelayedCalls()


def test_a_late_dns_result_cannot_open_a_connection_after_disconnect(runtime):
    clock, connect = runtime
    clock.nameResolver.delayed = True
    proxy = connect("slow_dns")
    assert clock.nameResolver.names == ["slow_dns"]
    proxy.disconnect()
    clock.nameResolver.complete()
    assert proxy.closed and not clock.tcpClients


def test_dns_wait_counts_toward_the_vnc_operation_timeout(runtime):
    clock, connect = runtime
    clock.nameResolver.delayed = True
    proxy = connect("slow_dns", timeout=0.01)
    with pytest.raises(TimeoutError):
        proxy.connection_ready()
    clock.nameResolver.complete()
    assert proxy.closed and not clock.tcpClients


def test_an_unresolved_docker_name_reports_the_dns_failure(runtime):
    from twisted.internet.error import DNSLookupError

    clock, connect = runtime
    clock.nameResolver.addresses = []
    proxy = connect("missing_desktop")
    assert proxy.closed
    assert isinstance(proxy._ready.exception(), DNSLookupError)
    assert not clock.tcpClients


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="UNIX sockets are not available")
def test_unix_socket_paths_keep_their_own_endpoint(runtime):
    clock, _ = runtime
    proxy = vnc_transport.ManagedVNCProxy(vnc_transport.ManagedVNCFactory(), timeout=0.02)
    try:
        proxy.connect("/tmp/vnc_socket", family=socket.AF_UNIX)
        assert not clock.nameResolver.names
        assert clock.unixClients[0][0] == "/tmp/vnc_socket"
        assert not proxy.closed
    finally:
        proxy.disconnect()


@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "[::1]", "fe80::1%eth0"])
def test_vnc_client_formats_ipv6_literals_for_the_underlying_parser(monkeypatch, host):
    from unittest.mock import Mock

    from vncdotool import api, command

    connected = Mock(closed=False)
    connector = Mock(return_value=connected)
    monkeypatch.setattr(api, "connect", connector)
    display = VNCClient()
    try:
        display.connect(host, 5917)
        family, parsed_host, port = command.parse_server(connector.call_args.args[0])
        assert family == socket.AF_INET6
        assert parsed_host == host.strip("[]") and port == 5917
    finally:
        display.disconnect()
