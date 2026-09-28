"""Conversions between IEEE 754 doubles and decimal text, as SQLite 3.53 does.

A port of SQLite's util.c (sqlite3FpDecode, sqlite3Fp2Convert10,
sqlite3Fp10Convert2, sqlite3AtoF; the algorithm is adapted there from Russ
Cox's fpfmt).  Python's own conversions are correctly rounded; SQLite's
differ in a few ways that are visible in SQL:

* REAL -> TEXT is printf("%!.17g"), shortened only when a run of 9s or 0s
  allows a shorter text that converts back to the same double.
* Other printf() precisions round the first 18 digits of the value (not
  the exact binary value) to the requested number of digits.
* TEXT -> REAL uses at most about 19 significant digits of the input.

All the arithmetic is on unsigned 64-bit integers (and 128-bit products);
Python integers stand in for them, masked where C would wrap around.
"""

from __future__ import annotations

import math
import re
import struct

U64 = 0xFFFFFFFFFFFFFFFF
U32 = 0xFFFFFFFF
POWERS_OF_10_FIRST, POWERS_OF_10_LAST = -348, 347

# 10**p << k for p in 0..26 (normalized so the top bit is set).
_BASE = [
    0x8000000000000000, 0xa000000000000000, 0xc800000000000000, 0xfa00000000000000,
    0x9c40000000000000, 0xc350000000000000, 0xf424000000000000, 0x9896800000000000,
    0xbebc200000000000, 0xee6b280000000000, 0x9502f90000000000, 0xba43b74000000000,
    0xe8d4a51000000000, 0x9184e72a00000000, 0xb5e620f480000000, 0xe35fa931a0000000,
    0x8e1bc9bf04000000, 0xb1a2bc2ec5000000, 0xde0b6b3a76400000, 0x8ac7230489e80000,
    0xad78ebc5ac620000, 0xd8d726b7177a8000, 0x878678326eac9000, 0xa968163f0a57b400,
    0xd3c21bcecceda100, 0x84595161401484a0, 0xa56fa5b99019a5c8,
]
# 10**(27*g) for g in -13..12 (normalized), and the next 32 bits of each.
_SCALE = [
    0x8049a4ac0c5811ae, 0xcf42894a5dce35ea, 0xa76c582338ed2621, 0x873e4f75e2224e68,
    0xda7f5bf590966848, 0xb080392cc4349dec, 0x8e938662882af53e, 0xe65829b3046b0afa,
    0xba121a4650e4ddeb, 0x964e858c91ba2655, 0xf2d56790ab41c2a2, 0xc428d05aa4751e4c,
    0x9e74d1b791e07e48, 0xcccccccccccccccc, 0xcecb8f27f4200f3a, 0xa70c3c40a64e6c51,
    0x86f0ac99b4e8dafd, 0xda01ee641a708de9, 0xb01ae745b101e9e4, 0x8e41ade9fbebc27d,
    0xe5d3ef282a242e81, 0xb9a74a0637ce2ee1, 0x95f83d0a1fb69cd9, 0xf24a01a73cf2dccf,
    0xc3b8358109e84f07, 0x9e19db92b4e31ba9,
]
_SCALE_LO = [
    0x205b896d, 0x52064cad, 0xaf2af2b8, 0x5a7744a7, 0xaf39a475, 0xbd8d794e, 0x547eb47b,
    0x0cb4a5a3, 0x92f34d62, 0x3a6a07f9, 0xfae27299, 0xaa97e14c, 0x775ea265, 0xcccccccc,
    0x00000000, 0x999090b6, 0x69a028bb, 0xe80e6f48, 0x5ec05dd0, 0x14588f14, 0x8f1668c9,
    0x6d953e2c, 0x4abdaf10, 0xbc633b39, 0x0a862f81, 0x6c07a2c2,
]


def _multiply128(a: int, b: int) -> tuple[int, int]:
    """(high, low) 64-bit halves of a * b."""
    r = a * b
    return r >> 64, r & U64


