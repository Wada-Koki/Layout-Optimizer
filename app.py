# app.py — 展示レイアウトGUI（Streamlit）
# 使い方:
#   1) 同じフォルダに svg2config.py / layout_optimizer.py を置く
#   2) pip install streamlit ortools svgwrite
#   3) streamlit run app.py

import streamlit as st
from pathlib import Path
import tempfile, shutil, subprocess, datetime, json, re, io, os
from pathlib import Path

APP_DIR = Path(__file__).parent  # app.py があるフォルダ
icon_path = APP_DIR / "favicon.png"  # 例: repo内 static/favicon.png

if icon_path.exists():
    st.set_page_config(page_title="Layout Optimizer", page_icon=str(icon_path))
else:
    # 画像が無い環境でも落ちないようにフォールバック
    st.set_page_config(page_title="Layout Optimizer", page_icon="🧩")

APP_DIR = Path(__file__).parent.resolve()

# 結果の永続化（ダウンロードでの再実行対策）
if "result" not in st.session_state:
    st.session_state.result = None

# 進捗ラベル用の小さなCSSスピナー
st.markdown("""
<style>
.pb-label{display:flex;align-items:center;gap:8px;color:#cfe6ff;font-size:13px;margin:6px 0;}
.pb-spin{width:14px;height:14px;border:2px solid #6ae3ff;border-right-color:transparent;border-radius:50%;
         display:inline-block;animation:pb-rot .8s linear infinite;}
@keyframes pb-rot{to{transform:rotate(360deg)}}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>
.main .block-container { max-width: 960px; margin: 0 auto; }
</style>
""", unsafe_allow_html=True)
    
# === PATCH2: 進捗ユーティリティ（スピナーとバーを別プレースホルダで管理） ===

class Progress:
    def __init__(self, zone=None):
        z = zone or st.container()
        cols = z.columns([0.08, 0.92])
        self.spin = cols[0].empty()
        self.text = cols[1].empty()
        self.bar_ph = z.empty()
        self.bar = None
        self.active = False

    def start(self, msg="準備中…"):
        self.spin.markdown("<span class='pb-spin'></span>", unsafe_allow_html=True)
        self.text.markdown(f"<div class='pb-label' style='margin:0'>{msg}</div>", unsafe_allow_html=True)
        self.bar = self.bar_ph.progress(0, text=msg)
        self.active = True

    def update(self, v:int, msg:str):
        if not self.active:
            self.start(msg)
        self.text.markdown(f"<div class='pb-label' style='margin:0'>{msg}</div>", unsafe_allow_html=True)
        self.bar.progress(v, text=msg)

    def finish(self, msg="完了", hide_bar=False):
        if self.active and self.bar is not None:
            self.bar.progress(100, text=msg)
        # スピナーだけ消す
        self.spin.empty()
        self.text.markdown(f"<div class='pb-label' style='margin:0'>{msg}</div>", unsafe_allow_html=True)
        if hide_bar:
            self.bar_ph.empty()
        self.active = False

st.markdown("<h1 style='text-align:center;'><span>展示レイアウト</span><span>最適化</span></h1>", unsafe_allow_html=True)

# ---- ファイル入力 ----
col1, col2 = st.columns(2)
with col1:
    booths_file = st.file_uploader("展示希望を選択 (CSV)", type=["csv"])
with col2:
    hall_file   = st.file_uploader("会場レイアウトを選択 (SVG または JSON)", type=["svg","json"])
    
if booths_file:
    st.session_state.booths_bytes = booths_file.getvalue()
    st.session_state.booths_name  = booths_file.name
if hall_file:
    st.session_state.hall_bytes = hall_file.getvalue()
    st.session_state.hall_name  = hall_file.name

col1, col2 = st.columns(2)
with col1:
    min_aisle_mm = st.number_input("ブース間隔[mm]", min_value=0, step=100, value=1000, help="ブースとブースの間の最低距離です。[min_aisle_mm]")
with col2:
    front_clear_mm = st.number_input("展示正面の確保距離[mm] ", min_value=0, step=100, value=0, help="ブース前に空ける通行・鑑賞スペースの距離です。[front_clear_mm]")
    
