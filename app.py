import os
import json
from typing import List, Optional, Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import main as conv

app = FastAPI(title="pdf-converter", version="api-1")


class ConvertRequest(BaseModel):
    input_gs: List[str] = Field(..., description="Input PDF GCS URIs (gs://bucket/object.pdf)")
    output_gs: Optional[str] = Field(None, description="Single: gs://bucket/prefix-  Multi: gs://bucket/dir/ (end with /)")

    target_w: int = 3307
    target_h: int = 4677
    use_cropbox: bool = True
    thread_count: int = 1
    gs_dpi: int = 400
    number_format: str = "03d"

    mysql_check: bool = True
    mysql_host: str = "10.146.0.2"
    mysql_port: int = 3306
    mysql_user: str = "IsplitAdmin"
    mysql_db: str = "dbtest1"
    mysql_connect_timeout: int = 3

    upload_file_keys: Optional[str] = ""

    ai_case_id: Optional[str] = Field(None, description="AI Case ID for progress callbacks")
    port: int = Field(8056, description="Analygent server port for progress callbacks")

    read_sga: bool = Field(False, description="販売費及び一般管理費ページを識別するか")
    read_mcr: bool = Field(False, description="製造原価報告書ページを識別するか")


def _set_converter_config(req: ConvertRequest) -> None:
    conv.INPUT_GS = json.dumps(req.input_gs, ensure_ascii=False)
    conv.OUTPUT_GS = (req.output_gs or "").strip()

    conv.TARGET_W = int(req.target_w)
    conv.TARGET_H = int(req.target_h)
    conv.USE_CROPBOX = bool(req.use_cropbox)
    conv.THREAD_COUNT = int(req.thread_count)
    conv.GS_DPI = int(req.gs_dpi)
    conv.NUMBER_FORMAT = str(req.number_format)

    conv.MYSQL_CHECK = bool(req.mysql_check)
    conv.MYSQL_HOST = str(req.mysql_host).strip() or "10.146.0.2"
    conv.MYSQL_PORT = int(req.mysql_port) if req.mysql_port else 3306
    conv.MYSQL_USER = "IsplitAdmin"
    _port_db_map = {
        8056: "dbtest1",
        8012: "dbwindow",
    }
    conv.MYSQL_DB = _port_db_map.get(int(req.port), str(req.mysql_db).strip())
    conv.MYSQL_CONNECT_TIMEOUT = int(req.mysql_connect_timeout)

    conv.MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
    conv.UPLOAD_FILE_KEYS_RAW = (req.upload_file_keys or "").strip()

    conv.AI_CASE_ID = str(req.ai_case_id or "").strip()
    conv.ANALYGENT_PORT = int(req.port)
    conv.READ_SGA = bool(req.read_sga)
    conv.READ_MCR = bool(req.read_mcr)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/convert")
def convert(req: ConvertRequest):
    # Existing converter uses module globals; deploy Cloud Run with concurrency=1.
    try:
        if not req.input_gs:
            raise HTTPException(status_code=400, detail="input_gs is required")
        if len(req.input_gs) > 1 and req.output_gs and not req.output_gs.endswith("/"):
            raise HTTPException(status_code=400, detail="For multiple inputs, output_gs must end with '/'")

        ai_case_id_str = str(req.ai_case_id or "").strip()
        if not ai_case_id_str or not ai_case_id_str.isdigit():
            raise HTTPException(status_code=400, detail="ai_case_id is required and must be numeric")

        _set_converter_config(req)

        result: Dict[str, Any] = conv.main()  # ★ main() の return を受け取る

        # ★ APIレスポンスに images を含める
        return {
            "ok": True,
            "ai_case_id": result.get("ai_case_id"),
            "img_urls_count": result.get("img_urls_count"),
            "images": result.get("images", []),
            "mysql_update": result.get("mysql_update"),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))