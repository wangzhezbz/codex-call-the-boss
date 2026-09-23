"""Per-call UDP transport through an already configured loopback SOCKS proxy.

No global routing changes, proxy installation, credentials or public relay.
The WebRTC payload remains end-to-end encrypted. Only this ICE connection is
adapted; direct calls and third-party aioice modules remain untouched.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
import subprocess
from types import MethodType
from aioice.ice import StunProtocol, server_reflexive_candidate
from aioice.candidate import Candidate, candidate_foundation, candidate_priority


def system_socks_proxy():
    try:
        result = subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True,
                                text=True, timeout=2, check=True)
        values = {}
        for line in result.stdout.splitlines():
            if ' : ' in line:
                key, value = line.strip().split(' : ', 1)
                values[key] = value
        host, port = values.get('SOCKSProxy', ''), int(values.get('SOCKSPort', '0'))
        if values.get('SOCKSEnable') == '1' and ipaddress.ip_address(host).is_loopback and 0 < port < 65536:
            return host, port
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return None


def encode_address(address):
    host, port = address[:2]
    parsed = ipaddress.ip_address(host)
    return bytes([1 if parsed.version == 4 else 4]) + parsed.packed + struct.pack('!H', port)


def decode_datagram(data):
    if len(data) < 4 or data[:3] != b'\x00\x00\x00':
        raise ValueError('Fragmented or invalid SOCKS UDP datagram')
    family = data[3]
    length = {1:4, 4:16}.get(family)
    if length is None or len(data) < 6 + length:
        raise ValueError('Unsupported SOCKS UDP address')
    host = str(ipaddress.ip_address(data[4:4+length]))
    port = struct.unpack('!H',data[4+length:6+length])[0]
    return data[6+length:], (host, port)


class SocksDatagram(asyncio.DatagramProtocol):
    def __init__(self, protocol, relay, writer, metrics):
        self.protocol, self.relay, self.writer, self.metrics = protocol, relay, writer, metrics
        self.raw = None
        self.monitor = None

    def connection_made(self, transport):
        self.raw = transport
        self.protocol.connection_made(self)

    def datagram_received(self, data, address):
        if address[:2] != self.relay:
            self.metrics['untrusted_relay_packets'] += 1
            return
        try:
            payload, source = decode_datagram(data)
        except ValueError:
            self.metrics['invalid_datagrams'] += 1
            return
        self.metrics['received_datagrams'] += 1
        self.protocol.datagram_received(payload, source)

    def sendto(self, data, address):
        self.metrics['sent_datagrams'] += 1
        self.raw.sendto(b'\x00\x00\x00' + encode_address(address) + data, self.relay)

    def get_extra_info(self, name, default=None):
        return self.raw.get_extra_info(name, default)

    def close(self):
        if self.monitor and self.monitor is not asyncio.current_task():
            self.monitor.cancel()
        self.writer.close()
        if self.raw:
            self.raw.close()

    def connection_lost(self, exc):
        self.writer.close()
        self.protocol.connection_lost(exc)

    def error_received(self, exc):
        self.protocol.error_received(exc)


async def create_socks_protocol(connection, endpoint, metrics):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*endpoint), 3)
    try:
        async with asyncio.timeout(3):
            writer.write(b'\x05\x01\x00')
            await writer.drain()
            if await reader.readexactly(2) != b'\x05\x00':
                raise RuntimeError('Existing SOCKS proxy does not permit unauthenticated media association')
            writer.write(b'\x05\x03\x00\x01' + b'\x00' * 6)
            await writer.drain()
            header = await reader.readexactly(4)
            if header[:3] != b'\x05\x00\x00' or header[3] not in (1,4):
                raise RuntimeError('Existing SOCKS proxy refused UDP association')
            host = str(ipaddress.ip_address(await reader.readexactly(4 if header[3] == 1 else 16)))
            port = struct.unpack('!H', await reader.readexactly(2))[0]
            if ipaddress.ip_address(host).is_unspecified:
                host = endpoint[0]
            # The configured loopback proxy must not redirect us to an unknown
            # third-party UDP relay outside the user's existing local service.
            if not ipaddress.ip_address(host).is_loopback or not port:
                raise RuntimeError('SOCKS association returned a non-loopback relay')
        protocol = StunProtocol(connection)
        wrapper = SocksDatagram(protocol, (host,port), writer, metrics)
        await asyncio.get_running_loop().create_datagram_endpoint(lambda:wrapper,
                                                                  local_addr=(endpoint[0],0))
        async def watch_association():
            try:
                await reader.read()
            finally:
                wrapper.close()
        wrapper.monitor = asyncio.create_task(watch_association())
        return protocol
    except BaseException:
        writer.close()
        raise


def install_socks_media(connection, endpoint, metrics):
    """Adapt one pinned aioice instance; no module monkeypatching."""
    metrics.update(route='existing_system_loopback_socks', sent_datagrams=0,
                   received_datagrams=0, invalid_datagrams=0, untrusted_relay_packets=0)
    async def gather(instance, component, addresses, timeout=5):
        protocol = await create_socks_protocol(instance, endpoint, metrics)
        instance._protocols.append(protocol)
        local = protocol.transport.get_extra_info('sockname')
        protocol.local_candidate = Candidate(
            foundation=candidate_foundation('host','udp',local[0]), component=component,
            transport='udp', priority=candidate_priority(component,'host'),
            host=local[0],port=local[1],type='host')
        candidates = [protocol.local_candidate]
        if instance.stun_server:
            try:
                candidate, _ = await asyncio.wait_for(server_reflexive_candidate(protocol, instance.stun_server), timeout)
                candidates.append(candidate)
            except (TimeoutError, OSError):
                metrics['stun_reflexive_unavailable'] = True
        return candidates
    connection.get_component_candidates = MethodType(gather, connection)
