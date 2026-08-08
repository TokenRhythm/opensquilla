"""Conservative detection of TeX constructs that can hide counted prose."""

from __future__ import annotations

import re

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_ZERO_BOX_RE = re.compile(
    r"\\(?:phantom|hphantom|vphantom|smash|rlap|llap)\b",
    re.IGNORECASE,
)
_BACKGROUND_COLOR_RE = re.compile(
    r"\\(?:pagecolor|colorbox|fcolorbox|rowcolor|rowcolors|cellcolor|columncolor)\b",
    re.IGNORECASE,
)
_ABSOLUTE_POSITION_RE = re.compile(
    r"\\begin\s*\{picture\}|\\put\s*\(",
    re.IGNORECASE,
)
_NON_RENDERED_CONTENT_RE = re.compile(
    r"\\(?:typeout|message|write|immediate|savebox|sbox|setbox)\b|"
    r"\\begin\s*\{(?:lrbox|filecontents\*?)\}",
    re.IGNORECASE,
)
_COLOR_DEFINITION_RE = re.compile(
    r"\\(?:definecolor|providecolor)\s*\{([^{}]+)\}\s*"
    r"\{([^{}]+)\}\s*\{([^{}]+)\}",
    re.IGNORECASE,
)
_COLOR_ALIAS_RE = re.compile(
    r"\\colorlet\s*\{([^{}]+)\}\s*(?:\[[^\]]+\]\s*)?\{([^{}]+)\}",
    re.IGNORECASE,
)
_TEXT_COLOR_RE = re.compile(
    r"\\(color|textcolor)\s*(?:\[([^\]]+)\]\s*)?\{([^{}]+)\}",
    re.IGNORECASE,
)
_OPACITY_COMMAND_RE = re.compile(
    r"\\(transparent|texttransparent|pgfsetfillopacity|pgfsetstrokeopacity)\b",
    re.IGNORECASE,
)
_OPACITY_ARGS_RE = re.compile(
    r"\\(?:transparent|texttransparent|pgfsetfillopacity|pgfsetstrokeopacity)"
    r"\s*\{([^{}]+)\}",
    re.IGNORECASE,
)
_FONT_SIZE_COMMAND_RE = re.compile(r"\\fontsize\b", re.IGNORECASE)
_FONT_SIZE_ARGS_RE = re.compile(
    r"\\fontsize\s*\{([^{}]+)\}\s*\{([^{}]+)\}",
    re.IGNORECASE,
)
_SCALE_COMMAND_RE = re.compile(r"\\scalebox\b", re.IGNORECASE)
_SCALE_ARGS_RE = re.compile(
    r"\\scalebox\s*\{([^{}]+)\}(?:\s*\[([^\]]+)\])?",
    re.IGNORECASE,
)
_RESIZE_COMMAND_RE = re.compile(r"\\resizebox\b", re.IGNORECASE)
_RESIZE_ARGS_RE = re.compile(
    r"\\resizebox\s*\{([^{}]+)\}\s*\{([^{}]+)\}",
    re.IGNORECASE,
)
_RAISE_COMMAND_RE = re.compile(r"\\raisebox\b", re.IGNORECASE)
_RAISE_ARGS_RE = re.compile(
    r"\\raisebox\s*\{([^{}]+)\}",
    re.IGNORECASE,
)
_DIMENSION_RE = re.compile(
    rf"^\s*({_NUMBER})\s*(pt|bp|in|cm|mm|pc|em|ex|sp)?\s*$",
    re.IGNORECASE,
)
_RELATIVE_DIMENSION_RE = re.compile(
    rf"^\s*({_NUMBER})\s*\\(?:linewidth|textwidth|paperwidth|columnwidth|"
    r"textheight|paperheight|hsize|vsize)\s*$",
    re.IGNORECASE,
)
_RELATIVE_DIMENSION_NAME_RE = re.compile(
    r"^\s*\\(?:linewidth|textwidth|paperwidth|columnwidth|textheight|paperheight|"
    r"hsize|vsize)\s*$",
    re.IGNORECASE,
)
_BOX_RELATIVE_OFFSET_RE = re.compile(
    rf"^\s*({_NUMBER})\s*\\(?:height|depth|totalheight)\s*$",
    re.IGNORECASE,
)
_POINTS_PER_UNIT = {
    "pt": 1.0,
    "bp": 72.27 / 72.0,
    "in": 72.27,
    "cm": 72.27 / 2.54,
    "mm": 72.27 / 25.4,
    "pc": 12.0,
    "em": 10.0,
    "ex": 5.0,
    "sp": 1.0 / 65536.0,
}


