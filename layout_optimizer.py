# layout_optimizer.py
# 展示レイアウト最適化
# - カーテン必須: 背面をレールの線にピッタリ接触（水平/垂直のいずれか）
# - それ以外: 壁にピッタリ（特に制約が無ければ）
# - 回転: 「タッチ優先→バンド」（ユーザー指定）
# - 出力: solver.Value(...) のみ使用（巨大値事故を防止）
# - SVG: コンセント図形／レール帯／ブース名テキスト

import re, json, csv, os, shutil
from ortools.sat.python import cp_model
import svgwrite

# ===== config.json（コメント許容） =====
with open("config.json", "r", encoding="utf-8") as f:
    txt = f.read()
txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
txt = re.sub(r"(?m)//.*$", "", txt)
config = json.loads(txt)

# ===== 設定 =====
room_w = int(config["room"]["width_mm"])
room_h = int(config["room"]["depth_mm"])
wall_band = int(config["room"]["wall_band_mm"])
aisle = int(config["room"]["min_aisle_mm"])

outlets = [tuple(map(int, p)) for p in config["infrastructure"]["outlets"]]
rails_cfg = config["infrastructure"]["curtain_rails"]

req = config.get("requirements", {})
rail_mode = req.get("curtain_rail_mode", "if_wanted")  # "if_wanted" | "all" | "none"
prefer_wall = bool(req.get("wall_contact_prefer", True))
hard_wall   = bool(req.get("wall_contact_hard", False))
wall_default_hard = bool(req.get("wall_contact_default_hard", True))

infra = config["infrastructure"]
no_go = infra.get("no_go_zones", [])
inner_walls = infra.get("inner_walls", [])

inner_as_wall = bool(req.get("inner_walls_count_as_wall_contact", True))

front_clear = int(req.get("front_clear_mm", 0))
front_mode  = req.get("front_clear_mode", "hard")  # "hard" or "none"

reserve_radius = int(req.get("outlet_reserve_radius_mm", 0))
demand_hard_r  = int(req.get("outlet_demand_hard_radius_mm", 0))

def W(v, scale=100):
    return int(round(float(v) * scale))

W_COMPACT       = W(config["weights"].get("compactness", 3000.0))
W_WALL_BON      = W(config["weights"].get("wall_contact_bonus", 500.0))
W_OUTLET        = W(config["weights"].get("outlet_distance", 1.0))
W_CURTAIN       = W(config["weights"].get("curtain_rail_match", 1.0))
W_OUTLET_REPEL  = W(config["weights"].get("outlet_repel_non_wanter", 0.0))
W_PREF_AREA     = W(config["weights"].get("preferred_area_bonus", 1000.0))

req = config.get("requirements", {})
PREF_DEFAULT_HARD = (str(req.get("preferred_area_default", "soft")).lower() == "hard")

