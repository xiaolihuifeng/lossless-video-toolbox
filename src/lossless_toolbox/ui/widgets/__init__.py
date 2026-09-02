"""Operation parameter panels producing validated ops specs (plan todo 14)."""

from .cut import CutPanel
from .merge import MergePanel
from .meta import MetaPanel
from .remux import RemuxPanel
from .subtitles import SubtitlePanel
from .tracks import TracksPanel

__all__ = [
    "CutPanel",
    "MergePanel",
    "MetaPanel",
    "RemuxPanel",
    "SubtitlePanel",
    "TracksPanel",
]
