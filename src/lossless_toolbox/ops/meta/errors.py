"""Typed errors raised by the meta (metadata/chapters/rotate/cover) operations."""

from __future__ import annotations


class UnsupportedRotateError(ValueError):
    """Rotation metadata cannot be persisted in the target container."""

    def __init__(self, suffix: str) -> None:
        """Create the error, special-casing Matroska's missing element."""
        self.suffix = suffix
        super().__init__(self._message(suffix))

    @staticmethod
    def _message(suffix: str) -> str:
        if suffix == ".mkv":
            return "Matroska 无标准旋转元素"
        return (
            f"rotation metadata is only supported for MP4/MOV, "
            f"not {suffix or 'no extension'!r}"
        )


class UnsupportedCoverError(ValueError):
    """Cover art cannot be embedded into the target container or image format."""

    def __init__(self, detail: str) -> None:
        """Create the error with a human-readable reason."""
        self.detail = detail
        super().__init__(detail)

    @classmethod
    def for_container(cls, suffix: str) -> UnsupportedCoverError:
        """Build the error for an unsupported output container."""
        return cls(
            f"cover art is only supported for MP4/MOV/MKV, "
            f"not {suffix or 'no extension'!r}"
        )

    @classmethod
    def for_image(cls, suffix: str) -> UnsupportedCoverError:
        """Build the error for an unsupported cover image extension."""
        return cls(
            f"unsupported cover image format {suffix!r}; expected .jpg/.jpeg/.png"
        )
