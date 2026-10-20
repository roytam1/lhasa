#!/usr/bin/env python3
"""
make_larc7_test_archives.py -- generate the LArc 7 test archives used by the
lhasa test suite.

These use the LArc 7 member header format:

  * the method ID is one of the LArc 7 methods (-lz1- .. -lz3-, -lz8-)
  * the two bytes that follow the file name are the "level field": for the
    bit oriented LZSS methods they hold the number of bits used for the
    position and the length of a copy, and there is no CRC-16 in the header.

The archives are written to the larc333 test archive directory.

Run:  python make_larc7_test_archives.py [destdir]
"""

import os
import struct
import sys
import time

# A fixed timestamp, so that the generated archives are reproducible.  It is
# a date in the middle of the day in the middle of the winter, so that the
# date does not change between time zones.

TIMESTAMP = (2010, 1, 6, 12, 0, 0)

PAYLOADS = {
    "hello.txt": (b"Hello from LArc 7!\r\n"
                  b"This file is used to test the LArc 7 compression "
                  b"methods.\r\n"),
    "data.bin": bytes(((i * 7) ^ (i >> 3)) & 0xFF for i in range(1500)),
}


# CRC-16 as used by LArc (reflected, polynomial 0xa001):

def crc16(data):
	crc = 0
	for b in data:
		crc ^= b
		for _ in range(8):
			crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
	return crc & 0xffff


# The LArc image: the initial contents of the history buffer used by the
# -lz5- algorithm.

def larc_image():
	image = bytearray()
	for i in range(256):
		image += bytes([i]) * 13
	image += bytes(range(256))
	image += bytes(range(255, -1, -1))
	image += bytes(128)
	image += b" " * 110
	return bytes(image)


# -lz5-: byte oriented LZSS with a 4 KiB window.

def encode_lz5(data):
	history = bytearray(larc_image())
	history += bytes(4096 - len(history))
	valid = bytearray(4096)
	pos = 4096 - 18
	out = bytearray()
	i = 0
	while i < len(data):
		# Collect a run of eight commands, then emit the control byte
		# that describes them: bit i (LSB first) belongs to command i,
		# and a set bit means "literal".

		commands = []
		for _ in range(8):
			if i >= len(data):
				break
			best_len = 0
			best_pos = 0
			limit = min(18, len(data) - i)
			for dist in range(1, 4096):
				start = (pos - dist) & 4095
				if not valid[start] or history[start] != data[i]:
					continue
				length = 0
				while length < limit:
					if length < dist:
						if history[(start + length) & 4095] != data[i + length]:
							break
					elif data[i + length - dist] != data[i + length]:
						break
					length += 1
				if length > best_len:
					best_len = length
					best_pos = start
					if length == limit:
						break
			if best_len >= 3:
				commands.append((best_pos, best_len))
				for k in range(best_len):
					history[pos] = history[(best_pos + k) & 4095]
					valid[pos] = 1
					pos = (pos + 1) & 4095
				i += best_len
			else:
				commands.append((data[i], None))
				history[pos] = data[i]
				valid[pos] = 1
				pos = (pos + 1) & 4095
				i += 1

		control = 0
		body = bytearray()
		for index, (value, length) in enumerate(commands):
			if length is None:
				control |= 1 << index
				body.append(value)
			else:
				body.append(value & 0xff)
				body.append(((((value >> 8) & 0x0f) << 4) | (length - 3)) & 0xff)
		out.append(control)
		out += body
	return bytes(out)


# -lz1- .. -lz3-: bit oriented LZSS, with the number of bits used for the
# position and the length taken from the archive header.