# ===== ブース読み込み =====
booths = []
with open("booths.csv", "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    def _to_int_or_none(s):
        try:
            if s is None: return None
            s = str(s).strip()
            if s == "": return None
            return int(float(s))
        except:
            return None

    pref_rects = []   # [(xmin,ymin,xmax,ymax) or None]
    pref_hard  = []   # [True/False]
    for row in reader:
        booths.append({
            "id": int(row["id"]),
            "name": row["name"],
            "w": int(row["width_mm"]),
            "h": int(row["depth_mm"]),
            "want_outlet": str(row["want_outlet"]).strip().upper() == "TRUE",
            "want_curtain": str(row["want_curtain_rail"]).strip().upper() == "TRUE",
            "group": row.get("group", "")
        })
        xmin = _to_int_or_none(row.get("pref_xmin_mm"))
        ymin = _to_int_or_none(row.get("pref_ymin_mm"))
        xmax = _to_int_or_none(row.get("pref_xmax_mm"))
        ymax = _to_int_or_none(row.get("pref_ymax_mm"))

        if None not in (xmin, ymin, xmax, ymax):
            pref_rects.append((xmin, ymin, xmax, ymax))
        else:
            pref_rects.append(None)

        hard_flag = row.get("pref_area_hard")
        if hard_flag is None or str(hard_flag).strip() == "":
            pref_hard.append(PREF_DEFAULT_HARD)
        else:
            pref_hard.append(str(hard_flag).strip().lower() in ("1","true","yes"))
n = len(booths)

# ===== モデル =====
model = cp_model.CpModel()

# 位置＆回転
x = [model.NewIntVar(0, room_w, f"x_{i}") for i in range(n)]
y = [model.NewIntVar(0, room_h, f"y_{i}") for i in range(n)]
rot = [model.NewBoolVar(f"rot_{i}") for i in range(n)]  # 0:(w,h), 1:(h,w)

# 回転後サイズ
w_eff, h_eff = [], []
for i, b in enumerate(booths):
    we = model.NewIntVar(0, room_w, f"w_eff_{i}")
    he = model.NewIntVar(0, room_h, f"h_eff_{i}")
    model.Add(we == rot[i] * b["h"] + (1 - rot[i]) * b["w"])
    model.Add(he == rot[i] * b["w"] + (1 - rot[i]) * b["h"])
    w_eff.append(we); h_eff.append(he)

# ===== 壁帯（4辺の帯） =====
# 外周壁からの帯に入っているかを表すブール（回転ロジックのフォールバック用）
# ※ 強制で使うかはフラグで切替（内壁も壁として使いたい場合は False 推奨）
enforce_outer_band = bool(req.get("enforce_outer_wall_band", False))

band_bottom, band_top, band_left, band_right = [], [], [], []
for i in range(n):
    cb = model.NewBoolVar(f"band_bottom_{i}")  # y <= wall_band
    ct = model.NewBoolVar(f"band_top_{i}")     # y + h >= room_h - wall_band
    cl = model.NewBoolVar(f"band_left_{i}")    # x <= wall_band
    cr = model.NewBoolVar(f"band_right_{i}")   # x + w >= room_w - wall_band

    # それぞれの帯に入っている ⇔ ブール の等価を、二方向の含意で表現
    model.Add(y[i] <= wall_band).OnlyEnforceIf(cb)
    model.Add(y[i] >  wall_band).OnlyEnforceIf(cb.Not())

    model.Add(y[i] + h_eff[i] >= room_h - wall_band).OnlyEnforceIf(ct)
    model.Add(y[i] + h_eff[i] <  room_h - wall_band).OnlyEnforceIf(ct.Not())

    model.Add(x[i] <= wall_band).OnlyEnforceIf(cl)
    model.Add(x[i] >  wall_band).OnlyEnforceIf(cl.Not())

    model.Add(x[i] + w_eff[i] >= room_w - wall_band).OnlyEnforceIf(cr)
    model.Add(x[i] + w_eff[i] <  room_w - wall_band).OnlyEnforceIf(cr.Not())

    # （オプション）外周壁帯のどれかに必ず入れ、を強制したい場合のみ有効化
    if enforce_outer_band:
        model.AddBoolOr([cb, ct, cl, cr])

    band_bottom.append(cb)
    band_top.append(ct)
    band_left.append(cl)
    band_right.append(cr)

# ===== 会場外禁止 =====
for i in range(n):
    model.Add(x[i] + w_eff[i] <= room_w)
    model.Add(y[i] + h_eff[i] <= room_h)

# ===== 非重複（通路幅込み） =====
for i in range(n):
    for j in range(i + 1, n):
        left  = model.NewBoolVar(f"left_{i}_{j}")
        right = model.NewBoolVar(f"right_{i}_{j}")
        below = model.NewBoolVar(f"below_{i}_{j}")
        above = model.NewBoolVar(f"above_{i}_{j}")
        model.AddBoolOr([left, right, below, above])
        model.Add(x[i] + w_eff[i] + aisle <= x[j]).OnlyEnforceIf(left)
        model.Add(x[j] + w_eff[j] + aisle <= x[i]).OnlyEnforceIf(right)
        model.Add(y[i] + h_eff[i] + aisle <= y[j]).OnlyEnforceIf(below)
        model.Add(y[j] + h_eff[j] + aisle <= y[i]).OnlyEnforceIf(above)
        
# ===== 各ブースの希望エリア（任意：hard / soft）=====
inside_pref = [None] * n  # soft用のインジケータ

for i in range(n):
    r = pref_rects[i]
    if r is None:
        continue
    xmin, ymin, xmax, ymax = r

    if pref_hard[i]:
        # ★ハード：必ず範囲内
        model.Add(x[i] >= xmin)
        model.Add(y[i] >= ymin)
        model.Add(x[i] + w_eff[i] <= xmax)
        model.Add(y[i] + h_eff[i] <= ymax)
    else:
        # ★ソフト：入ったら加点（z=1）
        z = model.NewBoolVar(f"inside_pref_{i}")
        model.Add(x[i] >= xmin).OnlyEnforceIf(z)
        model.Add(y[i] >= ymin).OnlyEnforceIf(z)
        model.Add(x[i] + w_eff[i] <= xmax).OnlyEnforceIf(z)
        model.Add(y[i] + h_eff[i] <= ymax).OnlyEnforceIf(z)
        inside_pref[i] = z
        
# ===== 内壁：跨いで配置しない（ハード）=====
# 垂直：x=x0, y∈[y1,y2] を跨いではいけない
# 水平：y=y0, x∈[x1,x2] を跨いではいけない
for w_idx, w in enumerate(inner_walls):
    (x1, y1) = w["p1"]; (x2, y2) = w["p2"]
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    xmin, xmax = min(x1, x2), max(x1, x2)
    ymin, ymax = min(y1, y2), max(y1, y2)

    if x1 == x2:
        x0 = x1
        # 条件：ブースが線分の y 範囲と縦方向で重なる場合、
        #       x + w <= x0  (左側) または x >= x0 (右側) のどちらか
        for i in range(n):
            left  = model.NewBoolVar(f"iw_v_left_{w_idx}_{i}")
            right = model.NewBoolVar(f"iw_v_right_{w_idx}_{i}")
            above = model.NewBoolVar(f"iw_v_above_{w_idx}_{i}")  # ブースが線分の上方（y >= ymax）
            below = model.NewBoolVar(f"iw_v_below_{w_idx}_{i}")  # 下方（y+h <= ymin）

            model.AddBoolOr([left, right, above, below])

            model.Add(x[i] + w_eff[i] <= x0).OnlyEnforceIf(left)
            model.Add(x[i] >= x0).OnlyEnforceIf(right)
            model.Add(y[i] >= ymax).OnlyEnforceIf(above)
            model.Add(y[i] + h_eff[i] <= ymin).OnlyEnforceIf(below)

    elif y1 == y2:
        y0 = y1
        for i in range(n):
            above = model.NewBoolVar(f"iw_h_above_{w_idx}_{i}")
            below = model.NewBoolVar(f"iw_h_below_{w_idx}_{i}")
            left  = model.NewBoolVar(f"iw_h_left_{w_idx}_{i}")
            right = model.NewBoolVar(f"iw_h_right_{w_idx}_{i}")

            model.AddBoolOr([above, below, left, right])

            model.Add(y[i] + h_eff[i] <= y0).OnlyEnforceIf(above)
            model.Add(y[i] >= y0).OnlyEnforceIf(below)
            model.Add(x[i] + w_eff[i] <= xmin).OnlyEnforceIf(left)
            model.Add(x[i] >= xmax).OnlyEnforceIf(right)

    else:
        # 斜めは対象外（必要なら拡張）
        pass
        
# ===== 展示禁止エリア：一切重ならない（ハード）=====
for z_idx, z in enumerate(no_go):
    rx1, ry1, rx2, ry2 = map(int, z["rect"])  # xmin, ymin, xmax, ymax
    for i in range(n):
        L = model.NewBoolVar(f"ngL_{z_idx}_{i}")  # ブースは禁止エリアの左側
        R = model.NewBoolVar(f"ngR_{z_idx}_{i}")  # 右側
        B = model.NewBoolVar(f"ngB_{z_idx}_{i}")  # 下側
        A = model.NewBoolVar(f"ngA_{z_idx}_{i}")  # 上側
        model.AddBoolOr([L, R, B, A])

        model.Add(x[i] + w_eff[i] <= rx1).OnlyEnforceIf(L)  # 左に完全に離れる
        model.Add(x[i] >= rx2).OnlyEnforceIf(R)             # 右に完全に離れる
        model.Add(y[i] + h_eff[i] <= ry1).OnlyEnforceIf(B)  # 下に完全に離れる
        model.Add(y[i] >= ry2).OnlyEnforceIf(A)             # 上に完全に離れる

# ===== 壁ピッタリ（タッチ） =====
touch_left   = [model.NewBoolVar(f"touch_left_{i}") for i in range(n)]
touch_right  = [model.NewBoolVar(f"touch_right_{i}") for i in range(n)]
touch_bottom = [model.NewBoolVar(f"touch_bottom_{i}") for i in range(n)]
touch_top    = [model.NewBoolVar(f"touch_top_{i}") for i in range(n)]
for i in range(n):
    model.Add(x[i] == 0).OnlyEnforceIf(touch_left[i])
    model.Add(x[i] + w_eff[i] == room_w).OnlyEnforceIf(touch_right[i])
    model.Add(y[i] == 0).OnlyEnforceIf(touch_bottom[i])
    model.Add(y[i] + h_eff[i] == room_h).OnlyEnforceIf(touch_top[i])

# ===== ブースごとの「カーテン必須」判定（Python側フラグ） =====
curtain_required = []
for b in booths:
    need = (rail_mode == "all") or (rail_mode == "if_wanted" and b["want_curtain"])
    curtain_required.append(need)

# ===== 中心（2倍スケール） =====
cx2 = [model.NewIntVar(0, 2*room_w, f"cx2_{i}") for i in range(n)]
cy2 = [model.NewIntVar(0, 2*room_h, f"cy2_{i}") for i in range(n)]
for i in range(n):
    model.Add(cx2[i] == 2*x[i] + w_eff[i])
    model.Add(cy2[i] == 2*y[i] + h_eff[i])
    
# ===== 内壁を「壁」として扱うタッチ変数 =====
inner_as_wall = bool(req.get("inner_walls_count_as_wall_contact", True))
inner_walls = config["infrastructure"].get("inner_walls", [])

iw_touch_v = [[] for _ in range(n)]  # 垂直内壁への接触（x==x0 or x+w==x0）
iw_touch_h = [[] for _ in range(n)]  # 水平内壁への接触（y==y0 or y+h==y0）

if inner_as_wall and inner_walls:
    for w_idx, w in enumerate(inner_walls):
        (x1, y1) = w["p1"]; (x2, y2) = w["p2"]
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)

        if x1 == x2:
            x0 = x1
            for i in range(n):
                t_on_left  = model.NewBoolVar(f"iw_v_left_{w_idx}_{i}")   # 右辺が内壁（x+w==x0）
                t_on_right = model.NewBoolVar(f"iw_v_right_{w_idx}_{i}")  # 左辺が内壁（x==x0）

                model.Add(x[i] + w_eff[i] == x0).OnlyEnforceIf(t_on_left)
                model.Add(x[i] == x0).OnlyEnforceIf(t_on_right)

                # 線分スパン内で接触させる（中心でOK）
                model.Add(cy2[i] >= 2*ymin).OnlyEnforceIf(t_on_left)
                model.Add(cy2[i] <= 2*ymax).OnlyEnforceIf(t_on_left)
                model.Add(cy2[i] >= 2*ymin).OnlyEnforceIf(t_on_right)
                model.Add(cy2[i] <= 2*ymax).OnlyEnforceIf(t_on_right)

                iw_touch_v[i] += [t_on_left, t_on_right]

        elif y1 == y2:
            y0 = y1
            for i in range(n):
                # 下辺=内壁（y==y0）／上辺=内壁（y+h==y0）
                t_on_bottom = model.NewBoolVar(f"iw_h_bottom_{w_idx}_{i}")
                t_on_top    = model.NewBoolVar(f"iw_h_top_{w_idx}_{i}")

                model.Add(y[i] == y0).OnlyEnforceIf(t_on_bottom)
                model.Add(y[i] + h_eff[i] == y0).OnlyEnforceIf(t_on_top)

                # 線分スパン内で接触（中心でガイド）
                model.Add(cx2[i] >= 2*xmin).OnlyEnforceIf(t_on_bottom)
                model.Add(cx2[i] <= 2*xmax).OnlyEnforceIf(t_on_bottom)
                model.Add(cx2[i] >= 2*xmin).OnlyEnforceIf(t_on_top)
                model.Add(cx2[i] <= 2*xmax).OnlyEnforceIf(t_on_top)

                iw_touch_h[i] += [t_on_bottom, t_on_top]
                
