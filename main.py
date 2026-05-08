import os
import re
import json
import subprocess
import tempfile
import socket
import urllib.request
import urllib.parse
import unicodedata
import time
from datetime import timedelta
from typing import List, Tuple, Optional, Dict, Any

from google.cloud import storage
from pdf2image import convert_from_path

# 追加: MySQL疎通チェック用（軽量・純Python）
try:
    import pymysql
except Exception:
    pymysql = None


# =============================
# Env config (Cloud Run Jobs)
# =============================
INPUT_GS = os.environ.get("INPUT_GS", "").strip()
OUTPUT_GS = os.environ.get("OUTPUT_GS", "").strip()
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "converted").strip()

TARGET_W = int(os.environ.get("TARGET_W", "3307"))
TARGET_H = int(os.environ.get("TARGET_H", "4677"))
USE_CROPBOX = os.environ.get("USE_CROPBOX", "true").lower() in ("1", "true", "yes", "y")
THREAD_COUNT = int(os.environ.get("THREAD_COUNT", "1"))
GS_DPI = int(os.environ.get("GS_DPI", "400"))
NUMBER_FORMAT = os.environ.get("NUMBER_FORMAT", "03d")

MYSQL_CHECK = os.environ.get("MYSQL_CHECK", "true").lower() in ("1", "true", "yes", "y")
MYSQL_HOST = os.environ.get("MYSQL_HOST", "10.146.0.2").strip()
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "IsplitAdmin").strip()
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("MYSQL_DB", "").strip()
MYSQL_CONNECT_TIMEOUT = int(os.environ.get("MYSQL_CONNECT_TIMEOUT", "3"))

UPLOAD_FILE_KEYS_RAW = os.environ.get("UPLOAD_FILE_KEYS", "").strip()

AI_CASE_ID = os.environ.get("AI_CASE_ID", "").strip()
ANALYGENT_PORT = int(os.environ.get("ANALYGENT_PORT", "8056"))

# Azure OCR
AZURE_KEY = os.environ.get("AZURE_KEY", "").strip()
AZURE_ENDPOINT = os.environ.get("AZURE_ENDPOINT", "").strip()

# ページ分類オプション（リクエスト経由でセット）
READ_SGA = os.environ.get("READ_SGA", "false").lower() in ("1", "true", "yes", "y")
READ_MCR = os.environ.get("READ_MCR", "false").lower() in ("1", "true", "yes", "y")

# デプロイ確認用（ログに出す）
CODE_VERSION = "2026-01-30-main-patched-v2"


# =============================
# Helpers
# =============================
def parse_gs_uri(gs_uri: str) -> Tuple[str, str]:
    m = re.match(r"^gs://([^/]+)/(.+)$", gs_uri)
    if not m:
        raise ValueError("gs://bucket/object の形式で指定してください")
    return m.group(1), m.group(2)


def split_gs_uri_allow_empty_object(gs_uri: str) -> Tuple[str, str]:
    """
    OUTPUT_GS で gs://bucket/dir/ のように末尾 / を許容したい場合に使う。
    - gs://bucket/dir/  -> bucket, dir/
    - gs://bucket/prefix- -> bucket, prefix-
    """
    m = re.match(r"^gs://([^/]+)/(.*)$", gs_uri)
    if not m:
        raise ValueError("OUTPUT_GS は gs://bucket/... の形式で指定してください")
    return m.group(1), m.group(2)


def input_pdf_basename_no_ext(obj_path: str) -> str:
    name = obj_path.rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf"):
        return name[:-4]
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name


def format_index(i: int) -> str:
    try:
        return format(i, NUMBER_FORMAT)
    except Exception:
        return f"{i:03d}"


def run_ghostscript_normalize(in_pdf: str, out_pdf: str) -> None:
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        "-dNOPAUSE",
        "-dBATCH",
        "-dSAFER",

        "-dFIXEDMEDIA",
        "-sPAPERSIZE=a4",
        "-dPDFFitPage",

        "-dAutoRotatePages=/None",
        "-dDetectDuplicateImages=true",

        "-dDownsampleColorImages=true",
        "-dColorImageDownsampleType=/Average",
        f"-dColorImageResolution={GS_DPI}",

        "-dDownsampleGrayImages=true",
        "-dGrayImageDownsampleType=/Average",
        f"-dGrayImageResolution={GS_DPI}",

        "-dDownsampleMonoImages=true",
        "-dMonoImageDownsampleType=/Subsample",
        f"-dMonoImageResolution={GS_DPI}",

        f"-sOutputFile={out_pdf}",
        in_pdf,
    ]

    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"Ghostscript failed (code={r.returncode}). stderr:\n{r.stderr[-2000:]}"
        )


def convert_pdf_to_pngs(
    fixed_pdf: str,
    out_dir: str,
    w: int,
    h: int,
    use_cropbox: bool,
    threads: int
) -> List[str]:
    return convert_from_path(
        fixed_pdf,
        size=(w, h),
        fmt="png",
        output_folder=out_dir,
        output_file="page",
        paths_only=True,
        thread_count=threads,
        use_cropbox=use_cropbox,
        use_pdftocairo=True,
        strict=False,
        single_file=False
    )


