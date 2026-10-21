"""Packet serialization and stream decoding for laboratory work No. 2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable


START_FLAG: Final[bytes] = b"450502-1"
SERVICE_BYTES: Final[int] = 3
RESERVED_BYTES: Final[int] = 10
MAX_FRAME_SIZE: Final[int] = 100
MAX_DATA_SIZE: Final[int] = (
    MAX_FRAME_SIZE - len(START_FLAG) - SERVICE_BYTES - RESERVED_BYTES
)
BODY_SIZE: Final[int] = MAX_FRAME_SIZE - len(START_FLAG)
BODY_BITS: Final[int] = BODY_SIZE * 8


def _bits_of_byte(value: int) -> Iterable[int]:
    """Yield one byte from the most significant bit to the least."""
    for shift in range(7, -1, -1):
        yield (value >> shift) & 1


def _flag_bits() -> tuple[int, ...]:
    return tuple(bit for byte in START_FLAG for bit in _bits_of_byte(byte))


FLAG_BITS: Final[tuple[int, ...]] = _flag_bits()
FLAG_LENGTH: Final[int] = len(FLAG_BITS)
STUFF_BIT: Final[int] = 1 - FLAG_BITS[-1]


def _build_failure_table(pattern: tuple[int, ...]) -> tuple[int, ...]:
    """Return the KMP fallback state for every prefix of ``pattern``."""
    table = [0] * len(pattern)
    state = 0
    for position in range(1, len(pattern)):
        while state and pattern[position] != pattern[state]:
            state = table[state - 1]
        if pattern[position] == pattern[state]:
            state += 1
        table[position] = state
    return tuple(table)


FLAG_FAILURE: Final[tuple[int, ...]] = _build_failure_table(FLAG_BITS)


class _FlagTracker:
    """KMP state machine used identically while stuffing and de-stuffing."""

    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state = 0

    def consume(self, bit: int) -> bool:
        """Consume a bit and report whether 63 flag bits now match."""
        while self.state and bit != FLAG_BITS[self.state]:
            self.state = FLAG_FAILURE[self.state - 1]
        if bit == FLAG_BITS[self.state]:
            self.state += 1
        return self.state == FLAG_LENGTH - 1


class _BitWriter:
    """Append individual MSB-first bits to a compact byte buffer."""

    __slots__ = ("_bytes", "_value", "_count")

    def __init__(self) -> None:
        self._bytes = bytearray()
        self._value = 0
        self._count = 0

    def append(self, bit: int) -> None:
        self._value = (self._value << 1) | bit
        self._count += 1
        if self._count == 8:
            self._bytes.append(self._value)
            self._value = 0
            self._count = 0

    def finish(self) -> bytes:
        if self._count:
            self._bytes.append(self._value << (8 - self._count))
            self._value = 0
            self._count = 0
        return bytes(self._bytes)


def _binary_byte(value: int) -> str:
    return f"{value:08b}"


def _format_field(field: bytes) -> str:
    return ",".join(_binary_byte(value) for value in field)


@dataclass(frozen=True)
class Frame:
    """One fixed-size frame before bit stuffing."""

    data: bytes
    frame_type: int = 0
    frame_number: int = 0
    checksum: int = 0
    reserved: bytes = bytes(RESERVED_BYTES)

    def __post_init__(self) -> None:
        if not self.data or len(self.data) > MAX_DATA_SIZE:
            raise ValueError("Data field does not fit into a frame.")
        if any((self.frame_type, self.frame_number, self.checksum)):
            raise ValueError("Service fields must be zero in this laboratory work.")
        if self.reserved != bytes(RESERVED_BYTES):
            raise ValueError("Reserved bytes must be zero and have length 10.")

    @property
    def flag(self) -> bytes:
        return START_FLAG

    def padded_data(self) -> bytes:
        return self.data.ljust(MAX_DATA_SIZE, b"\x00")

    @property
    def body(self) -> bytes:
        return b"".join(
            (
                bytes((self.frame_type, self.frame_number)),
                self.padded_data(),
                bytes((self.checksum,)),
                self.reserved,
            )
        )

    def to_bytes(self) -> bytes:
        return START_FLAG + self.body


@dataclass(frozen=True)
class FrameTransmission:
    """Bytes written to the port and values displayed in the status area."""

    wire: bytes
    before: str
    after_html: str


def _stuff_body(fields: tuple[bytes, ...]) -> tuple[bytes, list[str]]:
    """Serialize body fields and interrupt every 63-bit flag prefix."""
    tracker = _FlagTracker()
    writer = _BitWriter()
    displayed: list[str] = []

    for field in fields:
        # Каждый исходный байт остаётся отдельным значением в представлении:
        # это позволяет отделять значения запятыми и при этом подчёркивать
        # вставленный бит ровно в том месте, где он появился.
        byte_views: list[str] = []
        for value in field:
            view: list[str] = []
            for bit in _bits_of_byte(value):
                writer.append(bit)
                view.append(str(bit))
                if tracker.consume(bit):
                    writer.append(STUFF_BIT)
                    view.append(f"<u>{STUFF_BIT}</u>")
                    tracker.consume(STUFF_BIT)
            byte_views.append("".join(view))
        displayed.append(",".join(byte_views))

    return writer.finish(), displayed


def encode_frame(text: str | bytes) -> FrameTransmission:
    """Make a 100-byte frame, then apply bit stuffing to its body."""
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    frame = Frame(data=data)
    # В физический кадр входят и резервные байты: они участвуют в stuffing и
    # передаются по COM-порту. В окно состояния они не попадают по условию.
    wire_fields = (
        bytes((frame.frame_type,)),
        bytes((frame.frame_number,)),
        frame.padded_data(),
        bytes((frame.checksum,)),
        frame.reserved,
    )
    stuffed_body, displayed = _stuff_body(wire_fields)

    # Значения выводятся в порядке полей кадра, но без резервных байтов.
    visible_fields = wire_fields[:-1]
    before = " ".join(
        (_format_field(START_FLAG), *map(_format_field, visible_fields))
    )
    after = " ".join((_format_field(START_FLAG), *displayed[:-1]))
    return FrameTransmission(START_FLAG + stuffed_body, before, after)


class FrameDecoder:
    """Accumulate serial bytes and yield only complete valid frames."""

    def __init__(self) -> None:
        self._source = bytearray()

    def feed(self, received: bytes) -> list[Frame]:
        self._source.extend(received)
        ready: list[Frame] = []

        while True:
            flag_offset = self._source.find(START_FLAG)
            if flag_offset < 0:
                self._keep_possible_flag_prefix()
                return ready
            if flag_offset:
                del self._source[:flag_offset]

            decoded = self._decode_one(memoryview(self._source)[len(START_FLAG) :])
            if decoded is None:
                return ready
            frame, wire_bytes = decoded
            if frame is None:
                # Discard one byte and scan again: a real flag may start in
                # the remainder of a malformed candidate.
                del self._source[0]
                continue
            ready.append(frame)
            del self._source[: len(START_FLAG) + wire_bytes]

    def _keep_possible_flag_prefix(self) -> None:
        """Retain only bytes that could still start a split start flag."""
        keep = min(len(self._source), len(START_FLAG) - 1)
        if len(self._source) > keep:
            del self._source[:-keep]

    @staticmethod
    def _decode_one(raw: memoryview) -> tuple[Frame | None, int] | None:
        tracker = _FlagTracker()
        body = bytearray()
        byte_value = 0
        bits_in_byte = 0
        skip_stuff_bit = False
        consumed_bits = 0

        for value in raw:
            for bit in _bits_of_byte(value):
                consumed_bits += 1
                if skip_stuff_bit:
                    if bit != STUFF_BIT:
                        return None, 0
                    tracker.consume(bit)
                    skip_stuff_bit = False
                    if len(body) == BODY_SIZE:
                        return FrameDecoder._build_frame(body, consumed_bits)
                    continue

                byte_value = (byte_value << 1) | bit
                bits_in_byte += 1
                if bits_in_byte == 8:
                    body.append(byte_value)
                    byte_value = 0
                    bits_in_byte = 0

                if tracker.consume(bit):
                    skip_stuff_bit = True
                if len(body) == BODY_SIZE and not skip_stuff_bit:
                    return FrameDecoder._build_frame(body, consumed_bits)
        return None

    @staticmethod
    def _build_frame(body: bytearray, consumed_bits: int) -> tuple[Frame | None, int]:
        data_start = 2
        data_end = data_start + MAX_DATA_SIZE
        payload = bytes(body[data_start:data_end]).rstrip(b"\x00")
        try:
            payload.decode("utf-8")
            frame = Frame(
                data=payload,
                frame_type=body[0],
                frame_number=body[1],
                checksum=body[data_end],
                reserved=bytes(body[-RESERVED_BYTES:]),
            )
        except (UnicodeDecodeError, ValueError):
            return None, 0
        return frame, (consumed_bits + 7) // 8
