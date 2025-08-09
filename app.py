# app.py — 展示レイアウト 最適化GUI（モダンUIスキン）
# 依存: streamlit, svgwrite, ortools, protobuf>=5.26,<6
# 実行: python -m streamlit run app.py

import streamlit as st
from pathlib import Path
import subprocess, json, re, shutil, datetime, os

APP_DIR = Path(__file__).parent.resolve()
st.set_page_config(page_title="展示レイアウト 最適化GUI", layout="wide")

# ====== スキン（CSS） =========================================================
SKIN = """
<style>
:root{
  --bg:#0f1115; --panel:#151821; --panel2:#11131a; --text:#e9ecf1; --muted:#a8b0c0;
  --brand:#6ae3ff; --brand2:#4cc9f0; --accent:#8ef6c8; --stroke:#2a3040;
}
html,body{background:radial-gradient(1200px 600px at 70% -10%, rgba(76,201,240,.08), transparent),
          radial-gradient(1000px 500px at -10% 20%, rgba(138,255,212,.07), transparent), var(--bg) !important;}
/* Streamlit既定の余白・ヘッダ調整 */
header { background: transparent !important; }
section.main > div { padding-top: 1rem; }

/* タイトル */
h1, h2, h3, h4, h5, h6 { letter-spacing: .2px; }

/* カード風 */
.st-card {
  border:1px solid var(--stroke);
  border-radius:16px;
  background:linear-gradient(180deg, var(--panel), var(--panel2));
  box-shadow: 0 10px 30px rgba(0,0,0,.35);
  padding: 14px 16px 10px;
  margin-bottom: 14px;
}
.st-card .hd {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding: 6px 2px 10px; border-bottom:1px solid var(--stroke);
}
.st-card .hd h3 { margin:0; font-size:14px; color:#cfe6ff; text-transform:uppercase; letter-spacing:.12em; }
.st-card .bd { padding: 12px 2px; }
.st-card .ft { padding: 10px 2px; border-top:1px solid var(--stroke); }

/* バッジ・チップ */
.badge { display:inline-flex; gap:8px; padding:6px 10px; border:1px solid var(--stroke); border-radius:999px; background:#101421; color:#d9e6ff; font-size:12px; }
.chips { display:flex; gap:8px; flex-wrap:wrap; }
.chip { background:#0f1422; border:1px solid var(--stroke); border-radius:999px; padding:6px 10px; font-size:12px; color:#c0c7d6; }

/* タイムライン */
.step { display:flex; gap:10px; align-items:flex-start; padding:10px 12px; background:#0f1422; border:1px solid var(--stroke); border-radius:12px; margin-bottom:10px; }
.dot { width:10px; height:10px; border-radius:50%; margin-top:6px; }
.ok .dot { background:#70e000; } .warn .dot { background:#ffd166; } .err .dot { background:#ff6b6b; }
.step .title { font-weight:600; font-size:13px; margin:0 0 2px 0; }
.step .desc { margin:0; color:#9db0cc; font-size:12px; }

/* SVGプレビュー枠 */
.preview { background:#0b0e14; border:1px dashed #2b3142; border-radius:14px; padding:10px; min-height:360px; display:flex; align-items:center; justify-content:center; }
.placeholder { font-size:13px; color:#98a3b8; text-align:center; }

/* テキスト色 */
html, body, [class^="css"] { color: var(--text); }
</style>
"""
st.markdown(SKIN, unsafe_allow_html=True)

# ====== ヘルパ ===============================================================
def _read_json_with_comments(p: Path):
    txt = p.read_text(encoding="utf-8")
    txt = re.sub(r"/\\*.*?\\*/", "", txt, flags=re.S)
    txt = re.sub(r"(?m)//.*$", "", txt)
    return json.loads(txt)