# ── 追加: requirements / weights のUI ─────────────────────────
with st.expander("高度な設定", expanded=False):
    st.subheader("制約")
    r1, r2 = st.columns(2)
    with r1:
        curtain_rail_mode = st.selectbox(
            "カーテンレールの使い方", ["if_wanted", "all", "none"], index=0,
            help="カーテン必須ブースの“背面”をレールに密着させるかの方針です。 希望ブースのみ必須（推奨）：if_wanted / 全ブース必須：all / 無視：none [curtain_rail_mode]"
        )
        front_clear_mode = st.selectbox("正面の確保の厳しさ", ["hard", "soft"], index=0, help="正面スペースの確保の優先度を設定します。必須：hard / なるべく：soft [front_clear_mode]")
        wall_contact_prefer = st.checkbox("壁沿い配置を優先", True, help="可能な限りブースを壁にぴったり付けるようにします。[wall_contact_prefer]")
        wall_contact_default_hard = st.checkbox("壁沿いを基本ルールにする", True, help="特に指定がないブースも原則“壁付け”にします（やや厳しめ）。[wall_contact_default_hard]")
        wall_contact_hard = st.checkbox("壁沿いを厳密に判定する", False, help="ブースを厳格に壁沿いに配置します。満たせないと配置不可になる可能性があります（かなり厳しめ）。[wall_contact_hard]")
    with r2:
        outlet_demand_hard_radius_mm = st.number_input("コンセント必須距離 [mm]", 0, 1_000_000, 0, step=100, help="コンセント希望ブースは、この半径以内にコンセントが必須。[outlet_demand_hard_radius_mm]")
        outlet_reserve_radius_mm = st.number_input("コンセント予約帯 [mm]", 0, 1_000_000, 0, step=100, help="この半径内は希望者を優先配置（非希望者は入りづらく）。[outlet_reserve_radius_mm]")
        inner_walls_count_as_wall_contact = st.checkbox("内壁も『壁沿い』として扱う", True, help="内壁に密着しても壁沿い扱いにします。[inner_walls_count_as_wall_contact]")
        enforce_outer_wall_band = st.checkbox("外壁帯に必ず触れる", False, help="外周から一定幅の帯に必ず接触させます（解が出にくい場合あり）。[enforce_outer_wall_band]")

    st.subheader("重み")
    w1, w2 = st.columns(2)
    with w1:
        compactness = st.number_input("全体のまとまり度合い", 0.0, 1_000_000.0, 3000.0, step=100.0, help="大きいほどブース群をコンパクトに集めます。[compactness]")
        wall_contact_bonus = st.number_input("壁沿いの度合い", 0.0, 1_000_000.0, 500.0, step=50.0, help="大きいほど壁に沿いやすくなります[wall_contact_bonus]")
        curtain_rail_match = st.number_input("レール一致度合い", 0.0, 1_000_000.0, 1.0, step=0.1, help="大きいほどバナーのレールに沿いやすくなります。[curtain_rail_match]")
    with w2:
        outlet_distance = st.number_input("コンセント接近度合い", 0.0, 1_000_000.0, 1.0, step=0.1, help="大きいほど希望者をコンセント近くへ配置します。[outlet_distance]")
        outlet_repel_non_wanter = st.number_input("非希望者のコンセント距離", 0.0, 1_000_000.0, 0.0, step=0.1, help="大きいほどコンセント不要ブースがコンセント付近を占有しないようにします。[outlet_repel_non_wanter]")
        preferred_area_bonus = st.number_input("希望エリア配置度合い", 0.0, 1_000_000.0, 1000.0, step=10.0, help="大きいほどブースを希望エリア内に配置しやすくなります。[preferred_area_bonus]")
        
    st.subheader("ソルバー")
    max_time_s = st.number_input(
        "最大計算時間 [秒]",
        min_value=1, max_value=3600, value=30, step=5,
        help="OR-Tools CP-SAT の最大実行時間。時間内で最良解を返します。"
    )
    solver_ui = {"max_time_in_seconds": float(max_time_s)}

    # 実行時に使う辞書（グローバルにせず、この下の if run_btn: で参照）
    req_ui = {
        "curtain_rail_mode": curtain_rail_mode,
        "wall_contact_prefer": bool(wall_contact_prefer),
        "wall_contact_default_hard": bool(wall_contact_default_hard),
        "wall_contact_hard": bool(wall_contact_hard),
        "inner_walls_count_as_wall_contact": bool(inner_walls_count_as_wall_contact),
        "enforce_outer_wall_band": bool(enforce_outer_wall_band),
        "front_clear_mm": int(front_clear_mm),  # 既存入力も反映
        "front_clear_mode": front_clear_mode,
        "outlet_demand_hard_radius_mm": int(outlet_demand_hard_radius_mm),
        "outlet_reserve_radius_mm": int(outlet_reserve_radius_mm),
    }
    weights_ui = {
        "compactness": float(compactness),
        "wall_contact_bonus": float(wall_contact_bonus),
        "outlet_distance": float(outlet_distance),
        "curtain_rail_match": float(curtain_rail_match),
        "outlet_repel_non_wanter": float(outlet_repel_non_wanter),
        "preferred_area_bonus": float(preferred_area_bonus),
    }
