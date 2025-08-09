#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SVG → config.json 変換（色→クラス自動判定 / no-go 改良 / 引数なしでOK / 出力寸法は常に10倍）

使い方（引数なしでOK）:
    python svg2config.py
  - 入力SVG: hall.svg があればそれ。なければカレントの *.svg を自動検出
  - 出力JSON: config.json
  - 壁帯: 500mm, 通路: 1000mm
  - color_map.json があれば自動適用
  - 出力される config.json の寸法は **常に 10 倍** に拡大されます（固定）

必要なら明示指定も可:
    python svg2config.py in.svg out.json --wall-band 500 --aisle 1000 --color-map color_map.json
"""

import json
import re
import sys
import glob
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from typing import Optional

EPS_ALIGN = 0.5  # mm 許容（水平・垂直の判定用）

def _css_to_dict(style_str: Optional[str]):
    d = {}
    if not style_str:
        return d
    for kv in style_str.split(";"):
        if ":" in kv:
            k, v = kv.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d

def _norm_hex(c: Optional[str]) -> Optional[str]:
    if not c:
        return None
    c = c.strip().lower()
    import re
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", c)
    if m:
        r, g, b = [max(0, min(255, int(x))) for x in m.groups()]
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.match(r"^#[0-9a-f]{3}$", c):
        return "#" + "".join(ch*2 for ch in c[1:])
    if re.match(r"^#[0-9a-f]{6}$", c):
        return c
    return c

def _effective_stroke(elem, parent_map: Optional[dict] = None) -> Optional[str]:
    """
    stroke を 属性→style→親へ遡って探索し、#rrggbb に正規化して返す。
    parent_map が None の場合は親遡りを行わない（後方互換）。
    """
    e = elem
    while e is not None:
        st = e.get("stroke")
        if st and st != "none":
            return _norm_hex(st)
        style = e.get("style")
        if style:
            d = _css_to_dict(style)
            st2 = d.get("stroke")
            if st2 and st2 != "none":
                return _norm_hex(st2)
        e = parent_map.get(e) if parent_map is not None else None
    return None

def _qname(root, local: str):
    """root の名前空間を考慮したタグ名を返す"""
    if "}" in root.tag:
        ns = root.tag.split("}")[0].strip("{")
        return f"{{{ns}}}{local}"
    return local

def _iter_elems(root, names=("line","path","polyline")):
    for n in names:
        yield from root.findall(f".//{_qname(root, n)}")


def _path_first_last_xy(d: str | None):
    """path の d から最初(M 近辺)と最後の点を抜く（直線想定の簡易版）"""
    if not d:
        return None
    nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", d)]
    if len(nums) < 4:
        return None
    x1, y1 = nums[0], nums[1]
    x2, y2 = nums[-2], nums[-1]
    return (x1, y1, x2, y2)

def _polyline_first_last_xy(points: str | None):
    if not points:
        return None
    toks = [t for t in re.split(r"[,\s]+", points.strip()) if t]
    try:
        vals = [float(t) for t in toks]
    except:
        return None
    if len(vals) < 4:
        return None
    x1, y1 = vals[0], vals[1]
    x2, y2 = vals[-2], vals[-1]
    return (x1, y1, x2, y2)

def _get_data_band_mm(elem, parent_map: Optional[dict] = None) -> float:
    """data-band-mm を 要素→親へ遡って探索。見つからなければ 1000."""
    e = elem
    while e is not None:
        v = e.get("data-band-mm")
        if v:
            try:
                return float(v)
            except:
                break
        e = parent_map.get(e) if parent_map is not None else None
    return 1000.0

def extract_curtain_rails(svg_root, color_map: dict, parent_map: dict):
    """
    レールを色で検出し、水平/垂直の line/path/polyline を抽出。
    返り値: [{"p1":[x,y], "p2":[x,y], "band_mm": float}, ...]（yは未反転）
    """
    # color_map から対象色を抽出（無ければ既定2色を拾う）
    target_hexes = set()
    for tag in ("line","path","polyline"):
        m = color_map.get(tag, {}).get("stroke", {})
        for k, v in m.items():
            if str(v).strip().lower() == "curtain-rail":
                target_hexes.add(_norm_hex(k))
    if not target_hexes:
        target_hexes = {"#0a7a0a", "#009944"}

    rails = []
    for el in _iter_elems(svg_root, names=("line","path","polyline")):
        st = _effective_stroke(el, parent_map)
        if st not in target_hexes:
            continue

        tag = el.tag.split("}")[-1].lower()
        xy = None
        if tag == "line":
            try:
                x1 = float(el.get("x1")); y1 = float(el.get("y1"))
                x2 = float(el.get("x2")); y2 = float(el.get("y2"))
                xy = (x1, y1, x2, y2)
            except:
                pass
        elif tag == "path":
            xy = _path_first_last_xy(el.get("d"))
        elif tag == "polyline":
            xy = _polyline_first_last_xy(el.get("points"))

        if not xy:
            continue

        x1, y1, x2, y2 = xy
        if abs(x1 - x2) <= EPS_ALIGN:
            # 垂直
            X = (x1 + x2) / 2.0
            y_min, y_max = sorted([y1, y2])
            band = _get_data_band_mm(el, parent_map)
            rails.append({
                "p1": [round(X, 3), round(y_min, 3)],
                "p2": [round(X, 3), round(y_max, 3)],
                "band_mm": band
            })
        elif abs(y1 - y2) <= EPS_ALIGN:
            # 水平
            Y = (y1 + y2) / 2.0
            x_min, x_max = sorted([x1, x2])
            band = _get_data_band_mm(el, parent_map)
            rails.append({
                "p1": [round(x_min, 3), round(Y, 3)],
                "p2": [round(x_max, 3), round(Y, 3)],
                "band_mm": band
            })
        else:
            # 斜めはスキップ（必要なら矩形近似に拡張可）
            continue

    return rails

SVG_NS = "http://www.w3.org/2000/svg"
SCALE_OUT = 2108407/597700  # ★ 出力JSONの寸法倍率（固定で10倍）

# ---------- 色の正規化 ----------


NAMED = {
    "black":"#000000","white":"#ffffff","red":"#ff0000","green":"#008000","blue":"#0000ff",
    "magenta":"#ff00ff","fuchsia":"#ff00ff","yellow":"#ffff00","gray":"#808080","grey":"#808080",
    "orange":"#ffa500","cyan":"#00ffff","aqua":"#00ffff","lime":"#00ff00","navy":"#000080"
}
def _to_hex(color):
    if not color: return None
    c = color.strip().lower()
    if c == "none": return None
    if c in NAMED: return NAMED[c]
    m = re.match(r"#([0-9a-f]{3})$", c)
    if m:
        s = m.group(1)
        return "#" + "".join(ch*2 for ch in s)
    m = re.match(r"#([0-9a-f]{6})$", c)
    if m:
        return "#" + m.group(1)
    m = re.match(r"rgba?\(([^)]+)\)", c)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            def _clamp255(v):
                v = v.strip()
                if v.endswith("%"):
                    return round(float(v[:-1]) * 2.55)
                return float(v)
            r = int(round(_clamp255(parts[0])))
            g = int(round(_clamp255(parts[1])))
            b = int(round(_clamp255(parts[2])))
            r = max(0, min(255, r)); g = max(0, min(255, g)); b = max(0, min(255, b))
            return "#{:02x}{:02x}{:02x}".format(r,g,b)
    return None

# ---------- ユーティリティ ----------
def _num(s):
    if s is None: return 0.0
    return float(re.sub(r"[^\d\.\-eE]", "", str(s)))

def _attr(el, name, default=None):
    v = el.get(name)
    return v if v is not None else default

def _style_color(style_str, key):
    # style="fill:#rrggbb; stroke: rgb(...)" から色を抜く
    if not style_str: return None
    d = {}
    for part in style_str.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return _to_hex(d.get(key))

def _bool(s, default=False):
    if s is None: return default
    return str(s).strip().lower() in ("1","true","yes","y","on")

def _has_class_or_id_prefix(el, klass=None, id_prefixes=()):
    c = el.get("class") or ""
    if klass and klass in c.split():
        return True
    el_id = el.get("id") or ""
    return any(el_id.startswith(p) for p in id_prefixes)

def _deep_merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _iter(svg_root, tag):
    # namespace あり/なし両対応で要素列挙
    return list(svg_root.findall(f".//{{{SVG_NS}}}{tag}")) + list(svg_root.findall(f".//{tag}"))

def _find_one(svg_root, xpath_ns, xpath_plain):
    el = svg_root.find(xpath_ns)
    if el is not None: return el
    return svg_root.find(xpath_plain)

def _auto_pick_svg():
    # 優先: hall.svg → *hall*.svg → 更新日時が新しい順
    cwd = Path.cwd()
    if (cwd / "hall.svg").exists():
        return str(cwd / "hall.svg")
    svgs = sorted(glob.glob("*.svg"))
    if not svgs:
        raise FileNotFoundError("カレントディレクトリに SVG が見つかりません。")
    hallish = [s for s in svgs if "hall" in s.lower()]
    if hallish:
        return hallish[0]
    svgs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return svgs[0]

def _scale_dims(obj, s):
    """JSONツリー内の数値だけスケール（bool は除外）"""
    if isinstance(obj, dict):
        return {k: _scale_dims(v, s) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scale_dims(v, s) for v in obj]
    if isinstance(obj, bool):  # bool は int のサブクラスなので先に弾く
        return obj
    if isinstance(obj, (int, float)):
        return int(round(obj * s))
    return obj

# 親マップ・祖先探索（レイヤー/グループ由来の判定に使用）
def _make_parent_map(root):
    return {child: parent for parent in root.iter() for child in parent}

def _has_ancestor(el, parent_map, classes=(), id_prefixes=(), ids=()):
    cur = el
    while cur is not None:
        c = (cur.get('class') or '').split()
        if any(k in c for k in classes):
            return True
        eid = cur.get('id') or ''
        if eid in ids or any(eid.startswith(p) for p in id_prefixes):
            return True
        cur = parent_map.get(cur)
    return False

# ---------- 変換本体 ----------
def parse_svg(svg_path, wall_band_mm, aisle_mm, color_map):
    tree = ET.parse(svg_path)
    svg_root = tree.getroot()
    parent_map = {child: parent for parent in svg_root.iter() for child in parent}

    # room を取得（id='room' が基本。なければ色マップの stroke=room で代替）
    room_rect = _find_one(svg_root, f".//*[@id='room']", f".//*[@id='room']")
    if room_rect is None:
        cand = None
        for el in _iter(svg_root, "rect"):
            stroke = _to_hex(_attr(el, "stroke")) or _style_color(_attr(el, "style"), "stroke")
            if color_map.get("rect", {}).get("stroke", {}).get(stroke, "") == "room":
                cand = el; break
        if cand is None:
            raise ValueError("SVGに <rect id='room'> がありません（色マップで room 指定も見つからず）")
        room_rect = cand

    room_w = _num(_attr(room_rect, "width"))
    room_h = _num(_attr(room_rect, "height"))

    # SVGは上が0なので、yは反転
    def flip_y(y): return room_h - y

    cfg = {
        "room": {
            "width_mm": int(round(room_w)),
            "depth_mm": int(round(room_h)),
            "wall_band_mm": int(wall_band_mm),
            "min_aisle_mm": int(aisle_mm)
        },
        "infrastructure": {
            "outlets": [],
            "curtain_rails": [],
            "no_go_zones": [],
            "inner_walls": []
        },
        "requirements": {
        "curtain_rail_mode": "if_wanted",
        "wall_contact_prefer": True,
        "wall_contact_default_hard": True,
        "wall_contact_hard": False,
        "inner_walls_count_as_wall_contact": True,
        "enforce_outer_wall_band": False,
        "front_clear_mm": 0,
        "front_clear_mode": "hard",
        "outlet_demand_hard_radius_mm": 0,
        "outlet_reserve_radius_mm": 0,
        "preferred_area_default": "soft"
        },
        "weights": {
            "compactness": 3000.0,
            "wall_contact_bonus": 500.0,
            "outlet_distance": 1.0,
            "curtain_rail_match": 1.0,
            "outlet_repel_non_wanter": 0.0,
            "preferred_area_bonus": 1000.0
        }
    }

    parent_map = _make_parent_map(svg_root)

    # ---- outlets: circle or rect（中心を採用）。class / 色 で判定
    for el in _iter(svg_root, "circle") + _iter(svg_root, "rect"):
        good = False
        if _has_class_or_id_prefix(el, "outlet", ("outlet",)):
            good = True
        else:
            fill = _to_hex(_attr(el, "fill")) or _style_color(_attr(el, "style"), "fill")
            stroke = _to_hex(_attr(el, "stroke")) or _style_color(_attr(el, "style"), "stroke")
            m_rect = color_map.get("rect", {})
            m_circ = color_map.get("circle", {})
            if el.tag.endswith("rect") and (m_rect.get("fill", {}).get(fill) == "outlet" or m_rect.get("stroke", {}).get(stroke) == "outlet"):
                good = True
            if el.tag.endswith("circle") and (m_circ.get("fill", {}).get(fill) == "outlet" or m_circ.get("stroke", {}).get(stroke) == "outlet"):
                good = True
        if not good:
            continue
        if el.tag.endswith("circle"):
            cx = _num(_attr(el, "cx")); cy = _num(_attr(el, "cy"))
        else:  # rect center
            cx = _num(_attr(el, "x")) + _num(_attr(el, "width")) / 2.0
            cy = _num(_attr(el, "y")) + _num(_attr(el, "height")) / 2.0
        cfg["infrastructure"]["outlets"].append([int(round(cx)), int(round(flip_y(cy)))])

    # --- curtain rails（line / path / polyline 対応・色解決つき）---
    # ここは “アウトレット append の直後、内壁/No-Go の前” に置いてください
    for r in extract_curtain_rails(svg_root, color_map, parent_map):   # ← 変数名が svg_root の場合は svg_root に
        (x1, y1) = r["p1"]
        (x2, y2) = r["p2"]
        band = int(round(r.get("band_mm", 1000)))
        cfg["infrastructure"]["curtain_rails"].append({
            "p1": [int(round(x1)), int(round(flip_y(y1)))],  # ← 必ず flip_y を通す
            "p2": [int(round(x2)), int(round(flip_y(y2)))],
            "band_mm": band
        })

    # ---- inner walls: line。class / 色 で判定
    for el in _iter(svg_root, "line"):
        is_iw = _has_class_or_id_prefix(el, "inner-wall", ("inner", "wall"))
        if not is_iw:
            stroke = _to_hex(_attr(el, "stroke")) or _style_color(_attr(el, "style"), "stroke")
            if color_map.get("line", {}).get("stroke", {}).get(stroke) == "inner-wall":
                is_iw = True
        if not is_iw:
            continue
        x1 = _num(_attr(el, "x1")); y1 = _num(_attr(el, "y1"))
        x2 = _num(_attr(el, "x2")); y2 = _num(_attr(el, "y2"))
        name = _attr(el, "data-name") or _attr(el, "id") or ""
        thick = int(round(_num(el.get("data-thickness-mm") or 100)))
        attachable = _bool(el.get("data-attachable"), True)
        cfg["infrastructure"]["inner_walls"].append({
            "name": name,
            "p1": [int(round(x1)), int(round(flip_y(y1)))],
            "p2": [int(round(x2)), int(round(flip_y(y2)))],
            "thickness_mm": thick,
            "attachable": attachable
        })

    # ---- no-go zones: rect / polygon（room は除外）。class / 色 / 親レイヤー で判定 ----
    room_id = room_rect.get("id") if room_rect is not None else None

    def _is_no_go(el):
        # 1) 自身の class / id
        if _has_class_or_id_prefix(el, "no-go", ("no-go", "nogozone", "no-go-zone")):
            return True
        # 2) 親グループ/レイヤーに no-go 指定
        if _has_ancestor(el, parent_map,
                         classes=("no-go", "no-go-zone"),
                         id_prefixes=("no-go", "nogozone"),
                         ids=("no-go-zones",)):
            return True
        # 3) 色（fill/stroke）で判定
        fill = _to_hex(_attr(el, "fill")) or _style_color(_attr(el, "style"), "fill")
        stroke = _to_hex(_attr(el, "stroke")) or _style_color(_attr(el, "style"), "stroke")
        m = {**color_map.get("rect", {})}  # polygon も rect の色設定を流用
        if (m.get("fill", {}).get(fill) == "no-go") or (m.get("stroke", {}).get(stroke) == "no-go"):
            return True
        return False

    def _rect_bbox(el):
        x = _num(_attr(el, "x") or 0); y = _num(_attr(el, "y") or 0)
        w = _num(_attr(el, "width") or 0); h = _num(_attr(el, "height") or 0)
        return (x, y, x+w, y+h)  # (xmin, ymin, xmax, ymax) SVG座標系（上原点）

    def _poly_bbox(el):
        pts = (_attr(el, "points") or "").strip()
        if not pts:
            return None
        nums = re.split(r"[ ,]+", pts)
        xs, ys = [], []
        for i in range(0, len(nums)-1, 2):
            try:
                xs.append(float(nums[i])); ys.append(float(nums[i+1]))
            except ValueError:
                pass
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    # rect + polygon を対象に
    for el in _iter(svg_root, "rect") + _iter(svg_root, "polygon"):
        # room 自体は除外（背景用の大矩形なども、id一致で除外）
        if el is room_rect or (room_id and el.get("id") == room_id):
            continue
        if not _is_no_go(el):
            continue

        if el.tag.endswith("rect"):
            xmin_s, ymin_s, xmax_s, ymax_s = _rect_bbox(el)
        else:
            bb = _poly_bbox(el)
            if not bb:
                continue
            xmin_s, ymin_s, xmax_s, ymax_s = bb

        # SVG座標 → 下原点座標へ反転
        xmin = xmin_s
        xmax = xmax_s
        ymin = int(round(flip_y(ymax_s)))  # 上原点の ymax → 反転で ymin
        ymax = int(round(flip_y(ymin_s)))  # 上原点の ymin → 反転で ymax

        cfg["infrastructure"]["no_go_zones"].append({
            "name": _attr(el, "data-name") or _attr(el, "id") or "",
            "rect": [int(round(xmin)), int(round(ymin)), int(round(xmax)), int(round(ymax))]
        })

    return cfg

# ---------- メイン ----------
def main():
    # 自動検出
    try:
        svg_path = _auto_pick_svg()
        print(f"[auto] SVG: {svg_path}")
    except FileNotFoundError as e:
        print(str(e))
        print("ヒント: hall.svg を置くか、`python svg2config.py input.svg` のように指定してください。")
        sys.exit(2)

    # color map の既定値＋上書き（任意）
    default_color_map = {
        "line":   {"stroke": {"#0a7a0a":"curtain-rail", "#0080ff":"inner-wall"}},
        "rect":   {"fill":   {"#ffa500":"no-go"},
                   "stroke": {"#000000":"room"}},
        "circle": {"fill":   {"#ff00ff":"outlet"}}
    }
    color_map = default_color_map
    if Path("color_map.json").exists():
        try:
            with open("color_map.json", "r", encoding="utf-8") as f:
                user_map = json.load(f)
            color_map = _deep_merge(default_color_map, user_map)
            print("[auto] color-map: color_map.json")
        except Exception as e:
            print(f"[warn] 色マップ color_map.json の読み込みに失敗しました: {e}")

    # 既定パラメータ
    wall_band_mm = 0
    aisle_mm = 0

    cfg = parse_svg(svg_path, wall_band_mm, aisle_mm, color_map)

    # ★ 寸法の一括スケーリング（常に 10 倍）。weights はスケールしない。
    if SCALE_OUT and SCALE_OUT != 1:
        cfg["room"] = _scale_dims(cfg["room"], SCALE_OUT)
        cfg["infrastructure"] = _scale_dims(cfg["infrastructure"], SCALE_OUT)
        # requirements の“mm”項目のみスケール
        for key in ("front_clear_mm", "outlet_demand_hard_radius_mm", "outlet_reserve_radius_mm"):
            if key in cfg["requirements"]:
                cfg["requirements"][key] = int(round(cfg["requirements"][key] * SCALE_OUT))

    out_path = "config.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"Wrote {out_path} (all dimensions x{int(SCALE_OUT)})")

if __name__ == "__main__":
    main()