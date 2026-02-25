#!/usr/bin/env python3
"""
ADB Touch Studio (Qt)

Features:
- Live Android screen stream via `adb exec-out screencap -p`
- Manual crop selection on the preview; preview auto-zooms to crop
- SVG/CSV loader sidebar with drag-and-drop onto the preview
- Dropping an SVG/CSV triggers ADB touch playback mapped to crop region

CSV formats supported:
1) Header CSV with x,y (or nx,ny / u,v)
2) Header CSV with optional time column: t, time, ms, dt, duration
3) Plain 2-column CSV (x,y)

Coordinate mapping:
- If all values are in [0,1], they are treated as normalized.
- Otherwise values are min-max normalized to [0,1] per axis.
"""

import csv
import html
import math
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

try:
    from svgpathtools import Path as SvgPath
    from svgpathtools import parse_path
except Exception:
    SvgPath = None
    parse_path = None


SEGMENT_LEN_PIXELS = 4.0
STROKE_DURATION_MS = 28


def adb(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["adb"] + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def adb_bytes(cmd: List[str]) -> Tuple[int, bytes, str]:
    proc = subprocess.run(["adb"] + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout, proc.stderr.decode(errors="replace").strip()


def get_device_size() -> Tuple[int, int]:
    rc, out, err = adb(["shell", "wm", "size"])
    if rc != 0:
        raise RuntimeError(f"adb wm size failed: {err or out}")
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        raise RuntimeError(f"Could not parse device size from: {out!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass
class TouchBackend:
    mode: str = "swipe"
    event_path: str = ""
    max_x: int = 0
    max_y: int = 0
    screen_w: int = 0
    screen_h: int = 0


def detect_touch_backend(screen_w: int, screen_h: int) -> TouchBackend:
    rc, out, _ = adb(["shell", "getevent", "-lp"])
    if rc != 0 or not out:
        return TouchBackend(mode="swipe", screen_w=screen_w, screen_h=screen_h)

    blocks = re.split(r"(?=add device\s+\d+:)", out)
    candidates: List[Tuple[int, str, int, int]] = []
    for b in blocks:
        pm = re.search(r"add device\s+\d+:\s*(/dev/input/event\d+)", b)
        if not pm:
            continue
        path = pm.group(1)

        mx = None
        my = None
        m1 = re.search(r"ABS_MT_POSITION_X[^\n]*max\s+([0-9]+)", b)
        m2 = re.search(r"ABS_MT_POSITION_Y[^\n]*max\s+([0-9]+)", b)
        if m1:
            mx = int(m1.group(1))
        if m2:
            my = int(m2.group(1))

        if mx is None:
            m1 = re.search(r"\b0035\b[^\n]*max\s+([0-9]+)", b)
            if m1:
                mx = int(m1.group(1))
        if my is None:
            m2 = re.search(r"\b0036\b[^\n]*max\s+([0-9]+)", b)
            if m2:
                my = int(m2.group(1))

        if mx is None or my is None:
            continue

        score = 0
        low = b.lower()
        if "input_prop_direct" in low:
            score += 4
        if "touch" in low:
            score += 2
        if "mt_position_x" in low:
            score += 1
        candidates.append((score, path, mx, my))

    if not candidates:
        return TouchBackend(mode="swipe", screen_w=screen_w, screen_h=screen_h)

    candidates.sort(key=lambda t: t[0], reverse=True)
    _, path, mx, my = candidates[0]

    rc2, _, _ = adb(["shell", "sendevent", path, "0", "0", "0"])
    if rc2 == 0:
        return TouchBackend(
            mode="sendevent",
            event_path=path,
            max_x=max(1, mx),
            max_y=max(1, my),
            screen_w=max(1, screen_w),
            screen_h=max(1, screen_h),
        )

    rc3, out3, _ = adb(["shell", "input"])
    if rc3 == 0 and "motionevent" in (out3 or ""):
        return TouchBackend(
            mode="motionevent",
            screen_w=max(1, screen_w),
            screen_h=max(1, screen_h),
        )

    return TouchBackend(
        mode="swipe",
        screen_w=max(1, screen_w),
        screen_h=max(1, screen_h),
    )


@dataclass
class GestureTrack:
    path: str
    kind: str
    strokes: List[List[Tuple[float, float]]]
    svg_paths: Optional[List[Any]] = None
    default_duration_ms: int = STROKE_DURATION_MS
    durations_ms: Optional[List[int]] = None


def _pick_header(headers: List[str], names: List[str]) -> Optional[str]:
    lower = {h.strip().lower(): h for h in headers}
    for n in names:
        if n in lower:
            return lower[n]
    return None


def _parse_float(v: str) -> Optional[float]:
    try:
        return float(v.strip())
    except Exception:
        return None


def _normalize_points(raw_pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not raw_pts:
        return []

    xs = [p[0] for p in raw_pts]
    ys = [p[1] for p in raw_pts]

    if min(xs) >= 0.0 and max(xs) <= 1.0 and min(ys) >= 0.0 and max(ys) <= 1.0:
        return raw_pts

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    dx = max(max_x - min_x, 1e-9)
    dy = max(max_y - min_y, 1e-9)

    return [((x - min_x) / dx, (y - min_y) / dy) for (x, y) in raw_pts]


def _normalize_strokes(
    raw_strokes: List[List[Tuple[float, float]]],
) -> List[List[Tuple[float, float]]]:
    all_pts = [pt for stroke in raw_strokes for pt in stroke]
    if not all_pts:
        return []
    norm = _normalize_points(all_pts)
    out: List[List[Tuple[float, float]]] = []
    i = 0
    for stroke in raw_strokes:
        n = len(stroke)
        if n >= 2:
            out.append(norm[i : i + n])
        i += n
    return out


def load_csv_track(path: str) -> GestureTrack:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    raw_pts: List[Tuple[float, float]] = []
    times: List[Optional[float]] = []

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except Exception:
            dialect = csv.excel

        reader = csv.reader(f, dialect)
        rows = [r for r in reader if any(c.strip() for c in r)]

    if not rows:
        raise ValueError("CSV is empty")

    header_like = any(not _parse_float(c) for c in rows[0][:2])

    if header_like:
        headers = [h.strip() for h in rows[0]]
        xh = _pick_header(headers, ["x", "nx", "u", "px"])
        yh = _pick_header(headers, ["y", "ny", "v", "py"])
        th = _pick_header(headers, ["t", "time", "ms", "dt", "duration"])
        if not xh or not yh:
            raise ValueError("Could not find x/y headers")

        index = {h: i for i, h in enumerate(headers)}
        for row in rows[1:]:
            if len(row) <= max(index[xh], index[yh]):
                continue
            xv = _parse_float(row[index[xh]])
            yv = _parse_float(row[index[yh]])
            if xv is None or yv is None:
                continue
            raw_pts.append((xv, yv))

            tv = None
            if th and len(row) > index[th]:
                tv = _parse_float(row[index[th]])
            times.append(tv)
    else:
        for row in rows:
            if len(row) < 2:
                continue
            xv = _parse_float(row[0])
            yv = _parse_float(row[1])
            if xv is None or yv is None:
                continue
            raw_pts.append((xv, yv))
            tv = _parse_float(row[2]) if len(row) >= 3 else None
            times.append(tv)

    if len(raw_pts) < 2:
        raise ValueError("Need at least 2 valid points")

    pts = _normalize_points(raw_pts)

    durations_ms: List[int] = []
    has_time = any(t is not None for t in times)
    if has_time:
        clean = [0.0 if t is None else float(t) for t in times]
        increasing = all(clean[i] <= clean[i + 1] for i in range(len(clean) - 1))
        if increasing:
            for i in range(len(clean) - 1):
                dt = int(round(max(8.0, clean[i + 1] - clean[i])))
                durations_ms.append(dt)
        else:
            durations_ms = [
                int(round(max(8.0, t if t is not None else 16.0))) for t in clean[:-1]
            ]
    else:
        durations_ms = [16] * (len(pts) - 1)

    return GestureTrack(
        path=str(p),
        kind="csv",
        strokes=[pts],
        default_duration_ms=16,
        durations_ms=durations_ms,
    )


ATTR_RE = lambda name: re.compile(rf'\b{name}\s*=\s*"([^"]*)"', re.IGNORECASE)


def parse_style(style_str: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in style_str.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _parse_num(value: str, default: float = 0.0) -> float:
    if not value:
        return default
    m = re.search(r"[-+]?\d*\.?\d+", value)
    if not m:
        return default
    try:
        return float(m.group(0))
    except Exception:
        return default


def _qfont_from_attrs(tag: str, style: Dict[str, str]) -> QtGui.QFont:
    fam_raw = style.get("font-family", get_attr(tag, "font-family") or "Sans Serif")
    family = fam_raw.split(",")[0].strip().strip("'\"")

    size_raw = style.get("font-size", get_attr(tag, "font-size") or "64")
    px = max(8, int(round(_parse_num(size_raw, 64.0))))

    font = QtGui.QFont(family)
    font.setPixelSize(px)

    weight_raw = (style.get("font-weight", get_attr(tag, "font-weight") or "")).lower()
    if "bold" in weight_raw:
        font.setBold(True)
    else:
        n = int(_parse_num(weight_raw, 0.0))
        if n >= 600:
            font.setBold(True)

    fs = (style.get("font-style", get_attr(tag, "font-style") or "")).lower()
    if "italic" in fs:
        font.setItalic(True)

    return font


def extract_text_paths(svg_text: str) -> List[Any]:
    if parse_path is None:
        return []

    out: List[Any] = []
    text_blocks = re.finditer(
        r"<text\b([^>]*)>(.*?)</text>", svg_text, re.IGNORECASE | re.DOTALL
    )

    for m in text_blocks:
        text_tag = f"<text {m.group(1)}>"
        body = m.group(2)
        style = parse_style(get_attr(text_tag, "style"))
        font = _qfont_from_attrs(text_tag, style)

        base_x = _parse_num(get_attr(text_tag, "x"), 0.0)
        base_y = _parse_num(get_attr(text_tag, "y"), 0.0)
        anchor = (
            get_attr(text_tag, "text-anchor") or style.get("text-anchor") or "start"
        ).lower()

        tspans = list(
            re.finditer(
                r"<tspan\b([^>]*)>(.*?)</tspan>", body, re.IGNORECASE | re.DOTALL
            )
        )

        lines: List[Tuple[str, float, float, QtGui.QFont]] = []
        cur_y = base_y

        if tspans:
            for tm in tspans:
                ttag = f"<tspan {tm.group(1)}>"
                tstyle = parse_style(get_attr(ttag, "style"))
                merged = dict(style)
                merged.update(tstyle)
                tf = _qfont_from_attrs(ttag, merged)

                raw = re.sub(r"<[^>]+>", "", tm.group(2))
                txt = html.unescape(raw).replace("\n", " ").strip()
                if not txt:
                    continue

                x = _parse_num(get_attr(ttag, "x"), base_x)
                y_attr = get_attr(ttag, "y")
                dy_attr = get_attr(ttag, "dy")
                if y_attr:
                    cur_y = _parse_num(y_attr, cur_y)
                if dy_attr:
                    cur_y += _parse_num(dy_attr, 0.0)
                lines.append((txt, x, cur_y, tf))
        else:
            raw = re.sub(r"<[^>]+>", "", body)
            txt = html.unescape(raw).replace("\n", " ").strip()
            if txt:
                lines.append((txt, base_x, base_y, font))

        for txt, x, y, tf in lines:
            if anchor != "start":
                adv = QtGui.QFontMetricsF(tf).horizontalAdvance(txt)
                if anchor == "middle":
                    x = x - adv * 0.5
                elif anchor == "end":
                    x = x - adv

            qp = QtGui.QPainterPath()
            qp.addText(QtCore.QPointF(x, y), tf, txt)
            for poly in qp.toSubpathPolygons():
                n = poly.count()
                if n < 2:
                    continue
                pts = [(float(poly.at(i).x()), float(poly.at(i).y())) for i in range(n)]
                d = "M " + " L ".join(f"{px:.3f} {py:.3f}" for px, py in pts) + " Z"
                try:
                    out.append(parse_path(d))
                except Exception:
                    continue

    return out


def extract_transform(tag: str):
    transforms = []
    for kind, args in re.findall(
        r"(translate|scale|matrix)\s*\(([^)]*)\)", tag, re.IGNORECASE
    ):
        nums = [float(x) for x in re.split(r"[, \s]+", args.strip()) if x.strip()]
        if kind.lower() == "translate":
            tx = nums[0] if len(nums) >= 1 else 0.0
            ty = nums[1] if len(nums) >= 2 else 0.0
            transforms.append(("translate", (tx, ty)))
        elif kind.lower() == "scale":
            sx = nums[0] if len(nums) >= 1 else 1.0
            sy = nums[1] if len(nums) >= 2 else sx
            transforms.append(("scale", (sx, sy)))
        elif kind.lower() == "matrix" and len(nums) >= 6:
            a, b, c, d, e, f = nums[:6]
            transforms.append(("matrix", (a, b, c, d, e, f)))
    return transforms


def apply_simple_transforms(path_obj: Any, transforms) -> Any:
    p = path_obj
    for tr in transforms:
        kind = tr[0]
        if kind == "translate":
            a, b = tr[1]
            p = p.translated(a + b * 1j)
        elif kind == "scale":
            a, b = tr[1]
            p = p.scaled(a, b)
        elif kind == "matrix":
            a, b, c, d, e, f = tr[1]
            if abs(b) > 1e-9 or abs(c) > 1e-9:
                raise ValueError("Unsupported matrix transform with rotation/shear")
            p = p.scaled(a, d)
            p = p.translated(e + f * 1j)
    return p


def get_attr(tag: str, name: str) -> str:
    m = ATTR_RE(name).search(tag)
    return m.group(1) if m else ""


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _entry_from_path_el(path_el: ET.Element, transforms) -> Optional[Dict[str, Any]]:
    d = (path_el.attrib.get("d") or "").strip()
    if not d:
        return None

    fill = path_el.attrib.get("fill", "")
    frule = path_el.attrib.get("fill-rule", "")
    style = path_el.attrib.get("style", "")
    if style:
        st = parse_style(style)
        fill = st.get("fill", fill)
        frule = st.get("fill-rule", frule)

    return {
        "kind": "path",
        "data": d,
        "fill": fill,
        "fill_rule": (frule or "nonzero").lower(),
        "transforms": list(transforms),
    }


def _collect_paths_under(elem: ET.Element, inherited) -> List[Dict[str, Any]]:
    own = extract_transform(elem.attrib.get("transform", ""))
    cur = list(inherited) + own
    out: List[Dict[str, Any]] = []

    if _local_name(elem.tag) == "path":
        e = _entry_from_path_el(elem, cur)
        if e is not None:
            out.append(e)

    for ch in list(elem):
        out.extend(_collect_paths_under(ch, cur))
    return out


def extract_paths(svg_text: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(svg_text)
    except Exception:
        # Fallback for malformed SVGs.
        nodes: List[Dict[str, Any]] = []
        for tag in re.findall(r"<path[^>]*?>", svg_text, re.IGNORECASE | re.DOTALL):
            d = get_attr(tag, "d")
            if not d:
                continue
            fill = get_attr(tag, "fill")
            frule = get_attr(tag, "fill-rule")
            style = get_attr(tag, "style")
            if style:
                st = parse_style(style)
                fill = st.get("fill", fill)
                frule = st.get("fill-rule", frule)
            transforms = extract_transform(tag)
            nodes.append(
                {
                    "kind": "path",
                    "data": d,
                    "fill": fill,
                    "fill_rule": (frule or "nonzero").lower(),
                    "transforms": transforms,
                }
            )
        return nodes

    ref_map: Dict[str, List[Dict[str, Any]]] = {}

    def walk_collect_defs(elem: ET.Element, inherited, in_defs: bool) -> None:
        own = extract_transform(elem.attrib.get("transform", ""))
        cur = list(inherited) + own
        local = _local_name(elem.tag)
        now_defs = in_defs or (local == "defs")

        if now_defs:
            elem_id = elem.attrib.get("id", "")
            if elem_id:
                ref_map[elem_id] = _collect_paths_under(elem, [])

        for ch in list(elem):
            walk_collect_defs(ch, cur, now_defs)

    walk_collect_defs(root, [], False)

    nodes: List[Dict[str, Any]] = []

    def walk_nodes(elem: ET.Element, inherited, in_defs: bool) -> None:
        own = extract_transform(elem.attrib.get("transform", ""))
        cur = list(inherited) + own
        local = _local_name(elem.tag)
        now_defs = in_defs or (local == "defs")

        if not now_defs and local == "path":
            e = _entry_from_path_el(elem, cur)
            if e is not None:
                nodes.append(e)

        if not now_defs and local == "use":
            href = (
                elem.attrib.get("{http://www.w3.org/1999/xlink}href")
                or elem.attrib.get("href")
                or ""
            )
            if href.startswith("#"):
                ref_id = href[1:]
                refs = ref_map.get(ref_id, [])
                tx = _parse_num(elem.attrib.get("x", "0"), 0.0)
                ty = _parse_num(elem.attrib.get("y", "0"), 0.0)
                inst = list(cur)
                if abs(tx) > 1e-9 or abs(ty) > 1e-9:
                    inst.append(("translate", (tx, ty)))

                for r in refs:
                    rr = dict(r)
                    rr["transforms"] = list(r["transforms"]) + inst
                    nodes.append(rr)

        for ch in list(elem):
            walk_nodes(ch, cur, now_defs)

    walk_nodes(root, [], False)
    return nodes


def split_strokes(path_obj: Any, eps: float = 1e-9):
    strokes = []
    current = []
    prev_end = None
    for seg in path_obj:
        if prev_end is None or abs(seg.start - prev_end) > eps:
            if current:
                strokes.append(current)
            current = [seg]
        else:
            current.append(seg)
        prev_end = seg.end
    if current:
        strokes.append(current)
    return strokes


def combined_bbox(paths: List[Any]) -> Tuple[float, float, float, float]:
    xs, ys = [], []
    for p in paths:
        for seg in p:
            xmin, xmax, ymin, ymax = seg.bbox()
            xs.extend([xmin, xmax])
            ys.extend([ymin, ymax])
    if not xs or not ys:
        return (0.0, 0.0, 1.0, 1.0)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    return xmin, ymin, (xmax - xmin), (ymax - ymin)


def stroke_polyline_from_segs(
    segs, device_scale: float, segpx: float
) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    for seg in segs:
        length_svg = seg.length(error=1e-4)
        curvature = getattr(seg, "control1", None)
        base_n = int(max(ceil(length_svg * device_scale / segpx), 1))
        if curvature is not None:
            base_n = int(base_n * 1.8)
        for i in range(base_n + 1):
            t = i / base_n
            z = seg.point(t)
            pts.append((z.real, z.imag))
    smooth = []
    for i in range(len(pts)):
        if i == 0 or i == len(pts) - 1:
            smooth.append(pts[i])
        else:
            x = (pts[i - 1][0] + 2 * pts[i][0] + pts[i + 1][0]) / 4
            y = (pts[i - 1][1] + 2 * pts[i][1] + pts[i + 1][1]) / 4
            smooth.append((x, y))
    return smooth


def load_svg_track(path: str) -> GestureTrack:
    if parse_path is None or SvgPath is None:
        raise RuntimeError("Missing dependency: install svgpathtools in .venv")

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    svg_text = p.read_text(encoding="utf-8", errors="ignore")
    nodes = extract_paths(svg_text)

    path_objs: List[Any] = []
    for n in nodes:
        try:
            po = parse_path(n["data"])
            if n["transforms"]:
                po = apply_simple_transforms(po, n["transforms"])
            path_objs.append(po)
        except Exception:
            continue

    text_paths = extract_text_paths(svg_text)
    if text_paths:
        path_objs.extend(text_paths)

    if not path_objs:
        raise ValueError("No drawable paths/text found in SVG")

    return GestureTrack(
        path=str(p),
        kind="svg",
        strokes=[],
        svg_paths=path_objs,
        default_duration_ms=STROKE_DURATION_MS,
    )


def load_track(path: str) -> GestureTrack:
    suffix = Path(path).suffix.lower()
    if suffix == ".svg":
        return load_svg_track(path)
    if suffix == ".csv":
        return load_csv_track(path)
    raise ValueError("Unsupported file type. Use .svg or .csv")


class ScreenStreamThread(QtCore.QThread):
    frame_ready = QtCore.Signal(QtGui.QImage)
    stream_error = QtCore.Signal(str)

    def __init__(self, fps: float = 4.0, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self._running = True
        self.fps = max(0.5, fps)

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        frame_interval = 1.0 / self.fps
        while self._running:
            t0 = time.time()
            rc, data, err = adb_bytes(["exec-out", "screencap", "-p"])
            if rc != 0:
                self.stream_error.emit(err or "adb screencap failed")
                time.sleep(0.5)
                continue

            img = QtGui.QImage.fromData(data, "PNG")
            if img.isNull():
                img = QtGui.QImage.fromData(data.replace(b"\r\n", b"\n"), "PNG")
            if img.isNull():
                self.stream_error.emit("Could not decode frame PNG")
                time.sleep(0.2)
                continue

            self.frame_ready.emit(img)

            elapsed = time.time() - t0
            remain = frame_interval - elapsed
            if remain > 0:
                time.sleep(remain)


class TouchPlaybackThread(QtCore.QThread):
    status = QtCore.Signal(str)
    done = QtCore.Signal()

    def __init__(
        self,
        device_strokes: List[List[Tuple[int, int]]],
        default_duration_ms: int,
        touch_backend: Optional[TouchBackend] = None,
        durations_ms: Optional[List[int]] = None,
        parent: Optional[QtCore.QObject] = None,
    ):
        super().__init__(parent)
        self._running = True
        self.strokes = device_strokes
        self.default_duration_ms = max(8, int(default_duration_ms))
        self.touch_backend = touch_backend or TouchBackend(mode="swipe")
        self.durations_ms = durations_ms

    def stop(self) -> None:
        self._running = False

    def _to_raw(self, x: int, y: int) -> Tuple[int, int]:
        b = self.touch_backend
        rx = int(
            round(max(0, min(b.screen_w - 1, x)) * b.max_x / max(1, b.screen_w - 1))
        )
        ry = int(
            round(max(0, min(b.screen_h - 1, y)) * b.max_y / max(1, b.screen_h - 1))
        )
        return rx, ry

    def _play_stroke_sendevent(
        self,
        stroke: List[Tuple[int, int]],
        per_seg_ms: Optional[List[int]] = None,
    ) -> Tuple[int, str]:
        b = self.touch_backend
        if b.mode != "sendevent" or not b.event_path:
            return 1, "invalid sendevent backend"

        dev = b.event_path
        tid = int(time.time() * 1000) % 65535
        x0, y0 = self._to_raw(stroke[0][0], stroke[0][1])

        cmds: List[str] = [
            f"sendevent {dev} 3 47 0",
            f"sendevent {dev} 3 57 {tid}",
            f"sendevent {dev} 3 53 {x0}",
            f"sendevent {dev} 3 54 {y0}",
            f"sendevent {dev} 1 330 1",
            f"sendevent {dev} 0 0 0",
        ]

        for i, (x, y) in enumerate(stroke[1:]):
            rx, ry = self._to_raw(x, y)
            cmds.append(f"sendevent {dev} 3 47 0")
            cmds.append(f"sendevent {dev} 3 53 {rx}")
            cmds.append(f"sendevent {dev} 3 54 {ry}")
            cmds.append(f"sendevent {dev} 0 0 0")
            if per_seg_ms is not None:
                d = per_seg_ms[i] if i < len(per_seg_ms) else self.default_duration_ms
            else:
                d = self.default_duration_ms
            d = max(0, int(d))
            if d > 0:
                cmds.append(f"sleep {d / 1000.0:.3f}")

        cmds.extend(
            [
                f"sendevent {dev} 3 47 0",
                f"sendevent {dev} 3 57 -1",
                f"sendevent {dev} 1 330 0",
                f"sendevent {dev} 0 0 0",
            ]
        )

        rc, _, err = adb(["shell", "sh", "-c", "; ".join(cmds)])
        return rc, err

    def _play_stroke_motionevent(
        self,
        stroke: List[Tuple[int, int]],
        per_seg_ms: Optional[List[int]] = None,
    ) -> Tuple[int, str]:
        if len(stroke) < 2:
            return 0, ""

        x0, y0 = stroke[0]
        rc, _, err = adb(["shell", "input", "motionevent", "DOWN", str(x0), str(y0)])
        if rc != 0:
            return rc, err

        for i, (x, y) in enumerate(stroke[1:]):
            rc, _, err = adb(["shell", "input", "motionevent", "MOVE", str(x), str(y)])
            if rc != 0:
                adb(["shell", "input", "motionevent", "UP", str(x), str(y)])
                return rc, err
            if per_seg_ms is not None:
                d = per_seg_ms[i] if i < len(per_seg_ms) else self.default_duration_ms
            else:
                d = max(0, min(10, int(self.default_duration_ms // 4)))
            if d > 0:
                time.sleep(d / 1000.0)

        x1, y1 = stroke[-1]
        rc, _, err = adb(["shell", "input", "motionevent", "UP", str(x1), str(y1)])
        return rc, err

    def run(self) -> None:
        total_points = sum(len(s) for s in self.strokes)
        if total_points < 2:
            self.done.emit()
            return

        backend_name = self.touch_backend.mode
        self.status.emit(
            f"Playing {len(self.strokes)} stroke(s), {total_points} points [{backend_name}]"
        )
        for stroke in self.strokes:
            if len(stroke) < 2:
                continue
            if not self._running:
                self.status.emit("Playback stopped")
                self.done.emit()
                return

            if self.touch_backend.mode == "sendevent":
                per_seg = self.durations_ms if len(self.strokes) == 1 else None
                rc, err = self._play_stroke_sendevent(stroke, per_seg_ms=per_seg)
                if rc != 0:
                    self.status.emit(f"ADB sendevent error: {err}")
                    self.done.emit()
                    return
                continue

            if self.touch_backend.mode == "motionevent":
                per_seg = self.durations_ms if len(self.strokes) == 1 else None
                rc, err = self._play_stroke_motionevent(stroke, per_seg_ms=per_seg)
                if rc != 0:
                    self.status.emit(f"ADB motionevent error: {err}")
                    self.done.emit()
                    return
                continue

            for i in range(len(stroke) - 1):
                if not self._running:
                    self.status.emit("Playback stopped")
                    self.done.emit()
                    return

                x1, y1 = stroke[i]
                x2, y2 = stroke[i + 1]
                if self.durations_ms is not None and len(self.strokes) == 1:
                    dur = (
                        self.durations_ms[i]
                        if i < len(self.durations_ms)
                        else self.default_duration_ms
                    )
                else:
                    dur = self.default_duration_ms
                dur = int(max(8, min(800, dur)))

                rc, _, err = adb(
                    [
                        "shell",
                        "input",
                        "swipe",
                        str(x1),
                        str(y1),
                        str(x2),
                        str(y2),
                        str(dur),
                    ]
                )
                if rc != 0:
                    self.status.emit(f"ADB input error: {err}")
                    self.done.emit()
                    return

        self.status.emit("Playback finished")
        self.done.emit()


class CsvListWidget(QtWidgets.QListWidget):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDefaultDropAction(QtCore.Qt.CopyAction)

    def startDrag(self, supported_actions: QtCore.Qt.DropActions) -> None:
        item = self.currentItem()
        if not item:
            return

        path = item.data(QtCore.Qt.UserRole)
        if not path:
            return

        mime = QtCore.QMimeData()
        mime.setText(path)

        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        drag.exec(QtCore.Qt.CopyAction)


class ScreenView(QtWidgets.QWidget):
    crop_changed = QtCore.Signal(QtCore.QRectF)
    track_dropped = QtCore.Signal(str)
    track_hovered = QtCore.Signal(str)
    track_hover_moved = QtCore.Signal(str, float, float)
    track_hover_left = QtCore.Signal()
    draw_anchor_moved = QtCore.Signal(float, float)
    draw_anchor_clicked = QtCore.Signal(float, float)
    draw_manual_stroke = QtCore.Signal(object)
    draw_anchor_left = QtCore.Signal()

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.frame: Optional[QtGui.QImage] = None

        self.crop_rect_dev: Optional[QtCore.QRectF] = None
        self._selection_start: Optional[QtCore.QPointF] = None
        self._selection_end: Optional[QtCore.QPointF] = None
        self._preview_device_strokes: List[List[Tuple[int, int]]] = []
        self._overlay_image: Optional[QtGui.QImage] = None
        self._overlay_center_dev: Optional[QtCore.QPointF] = None
        self._overlay_opacity = 0.42
        self._last_mouse_dev: Optional[QtCore.QPointF] = None
        self._interaction_mode = "crop"
        self._draw_press_dev: Optional[QtCore.QPointF] = None
        self._draw_is_dragging = False
        self._draw_points_dev: List[Tuple[float, float]] = []

    def set_frame(self, frame: QtGui.QImage) -> None:
        self.frame = frame
        self.update()

    def reset_crop(self) -> None:
        self.crop_rect_dev = None
        self.crop_changed.emit(self.current_source_rect())
        self.update()

    def set_preview_device_strokes(
        self, device_strokes: Optional[List[List[Tuple[int, int]]]]
    ) -> None:
        self._preview_device_strokes = device_strokes or []
        self.update()

    def set_overlay_image(
        self,
        image: Optional[QtGui.QImage],
        center_dev: Optional[Tuple[float, float]] = None,
    ) -> None:
        if image is None or image.isNull():
            self._overlay_image = None
            self._overlay_center_dev = None
        else:
            self._overlay_image = image.copy()
            if center_dev is not None:
                self._overlay_center_dev = QtCore.QPointF(
                    float(center_dev[0]), float(center_dev[1])
                )
            elif self._overlay_center_dev is None:
                src = self.current_source_rect()
                self._overlay_center_dev = QtCore.QPointF(src.center())
        self.update()

    def clear_overlay_image(self) -> None:
        self._overlay_image = None
        self._overlay_center_dev = None
        self.update()

    def current_mouse_device(self) -> Optional[Tuple[float, float]]:
        if self._last_mouse_dev is None:
            return None
        return float(self._last_mouse_dev.x()), float(self._last_mouse_dev.y())

    def set_interaction_mode(self, mode: str) -> None:
        self._interaction_mode = mode if mode in ("crop", "draw") else "crop"
        if self._interaction_mode == "draw":
            self.setCursor(QtCore.Qt.CrossCursor)
        else:
            self.unsetCursor()
        self._selection_start = None
        self._selection_end = None
        self._draw_press_dev = None
        self._draw_is_dragging = False
        self._draw_points_dev = []
        self.update()

    @staticmethod
    def _drop_path_from_mime(mime: QtCore.QMimeData) -> str:
        if mime.hasText():
            txt = mime.text().strip()
            if txt:
                if txt.startswith("file://"):
                    url = QtCore.QUrl(txt)
                    if url.isLocalFile():
                        return url.toLocalFile()
                return txt
        if mime.hasUrls() and mime.urls():
            url = mime.urls()[0]
            if url.isLocalFile():
                return url.toLocalFile()
        return ""

    def current_source_rect(self) -> QtCore.QRectF:
        if self.frame is None:
            return QtCore.QRectF(0, 0, 1, 1)
        if self.crop_rect_dev is not None:
            return self.crop_rect_dev
        return QtCore.QRectF(
            0, 0, float(self.frame.width()), float(self.frame.height())
        )

    def _target_rect(self) -> QtCore.QRectF:
        src = self.current_source_rect()
        ww = float(self.width())
        wh = float(self.height())
        if ww <= 1 or wh <= 1 or src.width() <= 1e-9 or src.height() <= 1e-9:
            return QtCore.QRectF(0, 0, ww, wh)

        src_ratio = src.width() / src.height()
        widget_ratio = ww / wh

        if src_ratio > widget_ratio:
            tw = ww
            th = ww / src_ratio
            tx = 0.0
            ty = (wh - th) * 0.5
        else:
            th = wh
            tw = wh * src_ratio
            tx = (ww - tw) * 0.5
            ty = 0.0
        return QtCore.QRectF(tx, ty, tw, th)

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    def _widget_to_device(self, p: QtCore.QPointF) -> QtCore.QPointF:
        src = self.current_source_rect()
        tgt = self._target_rect()
        u = (p.x() - tgt.left()) / max(tgt.width(), 1e-9)
        v = (p.y() - tgt.top()) / max(tgt.height(), 1e-9)
        u = self._clamp(u, 0.0, 1.0)
        v = self._clamp(v, 0.0, 1.0)

        x = src.left() + u * src.width()
        y = src.top() + v * src.height()
        return QtCore.QPointF(x, y)

    def _device_to_widget(self, p: QtCore.QPointF) -> QtCore.QPointF:
        src = self.current_source_rect()
        tgt = self._target_rect()
        u = (p.x() - src.left()) / max(src.width(), 1e-9)
        v = (p.y() - src.top()) / max(src.height(), 1e-9)
        x = tgt.left() + u * tgt.width()
        y = tgt.top() + v * tgt.height()
        return QtCore.QPointF(x, y)

    def _selection_rect_widget(self) -> Optional[QtCore.QRectF]:
        if self._selection_start is None or self._selection_end is None:
            return None
        return QtCore.QRectF(self._selection_start, self._selection_end).normalized()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(12, 12, 12))

        if self.frame is not None and not self.frame.isNull():
            src = self.current_source_rect()
            tgt = self._target_rect()
            painter.drawImage(tgt, self.frame, src)

        if (
            self._overlay_image is not None
            and not self._overlay_image.isNull()
            and self._overlay_center_dev is not None
        ):
            src = self.current_source_rect()
            tgt = self._target_rect()
            sx = tgt.width() / max(1e-9, src.width())
            sy = tgt.height() / max(1e-9, src.height())

            iw = float(self._overlay_image.width())
            ih = float(self._overlay_image.height())
            left_dev = self._overlay_center_dev.x() - iw * 0.5
            top_dev = self._overlay_center_dev.y() - ih * 0.5

            wr = QtCore.QRectF(
                tgt.left() + (left_dev - src.left()) * sx,
                tgt.top() + (top_dev - src.top()) * sy,
                iw * sx,
                ih * sy,
            )

            painter.save()
            painter.setClipRect(tgt)
            painter.setOpacity(self._overlay_opacity)
            painter.drawImage(wr, self._overlay_image)
            painter.setOpacity(1.0)
            painter.setPen(QtGui.QPen(QtGui.QColor(240, 240, 240, 130), 1))
            painter.drawRect(wr)
            painter.restore()

        sel = self._selection_rect_widget()
        if sel is not None:
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 170, 40), 2))
            painter.setBrush(QtGui.QColor(255, 170, 40, 50))
            painter.drawRect(sel)

        if self._preview_device_strokes:
            tgt = self._target_rect()

            painter.save()
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            painter.setClipRect(tgt)

            shadow_pen = QtGui.QPen(QtGui.QColor(0, 0, 0, 140), 6)
            shadow_pen.setCapStyle(QtCore.Qt.RoundCap)
            shadow_pen.setJoinStyle(QtCore.Qt.RoundJoin)
            main_pen = QtGui.QPen(QtGui.QColor(80, 255, 190, 220), 2)
            main_pen.setCapStyle(QtCore.Qt.RoundCap)
            main_pen.setJoinStyle(QtCore.Qt.RoundJoin)

            for stroke in self._preview_device_strokes:
                if len(stroke) < 2:
                    continue
                x0, y0 = stroke[0]
                p0 = self._device_to_widget(QtCore.QPointF(float(x0), float(y0)))

                path = QtGui.QPainterPath(p0)
                for dx, dy in stroke[1:]:
                    path.lineTo(
                        self._device_to_widget(QtCore.QPointF(float(dx), float(dy)))
                    )

                painter.setPen(shadow_pen)
                painter.drawPath(path)
                painter.setPen(main_pen)
                painter.drawPath(path)

            painter.restore()

        painter.setPen(QtGui.QPen(QtGui.QColor(80, 200, 255), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._last_mouse_dev = self._widget_to_device(QtCore.QPointF(event.position()))
        if self._interaction_mode == "draw":
            if event.button() == QtCore.Qt.LeftButton:
                dev = self._widget_to_device(QtCore.QPointF(event.position()))
                self._draw_press_dev = dev
                self._draw_is_dragging = False
                self._draw_points_dev = [(float(dev.x()), float(dev.y()))]
                self.draw_anchor_moved.emit(float(dev.x()), float(dev.y()))
                event.accept()
                return
            super().mousePressEvent(event)
            return

        if event.button() == QtCore.Qt.LeftButton:
            self._selection_start = QtCore.QPointF(event.position())
            self._selection_end = QtCore.QPointF(event.position())
            self.update()
        elif event.button() == QtCore.Qt.RightButton:
            self.reset_crop()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        dev = self._widget_to_device(QtCore.QPointF(event.position()))
        self._last_mouse_dev = dev

        if self._interaction_mode == "draw":
            if (
                event.buttons() & QtCore.Qt.LeftButton
            ) and self._draw_press_dev is not None:
                self._draw_points_dev.append((float(dev.x()), float(dev.y())))
                if not self._draw_is_dragging:
                    if (
                        math.hypot(
                            dev.x() - self._draw_press_dev.x(),
                            dev.y() - self._draw_press_dev.y(),
                        )
                        >= 6.0
                    ):
                        self._draw_is_dragging = True
                if not self._draw_is_dragging:
                    self.draw_anchor_moved.emit(float(dev.x()), float(dev.y()))
            else:
                self.draw_anchor_moved.emit(float(dev.x()), float(dev.y()))
            return

        if self._selection_start is not None:
            self._selection_end = QtCore.QPointF(event.position())
            self.update()

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._interaction_mode == "draw":
            super().mouseDoubleClickEvent(event)
            return

        if event.button() == QtCore.Qt.LeftButton:
            self._selection_start = None
            self._selection_end = None
            self.reset_crop()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._interaction_mode == "draw":
            if (
                event.button() == QtCore.Qt.LeftButton
                and self._draw_press_dev is not None
            ):
                dev = self._widget_to_device(QtCore.QPointF(event.position()))
                self._draw_points_dev.append((float(dev.x()), float(dev.y())))

                if self._draw_is_dragging and len(self._draw_points_dev) >= 2:
                    self.draw_manual_stroke.emit(self._draw_points_dev.copy())
                else:
                    self.draw_anchor_clicked.emit(float(dev.x()), float(dev.y()))

                self._draw_press_dev = None
                self._draw_is_dragging = False
                self._draw_points_dev = []
            return

        if event.button() != QtCore.Qt.LeftButton:
            return
        if self._selection_start is None or self._selection_end is None:
            return

        rect_w = QtCore.QRectF(self._selection_start, self._selection_end).normalized()
        self._selection_start = None
        self._selection_end = None

        if rect_w.width() < 8 or rect_w.height() < 8:
            self.update()
            return

        top_left_dev = self._widget_to_device(rect_w.topLeft())
        bottom_right_dev = self._widget_to_device(rect_w.bottomRight())
        rect_dev = QtCore.QRectF(top_left_dev, bottom_right_dev).normalized()

        if self.frame is not None:
            fw, fh = float(self.frame.width()), float(self.frame.height())
            rect_dev = rect_dev.intersected(QtCore.QRectF(0, 0, fw, fh))

        if rect_dev.width() >= 2 and rect_dev.height() >= 2:
            self.crop_rect_dev = rect_dev
            self.crop_changed.emit(rect_dev)

        self.update()

    def leaveEvent(self, event: QtCore.QEvent) -> None:
        if self._interaction_mode == "draw":
            if self._draw_press_dev is not None and self._draw_is_dragging:
                if len(self._draw_points_dev) >= 2:
                    self.draw_manual_stroke.emit(self._draw_points_dev.copy())
                self._draw_press_dev = None
                self._draw_is_dragging = False
                self._draw_points_dev = []
            self.draw_anchor_left.emit()
        super().leaveEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        path = self._drop_path_from_mime(event.mimeData())
        low = path.lower()
        if low.endswith(".csv") or low.endswith(".svg"):
            self.track_hovered.emit(path)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        path = self._drop_path_from_mime(event.mimeData())
        low = path.lower()
        if low.endswith(".csv") or low.endswith(".svg"):
            self.track_hovered.emit(path)
            pos = event.position()
            dev = self._widget_to_device(QtCore.QPointF(pos.x(), pos.y()))
            self.track_hover_moved.emit(path, float(dev.x()), float(dev.y()))
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QtGui.QDragLeaveEvent) -> None:
        self.track_hover_left.emit()
        event.accept()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        path = self._drop_path_from_mime(event.mimeData())
        if path.lower().endswith(".csv") or path.lower().endswith(".svg"):
            self.track_dropped.emit(path)
            self.track_hover_left.emit()
            event.acceptProposedAction()
        else:
            self.track_hover_left.emit()
            event.ignore()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ADB Touch Studio")
        self.resize(1300, 820)

        self.device_size = get_device_size()
        self.touch_backend = detect_touch_backend(
            self.device_size[0], self.device_size[1]
        )

        self.stream_thread: Optional[ScreenStreamThread] = None
        self.play_thread: Optional[TouchPlaybackThread] = None

        self.track_cache = {}
        self.track_cache_mtime = {}
        self._hover_track_path: Optional[str] = None
        self._hover_anchor_dev: Optional[Tuple[float, float]] = None
        self._mode = "crop"

        self._build_ui()
        self._start_stream()

    def _make_mode_icon(self, mode: str) -> QtGui.QIcon:
        pm = QtGui.QPixmap(20, 20)
        pm.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        if mode == "draw":
            pen = QtGui.QPen(QtGui.QColor(40, 170, 240), 2.2)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            path = QtGui.QPainterPath(QtCore.QPointF(3, 14))
            path.cubicTo(
                QtCore.QPointF(7, 3), QtCore.QPointF(13, 17), QtCore.QPointF(17, 6)
            )
            painter.drawPath(path)
        else:
            pen = QtGui.QPen(QtGui.QColor(255, 170, 40), 2)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRect(4, 4, 12, 12)
            painter.drawLine(4, 8, 7, 8)
            painter.drawLine(4, 12, 7, 12)
            painter.drawLine(13, 8, 16, 8)
            painter.drawLine(13, 12, 16, 12)

        painter.end()
        return QtGui.QIcon(pm)

    def _selected_track_path(self) -> Optional[str]:
        item = self.list_csv.currentItem() if hasattr(self, "list_csv") else None
        if not item:
            return None
        path = item.data(QtCore.Qt.UserRole)
        return str(path) if path else None

    def _default_anchor_dev(self) -> Tuple[float, float]:
        crop = self._active_crop()
        return (crop.x() + crop.width() / 2.0, crop.y() + crop.height() / 2.0)

    def _set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("crop", "draw") else "crop"
        self.screen.set_interaction_mode(self._mode)

        if self._mode == "draw":
            if self._hover_anchor_dev is None:
                self._hover_anchor_dev = self._default_anchor_dev()
            sel = self._selected_track_path()
            if sel:
                self._hover_track_path = sel
                self._refresh_hover_preview()
            self.status.showMessage("Mode: draw", 1200)
        else:
            self._hover_track_path = None
            self._hover_anchor_dev = None
            self.screen.set_preview_device_strokes([])
            self.status.showMessage("Mode: crop", 1200)

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        lay = QtWidgets.QHBoxLayout(root)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        left = QtWidgets.QFrame()
        left.setFrameShape(QtWidgets.QFrame.StyledPanel)
        left_l = QtWidgets.QVBoxLayout(left)
        left_l.setContentsMargins(8, 8, 8, 8)
        left_l.setSpacing(8)
        left.setMinimumWidth(320)
        left.setMaximumWidth(420)

        title = QtWidgets.QLabel("SVG / CSV Tracks")
        f = title.font()
        f.setPointSize(max(10, f.pointSize() + 2))
        f.setBold(True)
        title.setFont(f)
        left_l.addWidget(title)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_load = QtWidgets.QPushButton("Load SVG/CSV")
        self.btn_remove = QtWidgets.QPushButton("Remove Selected")
        btn_row.addWidget(self.btn_load)
        btn_row.addWidget(self.btn_remove)
        left_l.addLayout(btn_row)

        self.list_csv = CsvListWidget()
        left_l.addWidget(self.list_csv, 1)

        self.lbl_info = QtWidgets.QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setText(
            "Use Crop mode to select allowed region (left-drag), double-click/right-click resets crop.\n"
            "Use Draw mode: move cursor to place selected SVG/CSV and left-click to stamp, "
            "or left-drag to freehand draw manually. Ctrl+V pastes a PNG overlay guide on canvas."
        )
        left_l.addWidget(self.lbl_info)

        self.btn_stop = QtWidgets.QPushButton("Stop Playback")
        left_l.addWidget(self.btn_stop)

        right = QtWidgets.QFrame()
        right.setFrameShape(QtWidgets.QFrame.StyledPanel)
        right_l = QtWidgets.QVBoxLayout(right)
        right_l.setContentsMargins(8, 8, 8, 8)
        right_l.setSpacing(8)

        self.lbl_device = QtWidgets.QLabel(
            f"Device: {self.device_size[0]} x {self.device_size[1]}"
        )
        self.lbl_backend = QtWidgets.QLabel(f"Touch backend: {self.touch_backend.mode}")
        self.lbl_crop = QtWidgets.QLabel("Crop: full screen")
        right_l.addWidget(self.lbl_device)
        right_l.addWidget(self.lbl_backend)
        right_l.addWidget(self.lbl_crop)

        mode_row = QtWidgets.QHBoxLayout()
        mode_row.addWidget(QtWidgets.QLabel("Mode:"))
        self.btn_mode_draw = QtWidgets.QToolButton()
        self.btn_mode_draw.setCheckable(True)
        self.btn_mode_draw.setIcon(self._make_mode_icon("draw"))
        self.btn_mode_draw.setText("Draw")
        self.btn_mode_draw.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.btn_mode_draw.setAutoRaise(False)

        self.btn_mode_crop = QtWidgets.QToolButton()
        self.btn_mode_crop.setCheckable(True)
        self.btn_mode_crop.setIcon(self._make_mode_icon("crop"))
        self.btn_mode_crop.setText("Crop")
        self.btn_mode_crop.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.btn_mode_crop.setAutoRaise(False)
        self.btn_mode_crop.setChecked(True)

        mode_group = QtWidgets.QButtonGroup(self)
        mode_group.setExclusive(True)
        mode_group.addButton(self.btn_mode_draw)
        mode_group.addButton(self.btn_mode_crop)

        mode_row.addWidget(self.btn_mode_draw)
        mode_row.addWidget(self.btn_mode_crop)
        mode_row.addStretch(1)
        right_l.addLayout(mode_row)

        self.screen = ScreenView()
        self.screen.setMinimumSize(760, 600)
        right_l.addWidget(self.screen, 1)

        lay.addWidget(left)
        lay.addWidget(right, 1)

        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready")

        self.btn_load.clicked.connect(self._on_load_tracks)
        self.btn_remove.clicked.connect(self._on_remove_selected)
        self.btn_stop.clicked.connect(self._on_stop_playback)
        self.screen.crop_changed.connect(self._on_crop_changed)
        self.screen.track_dropped.connect(self._on_track_dropped)
        self.screen.track_hovered.connect(self._on_track_hovered)
        self.screen.track_hover_moved.connect(self._on_track_hover_moved)
        self.screen.track_hover_left.connect(self._on_track_hover_left)
        self.screen.draw_anchor_moved.connect(self._on_draw_anchor_moved)
        self.screen.draw_anchor_clicked.connect(self._on_draw_anchor_clicked)
        self.screen.draw_manual_stroke.connect(self._on_draw_manual_stroke)
        self.screen.draw_anchor_left.connect(self._on_draw_anchor_left)

        self.list_csv.currentItemChanged.connect(self._on_selected_track_changed)
        self.btn_mode_draw.clicked.connect(lambda: self._set_mode("draw"))
        self.btn_mode_crop.clicked.connect(lambda: self._set_mode("crop"))

        self.shortcut_paste = QtGui.QShortcut(QtGui.QKeySequence.Paste, self)
        self.shortcut_paste.activated.connect(self._on_paste_overlay)

        self._set_mode("crop")

    def _start_stream(self) -> None:
        self.stream_thread = ScreenStreamThread(fps=4.0, parent=self)
        self.stream_thread.frame_ready.connect(self.screen.set_frame)
        self.stream_thread.stream_error.connect(self._on_stream_error)
        self.stream_thread.start()

    def _on_stream_error(self, msg: str) -> None:
        self.status.showMessage(msg, 3000)

    def _on_crop_changed(self, rect: QtCore.QRectF) -> None:
        if self.screen.crop_rect_dev is None:
            self.lbl_crop.setText("Crop: full screen")
        else:
            self.lbl_crop.setText(
                f"Crop: x={int(rect.x())}, y={int(rect.y())}, w={int(rect.width())}, h={int(rect.height())}"
            )

        self._refresh_hover_preview()

    def _refresh_hover_preview(self) -> None:
        if not self._hover_track_path:
            self.screen.set_preview_device_strokes([])
            return

        try:
            track = self._get_or_load_track(self._hover_track_path)
            self.screen.set_preview_device_strokes(
                self._map_track_to_device(track, anchor_dev=self._hover_anchor_dev)
            )
        except Exception:
            self.screen.set_preview_device_strokes([])

    def _get_or_load_track(self, path: str) -> GestureTrack:
        p = Path(path)
        mtime = p.stat().st_mtime if p.exists() else None

        track = self.track_cache.get(path)
        cached_mtime = self.track_cache_mtime.get(path)
        if track is not None and mtime is not None and cached_mtime == mtime:
            return track

        track = load_track(path)
        self.track_cache[path] = track
        if mtime is not None:
            self.track_cache_mtime[path] = mtime
        return track

    def _on_track_hovered(self, path: str) -> None:
        if self._hover_track_path != path:
            self._hover_anchor_dev = None
        self._hover_track_path = path
        self._refresh_hover_preview()

    def _on_track_hover_moved(self, path: str, dev_x: float, dev_y: float) -> None:
        self._hover_track_path = path
        self._hover_anchor_dev = (dev_x, dev_y)
        self._refresh_hover_preview()

    def _on_track_hover_left(self) -> None:
        self._hover_track_path = None
        self._hover_anchor_dev = None
        self.screen.set_preview_device_strokes([])

    def _on_selected_track_changed(self, current, previous) -> None:
        if self._mode != "draw":
            return
        path = self._selected_track_path()
        self._hover_track_path = path
        if path:
            if self._hover_anchor_dev is None:
                self._hover_anchor_dev = self._default_anchor_dev()
            self._refresh_hover_preview()
        else:
            self.screen.set_preview_device_strokes([])

    def _on_draw_anchor_moved(self, dev_x: float, dev_y: float) -> None:
        if self._mode != "draw":
            return
        path = self._selected_track_path()
        if not path:
            return
        self._hover_track_path = path
        self._hover_anchor_dev = (dev_x, dev_y)
        self._refresh_hover_preview()

    def _on_draw_anchor_clicked(self, dev_x: float, dev_y: float) -> None:
        if self._mode != "draw":
            return
        path = self._selected_track_path()
        if not path:
            self.status.showMessage("Select an SVG/CSV first", 1500)
            return
        self._hover_track_path = path
        self._hover_anchor_dev = (dev_x, dev_y)
        self._on_track_dropped(path)

    def _on_draw_manual_stroke(self, points_obj: object) -> None:
        if self._mode != "draw":
            return
        if not isinstance(points_obj, list):
            return

        raw: List[Tuple[int, int]] = []
        for it in points_obj:
            if not isinstance(it, (tuple, list)) or len(it) != 2:
                continue
            raw.append((int(round(float(it[0]))), int(round(float(it[1])))))

        if len(raw) < 2:
            return

        pts = [raw[0]]
        for x, y in raw[1:]:
            if math.hypot(x - pts[-1][0], y - pts[-1][1]) >= 2.0:
                pts.append((x, y))

        if len(pts) < 2:
            return

        chunks: List[List[Tuple[int, int]]] = []
        cur: List[Tuple[int, int]] = [pts[0]]
        for p in pts[1:]:
            d = math.hypot(p[0] - cur[-1][0], p[1] - cur[-1][1])
            if d > 140.0:
                if len(cur) >= 2:
                    chunks.append(cur)
                cur = [p]
                continue
            cur.append(p)
        if len(cur) >= 2:
            chunks.append(cur)

        if not chunks:
            return

        def densify(
            stroke: List[Tuple[int, int]], step_px: float = 8.0
        ) -> List[Tuple[int, int]]:
            out: List[Tuple[int, int]] = [stroke[0]]
            for (x0, y0), (x1, y1) in zip(stroke, stroke[1:]):
                dist = math.hypot(x1 - x0, y1 - y0)
                n = max(1, int(dist / max(1e-6, step_px)))
                for i in range(1, n + 1):
                    t = i / n
                    xi = int(round(x0 + (x1 - x0) * t))
                    yi = int(round(y0 + (y1 - y0) * t))
                    if xi != out[-1][0] or yi != out[-1][1]:
                        out.append((xi, yi))
            return out

        chunks = [densify(c) for c in chunks if len(c) >= 2]
        chunks = [c for c in chunks if len(c) >= 2]

        if not chunks:
            return

        if self.play_thread and self.play_thread.isRunning():
            self.play_thread.stop()
            self.play_thread.wait(1500)

        self.play_thread = TouchPlaybackThread(
            chunks,
            10,
            touch_backend=self.touch_backend,
            parent=self,
        )
        self.play_thread.status.connect(lambda s: self.status.showMessage(s, 3000))
        self.play_thread.done.connect(lambda: self.status.showMessage("Idle", 1200))
        self.play_thread.start()
        self.status.showMessage(f"Manual stroke: {len(pts)} points", 1200)

    def _on_draw_anchor_left(self) -> None:
        if self._mode == "draw":
            self.screen.set_preview_device_strokes([])

    def _overlay_anchor_dev(self) -> Tuple[float, float]:
        mouse = self.screen.current_mouse_device()
        if mouse is not None:
            return mouse
        if self._hover_anchor_dev is not None:
            return self._hover_anchor_dev
        return self._default_anchor_dev()

    def _clipboard_image(self) -> Optional[QtGui.QImage]:
        cb = QtGui.QGuiApplication.clipboard()
        if cb is None:
            return None

        img = cb.image()
        if img is not None and not img.isNull():
            return img

        pm = cb.pixmap()
        if pm is not None and not pm.isNull():
            return pm.toImage()

        md = cb.mimeData()
        if md is not None and md.hasUrls():
            for u in md.urls():
                if not u.isLocalFile():
                    continue
                fp = u.toLocalFile()
                low = fp.lower()
                if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
                    qimg = QtGui.QImage(fp)
                    if not qimg.isNull():
                        return qimg
        return None

    def _on_paste_overlay(self) -> None:
        img = self._clipboard_image()
        if img is None or img.isNull():
            self.status.showMessage("Clipboard has no image/PNG", 2000)
            return

        anchor = self._overlay_anchor_dev()
        self.screen.set_overlay_image(img, center_dev=anchor)
        self.status.showMessage(
            f"Overlay pasted: {img.width()}x{img.height()} at ({int(anchor[0])},{int(anchor[1])})",
            2600,
        )

    def _on_load_tracks(self) -> None:
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "Load SVG/CSV tracks",
            str(Path.home()),
            "Vector/CSV Files (*.svg *.csv)",
        )
        if not files:
            return

        added = 0
        for fp in files:
            if fp in self.track_cache:
                continue
            try:
                track = self._get_or_load_track(fp)
            except Exception as e:
                self.status.showMessage(f"Failed to load {Path(fp).name}: {e}", 6000)
                continue

            item = QtWidgets.QListWidgetItem(Path(fp).name)
            item.setToolTip(fp)
            item.setData(QtCore.Qt.UserRole, fp)
            self.list_csv.addItem(item)
            added += 1

        self.status.showMessage(f"Loaded {added} file(s)", 3000)

    def _on_remove_selected(self) -> None:
        row = self.list_csv.currentRow()
        if row < 0:
            return
        item = self.list_csv.takeItem(row)
        path = item.data(QtCore.Qt.UserRole)
        if path in self.track_cache:
            del self.track_cache[path]
        if path in self.track_cache_mtime:
            del self.track_cache_mtime[path]
        self.status.showMessage("Removed item", 2000)

    def _on_stop_playback(self) -> None:
        if self.play_thread and self.play_thread.isRunning():
            self.play_thread.stop()
            self.play_thread.wait(1500)
            self.status.showMessage("Playback stopped", 2000)

    def _active_crop(self) -> QtCore.QRectF:
        if self.screen.crop_rect_dev is not None:
            return self.screen.crop_rect_dev
        w, h = self.device_size
        return QtCore.QRectF(0, 0, float(w), float(h))

    def _map_track_to_device(
        self, track: GestureTrack, anchor_dev: Optional[Tuple[float, float]] = None
    ) -> List[List[Tuple[int, int]]]:
        crop = self._active_crop()

        if track.kind == "svg":
            if not track.svg_paths:
                return []

            path_objs = track.svg_paths
            xmin, ymin, bw, bh = combined_bbox(path_objs)
            cx, cy = xmin + bw / 2.0, ymin + bh / 2.0

            normalized = []
            for p in path_objs:
                q = p.translated(-(cx + cy * 1j))
                normalized.append(q)

            _, _, nbw, nbh = combined_bbox(normalized)
            fit_scale = (
                1.0
                if min(nbw, nbh) <= 1e-9
                else min(crop.width() / nbw, crop.height() / nbh)
            )
            device_scale = fit_scale * 1.0
            cx_dev = crop.x() + crop.width() / 2.0
            cy_dev = crop.y() + crop.height() / 2.0

            off_x = 0.0
            off_y = 0.0
            if anchor_dev is not None:
                off_x = float(anchor_dev[0]) - cx_dev
                off_y = float(anchor_dev[1]) - cy_dev

            out: List[List[Tuple[int, int]]] = []
            all_strokes = []
            for p in normalized:
                all_strokes.extend(split_strokes(p))

            for segs in all_strokes:
                poly = stroke_polyline_from_segs(segs, device_scale, SEGMENT_LEN_PIXELS)
                if not poly:
                    continue
                pts: List[Tuple[int, int]] = []
                for x, y in poly:
                    px = int(round(cx_dev + off_x + x * device_scale))
                    py = int(round(cy_dev + off_y + y * device_scale))
                    pts.append((px, py))
                if len(pts) >= 2:
                    out.append(pts)
            return out

        out: List[List[Tuple[int, int]]] = []
        for stroke in track.strokes:
            pts: List[Tuple[int, int]] = []
            for x, y in stroke:
                px = int(round(crop.left() + x * crop.width()))
                py = int(round(crop.top() + y * crop.height()))
                pts.append((px, py))
            if len(pts) >= 2:
                out.append(pts)
        return out

    def _on_track_dropped(self, path: str) -> None:
        try:
            track = self._get_or_load_track(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(
                self, "Load error", f"Failed to load file:\n{e}"
            )
            return

        dev_strokes = self._map_track_to_device(
            track, anchor_dev=self._hover_anchor_dev
        )
        if not dev_strokes:
            self.status.showMessage("Track has fewer than 2 points", 3000)
            return

        if self.play_thread and self.play_thread.isRunning():
            self.play_thread.stop()
            self.play_thread.wait(1500)

        self.play_thread = TouchPlaybackThread(
            dev_strokes,
            track.default_duration_ms,
            touch_backend=self.touch_backend,
            durations_ms=track.durations_ms,
            parent=self,
        )
        self.play_thread.status.connect(lambda s: self.status.showMessage(s, 3000))
        self.play_thread.done.connect(lambda: self.status.showMessage("Idle", 1500))
        self.play_thread.start()

        self.status.showMessage(f"Started playback: {Path(path).name}", 2500)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.play_thread and self.play_thread.isRunning():
            self.play_thread.stop()
            self.play_thread.wait(1200)

        if self.stream_thread and self.stream_thread.isRunning():
            self.stream_thread.stop()
            self.stream_thread.wait(1200)

        super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    try:
        win = MainWindow()
    except Exception as e:
        QtWidgets.QMessageBox.critical(None, "Startup error", str(e))
        return 1

    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
