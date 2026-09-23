import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from socks_media import (decode_datagram, encode_address, SocksDatagram,
                         system_socks_proxy, install_socks_media)


class SocksTests(unittest.TestCase):
    def test_proxy_requires_existing_enabled_loopback_endpoint(self):
        for enabled, host, expected in [('1','127.0.0.1',('127.0.0.1',10809)),
                                       ('0','127.0.0.1',None),('1','203.0.113.2',None)]:
            with patch('socks_media.subprocess.run', return_value=SimpleNamespace(stdout=
                f'SOCKSEnable : {enabled}\nSOCKSProxy : {host}\nSOCKSPort : 10809\n')):
                self.assertEqual(system_socks_proxy(),expected)

    def test_udp_header_roundtrip_ipv4_and_ipv6(self):
        for address in [('203.0.113.9',9999),('2001:db8::1',443)]:
            data = b'\x00\x00\x00'+encode_address(address)+b'encrypted-packet'
            self.assertEqual(decode_datagram(data),(b'encrypted-packet',address))

    def test_truncated_fragmented_and_unknown_address_rejected(self):
        for data in [b'',b'\0\0\1\1'+b'\0'*10,b'\0\0\0\3name',b'\0\0\0\1\1']:
            with self.assertRaises(ValueError):
                decode_datagram(data)

    def test_external_datagram_cannot_impersonate_configured_relay(self):
        metrics={'untrusted_relay_packets':0,'invalid_datagrams':0,'received_datagrams':0}
        protocol=Mock()
        wrapper=SocksDatagram(protocol,('127.0.0.1',10809),Mock(),metrics)
        wrapper.datagram_received(b'\0\0\0'+encode_address(('203.0.113.4',9999))+b'data', ('203.0.113.8',10809))
        protocol.datagram_received.assert_not_called()
        self.assertEqual(metrics['untrusted_relay_packets'],1)

    def test_adapter_is_bound_only_to_selected_connection(self):
        one, other = SimpleNamespace(), SimpleNamespace()
        install_socks_media(one,('127.0.0.1',10809),{})
        self.assertIs(one.get_component_candidates.__self__,one)
        self.assertFalse(hasattr(other,'get_component_candidates'))
