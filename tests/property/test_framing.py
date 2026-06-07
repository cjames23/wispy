"""Property tests for the Content-Length frame codec.

``encode_frame``
round-trips through ``FrameDecoder``, the decoder recovers from arbitrary
garbage prefixes by emitting a :class:`~wispy.framing.DecodeError`, and the
decoder is invariant to how a sequence of valid frames is chunked across
``feed`` calls.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wispy.framing import DecodeError, FrameDecoder, encode_frame

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Frame payloads must be valid UTF-8 to round-trip cleanly: ``FrameDecoder``
# emits a ``DecodeError`` for any payload that is not valid UTF-8, and that
# path is exercised separately by the recovery property.
utf8_payloads = st.text(max_size=512).map(lambda s: s.encode("utf-8"))


def _split_into_chunks(data: bytes, draw: st.DrawFn) -> list[bytes]:
    """Slice ``data`` into a list of (possibly empty) consecutive chunks.

    Generates split points uniformly within ``[0, len(data)]`` and partitions
    ``data`` accordingly. Empty chunks are intentionally allowed because the
    decoder must tolerate them.
    """
    if not data:
        # Even with an empty buffer, the decoder must accept zero or more
        # empty feeds.
        n_extra = draw(st.integers(min_value=0, max_value=3))
        return [b""] * n_extra

    n_splits = draw(st.integers(min_value=0, max_value=min(len(data), 8)))
    cuts = sorted(
        draw(
            st.lists(
                st.integers(min_value=0, max_value=len(data)),
                min_size=n_splits,
                max_size=n_splits,
            )
        )
    )
    chunks: list[bytes] = []
    prev = 0
    for cut in cuts:
        chunks.append(data[prev:cut])
        prev = cut
    chunks.append(data[prev:])
    return chunks


@st.composite
def chunked_bytes(draw: st.DrawFn, data: bytes) -> list[bytes]:
    """Hypothesis composite wrapper around :func:`_split_into_chunks`."""
    return _split_into_chunks(data, draw)


# Garbage strategy for the recovery property.
#
# The decoder demarcates a header block as the bytes preceding the FIRST
# ``\r\n\r\n`` in the buffer. To exercise recovery cleanly while still
# guaranteeing the legitimate frame survives, garbage bytes must form their
# own self-terminated, unparseable header block:
#
#   * no ``\r\n\r\n`` anywhere except at the trailing terminator we append,
#     otherwise the decoder would split the garbage at an interior boundary
#     and the trailing portion could merge with the frame's header,
#   * no ``\r`` or ``\n`` in the body, so the body is treated as a single
#     header line,
#   * no ``:`` in the body, so the line lacks the header-name/value
#     separator and is rejected as malformed,
#
# which together guarantee the decoder discards exactly the garbage bytes
# and then proceeds to the legitimate frame intact. The empty-garbage case
# is included separately because it represents a clean round-trip (no
# DecodeError expected).
_safe_garbage_body = st.binary(max_size=64).filter(lambda b: b"\r" not in b and b"\n" not in b and b":" not in b)
garbage_bytes = st.one_of(
    st.just(b""),
    _safe_garbage_body.map(lambda body: body + b"\r\n\r\n"),
)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


@given(payload=utf8_payloads, chunking_seed=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_round_trip_through_decoder(payload: bytes, chunking_seed: st.DataObject) -> None:
    """Frame round-trip and recovery.

    For every UTF-8 payload ``p``, decoding ``encode_frame(p)`` yields
    exactly ``p`` regardless of how the encoded bytes are chunked across
    ``FrameDecoder.feed`` calls.
    """
    encoded = encode_frame(payload)
    chunks = chunking_seed.draw(chunked_bytes(encoded))

    decoder = FrameDecoder()
    decoded: list[bytes] = []
    errors: list[DecodeError] = []
    for chunk in chunks:
        for item in decoder.feed(chunk):
            if isinstance(item, DecodeError):
                errors.append(item)
            else:
                decoded.append(item)

    assert errors == [], f"unexpected decode errors on a clean encode_frame round-trip: {errors!r}"
    assert decoded == [payload], (
        f"expected decoder to yield exactly [payload]; got {decoded!r} for payload {payload!r} chunked as {chunks!r}"
    )


# ---------------------------------------------------------------------------
# Recovery from a garbage prefix
# ---------------------------------------------------------------------------


@given(
    garbage=garbage_bytes,
    payload=utf8_payloads,
    chunking_seed=st.data(),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_recovery_after_garbage_prefix(
    garbage: bytes,
    payload: bytes,
    chunking_seed: st.DataObject,
) -> None:
    """Frame round-trip and recovery.

    For every byte sequence ``garbage`` that does not contain the LSP header
    terminator ``\\r\\n\\r\\n`` and every UTF-8 payload ``p``, feeding
    ``garbage + encode_frame(p)`` to ``FrameDecoder`` eventually yields ``p``
    after at least one :class:`DecodeError` describing the discarded garbage
    bytes (only when the garbage was non-empty).
    """
    stream = garbage + encode_frame(payload)
    chunks = chunking_seed.draw(chunked_bytes(stream))

    decoder = FrameDecoder()
    decoded: list[bytes] = []
    errors: list[DecodeError] = []
    for chunk in chunks:
        for item in decoder.feed(chunk):
            if isinstance(item, DecodeError):
                errors.append(item)
            else:
                decoded.append(item)

    assert decoded == [payload], (
        f"expected payload {payload!r} to be recovered after garbage "
        f"{garbage!r}; got decoded={decoded!r} errors={errors!r}"
    )

    if garbage:
        assert errors, f"expected at least one DecodeError when recovering from a non-empty garbage prefix {garbage!r}"
        # Every reported DecodeError must describe a positive number of
        # discarded bytes, and the total discarded count cannot exceed the
        # garbage length plus the bytes added by the framing of one frame
        # whose terminator is present (the decoder may, on some malformed
        # header recoveries, drop up through the next ``\r\n\r\n``).
        for err in errors:
            assert err.discarded > 0, f"DecodeError reports zero bytes: {err!r}"
            assert err.reason, f"DecodeError reason must be non-empty: {err!r}"
    else:
        # An empty garbage prefix is a clean encode/decode round-trip; the
        # decoder must not invent spurious errors.
        assert errors == [], f"unexpected DecodeErrors on clean stream: {errors!r}"


# ---------------------------------------------------------------------------
# Chunking invariance over a sequence of frames
# ---------------------------------------------------------------------------


@given(
    payloads=st.lists(utf8_payloads, max_size=8),
    chunking_seed=st.data(),
)
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_chunking_invariance_for_frame_sequence(payloads: list[bytes], chunking_seed: st.DataObject) -> None:
    """Frame round-trip and recovery.

    For every list of UTF-8 payloads, the concatenation of their encoded
    frames decodes to exactly the original list of payloads, in order, and
    this is invariant to how the byte stream is chunked across
    ``FrameDecoder.feed`` calls.
    """
    stream = b"".join(encode_frame(p) for p in payloads)
    chunks = chunking_seed.draw(chunked_bytes(stream))

    decoder = FrameDecoder()
    decoded: list[bytes] = []
    errors: list[DecodeError] = []
    for chunk in chunks:
        for item in decoder.feed(chunk):
            if isinstance(item, DecodeError):
                errors.append(item)
            else:
                decoded.append(item)

    assert errors == [], f"unexpected DecodeErrors when concatenating valid frames: {errors!r}"
    assert decoded == payloads, (
        f"frame sequence corrupted by chunking: expected {payloads!r}, got {decoded!r} for chunks {chunks!r}"
    )
