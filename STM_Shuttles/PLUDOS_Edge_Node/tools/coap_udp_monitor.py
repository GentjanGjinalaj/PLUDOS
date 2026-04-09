#!/usr/bin/env python3
"""Minimal UDP/CoAP monitor for STM32 telemetry testing.

Prints every UDP datagram received on the selected port.
If a packet looks like a CoAP confirmable request, it also sends a
piggybacked ACK (2.04 Changed) with the same message ID and token.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from typing import Any


COAP_VERSION = 1
COAP_TYPE_CON = 0
COAP_TYPE_ACK = 2
COAP_CODE_CHANGED = 68  # 2.04 Changed
COAP_OPTION_URI_PATH = 11
COAP_OPTION_CONTENT_FORMAT = 12


def parse_coap(packet: bytes) -> dict[str, Any] | None:
    if len(packet) < 4:
        return None

    first = packet[0]
    version = first >> 6
    msg_type = (first >> 4) & 0x03
    token_len = first & 0x0F
    if version != COAP_VERSION or len(packet) < 4 + token_len:
        return None

    code = packet[1]
    message_id = (packet[2] << 8) | packet[3]
    token = packet[4 : 4 + token_len]
    pos = 4 + token_len
    option_number = 0
    uri_path: list[str] = []
    content_format = None

    while pos < len(packet):
        if packet[pos] == 0xFF:
            pos += 1
            break

        header = packet[pos]
        pos += 1
        delta = header >> 4
        length = header & 0x0F

        if delta >= 13 or length >= 13:
            return None

        if pos + length > len(packet):
            return None

        option_number += delta
        value = packet[pos : pos + length]
        pos += length

        if option_number == COAP_OPTION_URI_PATH:
            uri_path.append(value.decode("utf-8", errors="replace"))
        elif option_number == COAP_OPTION_CONTENT_FORMAT and len(value) == 1:
            content_format = value[0]

    payload = packet[pos:] if pos <= len(packet) else b""

    return {
        "version": version,
        "type": msg_type,
        "token_len": token_len,
        "code": code,
        "message_id": message_id,
        "token": token,
        "uri_path": "/".join(uri_path),
        "content_format": content_format,
        "payload": payload,
    }


def build_ack(message_id: int, token: bytes, code: int = COAP_CODE_CHANGED) -> bytes:
    first = (COAP_VERSION << 6) | (COAP_TYPE_ACK << 4) | len(token)
    return bytes([first, code, (message_id >> 8) & 0xFF, message_id & 0xFF]) + token


def decode_payload(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.hex()


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor UDP or CoAP traffic from the STM32.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host. Default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=5683, help="Bind UDP port. Default: 5683")
    parser.add_argument("--no-ack", action="store_true", help="Do not ACK CoAP confirmable messages")
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))

    print(f"Listening on udp://{args.host}:{args.port}", flush=True)

    while True:
        packet, addr = sock.recvfrom(4096)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        coap = parse_coap(packet)

        if coap is None:
            print(f"[{timestamp}] UDP {addr[0]}:{addr[1]} len={len(packet)}", flush=True)
            print(decode_payload(packet), flush=True)
            continue

        payload_text = decode_payload(coap["payload"])
        print(
            f"[{timestamp}] CoAP type={coap['type']} code={coap['code']} "
            f"mid=0x{coap['message_id']:04X} uri=/{coap['uri_path']} len={len(packet)} "
            f"from={addr[0]}:{addr[1]}",
            flush=True,
        )
        if payload_text:
            print(payload_text, flush=True)

        if coap["type"] == COAP_TYPE_CON and not args.no_ack:
            ack = build_ack(coap["message_id"], coap["token"])
            sock.sendto(ack, addr)
            print(f"ACK -> mid=0x{coap['message_id']:04X}", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        raise SystemExit(0)