def _numbers(value: str) -> tuple[float, ...] | None:
    try:
        return tuple(float(item.strip()) for item in value.split(","))
    except ValueError:
        return None


def _xcolor_whiteness(value: str, white_names: set[str]) -> float | None:
    """Return a provable lower white bound for an xcolor mix expression."""

    parts = [part.strip().casefold() for part in value.split("!")]
    if not parts:
        return None

    def base(name: str) -> tuple[float, float]:
        if name in white_names:
            return 1.0, 1.0
        if name == "black":
            return 0.0, 0.0
        # Unknown named colors range from black to white. Interval propagation
        # still proves attacks such as red!1 (99% default-white) are near-white.
        return 0.0, 1.0

    current = base(parts[0])
    index = 1
    while index < len(parts):
        try:
            weight = float(parts[index]) / 100.0
        except ValueError:
            return None
        if not 0.0 <= weight <= 1.0:
            return None
        other_name = parts[index + 1] if index + 1 < len(parts) and parts[index + 1] else "white"
        other = base(other_name)
        current = (
            weight * current[0] + (1.0 - weight) * other[0],
            weight * current[1] + (1.0 - weight) * other[1],
        )
        index += 2
    return current[0]


def _is_nearly_white(model: str, value: str, white_names: set[str]) -> bool:
    normalized_value = value.strip().casefold()
    if not model:
        whiteness = _xcolor_whiteness(normalized_value, white_names)
        return whiteness is not None and whiteness >= 0.98
    if model == "RGB":
        channels = _numbers(value)
        return bool(channels and len(channels) == 3 and min(channels) >= 250)
    if model == "Gray":
        channels = _numbers(value)
        return bool(channels and len(channels) == 1 and channels[0] >= 14.7)
    normalized_model = model.strip().casefold()
    if normalized_model == "named":
        return normalized_value in white_names
    if normalized_model == "rgb":
        channels = _numbers(value)
        return bool(channels and len(channels) == 3 and min(channels) >= 0.98)
    if normalized_model == "gray":
        channels = _numbers(value)
        return bool(channels and len(channels) == 1 and channels[0] >= 0.98)
    if normalized_model == "cmy":
        channels = _numbers(value)
        return bool(channels and len(channels) == 3 and max(channels) <= 0.02)
    if normalized_model == "cmyk":
        channels = _numbers(value)
        return bool(channels and len(channels) == 4 and max(channels) <= 0.02)
    if normalized_model == "hsb":
        channels = _numbers(value)
        return bool(
            channels
            and len(channels) == 3
            and channels[1] <= 0.02
            and channels[2] >= 0.98
        )
    if normalized_model == "html":
        compact = normalized_value.removeprefix("#")
        if not re.fullmatch(r"[0-9a-f]{6}", compact):
            return False
        return min(int(compact[index : index + 2], 16) for index in (0, 2, 4)) >= 250
    return False


def _dimension_points(value: str) -> float | None:
    match = _DIMENSION_RE.fullmatch(value)
    if match is None:
        return None
    unit = (match.group(2) or "pt").casefold()
    return float(match.group(1)) * _POINTS_PER_UNIT[unit]


def _plain_number(value: str) -> float | None:
    if re.fullmatch(rf"\s*{_NUMBER}\s*", value) is None:
        return None
    return float(value)


def _safe_resize_dimension(value: str) -> bool:
    points = _dimension_points(value)
    if points is not None:
        return abs(points) > 4.0
    relative = _RELATIVE_DIMENSION_RE.fullmatch(value)
    if relative is not None:
        return abs(float(relative.group(1))) > 0.1
    return _RELATIVE_DIMENSION_NAME_RE.fullmatch(value) is not None