def resolve_output_target(
    input_bucket: str,
    input_obj: str,
    output_gs: str
) -> Tuple[str, str]:
    """
    戻り値: (out_bucket, out_prefix)
    out_prefix は「オブジェクト名の途中まで（prefix-）」を返す。
    """
    base = input_pdf_basename_no_ext(input_obj)

    if not output_gs:
        out_dir = OUTPUT_DIR.strip().strip("/")
        if out_dir:
            return input_bucket, f"{out_dir}/{base}-"
        return input_bucket, f"{base}-"

    out_bucket, out_obj = split_gs_uri_allow_empty_object(output_gs)

    if out_obj.endswith("/"):
        return out_bucket, f"{out_obj}{base}-"

    if not out_obj:
        return out_bucket, f"{base}-"

    return out_bucket, out_obj


def log_json(payload: dict) -> None:
    # Cloud Run Jobs ではログ遅延/欠落を避けるため flush する
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def post_progress(message: str) -> None:
    """進捗メッセージを analygent サーバへ POST する（失敗しても処理は継続）"""
    global AI_CASE_ID, ANALYGENT_PORT  # app.py から conv.ANALYGENT_PORT = req.port でセットされる
    if not AI_CASE_ID:
        return
    # Cloud NAT静的IPでホワイトリスト経由アクセス
    url = f"https://corp.analygent.com:{ANALYGENT_PORT}/sapis/set_upload_files_status_bvvu0xwac2afl7ubhkqj.php"
    content = json.dumps({"message": message}, ensure_ascii=False)
    data = urllib.parse.urlencode({
        "pqlxf4xct4jdsphk8kgc": "uptprogress",
        "ai_case_id": AI_CASE_ID,
        "f1htbrtxki4x7s4s0xqj": content,
    }).encode("utf-8")
    log_json({"ok": True, "stage": "post_progress_url", "url": url, "ai_case_id": AI_CASE_ID, "analygent_port": ANALYGENT_PORT})
    for attempt in range(1, 4):  # 最大3回リトライ
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=10):
                pass
            log_json({"ok": True, "stage": "post_progress", "message": message, "url": url, "attempt": attempt})
            return
        except Exception as e:
            log_json({"ok": False, "stage": "post_progress_error", "message": message, "url": url, "attempt": attempt, "error": str(e)})
    log_json({"ok": False, "stage": "post_progress_give_up", "message": message, "url": url})


# =============================
# ai_case_id & img_urls update helpers (修正)
# =============================
def parse_input_gs_list(raw: str) -> List[str]:
    """INPUT_GS を単一 or 複数指定で受け取る。
    - JSON配列:  ["gs://...","gs://..."]
    - 区切り文字: カンマ / 改行 / 空白
    """
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    parts = re.split(r"[\s,]+", s)
    return [p.strip() for p in parts if p.strip()]


def parse_upload_file_keys(raw: str) -> List[str]:
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    parts = re.split(r"[\s,]+", s)
    return [p.strip() for p in parts if p.strip()]


def derive_ai_case_id_from_input_obj(obj_path: str) -> str:
    name = obj_path.rsplit("/", 1)[-1]
    m = re.match(r"^(\d+)[-_]", name)
    if not m:
        raise ValueError(f"ai_case_id をファイル名から抽出できません: {name} (例: 10-xxx.pdf)")
    return m.group(1)


def mysql_connect(database: Optional[str]):
    if pymysql is None:
        raise RuntimeError("pymysql_not_installed")
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=(database or None),  # Noneならスキーマ未指定で接続
        connect_timeout=MYSQL_CONNECT_TIMEOUT,
        read_timeout=MYSQL_CONNECT_TIMEOUT,
        write_timeout=MYSQL_CONNECT_TIMEOUT,
        charset="utf8mb4",
        autocommit=True,
    )