# ===== 回転ロジック：タッチ優先 → バンド（外壁 + 内壁）=====
v_touch_any = [model.NewBoolVar(f"v_touch_any_{i}") for i in range(n)]
h_touch_any = [model.NewBoolVar(f"h_touch_any_{i}") for i in range(n)]
v_band  = [model.NewBoolVar(f"v_band_{i}") for i in range(n)]
h_band  = [model.NewBoolVar(f"h_band_{i}") for i in range(n)]

for i in range(n):
    # 「縦方向の壁に触れているか」= 左右外壁 or 垂直内壁のいずれか
    model.AddMaxEquality(v_touch_any[i], [touch_left[i], touch_right[i]] + iw_touch_v[i])
    # 「横方向の壁に触れているか」= 上下外壁 or 水平内壁のいずれか
    model.AddMaxEquality(h_touch_any[i], [touch_bottom[i], touch_top[i]] + iw_touch_h[i])

    # 1) タッチがあれば優先
    model.Add(rot[i] == 1).OnlyEnforceIf(v_touch_any[i]).OnlyEnforceIf(h_touch_any[i].Not())
    model.Add(rot[i] == 0).OnlyEnforceIf(h_touch_any[i]).OnlyEnforceIf(v_touch_any[i].Not())

    # 2) タッチなし → “壁沿い帯”で決める（外壁の帯をそのまま使用）
    model.AddMaxEquality(v_band[i],  [band_left[i], band_right[i]])
    model.AddMaxEquality(h_band[i],  [band_bottom[i], band_top[i]])
    model.Add(rot[i] == 1).OnlyEnforceIf(v_band[i]).OnlyEnforceIf(h_band[i].Not())
    model.Add(rot[i] == 0).OnlyEnforceIf(h_band[i]).OnlyEnforceIf(v_band[i].Not())

