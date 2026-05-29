"""
viewer_main.py - FastAPI router

変更点:
- セッショントークン発行・検証
- /image/{session_token}/{filename} に変更
- /thumbnail を episode_thumbs/ から返すよう変更
- slowapi レート制限
- CF-Connecting-IP 対応
"""

import asyncio
import concurrent.futures
import hashlib
import os
import httpx
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import urllib.parse
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import Response as StarletteResponse

import manga as mg

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR    = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

PLATFORM_BASE_URL = os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000")

_executor       = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# ---------------------------------------------------------------------------
# IP取得（Cloudflared対応）
# ---------------------------------------------------------------------------
def get_client_ip(request: Request) -> str:
    cf_ip = request.headers.get("CF-Connecting-IP")
    return cf_ip if cf_ip else (request.client.host if request.client else "unknown")

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_client_ip)

# ---------------------------------------------------------------------------
# アプリ
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    mg.manage_cache_size()
    mg.cleanup_old_cache()
    yield
    _executor.shutdown(wait=False)
    _thumb_executor.shutdown(wait=False)

app = FastAPI(title="Manga Reader", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return StarletteResponse("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)

# ---------------------------------------------------------------------------
# List screen  GET /
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    share: Optional[str] = Query(default=None),
):
    manga_list:     list[dict] = []
    import_message: Optional[str] = None

    if share:
        manga_list, err = mg.decode_manga_list(share)
        if err:
            import_message = err

    return templates.TemplateResponse(request, "list.html", {
        "request":        request,
        "manga_list":     manga_list,
        "share_param":    share or "",
        "import_message": import_message,
        "max_count":      mg.MAX_MANGA_COUNT,
    })

# ---------------------------------------------------------------------------
# Public catalog screen  GET /public
# ---------------------------------------------------------------------------

@app.get("/public", response_class=HTMLResponse)
async def public_catalog(request: Request):
    catalog   = []
    error_msg = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{PLATFORM_BASE_URL}/api/public/catalog")
            resp.raise_for_status()
            catalog = resp.json()
    except Exception as e:
        error_msg = f"カタログの取得に失敗しました: {e}"

    return templates.TemplateResponse(request, "public.html", {
        "request":      request,
        "catalog":      catalog,
        "error_msg":    error_msg,
        "platform_url": PLATFORM_BASE_URL,
    })

# ---------------------------------------------------------------------------
# セッショントークン発行  GET /session
# ---------------------------------------------------------------------------

@app.get("/session")
@limiter.limit("30/minute")
async def create_session(
    request: Request,
    url: str = Query(...),
):
    """
    CBZ URLに対するセッショントークンを発行する。
    リーダーページを開くときに呼ばれる。
    """
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")

    client_ip = get_client_ip(request)
    token     = mg.create_session(url, client_ip)
    return {"session_token": token, "expires_in": mg.SESSION_TTL_SECONDS}

# ---------------------------------------------------------------------------
# Add manga  POST /manga/add
# ---------------------------------------------------------------------------

@app.post("/manga/add", response_class=HTMLResponse)
async def manga_add(
    request: Request,
    url:   Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    error: Optional[str] = None

    if not mg.validate_manga_url(url):
        error = "URLは .zip / .cbz / .rar / .cbr である必要があります"
    elif len(manga_list) >= mg.MAX_MANGA_COUNT:
        error = f"最大 {mg.MAX_MANGA_COUNT} 件まで登録できます"
    elif url in {m['url'] for m in manga_list}:
        error = "このURLはすでに追加されています"
    else:
        manga_list.append(mg.build_manga_entry(url, title))

    new_share = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      error,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Remove manga  POST /manga/remove
# ---------------------------------------------------------------------------

@app.post("/manga/remove", response_class=HTMLResponse)
async def manga_remove(
    request: Request,
    url:   Annotated[str, Form()],
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_list    = [m for m in manga_list if m['url'] != url]
    new_share     = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      None,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Reader screen  GET /reader
# ---------------------------------------------------------------------------

@app.get("/reader", response_class=HTMLResponse)
async def reader(
    request:  Request,
    url:      str = Query(...),
    share:    str = Query(default=""),
    webhook1: str = Query(default=""),
    webhook2: str = Query(default=""),
):
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")

    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_title   = next(
        (m['title'] for m in manga_list if m['url'] == url),
        mg.get_filename_from_url(url)
    )

    # settings.json から scrambled / tile_size / タイトル を取得する
    scrambled  = False
    tile_size  = 16
    manga_title_display = manga_title  # デフォルトはファイル名

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            catalog_url = f"{PLATFORM_BASE_URL}/api/public/catalog"
            resp = await client.get(catalog_url)
            if resp.status_code == 200:
                catalog = resp.json()
                for item in catalog:
                    if item.get("cbz_url") == url:
                        scrambled = item.get("scrambled", False)
                        tile_size = item.get("tile_size", 16)
                        # タイトルとエピソード名が設定されていれば使う
                        # 未設定（空文字）の場合はファイル名のまま
                        t = item.get("title_name", "").strip()
                        e = item.get("episode_name", "").strip()
                        if t and e:
                            manga_title_display = f"{t} — {e}"
                        elif t:
                            manga_title_display = t
                        elif e:
                            manga_title_display = e
                        break
    except Exception:
        pass

    return templates.TemplateResponse(request, "reader.html", {
        "request":     request,
        "manga_url":   url,
        "manga_title": manga_title_display,
        "share_param": share,
        "per_page":    mg.IMAGES_PER_LOAD,
        "webhook1":    webhook1,
        "webhook2":    webhook2,
        "scrambled":   scrambled,
        "tile_size":   tile_size,
    })

# ---------------------------------------------------------------------------
# Image list partial  GET /images
# セッショントークン必須
# ---------------------------------------------------------------------------

@app.get("/images", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def images_partial(
    request:       Request,
    url:           str = Query(...),
    offset:        int = Query(default=0),
    limit:         int = Query(default=0),
    webhook1:      str = Query(default=""),
    webhook2:      str = Query(default=""),
    session_token: str = Query(default=""),
):
    # セッション検証
    if session_token:
        session = mg.validate_session(session_token)
        if not session:
            raise HTTPException(status_code=403, detail="セッションが無効または期限切れです")
        if session["cbz_url"] != url:
            raise HTTPException(status_code=403, detail="セッションのURLが一致しません")

    url_hash     = hashlib.md5(url.encode()).hexdigest()
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        return templates.TemplateResponse(request, "partials/download_pending.html", {
            "request":   request,
            "manga_url": url,
            "offset":    offset,
            "webhook1":  webhook1,
            "webhook2":  webhook2,
        })

    image_files, warnings = mg.extract_archive(archive_path, extract_path)
    total     = len(image_files)
    page_size = limit if limit > 0 else mg.IMAGES_PER_LOAD
    batch     = image_files[offset: offset + page_size]
    next_offset = offset + len(batch)
    has_more  = next_offset < total

    # セッションにpage_countを記録
    if session_token:
        mg.set_session_page_count(session_token, total)

    image_items = []
    for i, path in enumerate(batch, start=offset):
        filename = os.path.basename(path)
        image_items.append({
            "src":      f"/image/{session_token}/{url_hash}/{filename}",
            "filename": filename,
            "index":    i,
            "total":    total,
        })

    return templates.TemplateResponse(request, "partials/image_batch.html", {
        "request":     request,
        "image_items": image_items,
        "next_offset": next_offset,
        "has_more":    has_more,
        "total":       total,
        "manga_url":   url,
        "webhook1":    webhook1,
        "webhook2":    webhook2,
        "warnings":    warnings,
    })

# ---------------------------------------------------------------------------
# Image binary  GET /image/{session_token}/{url_hash}/{filename}
# ---------------------------------------------------------------------------

@app.get("/image/{session_token}/{url_hash}/{filename}")
@limiter.limit("60/minute")
async def serve_image(
    request:       Request,
    session_token: str,
    url_hash:      str,
    filename:      str,
):
    # セッション検証
    session = mg.validate_session(session_token)
    if not session:
        raise HTTPException(status_code=403, detail="セッションが無効または期限切れです")

    # url_hashがセッションのcbz_urlと一致するか確認
    expected_hash = hashlib.md5(session["cbz_url"].encode()).hexdigest()
    if url_hash != expected_hash:
        raise HTTPException(status_code=403, detail="不正なアクセスです")

    path = os.path.join(mg.get_cache_dir(), f"{url_hash}_extracted", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画像が見つかりません")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(_executor, mg.load_image_bytes, path)

    if data is None:
        raise HTTPException(status_code=500, detail="画像の読み込みに失敗しました")

    media_type = "image/png" if data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"

    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

# ---------------------------------------------------------------------------
# Download + progress  GET /download/start  (SSE)
# ---------------------------------------------------------------------------

@app.get("/download/start")
@limiter.limit("3/minute")
async def download_start(request: Request, url: str = Query(...)):
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext

    async def event_stream():
        if os.path.exists(archive_path):
            yield "data: done\n\n"
            return

        loop:  asyncio.AbstractEventLoop = asyncio.get_running_loop()
        queue: asyncio.Queue             = asyncio.Queue()

        def progress_cb(p: float):
            loop.call_soon_threadsafe(queue.put_nowait, p)

        def run_download():
            success, err = mg.download_file(url, archive_path, progress_callback=progress_cb)
            if success:
                loop.call_soon_threadsafe(queue.put_nowait, "done")
            else:
                loop.call_soon_threadsafe(queue.put_nowait, f"error:{err}")

        loop.run_in_executor(_executor, run_download)

        while True:
            item = await queue.get()
            if item == "done":
                yield "data: done\n\n"
                break
            elif isinstance(item, str) and item.startswith("error:"):
                yield f"data: {item}\n\n"
                break
            else:
                yield f"data: {item:.3f}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Thumbnail  GET /thumbnail
# episode_thumbs/ から返す（非スクランブル・事前生成済み）
# ---------------------------------------------------------------------------

@app.get("/thumbnail")
@limiter.limit("30/minute")
async def serve_thumbnail(
    request:       Request,
    url:           str = Query(...),
    index:         int = Query(...),
    session_token: str = Query(default=""),
):
    # セッション検証
    if session_token:
        session = mg.validate_session(session_token)
        if not session or session["cbz_url"] != url:
            raise HTTPException(status_code=403, detail="セッションが無効です")

    # プラットフォームのCBZ URLから thumb URLを組み立てる
    # URL形式: {platform}/api/public/cbz/{uid}/{tid}/{eid}.cbz
    #       → {platform}/api/public/thumb/{uid}/{tid}/{eid}/{index}
    # プレビューURL: {platform}/api/author/preview/{tid}/{eid}.cbz?token=...
    #             → {platform}/api/author/thumb/{tid}/{eid}/{index}?token=...

    thumb_url = None

    if "/api/public/cbz/" in url:
        # 公開作品のサムネイル
        # /api/public/cbz/{uid}/{tid}/{eid}.cbz → /api/public/thumb/{uid}/{tid}/{eid}/{index}
        base    = url.split("/api/public/cbz/")[0]
        parts   = url.split("/api/public/cbz/")[1].replace(".cbz", "").split("/")
        if len(parts) == 3:
            thumb_url = f"{base}/api/public/thumb/{parts[0]}/{parts[1]}/{parts[2]}/{index}"

    elif "/api/author/preview/" in url:
        # プレビュー（作者のみ）
        # /api/author/preview/{tid}/{eid}.cbz?token=xxx → /api/author/thumb/{tid}/{eid}/{index}?token=xxx
        base_and_path = url.split("?")[0]
        token_part    = url.split("?")[1] if "?" in url else ""
        base   = base_and_path.split("/api/author/preview/")[0]
        parts  = base_and_path.split("/api/author/preview/")[1].replace(".cbz", "").split("/")
        if len(parts) == 2:
            thumb_url = f"{base}/api/author/thumb/{parts[0]}/{parts[1]}/{index}"
            if token_part:
                thumb_url += f"?{token_part}"

    if thumb_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(thumb_url)
                if resp.status_code == 200:
                    return Response(
                        content=resp.content,
                        media_type=resp.headers.get("content-type", "image/jpeg"),
                        headers={"Cache-Control": "private, max-age=86400"},
                    )
        except Exception:
            pass

    # フォールバック: キャッシュから生成（外部ZIP等）
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        raise HTTPException(status_code=404, detail="アーカイブが見つかりません")

    image_files, _ = mg.extract_archive(archive_path, extract_path)
    if index < 0 or index >= len(image_files):
        raise HTTPException(status_code=404, detail="ページが見つかりません")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        _thumb_executor,
        lambda: mg.load_image_bytes(image_files[index], max_size=(80, 120)),
    )
    if data is None:
        raise HTTPException(status_code=500, detail="サムネイル生成失敗")

    media_type = "image/png" if data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )

# ---------------------------------------------------------------------------
# Discord send  POST /discord/send
# ---------------------------------------------------------------------------

@app.post("/discord/send", response_class=HTMLResponse)
async def discord_send(
    request:  Request,
    url:      Annotated[str, Form()],
    filename: Annotated[str, Form()],
    webhook1: Annotated[str, Form()] = "",
    webhook2: Annotated[str, Form()] = "",
    session_token: Annotated[str, Form()] = "",
):
    # セッション検証
    if session_token:
        session = mg.validate_session(session_token)
        if not session or session["cbz_url"] != url:
            return HTMLResponse("<span class='status-error'>セッションが無効です</span>")

    url_hash   = hashlib.md5(url.encode()).hexdigest()
    image_path = os.path.join(mg.get_cache_dir(), f"{url_hash}_extracted", filename)

    webhooks = [w for w in [webhook1, webhook2] if w]
    if not webhooks:
        return HTMLResponse("<span class='status-error'>Webhookが設定されていません</span>")

    errors = mg.send_image_to_discord(image_path, webhooks, filename)
    if errors:
        return HTMLResponse(f"<span class='status-error'>{'<br>'.join(errors)}</span>")
    return HTMLResponse("<span class='status-ok'>✓ 送信しました</span>")

# ---------------------------------------------------------------------------
# Share URL  GET /share/url
# ---------------------------------------------------------------------------

@app.get("/share/url")
async def share_url(
    request: Request,
    share:   str  = Query(default=""),
    shorten: bool = Query(default=True),
):
    base         = str(request.base_url).rstrip("/")
    encoded      = urllib.parse.quote(share, safe="")
    full_url     = f"{base}/?share={encoded}"
    result       = mg.shorten_url(full_url) if shorten else full_url
    return Response(content=result, media_type="text/plain")