def _multiply160(a: int, a_lo: int, b: int) -> tuple[int, int]:
    """Upper 64 bits of ((a << 32) + a_lo) * b, and the 32 bits below them."""
    r = a * b + ((a_lo * b) >> 32)
    return (r >> 64) & U64, (r >> 32) & U32


def _power_of_ten(p: int) -> tuple[int, int]:
    """10**p as a normalized 64-bit mantissa and 32 more bits (SQLite's powerOfTen)."""
    if p < 0:
        if p == -1:
            return _SCALE[13], _SCALE_LO[13]
        g = int(p / 27)  # C division truncates toward zero
        n = p - g * 27
        if n:
            g -= 1
            n += 27
    elif p < 27:
        return _BASE[p], 0
    else:
        g, n = divmod(p, 27)
    s = _SCALE[g + 13]
    if n == 0:
        return s, _SCALE_LO[g + 13]
    x, lo = _multiply160(s, _SCALE_LO[g + 13], _BASE[n])
    if not x & (1 << 63):
        x = ((x << 1) | ((lo >> 31) & 1)) & U64
        lo = ((lo << 1) | 1) & U32
    return x, lo


def _pwr10to2(p: int) -> int:
    return (p * 108853) >> 15


def _pwr2to10(p: int) -> int:
    return (p * 78913) >> 18


def _fp2convert10(m: int, e: int, n: int) -> tuple[int, int]:
    """(d, p) with m * 2**e ~ d * 10**p and d having at least n digits."""
    p = n - 1 - _pwr2to10(e + 63)
    h = _multiply128(m, _power_of_ten(p)[0])[0]
    if n == 18:
        h >>= -(e + _pwr10to2(p) + 2)
        d = (h + ((h << 1) & 2)) >> 1
    else:
        d = h >> -(e + _pwr10to2(p) + 1)
    return d, -p


def fp10convert2(d: int, p: int) -> float:
    """The double nearest to d * 10**p (SQLite's sqlite3Fp10Convert2)."""
    if p < POWERS_OF_10_FIRST:
        return 0.0
    if p > POWERS_OF_10_LAST:
        return math.inf
    b = d.bit_length()
    lp = _pwr10to2(p)
    e = 53 - b - lp
    if e > 1074:
        if e >= 1130:
            return 0.0
        e = 1074
    s = -(e - (64 - b) + lp + 3)
    pwr10h, pwr10l = _power_of_ten(p)
    if pwr10l != 0:
        pwr10h += 1
        pwr10l = ~pwr10l & U32
    x = (d << (64 - b)) & U64
    hi, lo = _multiply128(x, pwr10h)
    mid1 = lo >> 32
    sticky = 1
    if hi & ((1 << s) - 1) == 0:
        mid2 = _multiply128(x, pwr10l << 32)[0] >> 32
        sticky = int(((mid1 - mid2) & U32) > 1)
        hi -= mid1 < mid2
    u = (hi >> s) | sticky
    adj = int(u >= (1 << 55) - 2)
    if adj:
        u = (u >> adj) | (u & 1)
        e -= adj
    m = (u + 1 + ((u >> 2) & 1)) >> 2
    if e <= -972:
        return math.inf
    if m & (1 << 52):
        m = (m & ~(1 << 52)) | ((1075 - e) << 52)
    return struct.unpack("<d", struct.pack("<Q", m & U64))[0]


