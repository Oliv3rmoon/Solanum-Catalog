# SPDX-License-Identifier: GPL-3.0-or-later
"""A QR code encoder with no dependencies, for the site's inline SVGs (tools/build_site.py).

Text goes in as bytes (UTF-8) at error correction level M, in the smallest version from 1 to 10 that holds it (up to
213 bytes); the mask with the lowest penalty is chosen, as ISO/IEC 18004 describes. The result is a square of
booleans (True: a dark module). tests/test_build_site.py holds codes made by Project Nayuki's QR Code generator
(the C version Solanum itself draws with) that this must match module for module.
"""
import html

MIN_VERSION, MAX_VERSION = 1, 10
# Level M: error correction codewords per block, and blocks, for versions 1 to 10 (index 0 is version 1).
ECC_PER_BLOCK = (10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCKS = (1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
FORMAT_BITS_M = 0  # the two-bit code of level M
PENALTY_N1, PENALTY_N2, PENALTY_N3, PENALTY_N4 = 3, 3, 40, 10


class TooLong(ValueError):
    """The text does not fit a version 10 code at level M."""


def raw_data_modules(version):
    """The modules that can hold data and error correction, the function patterns left out."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        aligns = version // 7 + 2
        result -= (25 * aligns - 10) * aligns - 55
        if version >= 7:
            result -= 36
    return result


def data_codewords(version):
    return raw_data_modules(version) // 8 - ECC_PER_BLOCK[version - 1] * BLOCKS[version - 1]


def char_count_bits(version):
    return 8 if version <= 9 else 16


def gf_multiply(x, y):
    """Multiplication in GF(2^8) with the QR polynomial 0x11D."""
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = gf_multiply(root, 2)
    return result


def rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= gf_multiply(coefficient, factor)
    return result


def encode_data(payload, version):
    """The data codewords: mode, count, the bytes, a terminator and the padding."""
    bits = []

    def append(value, count):
        bits.extend((value >> i) & 1 for i in range(count - 1, -1, -1))

    append(0b0100, 4)  # byte mode
    append(len(payload), char_count_bits(version))
    for byte in payload:
        append(byte, 8)
    capacity = data_codewords(version) * 8
    append(0, min(4, capacity - len(bits)))
    append(0, -len(bits) % 8)
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < capacity:
        append(pad[i % 2], 8)
        i += 1
    return [int(''.join(map(str, bits[k:k + 8])), 2) for k in range(0, len(bits), 8)]


def add_ecc_and_interleave(data, version):
    blocks_count = BLOCKS[version - 1]
    ecc_length = ECC_PER_BLOCK[version - 1]
    raw_codewords = raw_data_modules(version) // 8
    short_blocks = blocks_count - raw_codewords % blocks_count
    short_length = raw_codewords // blocks_count
    divisor = rs_divisor(ecc_length)
    blocks, k = [], 0
    for i in range(blocks_count):
        block = data[k:k + short_length - ecc_length + (0 if i < short_blocks else 1)]
        k += len(block)
        ecc = rs_remainder(block, divisor)
        if i < short_blocks:
            block = block + [0]
        blocks.append(block + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_length - ecc_length or j >= short_blocks:  # the padding byte of a short block is skipped
                result.append(block[i])
    return result


class Grid:
    """The modules of one code and which of them are function patterns (not data, so not masked)."""

    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self.draw_function_patterns()

    def set_function(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def alignment_positions(self):
        if self.version == 1:
            return []
        count = self.version // 7 + 2
        step = 26 if self.version == 32 else (self.version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
        return list(reversed([self.size - 7 - i * step for i in range(count - 1)] + [6]))

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < size and 0 <= yy < size:
                        self.set_function(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = self.alignment_positions()
        n = len(positions)
        for i in range(n):
            for j in range(n):
                if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(positions[i] + dx, positions[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        self.draw_version()

    def draw_format(self, mask):
        data = FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 != 0

        size = self.size
        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, bit(i))
        self.set_function(8, size - 8, True)

    def draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_function(a, b, dark)
            self.set_function(b, a, dark)

    def draw_codewords(self, data):
        size, i = self.size, 0
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vertical in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vertical if upward else vertical
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1

    def apply_mask(self, mask):
        for y in range(self.size):
            for x in range(self.size):
                invert = (
                    (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                    (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0, (x * y % 2 + x * y % 3) % 2 == 0,
                    ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
                if invert and not self.function[y][x]:
                    self.modules[y][x] = not self.modules[y][x]

    def penalty(self):
        size, modules, result = self.size, self.modules, 0

        def add_history(run, history):
            if history[0] == 0:
                run += size  # the light border counts as part of the first run
            history.insert(0, run)
            history.pop()

        def count_patterns(history):
            n = history[1]
            core = n > 0 and history[2] == history[4] == history[5] == n and history[3] == n * 3
            return (1 if core and history[0] >= n * 4 and history[6] >= n else 0) + \
                   (1 if core and history[6] >= n * 4 and history[0] >= n else 0)

        def lines(get):
            score = 0
            for a in range(size):
                color, run, history = False, 0, [0] * 7
                for b in range(size):
                    cell = get(a, b)
                    if cell == color:
                        run += 1
                        if run == 5:
                            score += PENALTY_N1
                        elif run > 5:
                            score += 1
                    else:
                        add_history(run, history)
                        if not color:
                            score += count_patterns(history) * PENALTY_N3
                        color, run = cell, 1
                if color:
                    add_history(run, history)
                    run = 0
                run += size  # the light border ends the last run
                add_history(run, history)
                score += count_patterns(history) * PENALTY_N3
            return score

        result += lines(lambda y, x: modules[y][x])
        result += lines(lambda x, y: modules[y][x])
        for y in range(size - 1):
            for x in range(size - 1):
                if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    result += PENALTY_N2
        dark = sum(cell for row in modules for cell in row)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return result + k * PENALTY_N4


def encode(text, mask=None):
    """The code for `text` as rows of booleans (True: dark), without its quiet zone. `mask` (0 to 7) forces a mask;
    None picks the one with the lowest penalty."""
    payload = text.encode('utf-8')
    version = MIN_VERSION
    while True:
        if 4 + char_count_bits(version) + len(payload) * 8 <= data_codewords(version) * 8:
            break
        version += 1
        if version > MAX_VERSION:
            raise TooLong(f'{len(payload)} bytes do not fit a version {MAX_VERSION} code at level M')
    grid = Grid(version)
    grid.draw_codewords(add_ecc_and_interleave(encode_data(payload, version), version))
    if mask is None:
        best = None
        for candidate in range(8):
            grid.apply_mask(candidate)
            grid.draw_format(candidate)
            score = grid.penalty()
            if best is None or score < best[0]:
                best = (score, candidate)
            grid.apply_mask(candidate)  # a second time undoes it
        mask = best[1]
    grid.apply_mask(mask)
    grid.draw_format(mask)
    return [row[:] for row in grid.modules]


def to_svg(text, label, border=4):
    """The code as an SVG with a white quiet zone: one path of black runs, no external file."""
    modules = encode(text)
    size = len(modules)
    side = size + 2 * border
    runs = []
    for y, row in enumerate(modules):
        x = 0
        while x < size:
            if row[x]:
                start = x
                while x < size and row[x]:
                    x += 1
                runs.append(f'M{start + border} {y + border}h{x - start}v1h-{x - start}z')
            else:
                x += 1
    label = html.escape(label, quote=True)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" role="img" aria-label="{label}" '
            f'shape-rendering="crispEdges"><rect width="{side}" height="{side}" fill="#fff"/>'
            f'<path d="{"".join(runs)}" fill="#000"/></svg>')
