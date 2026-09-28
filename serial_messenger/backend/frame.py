"""Frame format and bit stuffing for laboratory work No. 2, variant 1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


START_FLAG: Final[bytes] = b"450501-1"
RESERVED_BYTES: Final[int] = 10
SERVICE_BYTES: Final[int] = 3
MAX_FRAME_SIZE: Final[int] = 100
_BODY_TRAILER_SIZE: Final[int] = SERVICE_BYTES + RESERVED_BYTES


@dataclass(frozen=True)
class Frame:
    """One application frame.

    ``frame_type`` can later distinguish data and control frames,
    ``priority`` can select a transmission queue, and ``checksum`` can hold a
    check value.  The assignment requires these service fields to be zero now.
    """

    data: bytes
    frame_type: int = 0
    priority: int = 0
    checksum: int = 0
    reserved: bytes = bytes(RESERVED_BYTES)

    def __post_init__(self) -> None:
        if not self.data or len(self.data) > MAX_FRAME_SIZE - len(START_FLAG) - _BODY_TRAILER_SIZE:
            raise ValueError("Data field does not fit into a frame.")
        if any((self.frame_type, self.priority, self.checksum)):
            raise ValueError("Service fields must be zero in this laboratory work.")
        if self.reserved != bytes(RESERVED_BYTES):
            raise ValueError("Reserved bytes must be zero and have length 10.")

    @property
    def body(self) -> bytes:
        return self.data + bytes((self.frame_type, self.priority, self.checksum)) + self.reserved


@dataclass(frozen=True)
class FrameTransmission:
    """Wire bytes and binary representations for the status window."""

    wire: bytes
    before: str
    after_html: str


def _bits(value: int) -> str:
    return f"{value:08b}"


def _field_before(value: bytes) -> str:
    return ",".join(_bits(byte) for byte in value)


def _stuff_field_bits(fields: list[bytes]) -> tuple[list[str], str]:
    """Stuff a zero after every run of five one bits.

    The returned HTML keeps inserted zeros underlined.  Only those zeroes are
    replacements, so ordinary data remains unadorned.
    """
    ones = 0
    rendered: list[str] = []
    wire_bits: list[str] = []
    for field in fields:
        output: list[str] = []
        for byte in field:
            for bit in _bits(byte):
                output.append(bit)
                wire_bits.append(bit)
                ones = ones + 1 if bit == "1" else 0
                if ones == 5:
                    output.append("<u>0</u>")
                    wire_bits.append("0")
                    ones = 0
        rendered.append("".join(output))
    return rendered, "".join(wire_bits)


def _pack_bits(bits: str) -> bytes:
    padding = (-len(bits)) % 8
    return bytes(int((bits + "0" * padding)[offset : offset + 8], 2) for offset in range(0, len(bits) + padding, 8))


def encode_frame(character: str) -> FrameTransmission:
    """Build, bit-stuff and serialize the frame for one entered character."""
    data = character.encode("utf-8")
    frame = Frame(data=data)
    fields = [frame.data, bytes((0,)), bytes((0,)), bytes((0,)), frame.reserved]
    stuffed_fields, stuffed_bits = _stuff_field_bits(fields)
    wire = START_FLAG + _pack_bits(stuffed_bits)
    if len(wire) > MAX_FRAME_SIZE:
        raise ValueError("Stuffed frame exceeds 100 bytes.")

    before = " ".join(_field_before(field) for field in [START_FLAG, *fields[:-1]])
    after = " ".join([_field_before(START_FLAG), *stuffed_fields[:-1]])
    return FrameTransmission(wire=wire, before=before, after_html=after)


def _utf8_length(first_byte: int) -> int | None:
    if first_byte < 0x80:
        return 1
    if 0xC2 <= first_byte <= 0xDF:
        return 2
    if 0xE0 <= first_byte <= 0xEF:
        return 3
    if 0xF0 <= first_byte <= 0xF4:
        return 4
    return None


class FrameDecoder:
    """Incrementally recognize frames in arbitrary chunks of serial bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Return every complete valid frame present after adding ``data``."""
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            marker = self._buffer.find(START_FLAG)
            if marker < 0:
                del self._buffer[:-len(START_FLAG) + 1]
                return frames
            if marker:
                del self._buffer[:marker]
            decoded = self._decode_body(bytes(self._buffer[len(START_FLAG) :]))
            if decoded is None:
                return frames
            frame, consumed = decoded
            if frame is False:
                del self._buffer[0]
                continue
            frames.append(frame)
            del self._buffer[: len(START_FLAG) + consumed]

    @staticmethod
    def _decode_body(raw: bytes) -> tuple[Frame | bool, int] | None:
        output: list[int] = []
        current = 0
        bit_count = 0
        ones = 0
        data_size: int | None = None
        required_bits: int | None = None
        consumed_bits = 0
        for byte in raw:
            for shift in range(7, -1, -1):
                bit = (byte >> shift) & 1
                consumed_bits += 1
                if ones == 5:
                    if bit != 0:
                        return False, 0
                    ones = 0
                    continue
                current = (current << 1) | bit
                bit_count += 1
                ones = ones + 1 if bit else 0
                if bit_count != 8:
                    continue
                output.append(current)
                current = 0
                bit_count = 0
                if data_size is None:
                    data_size = _utf8_length(output[0])
                    if data_size is None:
                        return False, 0
                    required_bits = (data_size + _BODY_TRAILER_SIZE) * 8
                if required_bits is not None and len(output) * 8 == required_bits:
                    payload = bytes(output)
                    try:
                        text = payload[:data_size].decode("utf-8")
                        if len(text) != 1:
                            return False, 0
                        if any(payload[data_size : data_size + SERVICE_BYTES]):
                            return False, 0
                        frame = Frame(data=payload[:data_size], reserved=payload[-RESERVED_BYTES:])
                    except (UnicodeDecodeError, ValueError):
                        return False, 0
                    return frame, (consumed_bits + 7) // 8
        return None