# ===== カーテンレール：必須ブースは「背面ピッタリ」 =====
# レールを水平/垂直に分解（帯幅は描画用にのみ利用。接触は線で扱う）
rails_h = []  # (y0, xmin, xmax, ridx)
rails_v = []  # (x0, ymin, ymax, ridx)
for ridx, cr in enumerate(rails_cfg):
    (x1,y1), (x2,y2) = cr["p1"], cr["p2"]
    xmin, xmax = min(x1,x2), max(x1,x2)
    ymin, ymax = min(y1,y2), max(y1,y2)
    if y1 == y2:
        rails_h.append((y1, xmin, xmax, ridx))
    elif x1 == x2:
        rails_v.append((x1, ymin, ymax, ridx))
    else:
        # 斜めは今回は対象外（必要なら後で拡張）
        pass

attach_h_bottom = [[None]*len(rails_h) for _ in range(n)]  # y == y0
attach_h_top    = [[None]*len(rails_h) for _ in range(n)]  # y + h == y0
attach_v_left   = [[None]*len(rails_v) for _ in range(n)]  # x + w == x0
attach_v_right  = [[None]*len(rails_v) for _ in range(n)]  # x == x0

for i in range(n):
    # 水平レール（背面が下 or 上に接触）
    for r,(y0,xmin,xmax,_) in enumerate(rails_h):
        ab = model.NewBoolVar(f"attHb_{i}_{r}")
        at = model.NewBoolVar(f"attHt_{i}_{r}")
        attach_h_bottom[i][r] = ab
        attach_h_top[i][r]    = at

        # レールスパンに“幅”が収まる
        model.Add(x[i] >= xmin).OnlyEnforceIf(ab)
        model.Add(x[i] + w_eff[i] <= xmax).OnlyEnforceIf(ab)
        model.Add(x[i] >= xmin).OnlyEnforceIf(at)
        model.Add(x[i] + w_eff[i] <= xmax).OnlyEnforceIf(at)
        # 背面ピッタリ
        model.Add(y[i] == y0).OnlyEnforceIf(ab)
        model.Add(y[i] + h_eff[i] == y0).OnlyEnforceIf(at)
        # 向き：水平レールに沿わせる → rot=0
        model.Add(rot[i] == 0).OnlyEnforceIf(ab)
        model.Add(rot[i] == 0).OnlyEnforceIf(at)

    # 垂直レール（背面が左 or 右に接触）
    for r,(x0,ymin,ymax,_) in enumerate(rails_v):
        al = model.NewBoolVar(f"attVl_{i}_{r}")
        ar = model.NewBoolVar(f"attVr_{i}_{r}")
        attach_v_left[i][r]  = al
        attach_v_right[i][r] = ar

        # レールスパンに“高さ”が収まる
        model.Add(y[i] >= ymin).OnlyEnforceIf(al)
        model.Add(y[i] + h_eff[i] <= ymax).OnlyEnforceIf(al)
        model.Add(y[i] >= ymin).OnlyEnforceIf(ar)
        model.Add(y[i] + h_eff[i] <= ymax).OnlyEnforceIf(ar)
        # 背面ピッタリ
        model.Add(x[i] + w_eff[i] == x0).OnlyEnforceIf(al)
        model.Add(x[i] == x0).OnlyEnforceIf(ar)
        # 向き：垂直レールに沿わせる → rot=1
        model.Add(rot[i] == 1).OnlyEnforceIf(al)
        model.Add(rot[i] == 1).OnlyEnforceIf(ar)