# ─────────────────────────────────────────────────────────────

# 最初は非表示：押されたら中身を入れる
# pb_label_ph = st.empty()
# pb_bar_ph   = st.empty()

run_btn = st.button("▶ 実行", type="primary", use_container_width=True)

# 進捗表示専用ゾーン（ここ“だけ”にスピナー＆バーを出す）
progress_zone = st.container()

# # 単一の状態で管理（ここ以外で progress/spinner を作らない）
# if "prog" not in st.session_state:
#     st.session_state.prog = {
#         "zone": progress_zone,
#         "spin_ph": progress_zone.empty(),   # ← ゾーンの子として作る
#         "text_ph": progress_zone.empty(),
#         "bar_ph":  progress_zone.empty(),
#         "bar": None,
#         "active": False
#     }
# else:
#     # rerun のたびに最新のゾーンを参照（列の再構成対策）
#     st.session_state.prog["zone"] = progress_zone

# # >>> PROGRESS PATCH: 初期化（実行直前で）
# pbar = st.progress(0, text="準備中…")
# def _p(v, msg=""):
#     try:
#         pbar.progress(v, text=msg)
#     except Exception:
#         pass
# # <<< PROGRESS PATCH

# CSS はそのままでOK（.pb-spin / .pb-label 定義済み前提）

log_box = st.empty()

def _read_json_with_comments(p: Path):
    txt = p.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"(?m)//.*$", "", txt)
    return json.loads(txt)

def _write_json(p: Path, data: dict):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _embed_svg(svg_text: str):
    # Streamlit でSVGを直接埋め込む（PNG変換なし）
    st.markdown(f"""
    <div style="border:1px solid #ddd; padding:4px; overflow:auto; max-height:75vh">
      {svg_text}
    </div>
    """, unsafe_allow_html=True)

def _run_py(script: Path, cwd: Path):
    """script を cwd で実行し、(returncode, stdout, stderr) を返す"""
    proc = subprocess.run(
        [os.sys.executable, str(script.name)],
        cwd=str(cwd),
        capture_output=True,
        text=True
    )
    return proc.returncode, proc.stdout, proc.stderr

def _parse_status(text: str) -> str:
    # layout_optimizer の出力から status 行を拾う
    for line in text.splitlines():
        if "status" in line.lower():
            return line.strip()
    return "status: (未取得)"

