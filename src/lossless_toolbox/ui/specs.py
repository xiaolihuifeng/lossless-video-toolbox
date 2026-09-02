"""Spec-side helpers: default output naming and the spec→argv dispatcher.

:func:`default_output_path` centralises the ``<stem>.<ext>`` naming rule with
the per-operation default extensions (todo 13) plus a panel-supplied
``extension`` override (remux target container, track format, …).

:func:`build_job_argv` is the generic argv dispatcher handed to the
:class:`~lossless_toolbox.queue.JobQueue`; it runs inside the queue worker
thread, so the one parameterised builder it resolves itself —
:class:`~lossless_toolbox.ops.subtitles.DetachSpec` needing the source
subtitle codec — may probe the input there without ever touching the UI
thread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from lossless_toolbox.ops.subtitles import DetachSpec
from lossless_toolbox.probe import probe

from .texts import (
    BUILD_ARGV_FAILED_MSG,
    DEFAULT_EXT,
    DETACH_NO_SUB_MSG,
    NO_BUILD_ARGV_MSG,
    UNKNOWN_OP_MSG,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class JobSpecError(RuntimeError):
    """Raised when a submitted spec cannot be turned into an argv."""


def default_output_path(
    source: Path,
    op_key: str,
    output_dir: Path | None = None,
    *,
    extension: str | None = None,
) -> Path:
    """Return the default output path ``<stem>.<ext>`` for one job.

    The extension follows the operation rules (remux/cut/subtitles -> mkv,
    merge -> mp4, tracks -> m4a, meta -> same container) unless the caller
    passes an ``extension`` override (panel-chosen container/format). When
    the computed path would overwrite the source itself, the operation key
    is inserted (``<stem>.<op>.<ext>``) as a collision guard.
    """
    ext = extension if extension is not None else DEFAULT_EXT.get(op_key)
    if ext is None:
        raise JobSpecError(UNKNOWN_OP_MSG % op_key)
    if not ext:
        ext = source.suffix.lower()
    directory = output_dir if output_dir is not None else source.parent
    candidate = directory / f"{source.stem}{ext}"
    if candidate == source:
        candidate = directory / f"{source.stem}.{op_key}{ext}"
    return candidate


def build_job_argv(spec: object) -> list[str]:
    """Dispatch one ops spec to its argv builder and validate the argv.

    Parameter-free ``build_argv()`` methods are called directly; the detach
    spec's required source codec is resolved by probing the input (this runs
    inside the queue worker thread). Any failure raises
    :class:`JobSpecError`, which the queue records on ``JobRecord.error``.
    """
    if isinstance(spec, DetachSpec):
        return _build_detach_argv(spec)
    build = cast("Callable[[], object]", getattr(spec, "build_argv", None))
    if build is None:
        raise JobSpecError(NO_BUILD_ARGV_MSG % type(spec).__name__)
    try:
        argv = build()
    except TypeError as exc:
        raise JobSpecError(BUILD_ARGV_FAILED_MSG % (type(spec).__name__, exc)) from exc
    argv_list = cast("list[object]", argv)
    if not isinstance(argv, list) or any(
        not isinstance(token, str) for token in argv_list
    ):
        raise JobSpecError(
            BUILD_ARGV_FAILED_MSG % (type(spec).__name__, "返回了非法的 argv")
        )
    return cast("list[str]", argv)


def _build_detach_argv(spec: DetachSpec) -> list[str]:
    """Probe the source's subtitle codec and build the detach argv."""
    media = probe(spec.in_path)
    subtitles = [s for s in media.streams if s.codec_type == "subtitle"]
    if not 0 <= spec.stream_index < len(subtitles):
        raise JobSpecError(DETACH_NO_SUB_MSG % spec.in_path)
    return spec.build_argv(subtitles[spec.stream_index].codec_name)
