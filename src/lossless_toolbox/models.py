"""Typed domain models for the lossless-toolbox probe layer.

These Pydantic v2 models are the single typed representation of an ffprobe
result. No raw ``dict`` is passed across the boundary to the UI or the ops
layer — the probe layer parses untrusted ffprobe JSON into these models.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class KeyframeIndex(BaseModel):
    """Keyframe timestamps (seconds) for a video stream, ascending."""

    model_config = ConfigDict(frozen=True)

    times: list[float]


class StreamInfo(BaseModel):
    """A single media stream as reported by ffprobe."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    index: int
    codec_type: str
    codec_name: str
    width: int | None = None
    height: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None
    disposition: dict[str, bool]
    keyframe_index: KeyframeIndex | None = None
    rotation: int | None = None


class Chapter(BaseModel):
    """A chapter marker with a time range and optional title."""

    model_config = ConfigDict(frozen=True)

    id: int
    start_time: float
    end_time: float
    title: str | None = None


class MediaFile(BaseModel):
    """The full probe result for one media file."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    path: Path
    format_name: str
    duration: float
    streams: list[StreamInfo]
    chapters: list[Chapter] | None = None