# 必須ブースは“いずれか1つのレール面”に必ず付く／非必須は付かない
for i in range(n):
    attach_vars = []
    attach_vars += [v for v in attach_h_bottom[i] if v is not None]
    attach_vars += [v for v in attach_h_top[i]    if v is not None]
    attach_vars += [v for v in attach_v_left[i]   if v is not None]
    attach_vars += [v for v in attach_v_right[i]  if v is not None]
    if curtain_required[i]:
        if attach_vars:
            model.Add(sum(attach_vars) == 1)
        else:
            raise ValueError("カーテン必須ブースがありますが、レール定義がありません。")
    else:
        for v in attach_vars:
            model.Add(v == 0)
            
# ===== 内壁への“接触”を表す変数（任意。壁ピタ対象に含める用）=====
inner_touches = [[] for _ in range(n)]
if inner_as_wall and inner_walls:
    for w_idx, w in enumerate(inner_walls):
        (x1, y1) = w["p1"]; (x2, y2) = w["p2"]
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        xmin, xmax = min(x1, x2), max(x1, x2)
        ymin, ymax = min(y1, y2), max(y1, y2)

        if x1 == x2:
            x0 = x1
            for i in range(n):
                # 右辺=壁（x+w==x0）／左辺=壁（x==x0）
                t_left  = model.NewBoolVar(f"iw_touch_left_{w_idx}_{i}")
                t_right = model.NewBoolVar(f"iw_touch_right_{w_idx}_{i}")
                # 接触時は“中心”が線分スパン内（重なりの確実化）
                model.Add(x[i] + w_eff[i] == x0).OnlyEnforceIf(t_left)
                model.Add(x[i] == x0).OnlyEnforceIf(t_right)
                model.Add(cy2[i] >= 2*ymin).OnlyEnforceIf(t_left)
                model.Add(cy2[i] <= 2*ymax).OnlyEnforceIf(t_left)
                model.Add(cy2[i] >= 2*ymin).OnlyEnforceIf(t_right)
                model.Add(cy2[i] <= 2*ymax).OnlyEnforceIf(t_right)
                inner_touches[i] += [t_left, t_right]

        elif y1 == y2:
            y0 = y1
            for i in range(n):
                # 上辺=壁（y+h==y0）／下辺=壁（y==y0）
                t_bottom = model.NewBoolVar(f"iw_touch_bottom_{w_idx}_{i}")
                t_top    = model.NewBoolVar(f"iw_touch_top_{w_idx}_{i}")
                model.Add(y[i] == y0).OnlyEnforceIf(t_bottom)
                model.Add(y[i] + h_eff[i] == y0).OnlyEnforceIf(t_top)
                model.Add(cx2[i] >= 2*xmin).OnlyEnforceIf(t_bottom)
                model.Add(cx2[i] <= 2*xmax).OnlyEnforceIf(t_bottom)
                model.Add(cx2[i] >= 2*xmin).OnlyEnforceIf(t_top)
                model.Add(cx2[i] <= 2*xmax).OnlyEnforceIf(t_top)
                inner_touches[i] += [t_bottom, t_top]

# ===== 非カーテンは「外周壁 か 内壁」のどれかに必ずピッタリ =====
for i in range(n):
    if (not curtain_required[i]) and (hard_wall or wall_default_hard):
        model.AddBoolOr([touch_left[i], touch_right[i], touch_bottom[i], touch_top[i]] + inner_touches[i])

# ===== 「特に制約がなければ壁にピッタリ」 =====
# ===== 非カーテンは「外周壁 or 内壁」のどれかに必ずピッタリ =====
for i in range(n):
    if (not curtain_required[i]) and (hard_wall or wall_default_hard):
        model.AddBoolOr([touch_left[i], touch_right[i], touch_bottom[i], touch_top[i]] + iw_touch_v[i] + iw_touch_h[i])

# ===== まとまり（外接BBox） =====
right_edges = [model.NewIntVar(0, room_w, f"right_{i}") for i in range(n)]
tops        = [model.NewIntVar(0, room_h, f"top_{i}") for i in range(n)]
for i in range(n):
    model.Add(right_edges[i] == x[i] + w_eff[i])
    model.Add(tops[i]        == y[i] + h_eff[i])

x_min = model.NewIntVar(0, room_w, "x_min")
x_max = model.NewIntVar(0, room_w, "x_max")
y_min = model.NewIntVar(0, room_h, "y_min")
y_max = model.NewIntVar(0, room_h, "y_max")
model.AddMinEquality(x_min, x)
model.AddMaxEquality(x_max, right_edges)
model.AddMinEquality(y_min, y)
model.AddMaxEquality(y_max, tops)

bbox_w = model.NewIntVar(0, room_w, "bbox_w")
bbox_h = model.NewIntVar(0, room_h, "bbox_h")
model.Add(bbox_w == x_max - x_min)
model.Add(bbox_h == y_max - y_min)

