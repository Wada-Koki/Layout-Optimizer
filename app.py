# app.py — 展示レイアウトGUI（Streamlit）
# 使い方:
#   1) 同じフォルダに svg2config.py / layout_optimizer.py を置く
#   2) pip install streamlit ortools svgwrite
#   3) streamlit run app.py

import streamlit as st
from pathlib import Path
import tempfile, shutil, subprocess, datetime, json, re, io, os

APP_DIR = Path(__file__).parent.resolve()

st.set_page_config(page_title="展示レイアウト 最適化GUI", layout="wide")
st.title("展示レイアウト 最適化GUI")

st.markdown("""
- **入力**：booths.csv、会場レイアウト（SVG **または** 既存の config.json）、ブース間隔[min_aisle_mm]、正面スペース[front_clear_mm]  
- **出力**：layout.svg（プレビュー可）、placement.csv、status
""")

backgroundColor = "#999999"

# ---- ファイル入力 ----
col1, col2 = st.columns(2)
with col1:
    booths_file = st.file_uploader("1) booths.csv を選択", type=["csv"])
with col2:
    hall_file   = st.file_uploader("2) 会場レイアウト（SVG または config.json）を選択", type=["svg","json"])

col1, col2 = st.columns(2)
with col1:
    min_aisle_mm = st.number_input("3) ブース間隔 min_aisle_mm", min_value=0, step=100, value=1000)
with col2:
    front_clear_mm = st.number_input("4) 正面スペース front_clear_mm", min_value=0, step=100, value=0)

run_btn = st.button("▶ 変換→最適化を実行", type="primary", use_container_width=True)

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
    if not booths_file or not hall_file:
        st.error("booths.csv と 会場レイアウト（SVG または config.json）の両方を指定してください。")
        st.stop()

    # 作業フォルダ（run_YYYYmmdd_HHMMSS）
    run_dir = APP_DIR / f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 必要スクリプトを複製（そのまま使う）
    for script_name in ("svg2config.py", "layout_optimizer.py"):
        src = APP_DIR / script_name
        if not src.exists():
            st.error(f"{script_name} が見つかりません。app.py と同じフォルダに置いてください。")
            st.stop()
        shutil.copy2(src, run_dir / script_name)

    # 入力ファイルを保存
    booths_path = run_dir / "booths.csv"
    booths_path.write_bytes(booths_file.getvalue())

    # hall: SVG or JSON を受け入れ
    hall_suffix = (hall_file.name.split(".")[-1] or "").lower()
    is_svg = hall_suffix == "svg"
    is_json = hall_suffix == "json"

    if is_svg:
        layout_svg_in = run_dir / "layout.svg"
        layout_svg_in.write_bytes(hall_file.getvalue())
        # color_map.json を作業ディレクトリへ
        color_map_src = APP_DIR / "color_map.json"
        color_map_dst = run_dir / "color_map.json"
        if color_map_src.exists():
            shutil.copy2(color_map_src, color_map_dst)
        else:
            # 最低限のデフォルト（必要に応じて調整）
            default_cmap = {
                "line": {
                    "stroke": {
                    "#009944": "curtain-rail",
                    "#1d2088": "inner-wall"
                    }
                },
                "rect": {
                    "fill": {
                    "#e60012": "no-go"
                    },
                    "stroke": {
                    "#000000": "room" 
                    }
                },
                "circle": {
                    "fill": {
                    "#00a0e9": "outlet"
                    }
                }
            }
            color_map_dst.write_text(json.dumps(default_cmap, ensure_ascii=False, indent=2), encoding="utf-8")
        # SVG → config.json 変換
        with st.status("SVG を config.json に変換中...", expanded=False) as s:
            rc, out, err = _run_py(run_dir / "svg2config.py", run_dir)
            s.update(label="変換完了" if rc == 0 else "変換でエラー", state="complete")
            if rc != 0:
                st.error("svg2config.py の実行に失敗しました。ログを確認してください。")
                st.code(err or out, language="bash")
                st.stop()
            if err:
                st.info("【変換ログ】")
                st.code(err, language="bash")
    else:
        # 既存config.json を採用
        config_json_in = run_dir / "config.json"
        config_json_in.write_bytes(hall_file.getvalue())

    # config.json を開いて min_aisle_mm / front_clear_mm を上書き
    try:
        cfg_path = run_dir / "config.json"
        cfg = _read_json_with_comments(cfg_path)
    except Exception as e:
        st.error(f"config.json の読み込みに失敗しました: {e}")
        # 変換ログがあれば併せて表示
        st.stop()

    # 上書き（必要なキーが無ければ作る）
    if "room" not in cfg: cfg["room"] = {}
    if "requirements" not in cfg: cfg["requirements"] = {}
    cfg["room"]["min_aisle_mm"] = int(min_aisle_mm)
    cfg["requirements"]["front_clear_mm"] = int(front_clear_mm)
    # front_clear_mode は既存値を尊重（無ければ hard にする等はお好みで）
    if "front_clear_mode" not in cfg["requirements"]:
        cfg["requirements"]["front_clear_mode"] = "hard"
    _write_json(cfg_path, cfg)

    # 注意喚起（単位倍率）
    SCALE_NOTE = ""
    try:
        # svg2config が倍率を掛けている可能性があるため軽く注意書き
        room_w = int(cfg["room"]["width_mm"])
        room_h = int(cfg["room"]["depth_mm"])
        SCALE_NOTE = f"（会場 {room_w}×{room_h} mm。※ `svg2config.py` の倍率と booths.csv の単位を一致させてください）"
    except Exception:
        pass

    # 最適化の実行
    st.write("### 最適化を実行中…", SCALE_NOTE)
    rc2, out2, err2 = _run_py(run_dir / "layout_optimizer.py", run_dir)
    status_line = _parse_status(out2 + "\n" + err2)
    st.write(f"**status**: {status_line}")

    if rc2 != 0:
        st.error("最適化スクリプトがエラーで終了しました。ログを確認してください。")
        st.code(err2 or out2, language="bash")
        st.stop()

    # 成果物の取り出し
    layout_svg_path = run_dir / "layout.svg"
    placement_csv_path = run_dir / "placement.csv"

    cols = st.columns(2)
    with cols[0]:
        if layout_svg_path.exists():
            st.subheader("layout.svg プレビュー")
            try:
                svg_text = layout_svg_path.read_text(encoding="utf-8")
                _embed_svg(svg_text)
            except Exception:
                st.info("SVG の埋め込み表示に失敗しました。ダウンロードからご確認ください。")
        else:
            st.warning("layout.svg が見つかりませんでした。")

    with cols[1]:
        st.subheader("ダウンロード")
        if layout_svg_path.exists():
            st.download_button("layout.svg をダウンロード", data=layout_svg_path.read_bytes(),
                               file_name="layout.svg", mime="image/svg+xml", use_container_width=True)
        if placement_csv_path.exists():
            st.download_button("placement.csv をダウンロード", data=placement_csv_path.read_bytes(),
                               file_name="placement.csv", mime="text/csv", use_container_width=True)

    # ログ表示
    with st.expander("実行ログを表示"):
        st.code(out2, language="bash")
        if err2:
            st.code(err2, language="bash")

    st.success(f"完了: {run_dir}")