def detect_db_with_ai_case(conn) -> Optional[str]:
    """ai_case テーブルが存在するDBを自動検出する。"""
    # 1) information_schema
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_schema
                FROM information_schema.tables
                WHERE table_name='ai_case'
                  AND table_schema NOT IN ('information_schema','mysql','performance_schema','sys')
                ORDER BY table_schema
            """)
            rows = cur.fetchall()
        schemas = [r[0] for r in rows] if rows else []
        if len(schemas) == 1:
            return schemas[0]
        if len(schemas) > 1:
            raise RuntimeError(f"MYSQL_DB is empty and ai_case exists in multiple schemas: {schemas}")
    except Exception:
        pass

    # 2) fallback
    found = []
    with conn.cursor() as cur:
        cur.execute("SHOW DATABASES")
        dbs = [r[0] for r in cur.fetchall()]

    for db in dbs:
        if db in ("information_schema", "mysql", "performance_schema", "sys"):
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM `{db}`.`ai_case` LIMIT 1")
                _ = cur.fetchone()
            found.append(db)
        except Exception:
            continue

    if len(found) == 1:
        return found[0]
    if len(found) == 0:
        return None
    raise RuntimeError(f"MYSQL_DB is empty and ai_case exists in multiple visible schemas: {found}")


def mysql_update_ai_case_img_urls(ai_case_id: str, img_urls_joined: str) -> Tuple[bool, str]:
    """ai_case.img_urls を更新する。MYSQL_DB未指定なら自動検出して更新する。"""
    if not MYSQL_USER:
        return False, "skip_update: MYSQL_USER is empty"
    if pymysql is None:
        return False, "pymysql_not_installed"

    try:
        if MYSQL_DB:
            db = MYSQL_DB
        else:
            conn0 = mysql_connect(None)
            try:
                db = detect_db_with_ai_case(conn0)
            finally:
                conn0.close()
            if not db:
                return False, "skip_update: MYSQL_DB is empty and ai_case table not found"

        conn = mysql_connect(db)
        try:
            with conn.cursor() as cur:
                sql = f"UPDATE `{db}`.`ai_case` SET img_urls=%s WHERE ai_case_id=%s"
                cur.execute(sql, (img_urls_joined, ai_case_id))
                affected = cur.rowcount
        finally:
            conn.close()

        return True, f"updated: db={db}, affected_rows={affected}"
    except Exception as e:
        return False, f"update_failed: {e}"


# =============================
# MySQL connectivity check (追加)
# =============================
def tcp_probe(host: str, port: int, timeout_sec: int) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True, "tcp_ok"
    except Exception as e:
        return False, f"tcp_ng: {e}"


def mysql_probe(
    host: str,
    port: int,
    user: str,
    password: str,
    db: str,
    timeout_sec: int
) -> Tuple[bool, str]:
    if not user:
        return False, "skip_mysql_login: MYSQL_USER is empty"
    if pymysql is None:
        return False, "pymysql_not_installed"

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=db if db else None,
            connect_timeout=timeout_sec,
            read_timeout=timeout_sec,
            write_timeout=timeout_sec,
            charset="utf8mb4",
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        finally:
            conn.close()

        return True, f"mysql_ok: SELECT 1 => {row}"
    except Exception as e:
        return False, f"mysql_ng: {e}"


# =============================
# Image resize (PHP resizeImageToCanvas 移植)
# =============================
def resize_image_to_canvas(
    input_path: str,
    output_path: str,
    default_width: int = 2480,
    default_height: int = 3508,
) -> bool:
    """
    画像をA4キャンバスに収めてリサイズし、JPEGで保存。
    PHP resizeImageToCanvas() の Python 移植（Pillow使用）。
    """
    try:
        from PIL import Image as _PilImage
        with _PilImage.open(input_path) as src_img:
            src_w, src_h = src_img.size
            # 横向き判定
            if src_w > src_h:
                target_w, target_h = default_height, default_width  # 横A4
            else:
                target_w, target_h = default_width, default_height  # 縦A4
            # アスペクト比を保ちながらリサイズ
            src_ratio = src_w / src_h
            target_ratio = target_w / target_h
            if src_ratio > target_ratio:
                new_w = target_w
                new_h = int(target_w / src_ratio)
            else:
                new_h = target_h
                new_w = int(target_h * src_ratio)
            # 白キャンバスに中央貼り付け
            canvas = _PilImage.new("RGB", (target_w, target_h), (255, 255, 255))
            resized = src_img.resize((new_w, new_h), _PilImage.LANCZOS)
            if resized.mode != "RGB":
                resized = resized.convert("RGB")
            dst_x = (target_w - new_w) // 2
            dst_y = (target_h - new_h) // 2
            canvas.paste(resized, (dst_x, dst_y))
            canvas.save(output_path, "JPEG", quality=90)
        return True
    except Exception as e:
        log_json({"ok": False, "stage": "resize_image_to_canvas_error", "error": str(e)})
        return False


def generate_gcs_signed_url(blob_obj, expiration_hours: int = 168) -> Optional[str]:
    """
    GCS blob の署名付きURL（v4）を生成する。
    Cloud Run では service_account_email + access_token 方式で署名する。
    失敗時は None を返す。
    """
    try:
        import google.auth
        import google.auth.transport.requests as _gauth_req

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_request = _gauth_req.Request()
        credentials.refresh(auth_request)

        sa_email = getattr(credentials, "service_account_email", None)
        token = getattr(credentials, "token", None)

        log_json({
            "ok": True,
            "stage": "signed_url_credentials",
            "sa_email": sa_email,
            "has_token": bool(token),
        })

        if sa_email and token:
            # Cloud Run / GCE: service_account_email + access_token で IAM SignBlob 経由で署名
            signed_url = blob_obj.generate_signed_url(
                expiration=timedelta(hours=expiration_hours),
                method="GET",
                version="v4",
                service_account_email=sa_email,
                access_token=token,
            )
        else:
            # サービスアカウントキーファイル等の場合は credentials を直接渡す
            signed_url = blob_obj.generate_signed_url(
                expiration=timedelta(hours=expiration_hours),
                method="GET",
                version="v4",
                credentials=credentials,
            )
        return signed_url
    except Exception as e:
        log_json({"ok": False, "stage": "generate_signed_url_error", "error": str(e)})
        return None


# =============================
# Azure OCR
# =============================
def call_azure_ocr(image_path: str) -> Dict[str, Any]:
    """
    Azure Form Recognizer (prebuilt-read) を呼び出す。
    azure_ai.py と同じ API・同じレスポンス形式。
    返り値: {"text_annotations": [{"description": "全テキスト"}]}
    """
    global AZURE_KEY, AZURE_ENDPOINT
    if not AZURE_KEY or not AZURE_ENDPOINT:
        raise RuntimeError("AZURE_KEY または AZURE_ENDPOINT が未設定です")

    endpoint = AZURE_ENDPOINT.rstrip("/")
    # azure_ai.py と同じエンドポイント
    ocr_url = f"{endpoint}/formrecognizer/documentModels/prebuilt-read:analyze?api-version=2022-08-31"

    with open(image_path, "rb") as f:
        image_data = f.read()

    # Step1: POST → 202 + Operation-Location
    req = urllib.request.Request(
        ocr_url,
        data=image_data,
        headers={
            "Ocp-Apim-Subscription-Key": AZURE_KEY,
            "Content-Type": "application/octet-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        operation_url = resp.headers.get("Operation-Location")

    if not operation_url:
        raise RuntimeError("Azure OCR: Operation-Location ヘッダーが取得できませんでした")

    # Step2: ポーリング（azure_ai.py と同じ: 1秒ごと・最大60回）
    result = {}
    for _ in range(60):
        time.sleep(1)
        poll_req = urllib.request.Request(
            operation_url,
            headers={"Ocp-Apim-Subscription-Key": AZURE_KEY},
        )
        with urllib.request.urlopen(poll_req, timeout=30) as poll_resp:
            result = json.loads(poll_resp.read().decode("utf-8"))
        status = result.get("status")
        if status == "succeeded":
            break
        elif status == "failed":
            raise RuntimeError("Azure OCR 解析失敗（status=failed）")
        # "running" / "notStarted" → 続けてポーリング
    else:
        raise RuntimeError("Azure OCR タイムアウト（60秒超）")

    # Step3: azure_ai.py と同じ: analyzeResult.content から全テキスト取得
    azure_text = result["analyzeResult"]["content"]

    return {"text_annotations": [{"description": azure_text}]}


# =============================
# Page Classification  (upload_files_sub_8dj4.php 移植)
# =============================
def _normalize_classify(text: str) -> str:
    """全角/半角統一 + 空白除去 + 小文字化 + OCR揺らぎ補正（PHP $__normalize 移植）"""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\s+", "", text)  # 改行・タブ含む全空白除去
    # OCR 誤認識・旧字体の補正（PHP strtr と同等）
    _ocr_fix = {
        "販賣": "販売",
        "賣":   "売",
        "價":   "価",
        "及ヒ": "及び",
        "販売費及一般管理費": "販売費及び一般管理費",
    }
    for old, new in _ocr_fix.items():
        text = text.replace(old, new)
    return text.lower()


def _is_keyword_match(text: str, keyword: str, threshold: float = 1.0) -> bool:
    """PHP isKeywordMatch() の Python 移植（文字単位の順次マッチング）"""
    text = _normalize_classify(text)
    keyword = _normalize_classify(keyword)
    if not keyword:
        return True
    keyword_chars = list(keyword)
    text_chars = list(text)
    match_count = 0
    text_index = 0
    for char in keyword_chars:
        found = False
        while text_index < len(text_chars):
            if text_chars[text_index] == char:
                match_count += 1
                text_index += 1
                found = True
                break
            text_index += 1
        if not found:
            break
    return (match_count / len(keyword_chars)) >= threshold


def _contains_keyword_with_match(text: str, keywords: List[str]) -> Tuple[bool, Optional[str]]:
    for kw in keywords:
        if _is_keyword_match(text, kw):
            return True, kw
    return False, None


def _is_capital_change_like(
    text: str, keywords: List[str], threshold: float = 0.70
) -> Tuple[bool, Optional[str], Optional[str]]:
    lines = re.split(r"\r\n|\n|\r", text or "")
    for line in lines:
        normalized_line = _normalize_classify(line)
        for kw in keywords:
            if _is_keyword_match(normalized_line, kw, threshold):
                return True, kw, line
    return False, None, None


def _classify_page(text: str) -> Dict[str, Any]:
    """PHP classifyPage() の Python 移植"""
    bs_keywords = [
        "流動資産", "現金及び預金", "売掛金", "繰延資産", "資産の部",
        "流動負債", "買掛金", "短期借入金", "預り金", "固定負債", "負債の部",
        "純資産の部", "株主資本", "資本金", "資本剰余金", "利益剰余金",
        "その他利益剰余金", "評価換算差額等", "新株予約権", "純資産の部合計",
        "純資産合計", "負債純資産の部合計",
    ]
    pl_keywords = [
        "売上高", "売上原価", "期首棚卸高", "仕入高", "期末棚卸高", "売上総利益",
        "営業利益", "営業損失", "営業外収益", "受取利息", "受取配当金", "営業外費用",
        "支払利息", "特別利益", "特別損失", "税引前当期利益", "税引前当期損失",
        "法人税住民税及び事業税", "当期純利益", "当期純損失", "その他収益",
        "当期収益", "当期損失",
    ]
    cf_keywords = [
        "資本等変動計算書", "株主資本等変動計算書", "連結株主資本等変動計算書",
        "連結持分変動計算書", "一般管理費の計算内訳", "一般管理費計算内訳",
        "管理費の計算内訳", "管理費計算内訳", "棚卸資産の計算内訳", "棚卸資産計算内訳",
    ]

    cf_hit, cf_kw, cf_line = _is_capital_change_like(text, cf_keywords, 0.70)
    if cf_hit:
        return {
            "type": "対象外",
            "firstHalfMatch": [],
            "secondHalfMatch": [],
            "cfKeywords": "NG",
            "cfMatchedKeyword": cf_kw,
            "cfMatchedLine": cf_line,
        }

    lines = re.split(r"\r\n|\n|\r", text or "")
    half = -(-len(lines) // 2)  # ceil
    first_half = "\n".join(lines[:half])
    second_half = "\n".join(lines[half:])

    match_info: Dict[str, Any] = {"type": "", "firstHalfMatch": [], "secondHalfMatch": []}
    first_bs, m_fbs = _contains_keyword_with_match(first_half, bs_keywords)
    first_pl, m_fpl = _contains_keyword_with_match(first_half, pl_keywords)
    second_bs, m_sbs = _contains_keyword_with_match(second_half, bs_keywords)
    second_pl, m_spl = _contains_keyword_with_match(second_half, pl_keywords)

    if first_bs and m_fbs:
        match_info["firstHalfMatch"].append(m_fbs)
    if first_pl and m_fpl:
        match_info["firstHalfMatch"].append(m_fpl)
    if second_bs and m_sbs:
        match_info["secondHalfMatch"].append(m_sbs)
    if second_pl and m_spl:
        match_info["secondHalfMatch"].append(m_spl)

    if first_bs and second_pl:
        match_info["type"] = "BS=>PL"
    elif first_pl and second_bs:
        match_info["type"] = "PL=>BS"
    elif first_bs or second_bs:
        match_info["type"] = "BS"
    elif first_pl or second_pl:
        match_info["type"] = "PL"
    else:
        match_info["type"] = "対象外"

    return match_info


def _apply_extended_classification(
    print_images: List[Dict],
    image_txt: List[Dict],
    read_sga: bool,
    read_mcr: bool,
) -> None:
    """PHP の read_sga/read_mcr 拡張分類ブロックの Python 移植"""
    if not (read_sga or read_mcr):
        return

    mfg_titles = ["製造原価報告書", "製造原価の報告書"]
    mfg_required = [
        "当期製造原価", "当期総製造費用", "期首仕掛品", "期末仕掛品",
        "仕掛品", "材料費", "労務費", "製造間接費", "製造原価",
        "加工費", "製造部門", "月別製造原価",
    ]
    neg_strong = ["貸借対照表", "balance sheet", "balancesheet", "資産の部", "負債の部", "純資産の部"]

    for i, info in enumerate(print_images):
        cur_type = info["page_type"]["type"]
        raw_text = ""
        if i < len(image_txt):
            ocr = image_txt[i]
            if isinstance(ocr, dict) and ocr.get("text_annotations"):
                raw_text = ocr["text_annotations"][0].get("description", "")

        t = _normalize_classify(raw_text)
        if not t:
            # PHPでは '不明' → 即座に '対象外' で上書きされるため実質 '対象外'
            info["page_type"]["type"] = "対象外"
            continue

        # 1) 販売費及び一般管理費
        has_han = _is_keyword_match(t, _normalize_classify("販売費"), 0.86)
        has_ipp = (
            _is_keyword_match(t, _normalize_classify("一般管理"), 0.86)
            or _is_keyword_match(t, _normalize_classify("一般管理費"), 0.86)
        )
        if has_han and has_ipp:
            if read_sga and cur_type != "BS or PL":
                info["page_type"]["type"] = "販売費"
            continue

        # 2-1) 製造原価報告書タイトル一致
        mfg_title_hit = any(
            _is_keyword_match(t, _normalize_classify(ttl), 0.86) for ttl in mfg_titles
        )
        if mfg_title_hit:
            if read_mcr:
                info["page_type"]["type"] = "製造原価"
            continue

        # 2-2) 必須キーワード3語以上 + 強否定なし
        hits = sum(1 for kw in mfg_required if _is_keyword_match(t, _normalize_classify(kw), 0.85))
        has_neg = any(_is_keyword_match(t, _normalize_classify(ng), 0.90) for ng in neg_strong)
        if hits >= 3 and not has_neg:
            if read_mcr:
                info["page_type"]["type"] = "製造原価"
            continue

        if cur_type == "BS or PL":
            continue
        info["page_type"]["type"] = "対象外"


# =============================
# MySQL full update helpers
# =============================
def mysql_fetch_ai_case_data(ai_case_id: str) -> Dict[str, Any]:
    """ai_case から imgtype_request (dict), upload_file_name (str) を取得"""
    if pymysql is None or not MYSQL_DB:
        return {"request": {}, "upload_file_name": ""}
    try:
        conn = mysql_connect(MYSQL_DB)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT imgtype_request, upload_file_name FROM `{MYSQL_DB}`.`ai_case` WHERE ai_case_id=%s",
                    (ai_case_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row:
            req = {}
            try:
                req = json.loads(row[0]) if row[0] else {}
            except Exception:
                req = {}
            return {"request": req, "upload_file_name": row[1] or ""}
        return {"request": {}, "upload_file_name": ""}
    except Exception as e:
        log_json({"ok": False, "stage": "mysql_fetch_ai_case_data_error", "error": str(e)})
        return {"request": {}, "upload_file_name": ""}


def mysql_fetch_page_types() -> List[Dict]:
    """page_types テーブルを取得"""
    if pymysql is None or not MYSQL_DB:
        return []
    try:
        conn = mysql_connect(MYSQL_DB)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT page_type_id, type FROM `{MYSQL_DB}`.`page_types`")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [{"page_type_id": r[0], "type": r[1]} for r in (rows or [])]
    except Exception as e:
        log_json({"ok": False, "stage": "mysql_fetch_page_types_error", "error": str(e)})
        return []


def mysql_update_ai_case_full(
    ai_case_id: str,
    request_json: str,
    sizes_str: str,
    status: str = "IMED",
) -> Tuple[bool, str]:
    """ai_case の update_at, imgtype_request, sizes, status を一括更新"""
    if not MYSQL_USER:
        return False, "skip_update: MYSQL_USER is empty"
    if pymysql is None:
        return False, "pymysql_not_installed"
    try:
        db = MYSQL_DB
        if not db:
            conn0 = mysql_connect(None)
            try:
                db = detect_db_with_ai_case(conn0)
            finally:
                conn0.close()
            if not db:
                return False, "skip_update: MYSQL_DB is empty and ai_case table not found"

        conn = mysql_connect(db)
        try:
            with conn.cursor() as cur:
                sql = f"""
                    UPDATE `{db}`.`ai_case`
                    SET update_at=NOW(),
                        imgtype_request=%s,
                        sizes=%s,
                        status=%s
                    WHERE ai_case_id=%s
                """
                cur.execute(sql, (request_json, sizes_str, status, ai_case_id))
                affected = cur.rowcount
        finally:
            conn.close()
        return True, f"updated: db={db}, affected_rows={affected}"
    except Exception as e:
        return False, f"update_failed: {e}"


# =============================
# Main (Job entrypoint)
# =============================
def main() -> Dict[str, Any]:
    upload_file_keys = parse_upload_file_keys(UPLOAD_FILE_KEYS_RAW)

    # 追加: MySQL疎通チェック（最初に実施）
    if MYSQL_CHECK:
        tcp_ok, tcp_msg = tcp_probe(MYSQL_HOST, MYSQL_PORT, MYSQL_CONNECT_TIMEOUT)
        mysql_ok, mysql_msg = mysql_probe(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            db=MYSQL_DB,
            timeout_sec=MYSQL_CONNECT_TIMEOUT,
        )

        log_json({
            "ok": tcp_ok and (mysql_ok or MYSQL_USER == ""),
            "stage": "mysql_check",
            "mysql_host": MYSQL_HOST,
            "mysql_port": MYSQL_PORT,
            "tcp": {"ok": tcp_ok, "message": tcp_msg},
            "mysql": {"ok": mysql_ok, "message": mysql_msg},
            "note": "MYSQL_USER未設定の場合はTCP疎通のみチェックします",
        })

    inputs = parse_input_gs_list(INPUT_GS)
    if not inputs:
        raise ValueError("環境変数 INPUT_GS は必須です（単一: gs://... または複数: JSON配列/カンマ区切り）")

    if len(inputs) > 1 and OUTPUT_GS and not OUTPUT_GS.endswith("/"):
        raise ValueError("複数INPUT_GSの場合は OUTPUT_GS を 'gs://bucket/dir/' のように末尾/で指定してください（自動で <inputbasename>-NNN.png を作ります）")

    client = storage.Client()

    # ai_case_id を入力ファイル名の先頭から抽出（全入力で同じであることを期待）
    ai_case_ids = []
    for gs_uri in inputs:
        _b, _o = parse_gs_uri(gs_uri)
        ai_case_ids.append(derive_ai_case_id_from_input_obj(_o))
    ai_case_id = ai_case_ids[0]
    if any(x != ai_case_id for x in ai_case_ids):
        raise ValueError(f"入力ファイルの ai_case_id が一致しません: {ai_case_ids}")

    all_uploaded_images: List[str] = []
    all_signed_urls: List[str] = []           # _mini.jpg の GCS署名付きURL（ページ順）
    all_ocr_results: List[Dict] = []          # Azure OCR 結果（ページ順）
    all_image_sizes: List[str] = []           # "widthxheight"（ページ順）
    all_page_upload_keys: List[str] = []      # upload_file_key（ページ順）
    all_pdf_names: List[str] = []             # PDF ファイル名（ページ順）
    img_cont = 1                              # 進捗カウンタ

    log_json({
        "ok": True,
        "stage": "start",
        "code_version": CODE_VERSION,
        "inputs": inputs,
        "upload_file_keys": upload_file_keys,
        "output_gs": OUTPUT_GS or "(auto)",
        "target": [TARGET_W, TARGET_H],
        "use_cropbox": USE_CROPBOX,
        "thread_count": THREAD_COUNT,
        "gs_dpi": GS_DPI,
        "ai_case_id": ai_case_id,
        "mysql_db": MYSQL_DB or "(auto-detect)"
    })

    for idx_input, one_input in enumerate(inputs, start=1):
        post_progress(f"{idx_input}番目PDFを画像に変換中")

        in_bucket, in_obj = parse_gs_uri(one_input)
        out_bucket, out_prefix = resolve_output_target(in_bucket, in_obj, OUTPUT_GS)

        bucket_in = client.bucket(in_bucket)
        bucket_out = client.bucket(out_bucket)

        log_json({
            "ok": True,
            "stage": "convert_one",
            "input_gs": one_input,
            "resolved_output": f"gs://{out_bucket}/{out_prefix}<NNN>.png",
        })

        with tempfile.TemporaryDirectory() as td:
            in_pdf = os.path.join(td, "input.pdf")
            fixed_pdf = os.path.join(td, "fixed.pdf")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir, exist_ok=True)

            # 1) download PDF
            bucket_in.blob(in_obj).download_to_filename(in_pdf)

            # 2) normalize PDF
            run_ghostscript_normalize(in_pdf, fixed_pdf)

            # 3) render PNGs
            png_paths = convert_pdf_to_pngs(
                fixed_pdf=fixed_pdf,
                out_dir=out_dir,
                w=TARGET_W,
                h=TARGET_H,
                use_cropbox=USE_CROPBOX,
                threads=THREAD_COUNT
            )

            # 4) upload to GCS + Azure OCR + image size
            uploaded = []
            ufkey = (
                upload_file_keys[idx_input - 1]
                if idx_input - 1 < len(upload_file_keys)
                else f"file{idx_input}"
            )
            pdf_basename = in_obj.rsplit("/", 1)[-1]

            for i, path in enumerate(png_paths, start=1):
                idx = format_index(i)
                out_obj = f"{out_prefix}{idx}.png"
                bucket_out.blob(out_obj).upload_from_filename(
                    path,
                    content_type="image/png"
                )
                uploaded.append(f"gs://{out_bucket}/{out_obj}")

                # 画像サイズ取得
                try:
                    from PIL import Image as _PilImage
                    with _PilImage.open(path) as pil_img:
                        w_px, h_px = pil_img.size
                    all_image_sizes.append(f"{w_px}x{h_px}")
                except Exception as size_err:
                    log_json({"ok": False, "stage": "image_size_error", "path": path, "error": str(size_err)})
                    all_image_sizes.append("unknown")

                # リサイズ → _mini.jpg をGCSアップロード → 署名付きURL生成
                mini_path = os.path.splitext(path)[0] + "_mini.jpg"
                mini_obj = f"{out_prefix}{idx}_mini.jpg"
                signed_url: Optional[str] = None
                if resize_image_to_canvas(path, mini_path):
                    try:
                        mini_blob = bucket_out.blob(mini_obj)
                        mini_blob.upload_from_filename(mini_path, content_type="image/jpeg")
                        signed_url = generate_gcs_signed_url(mini_blob, expiration_hours=168)  # 1週間
                        log_json({
                            "ok": True,
                            "stage": "mini_upload",
                            "gcs": f"gs://{out_bucket}/{mini_obj}",
                            "signed_url_ok": signed_url is not None,
                        })
                    except Exception as mini_err:
                        log_json({"ok": False, "stage": "mini_upload_error", "error": str(mini_err)})
                else:
                    log_json({"ok": False, "stage": "mini_resize_skip", "image": os.path.basename(path)})
                # 署名付きURL取得失敗時は gs:// URI にフォールバック
                all_signed_urls.append(signed_url or f"gs://{out_bucket}/{mini_obj}")

                # Azure OCR
                post_progress(f"{img_cont}番目画像の帳票種類識別中")
                img_cont += 1
                try:
                    ocr_result = call_azure_ocr(path)
                    azure_text = ocr_result["text_annotations"][0]["description"]
                    all_ocr_results.append(ocr_result)
                    log_json({
                        "ok": True,
                        "stage": "azure_ocr",
                        "image": os.path.basename(path),
                        "text_length": len(azure_text),
                        "text_preview": azure_text[:300],  # 最初の300文字をログ出力
                    })
                except Exception as ocr_err:
                    log_json({"ok": False, "stage": "azure_ocr_error", "image": os.path.basename(path), "error": str(ocr_err)})
                    all_ocr_results.append({"text_annotations": [{"description": ""}]})

                all_page_upload_keys.append(ufkey)
                all_pdf_names.append(pdf_basename)

        all_uploaded_images.extend(uploaded)

    log_json({
        "ok": True,
        "stage": "pre_mysql_update",
        "ai_case_id": ai_case_id,
        "img_urls_count": len(all_uploaded_images),
        "ocr_results_count": len(all_ocr_results),
        "mysql_db_env": MYSQL_DB,
    })

    # img_urls を ai_case に保存
    img_urls_joined = "|,|".join(all_uploaded_images)
    upd_ok, upd_msg = mysql_update_ai_case_img_urls(ai_case_id=ai_case_id, img_urls_joined=img_urls_joined)
    log_json({"ok": upd_ok, "stage": "mysql_update_img_urls", "message": upd_msg})

    # ============================
    # ページ分類
    # ============================
    page_classifications = []
    for ocr in all_ocr_results:
        text = ""
        if isinstance(ocr, dict) and ocr.get("text_annotations"):
            text = ocr["text_annotations"][0].get("description", "")
        page_classifications.append(_classify_page(text))

    # ファイルキーごとの PL/BS 数カウント
    plbs_counts_per_file: Dict[str, Dict[str, int]] = {}
    for i, cls in enumerate(page_classifications):
        key = all_page_upload_keys[i] if i < len(all_page_upload_keys) else "unknown"
        if key not in plbs_counts_per_file:
            plbs_counts_per_file[key] = {"PL": 0, "BS": 0}
        if cls["type"] == "PL":
            plbs_counts_per_file[key]["PL"] += 1
        elif cls["type"] == "BS":
            plbs_counts_per_file[key]["BS"] += 1

    # print_images 構築
    print_images: List[Dict] = []
    global_subi = 0  # PHP の $subi と同じ：全ページ通し連番
    for i, path_gs in enumerate(all_uploaded_images):
        cls = page_classifications[i] if i < len(page_classifications) else {"type": "対象外"}
        key = all_page_upload_keys[i] if i < len(all_page_upload_keys) else "unknown"
        global_subi += 1
        page_info: Dict[str, Any] = {
            "page_type": dict(cls),
            "rotation": 0,
            # _mini.jpg の署名付きURL（フロントエンド表示用）
            "images_urls": all_signed_urls[i] if i < len(all_signed_urls) else path_gs,
            "pdf_names": all_pdf_names[i] if i < len(all_pdf_names) else None,
            "upload_file_key": key,
            "page_no": global_subi,
        }
        # 対象外以外は "BS or PL" に統一（PHP 準拠）
        if page_info["page_type"]["type"] != "対象外":
            page_info["page_type"]["type"] = "BS or PL"
        print_images.append(page_info)

    # 拡張分類（販売費・製造原価）
    _apply_extended_classification(print_images, all_ocr_results, READ_SGA, READ_MCR)

    # 最終集計
    setting: Dict[str, Dict[str, int]] = {}
    total_seted_all = 0
    for info in print_images:
        ufkey = info["upload_file_key"]
        if ufkey not in setting:
            setting[ufkey] = {"seted_all": 0, "seted_pl": 0, "seted_bs": 0, "seted_sga": 0, "seted_mfg": 0}
        ptype = info["page_type"]["type"]
        if ptype != "対象外":
            setting[ufkey]["seted_all"] += 1
            total_seted_all += 1
        if "PL" in ptype:
            setting[ufkey]["seted_pl"] += 1
        if "BS" in ptype:
            setting[ufkey]["seted_bs"] += 1
        if "販売費" in ptype:
            setting[ufkey]["seted_sga"] += 1
        if "製造原価" in ptype:
            setting[ufkey]["seted_mfg"] += 1

    log_json({
        "ok": True,
        "stage": "classify_done",
        "total_pages": len(print_images),
        "total_seted_all": total_seted_all,
    })

    # ============================
    # DB から request・page_types を取得して更新
    # ============================
    post_progress("読取完了まで")

    page_types = mysql_fetch_page_types()
    ai_case_data = mysql_fetch_ai_case_data(ai_case_id=ai_case_id)
    db_request = ai_case_data.get("request") or {}

    db_request["images"] = print_images
    db_request["message"] = "アップロードと判定が完了しました"
    db_request["total_seted_all"] = total_seted_all
    db_request["set_counts"] = setting
    db_request["types"] = page_types

    request_json_str = json.dumps(db_request, ensure_ascii=False)
    sizes_str = "|,|".join(all_image_sizes)

    upd_full_ok, upd_full_msg = mysql_update_ai_case_full(
        ai_case_id=ai_case_id,
        request_json=request_json_str,
        sizes_str=sizes_str,
        status="IMED",
    )
    log_json({"ok": upd_full_ok, "stage": "mysql_update_full", "message": upd_full_msg})

    post_progress("帳票種類識別完了")

    result = {
        "ok": True,
        "stage": "done",
        "ai_case_id": ai_case_id,
        "img_urls_delimiter": "|,|",
        "img_urls_count": len(all_uploaded_images),
        "total_seted_all": total_seted_all,
        "mysql_update": {"ok": upd_ok, "message": upd_msg},
        "mysql_update_full": {"ok": upd_full_ok, "message": upd_full_msg},
        "images": all_uploaded_images,
    }

    log_json(result)
    return result


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_json({"ok": False, "error": str(e)})
        raise