if run_btn:
    try:
        prog = None
        # ▼ まずはセッションから取り出す（アップローダーが None でも利用可能に）
        booths_bytes = st.session_state.get("booths_bytes")
        booths_name  = st.session_state.get("booths_name") or ""
        hall_bytes   = st.session_state.get("hall_bytes")
        hall_name    = st.session_state.get("hall_name") or ""

        if not booths_bytes or not hall_bytes:
            st.error("booths.csv と 会場レイアウト（SVG または config.json）の両方を指定してください。")
            st.stop()

        prog = Progress(progress_zone)
        prog.start("準備中…")
        prog.update(5, "入力を確認中…")

        # 作業フォルダ（run_YYYYmmdd_HHMMSS）
        run_dir = APP_DIR / f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)

        # 必要スクリプトを複製（そのまま使う）
        for script_name in ("svg2config.py", "layout_optimizer.py"):
            src = APP_DIR / script_name
            if not src.exists():
                if prog: prog.finish("エラーで停止", hide_bar=True)
                st.error(f"{script_name} が見つかりません。app.py と同じフォルダに置いてください。")
                st.stop()
            shutil.copy2(src, run_dir / script_name)

        # 入力ファイルを保存
        booths_path = run_dir / "booths.csv"
        booths_path.write_bytes(booths_bytes)

        # hall: SVG or JSON を受け入れ
        hall_suffix = (hall_name.split(".")[-1] or "").lower()
        is_svg = hall_suffix == "svg"
        is_json = hall_suffix == "json"
        
        # ▼ ここで1回だけ出す（最初は非表示）
        st.session_state.result = None  # 前回の表示をクリア

        if is_svg:
            layout_svg_in = run_dir / "layout.svg"
            layout_svg_in.write_bytes(hall_bytes)
            # color_map.json を作業ディレクトリへ
            color_map_src = APP_DIR / "color_map.json"
            color_map_dst = run_dir / "color_map.json"
            if color_map_src.exists():
                shutil.copy2(color_map_src, color_map_dst)
            else:
                # 最低限のデフォルト（必要に応じて調整）
                default_cmap = {
                    "line": {"stroke": {"#009944": "curtain-rail", "#1d2088": "inner-wall"}},
                    "rect": {"fill": {"#e60012": "no-go"}, "stroke": {"#000000": "room"}},
                    "circle": {"fill": {"#00a0e9": "outlet"}}
                }
                color_map_dst.write_text(json.dumps(default_cmap, ensure_ascii=False, indent=2), encoding="utf-8")

            # 進捗更新（置換ポイント②）
            prog.update(20, "SVG を解析中…")

            # エラー時のみステータス枠を出すためのプレースホルダ
            status_ph = st.empty()

            rc, out, err = _run_py(run_dir / "svg2config.py", run_dir)

            if rc != 0:
                # ✳ エラー時だけステータスUIを描画
                with status_ph.status("SVG を config.json に変換中...", expanded=True) as s:
                    s.update(label="変換でエラー", state="error")
                    if prog: prog.finish("エラーで停止", hide_bar=True)
                    st.error("svg2config.py の実行に失敗しました。ログを確認してください。")
                    st.code(err or out, language="bash")
                    st.stop()
            # 正常時は何も描画しない（枠ごと非表示）

            # 進捗更新（置換ポイント③）
            prog.update(40, "config.json を読み込み中…")

        else:
            # 既存config.json を採用
            config_json_in = run_dir / "config.json"
            config_json_in.write_bytes(hall_bytes)

            # 進捗更新（置換ポイント④）
            prog.update(50, "パラメータを反映中…")
            


        # config.json を開いて min_aisle_mm / front_clear_mm を上書き
        try:
            cfg_path = run_dir / "config.json"
            cfg = _read_json_with_comments(cfg_path)
        except Exception as e:
            if prog: prog.finish("エラーで停止", hide_bar=True)
            st.error(f"config.json の読み込みに失敗しました: {e}")
            # 変換ログがあれば併せて表示
            st.stop()

            # 上書き（必要なキーが無ければ作る）
        if "room" not in cfg: cfg["room"] = {}
        if "requirements" not in cfg: cfg["requirements"] = {}
        cfg["room"]["min_aisle_mm"] = int(min_aisle_mm)
        cfg["requirements"]["front_clear_mm"] = int(front_clear_mm)
        # front_clear_mode は既存値を尊重（無ければ hard）
        if "front_clear_mode" not in cfg["requirements"]:
            cfg["requirements"]["front_clear_mode"] = "hard"

        # >>> PATCH(2): UI の requirements / weights を反映（あれば）
        #   ※ req_ui / weights_ui は Inputs 側の expander で作った辞書を想定
        #   ※ もし別スコープなら st.session_state["req_ui"] 等から拾ってください
        
        # solver パラメータの反映
        cfg.setdefault("solver", {})
        if "solver_ui" in locals() and isinstance(solver_ui, dict):
            cfg["solver"].update(solver_ui)
        
        try:
            if "req_ui" in locals() and isinstance(req_ui, dict):
                cfg.setdefault("requirements", {}).update(req_ui)
            elif hasattr(st, "session_state") and isinstance(st.session_state.get("req_ui"), dict):
                cfg.setdefault("requirements", {}).update(st.session_state["req_ui"])
        except Exception:
            pass

        try:
            if "weights_ui" in locals() and isinstance(weights_ui, dict):
                cfg.setdefault("weights", {}).update(weights_ui)
            elif hasattr(st, "session_state") and isinstance(st.session_state.get("weights_ui"), dict):
                cfg.setdefault("weights", {}).update(st.session_state["weights_ui"])
        except Exception:
            pass

        # レール未定義なら安全側にフォールバック（解なし予防）
        rails = cfg.get("infrastructure", {}).get("curtain_rails", [])
        if not rails and cfg["requirements"].get("curtain_rail_mode") not in ("none", None):
            cfg["requirements"]["curtain_rail_mode"] = "none"
        # <<< PATCH(2) ここまで

        _write_json(cfg_path, cfg)

        # >>> PROGRESS PATCH
        # _p(70, "最適化の準備中…")
        # <<< PROGRESS PATCH

        # 注意喚起（単位倍率）
        # SCALE_NOTE = ""
        # try:
        #     # svg2config が倍率を掛けている可能性があるため軽く注意書き
        #     room_w = int(cfg["room"]["width_mm"])
        #     room_h = int(cfg["room"]["depth_mm"])
        #     SCALE_NOTE = f"（会場 {room_w}×{room_h} mm。※ `svg2config.py` の倍率と booths.csv の単位を一致させてください）"
        # except Exception:
        #     pass

        # 最適化の実行
        # st.write("### 最適化を実行中…")
        
        # >>> PROGRESS PATCH
        # _p(80, "最適化を実行中…")
        # <<< PROGRESS PATCH
        
        prog.update(70, f"最適化を実行中…（最大 {int(max_time_s)} 秒）")
        
        rc2, out2, err2 = _run_py(run_dir / "layout_optimizer.py", run_dir)
        status_line = _parse_status(out2 + "\n" + err2)
        st.write(f"**status**: {status_line}")
        
        # >>> PROGRESS PATCH
        # _p(100, "完了")
        # 進捗バーを消したい場合は：
        # pbar.empty()
        # <<< PROGRESS PATCH

        if rc2 != 0:
            if prog: prog.finish("エラーで停止", hide_bar=True)
            st.error("最適化スクリプトがエラーで終了しました。ログを確認してください。")
            st.code(err2 or out2, language="bash")
            st.stop()

        # 成果物の取り出し
        layout_svg_path = run_dir / "layout.svg"
        placement_csv_path = run_dir / "placement.csv"
        
        # ★★★ 追加：結果を session_state に保存（テキスト/バイト両方）
        res = {
            "status": status_line,
            "svg_text": layout_svg_path.read_text(encoding="utf-8") if layout_svg_path.exists() else None,
            "svg_bytes": layout_svg_path.read_bytes() if layout_svg_path.exists() else None,
            "csv_bytes": placement_csv_path.read_bytes() if placement_csv_path.exists() else None,
            "run_dir": str(run_dir),
        }
        st.session_state.result = res
        
        if prog: prog.finish("完了")
        st.success(f"完了: {res['run_dir']}")
        
    except Exception as e:
        if 'prog' in locals() and prog:
            if prog: prog.finish("エラーで停止", hide_bar=True)
        st.exception(e)

# === 共通: 結果の描画（ダウンロードでの再実行でも毎回出す） ===
res = st.session_state.result
if res:
    cols = st.columns(2)
    with cols[0]:
        st.subheader("layout.svg プレビュー")
        if res["svg_text"]:
            _embed_svg(res["svg_text"])
        else:
            st.warning("layout.svg がありません。")

    with cols[1]:
        st.subheader("ダウンロード")
        if res["svg_bytes"]:
            st.download_button("layout.svg をダウンロード",
                               data=res["svg_bytes"],
                               file_name="layout.svg",
                               mime="image/svg+xml",
                               use_container_width=True)
        if res["csv_bytes"]:
            st.download_button("placement.csv をダウンロード",
                               data=res["csv_bytes"],
                               file_name="placement.csv",
                               mime="text/csv",
                               use_container_width=True)

    # （任意）ステータスの再掲など
    if res.get("status"):
        st.caption(res["status"])
        