def _safe_raise_offset(value: str) -> bool:
    points = _dimension_points(value)
    if points is not None:
        return abs(points) < 100
    box_relative = _BOX_RELATIVE_OFFSET_RE.fullmatch(value)
    return bool(box_relative and abs(float(box_relative.group(1))) <= 2.0)


def find_unsafe_text_visibility_controls(tex: str) -> tuple[str, ...]:
    """Return only controls that are inherently hidden or have extreme arguments.

    Common scholarly formatting such as ``\\resizebox{\\linewidth}{!}``, named
    colors, ordinary font sizes, and small baseline adjustments remains valid.
    The checks intentionally target exact high-confidence cases so the content
    gate and the compile runtime enforce the same authoring contract.
    """

    controls = set(_ZERO_BOX_RE.findall(tex))
    controls.update(match.group(0) for match in _ABSOLUTE_POSITION_RE.finditer(tex))
    controls.update(match.group(0) for match in _NON_RENDERED_CONTENT_RE.finditer(tex))
    # Background painting is not needed by the paper template and makes a
    # local foreground/background equality impossible to prove with a safe,
    # non-executing parser (for example pagecolor black + default black text).
    controls.update(_BACKGROUND_COLOR_RE.findall(tex))
    white_names = {"white"}
    for match in _COLOR_DEFINITION_RE.finditer(tex):
        name, model, value = match.groups()
        if _is_nearly_white(model.strip(), value, white_names):
            white_names.add(name.strip().casefold())
    changed = True
    while changed:
        changed = False
        for name, source in _COLOR_ALIAS_RE.findall(tex):
            normalized_name = name.strip().casefold()
            if (
                _is_nearly_white("", source, white_names)
                and normalized_name not in white_names
            ):
                white_names.add(normalized_name)
                changed = True

    for match in _TEXT_COLOR_RE.finditer(tex):
        command, model, value = match.groups()
        if _is_nearly_white(model or "", value, white_names):
            controls.add(f"\\{command.casefold()}")
    for command_match in _OPACITY_COMMAND_RE.finditer(tex):
        args = _OPACITY_ARGS_RE.match(tex, command_match.start())
        opacity = _plain_number(args.group(1)) if args is not None else None
        if opacity is None or not 0.05 < opacity <= 1.0:
            controls.add(f"\\{command_match.group(1).casefold()}")
    for command_match in _FONT_SIZE_COMMAND_RE.finditer(tex):
        args = _FONT_SIZE_ARGS_RE.match(tex, command_match.start())
        font_size = _dimension_points(args.group(1)) if args is not None else None
        baseline_skip = _dimension_points(args.group(2)) if args is not None else None
        if (
            font_size is None
            or baseline_skip is None
            or font_size <= 4.0
            or baseline_skip <= 4.0
        ):
            controls.add(r"\fontsize")
    for command_match in _SCALE_COMMAND_RE.finditer(tex):
        args = _SCALE_ARGS_RE.match(tex, command_match.start())
        horizontal = _plain_number(args.group(1)) if args is not None else None
        vertical = (
            _plain_number(args.group(2)) if args is not None and args.group(2) else horizontal
        )
        if (
            horizontal is None
            or vertical is None
            or abs(horizontal) <= 0.1
            or abs(vertical) <= 0.1
        ):
            controls.add(r"\scalebox")
    for command_match in _RESIZE_COMMAND_RE.finditer(tex):
        args = _RESIZE_ARGS_RE.match(tex, command_match.start())
        if args is None:
            controls.add(r"\resizebox")
            continue
        dimensions = [value.strip() for value in args.groups()]
        concrete = [value for value in dimensions if value != "!"]
        if not concrete or any(not _safe_resize_dimension(value) for value in concrete):
            controls.add(r"\resizebox")
    for command_match in _RAISE_COMMAND_RE.finditer(tex):
        args = _RAISE_ARGS_RE.match(tex, command_match.start())
        if args is None or not _safe_raise_offset(args.group(1)):
            controls.add(r"\raisebox")
    return tuple(sorted(controls, key=str.casefold))
