"""Keyframe-aligned lossless cut operation (todo 6).

A lossless cut copies a contiguous ``[start, end)`` range of a media file by
stream-copying the elementary streams — no re-encoding. Because a stream copy
can only begin and end on a keyframe boundary, the requested cut points are
snapped forward to the nearest keyframe: ``actual_start`` is the first keyframe
at or after ``start`` (a copy cannot start mid-GOP) and ``actual_end`` is the
first keyframe at or after ``end`` (so the full requested range is included).
The snapped points are reported back in :class:`CutPlan` so the caller can
surface the true cut to the user.

The load-bearing ffmpeg rules (R1): ``-ss`` is placed BEFORE ``-i`` (input
seeking) so the copy starts on a keyframe; ``-avoid_negative_ts`` is placed
before ``-i`` too, shifting the first packet's timestamp to zero. A head cut
(``actual_start > 0``) uses ``make_zero`` unless an attached picture forces the
weaker ``auto``; a cut starting at zero omits the flag entirely.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .common import build_base_args, movflags


class CutRangeError(RuntimeError):
    """Raised when the requested ``[start, end)`` range is invalid.

    Carries the three range inputs so the caller can distinguish the exact
    failure (``start < 0``, ``end <= start`` or ``start >= duration``).
    """

    def __init__(self, start: float, end: float, duration: float) -> None:
        """Create a CutRangeError carrying the offending range values."""
        self.start = start
        self.end = end
        self.duration = duration
        super().__init__(
            f"invalid cut range: start={start:g}, end={end:g}, duration={duration:g}"
        )


class UnsupportedInputError(RuntimeError):
    """Raised when the input container starts at a nonzero timestamp.

    A nonzero-start container (e.g. an MPEG-TS muxed with a start-time offset)
    cannot be losslessly cut: re-timestamping stream-copied packets is a remux,
    not a cut.
    """

    def __init__(self, in_path: Path, format_start_time: float) -> None:
        """Create an UnsupportedInputError carrying the path and start time."""
        self.in_path = in_path
        self.format_start_time = format_start_time
        super().__init__(
            f"input {in_path} has a nonzero start time "
            f"(format_start_time={format_start_time:g}); a lossless keyframe cut "
            f"cannot re-timestamp a nonzero-start container. Re-mux the file to "
            f"a zero start time first (remux operation), then re-probe and retry."
        )


class CutPlan(BaseModel):
    """A built cut: the ffmpeg argv plus the snapped actual cut points."""

    model_config = ConfigDict(frozen=True)

    argv: list[str]
    actual_start: float
    actual_end: float


class CutSpec(BaseModel):
    """A lossless cut job: copy ``[start, end)`` of ``in_path`` into ``out_path``.

    ``keyframe_index`` (ascending pts_time list) and ``duration`` come from the
    probe layer (todo 4) and are supplied by the caller so :meth:`build_plan`
    stays a pure function of its inputs.
    """

    model_config = ConfigDict(frozen=True)

    in_path: Path
    start: float
    end: float
    out_path: Path
    keyframe_index: list[float]
    duration: float
    has_attached_pic: bool = False
    format_start_time: float = 0.0

    def build_argv(self) -> list[str]:
        """Build the keyframe-snapped cut argv (flags-only, no binary prefix).

        The batch queue's argv dispatcher calls this; the snapshot is computed
        by :meth:`build_plan` (a pure function), whose ``CutRangeError`` /
        ``UnsupportedInputError`` propagate to the queue and land on the job as
        ``failed``, mirroring the merge spec's ``MergeError`` semantics.
        """
        return self.build_plan().argv

    def build_plan(self) -> CutPlan:
        """Validate the cut, snap to keyframes and build the ffmpeg argv.

        Returns:
            A :class:`CutPlan` carrying the argv and the snapped cut points.

        Raises:
            CutRangeError: When ``start < 0``, ``end <= start`` or
                ``start >= duration``.
            UnsupportedInputError: When ``format_start_time > 0``.
        """
        if self.format_start_time > 0:
            raise UnsupportedInputError(self.in_path, self.format_start_time)
        if self.start < 0:
            raise CutRangeError(self.start, self.end, self.duration)
        if self.end <= self.start:
            raise CutRangeError(self.start, self.end, self.duration)
        if self.start >= self.duration:
            raise CutRangeError(self.start, self.end, self.duration)
        actual_start = _snap_start(self.keyframe_index, self.start)
        actual_end = _snap_end(self.keyframe_index, self.end, self.duration)
        return CutPlan(
            argv=self._build_argv(actual_start, actual_end),
            actual_start=actual_start,
            actual_end=actual_end,
        )

    def _build_argv(self, actual_start: float, actual_end: float) -> list[str]:
        """Assemble the ffmpeg argv for a ``-ss``-before-``-i`` stream copy."""
        # TODO(todo 6): converge on ops.common.copy_args once CutSpec carries
        # the probed stream list; a global `-c copy` stream-copies every mapped
        # stream without needing the stream count.
        duration = actual_end - actual_start
        args = build_base_args()
        args += ["-ss", _fmt_t(actual_start)]
        if actual_start > 0:
            mode = "auto" if self.has_attached_pic else "make_zero"
            args += ["-avoid_negative_ts", mode]
        args += ["-i", str(self.in_path), "-c", "copy"]
        args += ["-t", _fmt_t(duration)]
        args += movflags(self.out_path.suffix.lower().lstrip("."))
        args += [str(self.out_path)]
        return args


def _fmt_t(value: float) -> str:
    """Format a timestamp with up to 6 decimals, trimming trailing zeros."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _snap_start(times: list[float], start: float) -> float:
    """Return the first keyframe at or after ``start``, else the earliest."""
    for time in times:
        if time >= start:
            return time
    return times[0] if times else 0.0


def _snap_end(times: list[float], end: float, duration: float) -> float:
    """Return the first keyframe at or after ``end``, else the latest."""
    for time in times:
        if time >= end:
            return time
    return times[-1] if times else duration
