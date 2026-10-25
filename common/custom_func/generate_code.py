#!/usr/bin/env python3
"""Offline six-character access codes. Python 3; no third-party dependencies.

No arguments: generate from this computer's current Unix time in seconds.
One argument: decode the code and print local yyyy-MM-dd HH:mm:ss.
AES-128 / FF1: NIST SP 800-38G. This file belongs to revision 14's key set.
"""
import datetime
import functools
import re
import sys
import time

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
CODE_KEY_HEX = "30de68fb0a6f4c8cc2cbfef0db8de0dc"
TWEAK = b"DST-OFFLINE-V1"
CODE_LENGTH = 6


def _mul(a, b):
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        a = ((a << 1) ^ (0x11b if a & 128 else 0)) & 255
        b >>= 1
    return out


def _sbox(x):
    y, p, power = 1, x, 254
    while power:
        if power & 1:
            y = _mul(y, p)
        p = _mul(p, p)
        power >>= 1
    if x == 0:
        y = 0
    return y ^ ((y << 1 | y >> 7) & 255) ^ ((y << 2 | y >> 6) & 255) ^ ((y << 3 | y >> 5) & 255) ^ ((y << 4 | y >> 4) & 255) ^ 0x63


SBOX = tuple(_sbox(x) for x in range(256))


@functools.lru_cache(maxsize=8)
def _expand(key):
    if len(key) != 16:
        raise ValueError("AES-128 requires a 16-byte key")
    words = list(key)
    rc = 1
    while len(words) < 176:
        t = words[-4:]
        if len(words) % 16 == 0:
            t = [SBOX[t[1]] ^ rc, SBOX[t[2]], SBOX[t[3]], SBOX[t[0]]]
            rc = _mul(rc, 2)
        for x in t:
            words.append(words[-16] ^ x)
    return tuple(words)


def aes_block(key, block):
    if len(block) != 16:
        raise ValueError("AES block must contain 16 bytes")
    rk = _expand(key)
    s = [a ^ b for a, b in zip(block, rk[:16])]
    for rnd in range(1, 11):
        s = [SBOX[s[4 * ((col + row) % 4) + row]] for col in range(4) for row in range(4)]
        if rnd < 10:
            for i in range(0, 16, 4):
                a, b, c, d = s[i:i + 4]
                x = a ^ b ^ c ^ d
                s[i:i + 4] = [a ^ x ^ _mul(a ^ b, 2), b ^ x ^ _mul(b ^ c, 2), c ^ x ^ _mul(c ^ d, 2), d ^ x ^ _mul(d ^ a, 2)]
        s = [x ^ rk[16 * rnd + i] for i, x in enumerate(s)]
    return bytes(s)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _cbc_mac(key, data):
    assert len(data) % 16 == 0
    x = bytes(16)
    for i in range(0, len(data), 16):
        x = aes_block(key, _xor(x, data[i:i + 16]))
    return x


def cmac(key, data):
    def double(block):
        n = int.from_bytes(block, "big")
        return (((n << 1) & ((1 << 128) - 1)) ^ (0x87 if n >> 127 else 0)).to_bytes(16, "big")
    k1 = double(aes_block(key, bytes(16)))
    k2 = double(k1)
    if data and len(data) % 16 == 0:
        last = _xor(data[-16:], k1)
        prefix = data[:-16]
    else:
        n = len(data) // 16 * 16
        last = _xor(data[n:] + b"\x80" + bytes(15 - len(data[n:])), k2)
        prefix = data[:n]
    return _cbc_mac(key, prefix + last)


def _num(digits, radix):
    n = 0
    for digit in digits:
        n = n * radix + digit
    return n


def _digits(number, length, radix):
    result = [0] * length
    for i in range(length - 1, -1, -1):
        number, result[i] = divmod(number, radix)
    if number:
        raise ValueError("timestamp outside the code's representable range")
    return result


def ff1(key, digits, radix=62, tweak=TWEAK, decrypt=False):
    n = len(digits)
    if not (2 <= n <= 10 and 2 <= radix <= 62) or any(not 0 <= x < radix for x in digits):
        raise ValueError("unsupported FF1 input")
    u, v = n // 2, n - n // 2
    a, bpart = list(digits[:u]), list(digits[u:])
    b = ((radix ** v - 1).bit_length() + 7) // 8
    d = 4 * ((b + 3) // 4) + 4
    p = bytes([1, 2, 1]) + radix.to_bytes(3, "big") + bytes([10, u % 256]) + n.to_bytes(4, "big") + len(tweak).to_bytes(4, "big")
    for i in (range(9, -1, -1) if decrypt else range(10)):
        right = a if decrypt else bpart
        q = tweak + bytes((-len(tweak) - b - 1) % 16) + bytes([i]) + _num(right, radix).to_bytes(b, "big")
        r = _cbc_mac(key, p + q)
        # Supported lengths have d <= 16, so no expansion blocks are needed.
        y = int.from_bytes(r[:d], "big")
        m = u if i % 2 == 0 else v
        c = (_num(bpart if decrypt else a, radix) + (-y if decrypt else y)) % (radix ** m)
        c = _digits(c, m, radix)
        a, bpart = (c, a) if decrypt else (bpart, c)
    return a + bpart


def encode_timestamp(timestamp):
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or not 0 <= timestamp < 62 ** CODE_LENGTH:
        raise ValueError("timestamp must be a representable nonnegative integer in seconds")
    digits = _digits(timestamp, CODE_LENGTH, 62)
    return "".join(ALPHABET[x] for x in ff1(bytes.fromhex(CODE_KEY_HEX), digits))


def decode_code(code):
    code = code.strip()
    if re.fullmatch(r"[0-9A-Za-z]{6}", code) is None:
        raise ValueError("口令必须为 6 位大小写字母或数字，区分大小写。")
    digits = [ALPHABET.index(x) for x in code]
    return _num(ff1(bytes.fromhex(CODE_KEY_HEX), digits, decrypt=True), 62)


def main(args=None):
    args = sys.argv[1:] if args is None else args
    try:
        if not args:
            print(encode_timestamp(int(time.time())))
        elif len(args) == 1:
            timestamp = decode_code(args[0])
            print(datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S"))
        else:
            raise ValueError("用法：python3 generate_code.py [6位口令]；不传参数生成，传入口令解码。")
    except (ValueError, OverflowError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