# ===== コンセント距離（L1の2倍表現） =====
nearest2_all = [None]*n
if outlets:
    for i in range(n):
        dist_vars = []
        for k,(ox,oy) in enumerate(outlets):
            dx2 = model.NewIntVar(0, 2*room_w, f"dx2_{i}_{k}")
            dy2 = model.NewIntVar(0, 2*room_h, f"dy2_{i}_{k}")
            tmpx = model.NewIntVar(-2*room_w, 2*room_w, f"tmpx_{i}_{k}")
            tmpy = model.NewIntVar(-2*room_h, 2*room_h, f"tmpy_{i}_{k}")
            model.Add(tmpx == cx2[i] - 2*ox)
            model.Add(tmpy == cy2[i] - 2*oy)
            model.AddAbsEquality(dx2, tmpx)
            model.AddAbsEquality(dy2, tmpy)
            man2 = model.NewIntVar(0, 2*(room_w+room_h), f"man2_{i}_{k}")
            model.Add(man2 == dx2 + dy2)
            dist_vars.append(man2)
        nearest2 = model.NewIntVar(0, 2*(room_w+room_h), f"nearest2_{i}")
        model.AddMinEquality(nearest2, dist_vars)
        nearest2_all[i] = nearest2

# ===== 目的関数 =====
score_terms = []
score_terms.append(-W_COMPACT * (bbox_w + bbox_h))  # まとまり
if prefer_wall:
    for i in range(n):
        if not curtain_required[i]:
            any_wall_touch = model.NewBoolVar(f"any_wall_touch_{i}")
            model.AddMaxEquality(any_wall_touch, [touch_left[i], touch_right[i], touch_bottom[i], touch_top[i]] + iw_touch_v[i] + iw_touch_h[i])
            score_terms.append(W_WALL_BON * any_wall_touch)

# rail_mode="none" のときだけ、レール近傍を満たせたら加点（今回は背面ピタ仕様なので通常不要）
# （必要ならここにスコアを追加）

# コンセント（希望者は近いほど良い／非希望者は近すぎ減点・任意）
if outlets:
    for i,b in enumerate(booths):
        if b["want_outlet"]:
            score_terms.append(-W_OUTLET * nearest2_all[i])
    if demand_hard_r > 0:
        thr2 = 2 * demand_hard_r
        for i,b in enumerate(booths):
            if b["want_outlet"]:
                model.Add(nearest2_all[i] <= thr2)
    if reserve_radius > 0 and W_OUTLET_REPEL > 0:
        thr2 = 2 * reserve_radius
        for i,b in enumerate(booths):
            if not b["want_outlet"]:
                near = model.NewBoolVar(f"near_outlet_{i}")
                model.Add(nearest2_all[i] <= thr2).OnlyEnforceIf(near)
                model.Add(nearest2_all[i] >= thr2 + 1).OnlyEnforceIf(near.Not())
                score_terms.append(-W_OUTLET_REPEL * near * 1000)
                
for z in inside_pref:
    if z is not None and W_PREF_AREA != 0:
        score_terms.append(W_PREF_AREA * z)

model.Maximize(sum(score_terms))