def encode_lzs(data, pos_bits, len_bits):
	window = 1 << pos_bits
	mask = window - 1
	ce2 = (pos_bits + len_bits + 1) // 9
	history = bytearray(b" " * window)
	valid = bytearray(window)
	pos = (window - (1 << len_bits) - ce2) & mask
	bits = []
	i = 0
	while i < len(data):
		best_len = 0
		best_pos = 0
		limit = min((1 << len_bits) - 1 + ce2 + 1, len(data) - i)
		for dist in range(1, window):
			start = (pos - dist) & mask
			if not valid[start] or history[start] != data[i]:
				continue
			length = 0
			while length < limit:
				if length < dist:
					if history[(start + length) & mask] != data[i + length]:
						break
				elif data[i + length - dist] != data[i + length]:
					break
				length += 1
			if length > best_len:
				best_len = length
				best_pos = start
				if length == limit:
					break
		if best_len >= 3:
			bits.append(0)
			for n in range(pos_bits - 1, -1, -1):
				bits.append((best_pos >> n) & 1)
			for n in range(len_bits - 1, -1, -1):
				bits.append((best_len - ce2 - 1) >> n & 1)
			for k in range(best_len):
				history[pos] = history[(best_pos + k) & mask]
				valid[pos] = 1
				pos = (pos + 1) & mask
			i += best_len
		else:
			bits.append(1)
			for n in range(7, -1, -1):
				bits.append((data[i] >> n) & 1)
			history[pos] = data[i]
			valid[pos] = 1
			pos = (pos + 1) & mask
			i += 1
	out = bytearray()
	for i in range(0, len(bits), 8):
		byte = 0
		for bit in bits[i:i + 8]:
			byte = (byte << 1) | bit
		out.append(byte)
	return bytes(out)


# The LHA style member header used by LArc.

def build_member(name, payload, method, body, level, stamp):
	name = name.upper().encode()
	header = bytearray()
	header.append(0)                                # header size
	header.append(0)                                # header checksum
	header += method.encode()
	header += struct.pack("<II", len(body), len(payload))
	header += struct.pack("<I", stamp)
	header.append(0x20)                             # MS-DOS attribute
	header.append(0)                                # header level
	header.append(len(name))
	header += name
	header += level                                 # LArc 7 level field
	header += struct.pack("<H", crc16(payload))
	header[0] = len(header) - 2
	header[1] = sum(header[2:]) & 0xff
	return bytes(header) + body


def dos_stamp(t):
	date = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
	tod = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
	return tod | (date << 16)


def build(destdir, name, members):
	"""members: list of (filename, payload, method, pos_bits, len_bits)"""
	stamp = dos_stamp(time.struct_time(TIMESTAMP + (0, 0, -1)))
	out = bytearray()
	for filename, payload, method, pos_bits, len_bits in members:
		if method == "-lz8-":
			# the same algorithm as -lz5-
			body = encode_lz5(payload)
		else:
			body = encode_lzs(payload, pos_bits, len_bits)
		# LArc 7 level field: position bits, length bits
		level = bytes([pos_bits, len_bits])
		out += build_member(filename, payload, method, body, level, stamp)
	out += b"\x00"
	path = os.path.join(destdir, name)
	with open(path, "wb") as f:
		f.write(bytes(out))
	print("%-12s %5d bytes" % (name, len(out)))


def main():
	destdir = sys.argv[1] if len(sys.argv) > 1 else \
		os.path.join(os.path.dirname(os.path.abspath(__file__)),
		             "archives", "larc333")
	os.makedirs(destdir, exist_ok=True)

	# -lz8- uses the same algorithm as -lz5-
	build(destdir, "lz8.lzs",
	      [("hello.txt", PAYLOADS["hello.txt"], "-lz8-", 11, 4),
	       ("data.bin", PAYLOADS["data.bin"], "-lz8-", 11, 4)])

	# the bit oriented LZSS methods, with different level parameters
	build(destdir, "lz1.lzs",
	      [("hello.txt", PAYLOADS["hello.txt"], "-lz1-", 11, 4),
	       ("data.bin", PAYLOADS["data.bin"], "-lz1-", 11, 4)])
	build(destdir, "lz2.lzs",
	      [("hello.txt", PAYLOADS["hello.txt"], "-lz2-", 12, 4),
	       ("data.bin", PAYLOADS["data.bin"], "-lz2-", 12, 4)])
	build(destdir, "lz3.lzs",
	      [("hello.txt", PAYLOADS["hello.txt"], "-lz3-", 11, 5),
	       ("data.bin", PAYLOADS["data.bin"], "-lz3-", 11, 5)])


if __name__ == "__main__":
	main()