def fp_decode(r: float, round_to: int, max_digits: int) -> tuple[str, str, int, int]:
    """SQLite's sqlite3FpDecode: (sign, significant digits, position of the
    decimal point, special: 1 for infinity), r = 0.digits * 10**point.

    ``round_to`` > 0 rounds to that many significant digits (at most
    ``max_digits``); ``round_to`` <= 0 to -round_to digits after the point."""
    if r < 0.0:
        sign, r = "-", -r
    elif r == 0.0:
        return "+", "0", 1, 0
    else:
        sign = "+"
    bits = struct.unpack("<Q", struct.pack("<d", r))[0]
    e = (bits >> 52) & 0x7FF
    if e == 0x7FF:
        return sign, "", 0, 1 if bits & ((1 << 52) - 1) == 0 else 2
    v = bits & ((1 << 52) - 1)
    if e == 0:
        nn = 64 - v.bit_length()
        v <<= nn
        e = -1074 - nn
    else:
        v = (v << 11) | (1 << 63)
        e -= 1086
    v, exponent = _fp2convert10(v, e, 18 if round_to <= 0 or round_to >= 18 else round_to + 1)
    z = list(str(v))
    n = len(z)
    point = n + exponent
    if round_to <= 0:
        round_to = point - round_to
        if round_to == 0 and z[0] >= "5":
            round_to = 1
            z.insert(0, "0")
            n += 1
            point += 1
    if round_to > 0 and (round_to < n or n > max_digits):
        if round_to > max_digits:
            round_to = max_digits
        if round_to == 17:
            round_to = _shorter_round_trip(r, z, n, exponent, point)
        n = round_to
        if z[round_to] >= "5":
            j = round_to - 1
            while True:
                if z[j] != "9":
                    z[j] = chr(ord(z[j]) + 1)
                    break
                z[j] = "0"
                if j == 0:
                    z.insert(0, "1")
                    n += 1
                    point += 1
                    break
                j -= 1
    digits = "".join(z[:n]).rstrip("0")
    return sign, digits, point, 0


def _shorter_round_trip(r: float, z: list[str], n: int, exponent: int, point: int) -> int:
    """For 17 significant digits ("%!.17g"): fewer digits if a run of 9s or
    0s allows a shorter number that converts back to ``r``."""
    if z[15] == "9" and z[14] == "9":
        jj = 14
        while jj > 0 and z[jj - 1] == "9":
            jj -= 1
        v2 = 1 if jj == 0 else int("".join(z[:jj])) + 1
        if r == fp10convert2(v2, exponent + n - jj):
            return jj + 1
    elif point >= n or (z[15] == "0" and z[14] == "0" and z[13] == "0"):
        jj = 13
        while z[jj - 1] == "0":
            jj -= 1
        v2 = int("".join(z[:jj]))
        if r == fp10convert2(v2, exponent + n - jj):
            return jj + 1
    return 17


def format_real(value: float) -> str:
    """A REAL as text, as SQLite renders it: printf("%!.17g")."""
    if math.isinf(value):
        return "Inf" if value > 0 else "-Inf"
    sign, digits, point, _ = fp_decode(value, 17, 20)
    exponent = point - 1
    prefix = "-" if sign == "-" else ""
    if exponent < -4 or exponent > 16:
        head, tail = digits[0], digits[1:] or "0"
        exp_sign = "-" if exponent < 0 else "+"
        return f"{prefix}{head}.{tail}e{exp_sign}{abs(exponent):02d}"
    if point <= 0:
        return f"{prefix}0.{'0' * -point}{digits}"
    whole = digits[:point].ljust(point, "0")
    return f"{prefix}{whole}.{digits[point:] or '0'}"


_SPACE = " \t\n\v\f\r"
_REAL_TEXT = re.compile(r"([+-]?)([0-9]*)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]+))?")


def atof(literal: str) -> float:
    """A numeric literal (already validated) as a double, the way SQLite's
    sqlite3AtoF reads it: only about the first 19 significant digits count,
    and a number that fits in them is correctly rounded (as Python's)."""
    match = _REAL_TEXT.fullmatch(literal.strip(_SPACE))
    sign, whole, fraction, exp = match.groups()
    fraction = fraction or ""
    if len((whole + fraction).lstrip("0")) <= 19:
        return float(literal)
    s = 0
    d = 0
    for ch in whole.lstrip("0"):
        if s >= (U64 - 9) // 10:  # saturated: later digits only scale
            d += 1
        else:
            s = s * 10 + int(ch)
    for ch in fraction:
        if s < (U64 - 9) // 10:
            s = s * 10 + int(ch)
            d -= 1
    if exp:
        e = int(exp)
        d += max(min(e, 10000), -10000)
    value = 0.0 if s == 0 else fp10convert2(s, d)
    return -value if sign == "-" else value