# ===== front_clear（任意：hard のとき）=====
if front_mode == "hard" and front_clear > 0:
    fr_right = [model.NewBoolVar(f"fr_right_{i}") for i in range(n)]
    fr_left  = [model.NewBoolVar(f"fr_left_{i}")  for i in range(n)]
    fr_up    = [model.NewBoolVar(f"fr_up_{i}")    for i in range(n)]
    fr_down  = [model.NewBoolVar(f"fr_down_{i}")  for i in range(n)]

    # 壁/帯アクティブ
    left_act   = [model.NewBoolVar(f"left_act_{i}") for i in range(n)]
    right_act  = [model.NewBoolVar(f"right_act_{i}") for i in range(n)]
    bottom_act = [model.NewBoolVar(f"bottom_act_{i}") for i in range(n)]
    top_act    = [model.NewBoolVar(f"top_act_{i}") for i in range(n)]
    for i in range(n):
        model.AddMaxEquality(left_act[i],   [band_left[i],  touch_left[i]])
        model.AddMaxEquality(right_act[i],  [band_right[i], touch_right[i]])
        model.AddMaxEquality(bottom_act[i], [band_bottom[i],touch_bottom[i]])
        model.AddMaxEquality(top_act[i],    [band_top[i],   touch_top[i]])
        model.Add(fr_right[i] + fr_left[i] + fr_up[i] + fr_down[i] == 1)

    # カーテン必須：背面の反対側を正面に固定
    for i in range(n):
        if curtain_required[i]:
            for r,_ in enumerate(rails_h):
                ab = attach_h_bottom[i][r]; at = attach_h_top[i][r]
                model.Add(fr_up[i]   == 1).OnlyEnforceIf(ab)
                model.Add(fr_down[i] == 1).OnlyEnforceIf(at)
            for r,_ in enumerate(rails_v):
                al = attach_v_left[i][r]; ar = attach_v_right[i][r]
                model.Add(fr_right[i] == 1).OnlyEnforceIf(ar)  # 背面=左辺→正面=右
                model.Add(fr_left[i]  == 1).OnlyEnforceIf(al)  # 背面=右辺→正面=左
        else:
            # 非カーテン：回転＆壁帯から自動決定（標準ロジック）
            model.Add(fr_right[i] == 1).OnlyEnforceIf([rot[i], left_act[i], right_act[i].Not()])
            model.Add(fr_left[i]  == 1).OnlyEnforceIf([rot[i], right_act[i], left_act[i].Not()])
            model.Add(fr_up[i]    == 1).OnlyEnforceIf([rot[i].Not(), bottom_act[i], top_act[i].Not()])
            model.Add(fr_down[i]  == 1).OnlyEnforceIf([rot[i].Not(), top_act[i], bottom_act[i].Not()])
            model.Add(fr_right[i] == 1).OnlyEnforceIf([rot[i], left_act[i].Not(), right_act[i].Not()])
            model.Add(fr_up[i]    == 1).OnlyEnforceIf([rot[i].Not(), bottom_act[i].Not(), top_act[i].Not()])

    # 正面帯へ他ブース侵入禁止
    for i in range(n):
        for j in range(n):
            if i == j: continue
            # right
            L = model.NewBoolVar(f"frR_L_{i}_{j}")
            R = model.NewBoolVar(f"frR_R_{i}_{j}")
            B = model.NewBoolVar(f"frR_B_{i}_{j}")
            A = model.NewBoolVar(f"frR_A_{i}_{j}")
            model.AddBoolOr([L,R,B,A]).OnlyEnforceIf(fr_right[i])
            model.Add(x[j] + w_eff[j] <= x[i] + w_eff[i]).OnlyEnforceIf(fr_right[i]).OnlyEnforceIf(L)
            model.Add(x[j] >= x[i] + w_eff[i] + front_clear).OnlyEnforceIf(fr_right[i]).OnlyEnforceIf(R)
            model.Add(y[j] + h_eff[j] <= y[i]).OnlyEnforceIf(fr_right[i]).OnlyEnforceIf(B)
            model.Add(y[j] >= y[i] + h_eff[i]).OnlyEnforceIf(fr_right[i]).OnlyEnforceIf(A)
            # left
            L2 = model.NewBoolVar(f"frL_L_{i}_{j}")
            R2 = model.NewBoolVar(f"frL_R_{i}_{j}")
            B2 = model.NewBoolVar(f"frL_B_{i}_{j}")
            A2 = model.NewBoolVar(f"frL_A_{i}_{j}")
            model.AddBoolOr([L2,R2,B2,A2]).OnlyEnforceIf(fr_left[i])
            model.Add(x[j] + w_eff[j] <= x[i] - front_clear).OnlyEnforceIf(fr_left[i]).OnlyEnforceIf(L2)
            model.Add(x[j] >= x[i]).OnlyEnforceIf(fr_left[i]).OnlyEnforceIf(R2)
            model.Add(y[j] + h_eff[j] <= y[i]).OnlyEnforceIf(fr_left[i]).OnlyEnforceIf(B2)
            model.Add(y[j] >= y[i] + h_eff[i]).OnlyEnforceIf(fr_left[i]).OnlyEnforceIf(A2)
            # up
            L3 = model.NewBoolVar(f"frU_L_{i}_{j}")
            R3 = model.NewBoolVar(f"frU_R_{i}_{j}")
            B3 = model.NewBoolVar(f"frU_B_{i}_{j}")
            A3 = model.NewBoolVar(f"frU_A_{i}_{j}")
            model.AddBoolOr([L3,R3,B3,A3]).OnlyEnforceIf(fr_up[i])
            model.Add(x[j] + w_eff[j] <= x[i]).OnlyEnforceIf(fr_up[i]).OnlyEnforceIf(L3)
            model.Add(x[j] >= x[i] + w_eff[i]).OnlyEnforceIf(fr_up[i]).OnlyEnforceIf(R3)
            model.Add(y[j] + h_eff[j] <= y[i] + h_eff[i]).OnlyEnforceIf(fr_up[i]).OnlyEnforceIf(B3)
            model.Add(y[j] >= y[i] + h_eff[i] + front_clear).OnlyEnforceIf(fr_up[i]).OnlyEnforceIf(A3)
            # down
            L4 = model.NewBoolVar(f"frD_L_{i}_{j}")
            R4 = model.NewBoolVar(f"frD_R_{i}_{j}")
            B4 = model.NewBoolVar(f"frD_B_{i}_{j}")
            A4 = model.NewBoolVar(f"frD_A_{i}_{j}")
            model.AddBoolOr([L4,R4,B4,A4]).OnlyEnforceIf(fr_down[i])
            model.Add(x[j] + w_eff[j] <= x[i]).OnlyEnforceIf(fr_down[i]).OnlyEnforceIf(L4)
            model.Add(x[j] >= x[i] + w_eff[i]).OnlyEnforceIf(fr_down[i]).OnlyEnforceIf(R4)
            model.Add(y[j] + h_eff[j] <= y[i] - front_clear).OnlyEnforceIf(fr_down[i]).OnlyEnforceIf(B4)
            model.Add(y[j] >= y[i]).OnlyEnforceIf(fr_down[i]).OnlyEnforceIf(A4)

# ===== ソルブ =====
solver = cp_model.CpSolver()
solver_cfg = config.get("solver", {})
max_time = float(solver_cfg.get("max_time_in_seconds", 30.0))
solver.parameters.max_time_in_seconds = max_time
solver.parameters.num_search_workers = 8
status = solver.Solve(model)
if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    raise RuntimeError(f"Solver status={status}（解が見つかっていません）。placement.csv は書き出しません。")

# ===== 出力（必ず solver.Value を使う）=====
def b01(val): return 1 if int(val) >= 1 else 0

