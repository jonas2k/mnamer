import dataclasses
import re
from datetime import date
from pathlib import Path
from string import Formatter
from typing import Any, Union

from babelfish import Language
from guessit import guessit

from mnamer.types import MediaType
from mnamer.utils import (
    fn_pipe,
    normalize_container,
    parse_date,
    str_fix_padding,
    str_replace_slashes,
    str_title_case,
    year_parse,
)

__all__ = ["Metadata", "MetadataMovie", "MetadataEpisode", "parse_metadata"]


class _MetaFormatter(Formatter):
    def format_field(self, value, format_spec):
        return format(value, format_spec) if value else ""

    def get_value(self, key, args, kwargs):
        if isinstance(key, int):
            return args[key]
        else:
            return kwargs.get(key, "")


@dataclasses.dataclass
class Metadata:
    """A dataclass which transforms and stores media metadata information."""

    container: str = None
    group: str = None
    language: Language = None
    quality: str = None
    synopsis: str = None
    file_path: dataclasses.InitVar[Path] = None
    media: MediaType = None

    def __post_init__(self, file_path: Path):
        if file_path is None:
            return
        quality_keys = {
            "audio_codec",
            "audio_profile",
            "screen_size",
            "source",
            "video_codec",
            "video_profile",
        }
        # inspect path data
        self._path_data = {}
        self._parse_path_data(file_path)
        # set common attributes
        self.media = MediaType(self._path_data["type"])
        self.quality = (
            " ".join(
                self._path_data[key]
                for key in self._path_data
                if key in quality_keys
            )
            or None
        )
        self.language = self._path_data.get("subtitle_language")
        self.group = self._path_data.get("release_group")
        self.container = file_path.suffix or None

    def __setattr__(self, key: str, value: Any):
        converter = {
            "container": normalize_container,
            "group": str.upper,
            "media": MediaType,
            "quality": str.lower,
            "synopsis": str.capitalize,
        }.get(key)
        if value is not None and converter:
            value = converter(value)
        super().__setattr__(key, value)

    def __format__(self, format_spec: str):
        raise NotImplementedError

    def __str__(self):
        return self.__format__(None)

    @property
    def extension(self):
        if self.is_subtitle and self.language:
            return f".{self.language.alpha2}{self.container}"
        else:
            return self.container

    @property
    def is_subtitle(self):
        return self.container and self.container.endswith(".srt")

    def as_dict(self):
        d = dataclasses.asdict(self)
        d["extension"] = self.extension
        return d

    def _parse_path_data(self, file_path: Path):
        options = {"type": getattr(self.media, "value", None)}
        raw_data = dict(guessit(str(file_path), options))
        if isinstance(raw_data.get("season"), list):
            raw_data = dict(guessit(str(file_path.parts[-1]), options))
        for k, v in raw_data.items():
            if isinstance(v, (int, str, date, Language)):
                self._path_data[k] = v
            elif isinstance(v, list) and all(
                [isinstance(_, (int, str)) for _ in v]
            ):
                self._path_data[k] = v[0]

    def _format_repl(self, mobj):
        format_string, key = mobj.groups()
        value = _MetaFormatter().vformat(format_string, None, self.as_dict())
        if key in {"name", "series", "synopsis", "title"}:
            value = str_title_case(value)
        return value

    def update(self, metadata: "Metadata"):
        """Overlays all none value from another Metadata instance."""
        for field in dataclasses.asdict(self).keys():
            value = getattr(metadata, field)
            if value is None:
                continue
            super().__setattr__(field, value)


@dataclasses.dataclass
class MetadataMovie(Metadata):
    """
    A dataclass which transforms and stores media metadata information specific
    to movies.
    """

    name: str = None
    year: int = None
    id_imdb: str = None
    id_tmdb: Union[int, str] = None
    media: MediaType = MediaType.MOVIE

    def __post_init__(self, file_path: Path):
        if file_path is None:
            return
        super().__post_init__(file_path)
        self.name = self._path_data.get("title")
        self.year = self._path_data.get("year")

    def __format__(self, format_spec: str):
        default = "{name} ({year})"
        re_pattern = r"({(\w+)(?:\[[\w:]+\])?(?:\:\d{1,2})?})"
        s = re.sub(re_pattern, self._format_repl, format_spec or default)
        s = str_fix_padding(s)
        return s

    def __setattr__(self, key: str, value: Any):
        converter = {
            "name": fn_pipe(str_replace_slashes, str_title_case),
            "year": year_parse,
        }.get(key)
        if value is not None and converter:
            value = converter(value)
        super().__setattr__(key, value)


@dataclasses.dataclass
class MetadataEpisode(Metadata):
    """
    A dataclass which transforms and stores media metadata information specific
    to television episodes.
    """

    series: str = None
    season: Union[int, str] = None
    episode: Union[int, str] = None
    date: Union[date, str] = None
    title: str = None
    id_tvdb: Union[int, str] = None
    id_tvmaze: Union[int, str] = None
    media: MediaType = MediaType.EPISODE

    def __post_init__(self, file_path: Path):
        if file_path is None:
            return
        super().__post_init__(file_path)
        self.date = self._path_data.get("date")
        self.episode = self._path_data.get("episode")
        self.season = self._path_data.get("season")
        self.series = self._path_data.get("title")
        alternative_title = self._path_data.get("alternative_title")
        if alternative_title:
            self.series = f"{self.series} {alternative_title}"
        # adding year to title can reduce false positives
        # year = self._path_data.get("year")
        # if year:
        #     self.series = f"{self.series} {year}"

    def __format__(self, format_spec: str):
        default = "{series} - {season:02}x{episode:02} - {title}"
        re_pattern = r"({(\w+)(?:\[[\w:]+\])?(?:\:\d{1,2})?})"
        s = re.sub(re_pattern, self._format_repl, format_spec or default)
        s = str_fix_padding(s)
        return s

    def __setattr__(self, key: str, value: Any):
        converter = {
            "date": parse_date,
            "episode": int,
            "season": int,
            "series": fn_pipe(str_replace_slashes, str_title_case),
            "title": fn_pipe(str_replace_slashes, str_title_case),
        }.get(key)
        if value is not None and converter:
            value = converter(value)
        super().__setattr__(key, value)


def parse_metadata(file_path: Path, media_hint: MediaType = None) -> Metadata:
    """
    A factory function which parses a file path and returns the appropriate
    Metadata derived class for the given media_hint if provided, else best guess
    if omitted.
    """
    metadata = Metadata(file_path=file_path, media=media_hint)
    derived_cls = {
        MediaType.EPISODE: MetadataEpisode,
        MediaType.MOVIE: MetadataMovie,
    }[metadata.media]
    return derived_cls(**dataclasses.asdict(metadata), file_path=file_path)