def _write_json(p: Path, data: dict):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _run_py(script: Path, cwd: Path):
    proc = subprocess.run([os.sys.executable, str(script.name)], cwd=str(cwd), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr

def _embed_svg(svg_text: str, height: int = 600):
    # objectタグで埋め込み（縦横100%）
    html = f'''
      <div class="preview" style="min-height:{height}px">
        <object type="image/svg+xml" data='data:image/svg+xml;utf8,{svg_text.replace("'", "&apos;")}' style="width:100%; height:100%"></object>
      </div>
    '''
    st.components.v1.html(html, height=height + 40, scrolling=True)

# ====== ヘッダ ================================================================
left, right = st.columns([1, 1], gap="small")
with left:
    st.markdown(
        """
        <div class="st-card">
          <div class="hd"><h3>Exhibit Layout Optimizer</h3><div class="chips"><span class="chip">booths.csv</span><span class="chip">hall.svg / config.json</span></div></div>
          <div class="bd">
            <span class="badge">v1.0 · Modern UI</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ====== 上段：入力 / ステータス ==============================================
c1, c2 = st.columns([1.2, 1], gap="large")

with c1:
    st.markdown('<div class="st-card"><div class="hd"><h3>Inputs</h3></div><div class="bd">', unsafe_allow_html=True)

    colA, colB = st.columns(2, gap="small")
    with colA:
        booths_file = st.file_uploader("1) Booths CSV", type=["csv"], key="booths_csv")
    with colB:
        hall_file = st.file_uploader("2) Hall Layout（SVG または config.json）", type=["svg", "json"], key="hall_file")

    colC, colD = st.columns(2, gap="small")
    with colC:
        min_aisle_mm = st.number_input("3) ブース間隔（min_aisle_mm）", min_value=0, step=100, value=1000)
    with colD:
        front_clear_mm = st.number_input("4) 正面スペース（front_clear_mm）", min_value=0, step=100, value=0)

    st.markdown('</div><div class="ft">', unsafe_allow_html=True)
    run_btn = st.button("▶ 変換 → 最適化を実行", use_container_width=True, type="primary")
    st.markdown('</div></div>', unsafe_allow_html=True)

with c2:
    st.markdown('<div class="st-card"><div class="hd"><h3>Status</h3><span class="chip">現在: 待機中</span></div><div class="bd">', unsafe_allow_html=True)
    status_area = st.empty()
    st.markdown('</div></div>', unsafe_allow_html=True)

# ====== 実行ロジック ==========================================================
def run_pipeline(booths_file, hall_file, min_aisle_mm: int, front_clear_mm: int):
    if not booths_file or not hall_file:
        return None, None, "入力不足（booths.csv と会場レイアウトが必要です）", None, None

    run_dir = APP_DIR / f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # スクリプトを複製
    for script_name in ("svg2config.py", "layout_optimizer.py"):
        src = APP_DIR / script_name
        if not src.exists():
            return None, None, f"{script_name} が見つかりません。", None, None
        shutil.copy2(src, run_dir / script_name)

    # 入力保存
    booths_path = run_dir / "booths.csv"
    booths_path.write_bytes(booths_file.getvalue())

    hall_suffix = (hall_file.name.split(".")[-1] or "").lower()
    is_svg = hall_suffix == "svg"
    is_json = hall_suffix == "json"

    # color_map.json を run_dir へ
    cmap_src = APP_DIR / "color_map.json"
    cmap_dst = run_dir / "color_map.json"
    if cmap_src.exists():
        shutil.copy2(cmap_src, cmap_dst)

    # 1) 変換
    if is_svg:
        (run_dir / "layout.svg").write_bytes(hall_file.getvalue())
        rc, out, err = _run_py(run_dir / "svg2config.py", run_dir)
        if rc != 0:
            return None, None, "SVG→config.json 変換に失敗", out, err
    else:
        (run_dir / "config.json").write_bytes(hall_file.getvalue())

    # 2) config 上書き
    cfg_path = run_dir / "config.json"
    try:
        cfg = _read_json_with_comments(cfg_path)
    except Exception as e:
        return None, None, f"config.json 読込失敗: {e}", None, None

    cfg.setdefault("room", {})["min_aisle_mm"] = int(min_aisle_mm)
    cfg.setdefault("requirements", {})["front_clear_mm"] = int(front_clear_mm)
    if "front_clear_mode" not in cfg["requirements"]:
        cfg["requirements"]["front_clear_mode"] = "hard"
    _write_json(cfg_path, cfg)

    # レール未定義ならフォールバック
    rails = cfg.get("infrastructure", {}).get("curtain_rails", [])
    if not rails:
        cfg["requirements"]["curtain_rail_mode"] = "none"
        _write_json(cfg_path, cfg)

    # 3) 最適化
    rc2, out2, err2 = _run_py(run_dir / "layout_optimizer.py", run_dir)
    status_line = "status: 不明"
    for line in (out2 + "\n" + (err2 or "")).splitlines():
        if "status" in line.lower():
            status_line = line.strip()
            break

    layout_svg_path = run_dir / "layout.svg"
    placement_csv_path = run_dir / "placement.csv"
    svg_text = layout_svg_path.read_text(encoding="utf-8") if layout_svg_path.exists() else None
    csv_bytes = placement_csv_path.read_bytes() if placement_csv_path.exists() else None

    return svg_text, csv_bytes, status_line, out2, err2

# ====== 実行・出力UI ==========================================================
if run_btn:
    with st.spinner("処理中..."):
        svg_text, csv_bytes, status_line, out_log, err_log = run_pipeline(booths_file, hall_file, min_aisle_mm, front_clear_mm)

    # ステータス描画
    if "OPTIMAL" in (status_line or "").upper() or "FEASIBLE" in (status_line or "").upper():
        status_html = f"""
        <div class="step ok"><div class="dot"></div><div class="body">
          <p class="title">{status_line}</p><p class="desc">最適化は正常終了しました。</p></div></div>"""
    elif "INFEASIBLE" in (status_line or "").upper():
        status_html = f"""
        <div class="step err"><div class="dot"></div><div class="body">
          <p class="title">{status_line}</p><p class="desc">制約が厳しすぎる可能性があります。</p></div></div>"""
    else:
        status_html = f"""
        <div class="step warn"><div class="dot"></div><div class="body">
          <p class="title">{status_line}</p><p class="desc">ログを確認してください。</p></div></div>"""
    status_area.markdown(status_html, unsafe_allow_html=True)

    # 結果カード
    st.markdown('<div class="st-card"><div class="hd"><h3>Results</h3></div><div class="bd">', unsafe_allow_html=True)
    r1, r2 = st.columns([1.2, 1], gap="large")
    with r1:
        st.caption("layout.svg プレビュー")
        if svg_text:
            _embed_svg(svg_text, height=640)
        else:
            st.markdown('<div class="preview"><div class="placeholder">layout.svg がありません</div></div>', unsafe_allow_html=True)
    with r2:
        st.caption("出力ファイル")
        colx, coly = st.columns(2, gap="small")
        with colx:
            if svg_text:
                st.download_button("layout.svg をダウンロード", data=svg_text, file_name="layout.svg", mime="image/svg+xml", use_container_width=True)
        with coly:
            if csv_bytes:
                st.download_button("placement.csv をダウンロード", data=csv_bytes, file_name="placement.csv", mime="text/csv", use_container_width=True)

        st.caption("ログ")
        with st.expander("標準出力"):
            if out_log: st.code(out_log, language="bash")
        with st.expander("標準エラー"):
            if err_log: st.code(err_log, language="bash")
    st.markdown('</div></div>', unsafe_allow_html=True)

# ====== フッタ ================================================================
st.markdown(
    '<div style="margin-top:18px; color:#a8b0c0; font-size:12px;">© 2025 Exhibit Layout Optimizer</div>',
    unsafe_allow_html=True
)