placements = []
for i, b in enumerate(booths):
    xi = solver.Value(x[i]); yi = solver.Value(y[i])
    wi = solver.Value(w_eff[i]); hi = solver.Value(h_eff[i])
    ri = b01(solver.Value(rot[i]))
    # 範囲チェック
    assert 0 <= xi <= room_w and 0 <= yi <= room_h, f"{b['name']} 座標が範囲外"
    assert 1 <= wi <= room_w and 1 <= hi <= room_h, f"{b['name']} サイズが不正"
    assert xi + wi <= room_w and yi + hi <= room_h, f"{b['name']} はみ出し"
    placements.append({
        "id": b["id"], "name": b["name"],
        "x_mm": int(xi), "y_mm": int(yi),
        "width_mm": int(wi), "depth_mm": int(hi),
        "rotated": int(ri)
    })

if os.path.exists("placement.csv"):
    shutil.copyfile("placement.csv", "placement.prev.csv")
with open("placement.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=placements[0].keys())
    writer.writeheader(); writer.writerows(placements)

# ===== SVG描画 =====
scale = 6350 / 1800  # 10mm=1px
dwg = svgwrite.Drawing("layout.svg", size=(room_w/scale, room_h/scale))
dwg.add(dwg.rect(insert=(0,0), size=(room_w/scale, room_h/scale), fill="white", stroke="black"))

# コンセント
def draw_outlet_icon(dwg, ox, oy, room_h_mm, scale_px):
    body_w = 120; body_h = 80; r = 12
    slot_w = 8; slot_h = 24; offset = 30
    px = (ox - body_w/2)/scale_px; py = (room_h_mm - oy - body_h/2)/scale_px
    dwg.add(dwg.rect(insert=(px,py), size=(body_w/scale_px, body_h/scale_px),
                     rx=r/scale_px, ry=r/scale_px, fill="white", stroke="red"))
    for sx in (ox - offset - slot_w/2, ox + offset - slot_w/2):
        dwg.add(dwg.rect(insert=(sx/scale_px, (room_h_mm - oy - slot_h/2)/scale_px),
                         size=(slot_w/scale_px, slot_h/scale_px), fill="red"))

for (ox, oy) in outlets:
    draw_outlet_icon(dwg, ox, oy, room_h, scale)

# ==== レール（線で描画）====
# ※ これをブース描画の「後」に置くと線が上に出て見やすいです
for idx, cr in enumerate(rails_cfg):
    (x1, y1), (x2, y2) = cr["p1"], cr["p2"]
    # SVGはYが上向きなので上下反転
    dwg.add(dwg.line(
        start=(x1 / scale, (room_h - y1) / scale),
        end=(x2 / scale, (room_h - y2) / scale),
        stroke="#0a7a0a",
        stroke_width=6  # 画面上の太さ(px)。必要なら調整
        # stroke_dasharray="6,4",  # 破線にしたい場合はコメント解除
    ))
    # ラベル（任意）
    cx = (x1 + x2) / 2 / scale
    cy = (room_h - (y1 + y2) / 2) / scale
    dwg.add(dwg.text(f"R{idx+1}", insert=(cx, cy - 6),
                     text_anchor="middle", font_size=30,
                     font_family="sans-serif", fill="#0a7a0a"))

# ブース
for p in placements:
    px = p["x_mm"]/scale; py = (room_h - p["y_mm"] - p["depth_mm"])/scale
    pw = p["width_mm"]/scale; ph = p["depth_mm"]/scale
    dwg.add(dwg.rect(insert=(px,py), size=(pw,ph), fill="lightblue", stroke="blue"))
    cx = (p["x_mm"] + p["width_mm"]//2)/scale
    cy = (room_h - (p["y_mm"] + p["depth_mm"]//2))/scale
    dwg.add(dwg.text(p["name"], insert=(cx,cy),
                     text_anchor="middle", dominant_baseline="central",
                     font_size=32, font_family="sans-serif",
                     stroke="white", stroke_width=9, fill="white"))
    dwg.add(dwg.text(p["name"], insert=(cx,cy),
                     text_anchor="middle", dominant_baseline="central",
                     font_size=32, font_family="sans-serif", fill="black"))
    
# ==== 展示禁止エリア（赤半透明）====
for z in no_go:
    rx1, ry1, rx2, ry2 = map(int, z["rect"])
    dwg.add(dwg.rect(
        insert=(rx1/scale, (room_h - ry2)/scale),
        size=((rx2 - rx1)/scale, (ry2 - ry1)/scale),
        fill="#ff0000", fill_opacity=0.18, stroke="#cc0000"
    ))

# ==== 内壁（黒線）====
for idx, w in enumerate(inner_walls):
    (x1, y1), (x2, y2) = w["p1"], w["p2"]
    thick = int(w.get("thickness_mm", 100))
    dwg.add(dwg.line(
        start=(x1/scale, (room_h - y1)/scale),
        end=(x2/scale, (room_h - y2)/scale),
        stroke="#000000",
        stroke_width=max(6, thick/ (scale*5))  # 画面上見やすい太さに
    ))
    # ラベル（任意）
    cx = (x1 + x2) / 2 / scale
    cy = (room_h - (y1 + y2) / 2) / scale
    dwg.add(dwg.text(w.get("name", f"Wall{idx+1}"),
                     insert=(cx, cy - 6),
                     text_anchor="middle", font_size=30,
                     font_family="sans-serif", fill="#000"))

dwg.save()
print("OK: placement.csv / layout.svg を出力しました。status=", status)
