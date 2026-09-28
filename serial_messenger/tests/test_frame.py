"""Tests for the variant-1 frame format and stream recognition."""

from __future__ import annotations

import unittest

from backend.frame import MAX_FRAME_SIZE, START_FLAG, FrameDecoder, encode_frame


class FrameTests(unittest.TestCase):
    """Verify packet limits, stuffing and reception across arbitrary chunks."""

    def test_frame_starts_with_required_flag_and_fits_limit(self) -> None:
        transmission = encode_frame("Я")
        self.assertTrue(transmission.wire.startswith(START_FLAG))
        self.assertLessEqual(len(transmission.wire), MAX_FRAME_SIZE)

    def test_inserted_zero_is_underlined_in_status_representation(self) -> None:
        # U+07FF contains a byte with five consecutive one bits in UTF-8.
        transmission = encode_frame("߿")
        self.assertIn("<u>0</u>", transmission.after_html)

    def test_decoder_handles_split_and_joined_serial_reads(self) -> None:
        first = encode_frame("Я").wire
        second = encode_frame("A").wire
        decoder = FrameDecoder()
        frames = []
        for chunk in (first[:9], first[9:] + second[:3], second[3:]):
            frames.extend(decoder.feed(chunk))
        self.assertEqual([frame.data.decode("utf-8") for frame in frames], ["Я", "A"])

    def test_decoder_ignores_noise_before_a_frame(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(b"noise" + encode_frame("Z").wire)
        self.assertEqual([frame.data for frame in frames], [b"Z"])


if __name__ == "__main__":
    unittest.main()
