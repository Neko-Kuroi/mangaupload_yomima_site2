import os
import re
import uuid
import json
import time
import shutil
import zipfile
import logging
import secrets
import asyncio
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, UTC
from typing import List, Generator, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import jwt, JWTError
from PIL import Image
from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ディレクトリ準備
# ---------------------------------------------------------------------------
STATIC_DIR    = Path("/content/mangaupload_yomima_site/static")
TMP_DIR       = Path("./storage/tmp")
FINAL_ZIP_DIR = Path("./storage/zips")
for d in [STATIC_DIR, TMP_DIR, FINAL_ZIP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
SECRET_KEY                 = "MANGA_PLATFORM_SUPER_SECRET_KEY"
ALGORITHM                  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
MAX_PAGE_SIZE_MB            = 10
MAX_TOTAL_SIZE_MB           = 100
MAX_COVER_SIZE_MB           = 5
MAX_PAGE_SIZE_BYTES         = MAX_PAGE_SIZE_MB  * 1024 * 1024
MAX_TOTAL_SIZE_BYTES        = MAX_TOTAL_SIZE_MB * 1024 * 1024
MAX_COVER_SIZE_BYTES        = MAX_COVER_SIZE_MB * 1024 * 1024
DISPLAY_MAX_SIZE            = (1200, 1800)   # アップロード時リサイズ上限
THUMB_SIZE                  = (150, 220)     # サムネイルサイズ
TILE_SIZE_DEFAULT           = 16             # スクランブルタイルサイズ
CHARSET_62                  = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# ---------------------------------------------------------------------------
# データベース
# ---------------------------------------------------------------------------
DATABASE_URL = "sqlite:///./storage/manga_platform.db"
engine       = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base         = declarative_base()

class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String,  unique=True, index=True, nullable=False)
    email           = Column(String,  unique=True, index=True, nullable=False)
    hashed_password = Column(String,  nullable=False)
    is_active       = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------------------------------------------------------------------
# 認証
# ---------------------------------------------------------------------------
pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="認証トークンが無効です")
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンの期限切れ、または不正です")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
    return user

# ---------------------------------------------------------------------------
# Pydantic スキーマ
# ---------------------------------------------------------------------------
class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class EpisodePublishSettings(BaseModel):
    status:          str
    access_level:    str
    price:           int  = 0
    title_name:      str  = ""
    episode_name:    str  = ""
    caption:         str  = ""   # 公開：作品紹介文
    comment:         str  = ""   # 公開：作者コメント
    note:            str  = ""   # 非公開：作者備考
    scrambled:       bool = True
    tile_size:       int  = TILE_SIZE_DEFAULT
    webhook_enabled: bool = False

# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    """CloudflaredのCF-Connecting-IPヘッダーに対応したIP取得"""
    cf_ip = request.headers.get("CF-Connecting-IP")
    return cf_ip if cf_ip else (request.client.host if request.client else "unknown")

def natural_keys(text: str):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

def to_base62(n: int, width: int = 4) -> str:
    if n == 0:
        return "0".zfill(width)
    res = ""
    while n > 0:
        n, r = divmod(n, 62)
        res = CHARSET_62[r] + res
    return res.zfill(width)

def generate_page_filename(index: int) -> str:
    """スクランブルあり・なし共通のランダムファイル名生成"""
    prefix      = to_base62(index, width=4)
    random_part = secrets.token_hex(8)   # 16文字hex = 64bit
    return f"{prefix}{random_part}.png"

class Xorshift32:
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF
        if self.state == 0:
            self.state = 1

    def next(self) -> int:
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5)  & 0xFFFFFFFF
        self.state = x & 0xFFFFFFFF
        return self.state

def xorshift_shuffle(items: list, seed: int) -> list:
    rng = Xorshift32(seed)
    res = items[:]
    for i in range(len(res) - 1, 0, -1):
        j = rng.next() % (i + 1)
        res[i], res[j] = res[j], res[i]
    return res

def scramble_image(img: Image.Image, seed: int, tile_size: int) -> Image.Image:
    """画像をタイル単位でスクランブルする"""
    width, height  = img.size
    cols           = width  // tile_size
    rows           = height // tile_size
    num_tiles      = cols * rows
    shuffled       = xorshift_shuffle(list(range(num_tiles)), seed)
    scrambled_img  = img.copy()

    for i in range(num_tiles):
        dest_idx = shuffled[i]
        sx, sy   = (i        % cols) * tile_size, (i        // cols) * tile_size
        dx, dy   = (dest_idx % cols) * tile_size, (dest_idx // cols) * tile_size
        tile     = img.crop((sx, sy, sx + tile_size, sy + tile_size))
        scrambled_img.paste(tile, (dx, dy))

    # 端数領域はそのままコピー
    if width % tile_size > 0:
        ex = cols * tile_size
        scrambled_img.paste(img.crop((ex, 0, width, height)), (ex, 0))
    if height % tile_size > 0:
        ey = rows * tile_size
        scrambled_img.paste(img.crop((0, ey, width, height)), (0, ey))

    return scrambled_img

def process_page(
    raw_path: Path,
    work_dir: Path,
    index: int,
    do_scramble: bool,
    tile_size: int,
) -> Path:
    """
    1ページ処理:
      1. リサイズ（DISPLAY_MAX_SIZE以下に収める）
      2. スクランブル（オプション）
      3. ランダムファイル名で保存
    """
    filename = generate_page_filename(index)
    save_path = work_dir / filename

    with Image.open(raw_path) as img:
        img = img.convert("RGB")
        # アップロード時に一度だけリサイズ
        img.thumbnail(DISPLAY_MAX_SIZE, Image.LANCZOS)

        if do_scramble:
            # ファイル名の先頭8文字(prefix除く)がseed
            seed_hex = filename[4:12]
            seed     = int(seed_hex, 16)
            img      = scramble_image(img, seed, tile_size)

        img.save(save_path, "PNG")

    return save_path

def generate_thumbnail(img_path: Path, thumb_dir: Path, index: int) -> Path:
    """サムネイル生成（非スクランブル・小サイズ）"""
    thumb_path = thumb_dir / f"thumb_{index:04d}.jpg"
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        img.save(thumb_path, "JPEG", quality=80)
    return thumb_path

# ---------------------------------------------------------------------------
# アップロード進捗ストア（メモリ内）
# ---------------------------------------------------------------------------
upload_progress: dict[str, dict] = {}

def update_progress(job_id: str, phase: str, current: int, total: int, **kwargs):
    upload_progress[job_id] = {
        "phase":   phase,
        "current": current,
        "total":   total,
        **kwargs,
    }

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
def get_ip_for_limit(request: Request) -> str:
    return get_client_ip(request)

limiter = Limiter(key_func=get_ip_for_limit)

# ---------------------------------------------------------------------------
# リクエストボディサイズ制限ミドルウェア
# ---------------------------------------------------------------------------
class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "upload-and-zip" in str(request.url.path):
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024:
                return Response("リクエストサイズが上限を超えています", status_code=413)
        return await call_next(request)

# ---------------------------------------------------------------------------
# FastAPI アプリ
# ---------------------------------------------------------------------------
app = FastAPI()
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(LimitUploadSizeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return Response("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)

# ---------------------------------------------------------------------------
# 認証エンドポイント
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup")
@limiter.limit("3/hour")
async def signup(request: Request, user_data: UserRegister, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(400, "登録済みのメールアドレスです")
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(400, "使用済みのユーザー名です")
    hashed_pw = pwd_context.hash(user_data.password)
    db.add(User(username=user_data.username, email=user_data.email, hashed_password=hashed_pw))
    db.commit()
    return {"message": "ユーザー登録が完了しました。ログインしてください。"}

@app.post("/api/auth/token")
@limiter.limit("5/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(401, "ユーザー名またはパスワードが正しくありません")
    access_token = jwt.encode(
        {"sub": str(user.id), "exp": datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)},
        SECRET_KEY, algorithm=ALGORITHM
    )
    return {"access_token": access_token, "token_type": "bearer", "username": user.username}

# ---------------------------------------------------------------------------
# 作者：自分の作品一覧
# ---------------------------------------------------------------------------

@app.get("/api/author/works")
async def get_my_works(current_user: User = Depends(get_current_user)):
    works    = []
    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}"
    if not user_dir.exists():
        return []
    for settings_path in sorted(user_dir.glob("title_*/episode_*_settings.json")):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            cbz_path = settings_path.parent / f"episode_{meta['episode_id']}.cbz"
            cover_path = settings_path.parent / f"episode_{meta['episode_id']}_cover.jpg"
            meta["has_cbz"]   = cbz_path.exists()
            meta["has_cover"] = cover_path.exists()
            works.append(meta)
        except Exception:
            continue
    return works

# ---------------------------------------------------------------------------
# 作者：アップロード開始（job_idを返してバックグラウンドで処理）
# ---------------------------------------------------------------------------

@app.post("/api/author/titles/{title_id}/episodes/{episode_id}/upload-and-zip")
async def upload_manga(
    title_id:         int,
    episode_id:       int,
    background_tasks: BackgroundTasks,
    files:            List[UploadFile] = File(...),
    cover:            Optional[UploadFile] = File(default=None),
    do_scramble:      bool = True,
    tile_size:        int  = TILE_SIZE_DEFAULT,
    current_user:     User = Depends(get_current_user),
):
    # ── バリデーション ──
    total_size = 0
    for f in files:
        content = await f.read()
        await f.seek(0)
        size = len(content)
        if size > MAX_PAGE_SIZE_BYTES:
            raise HTTPException(400, f"{f.filename} が {MAX_PAGE_SIZE_MB}MB を超えています")
        total_size += size
        if total_size > MAX_TOTAL_SIZE_BYTES:
            raise HTTPException(400, f"合計サイズが {MAX_TOTAL_SIZE_MB}MB を超えています")

    if cover:
        cover_content = await cover.read()
        await cover.seek(0)
        if len(cover_content) > MAX_COVER_SIZE_BYTES:
            raise HTTPException(400, f"バンプ画像が {MAX_COVER_SIZE_MB}MB を超えています")

    # ── job_id 発行 ──
    job_id = uuid.uuid4().hex
    update_progress(job_id, "queued", 0, len(files))

    # ── バックグラウンドタスクに渡すためにファイル内容を読み込む ──
    file_contents = []
    for f in files:
        content = await f.read()
        file_contents.append((f.filename, content))

    cover_content = None
    cover_filename = None
    if cover:
        cover_content  = await cover.read()
        cover_filename = cover.filename

    background_tasks.add_task(
        _process_upload,
        job_id, title_id, episode_id,
        file_contents, cover_content, cover_filename,
        do_scramble, tile_size, current_user.id,
    )

    return {"job_id": job_id, "total_files": len(files)}

def _process_upload(
    job_id:         str,
    title_id:       int,
    episode_id:     int,
    file_contents:  list,
    cover_content:  Optional[bytes],
    cover_filename: Optional[str],
    do_scramble:    bool,
    tile_size:      int,
    user_id:        int,
):
    """バックグラウンドで実行されるアップロード処理"""
    user_dir  = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = user_dir / f"episode_{episode_id}_thumbs"
    thumb_dir.mkdir(exist_ok=True)
    final_zip_path = user_dir / f"episode_{episode_id}.cbz"
    work_dir = TMP_DIR / f"u{user_id}_t{title_id}_e{episode_id}_{uuid.uuid4().hex[:6]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── ファイル名でソート ──
        file_contents.sort(key=lambda x: natural_keys(x[0]))
        total = len(file_contents)
        saved_pages = []

        for index, (filename, content) in enumerate(file_contents):
            suffix = Path(filename).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue

            update_progress(job_id, "resize", index + 1, total)

            raw_path = work_dir / f"raw_{filename}"
            with open(raw_path, "wb") as f:
                f.write(content)

            # リサイズ（スクランブル前）
            with Image.open(raw_path) as img:
                img = img.convert("RGB")
                img.thumbnail(DISPLAY_MAX_SIZE, Image.LANCZOS)
                resized_path = work_dir / f"resized_{index:04d}.png"
                img.save(resized_path, "PNG")

            # サムネイル生成（非スクランブル）
            generate_thumbnail(resized_path, thumb_dir, index)

            update_progress(job_id, "scramble", index + 1, total)

            # スクランブル or そのまま保存
            page_filename = generate_page_filename(index)
            page_path     = work_dir / page_filename

            with Image.open(resized_path) as img:
                img = img.convert("RGB")
                if do_scramble:
                    seed_hex = page_filename[4:12]
                    seed     = int(seed_hex, 16)
                    img      = scramble_image(img, seed, tile_size)
                img.save(page_path, "PNG")

            saved_pages.append(page_path)
            raw_path.unlink()
            resized_path.unlink()

        # ── CBZ作成（ZIP_STORED: 画像は既に圧縮済みのため再圧縮不要）──
        update_progress(job_id, "zip", 0, len(saved_pages))
        with zipfile.ZipFile(final_zip_path, 'w', zipfile.ZIP_STORED) as comic_zip:
            for i, p in enumerate(saved_pages):
                update_progress(job_id, "zip", i + 1, len(saved_pages))
                comic_zip.write(p, arcname=p.name)

        # ── バンプ画像保存 ──
        if cover_content and cover_filename:
            cover_path = user_dir / f"episode_{episode_id}_cover.jpg"
            suffix = Path(cover_filename).suffix.lower()
            with Image.open(BytesIO(cover_content)) as img:
                img = img.convert("RGB")
                img.thumbnail((600, 900), Image.LANCZOS)
                img.save(cover_path, "JPEG", quality=85)

        update_progress(
            job_id, "done", len(saved_pages), len(saved_pages),
            page_count=len(saved_pages),
            scrambled=do_scramble,
            tile_size=tile_size,
        )

    except Exception as e:
        update_progress(job_id, "error", 0, 0, message=str(e))
        logger.error(f"Upload error [{job_id}]: {e}")
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir)

# ---------------------------------------------------------------------------
# 作者：アップロード進捗（ポーリング用GET）
# SSEはEventSourceがカスタムヘッダーを送れないため、
# 通常のGETポーリングで実装する
# ---------------------------------------------------------------------------

@app.get("/api/author/upload-progress/{job_id}")
async def get_upload_progress(
    job_id: str,
    current_user: User = Depends(get_current_user)
):
    progress = upload_progress.get(job_id)
    if not progress:
        # jobが見つからない場合はdone扱い（完了後に呼ばれた場合など）
        return {"phase": "done", "current": 0, "total": 0}

    # done/error の場合はストアから削除
    if progress["phase"] in ("done", "error"):
        upload_progress.pop(job_id, None)

    return progress

# ---------------------------------------------------------------------------
# 作者：設定保存
# ---------------------------------------------------------------------------

@app.patch("/api/author/titles/{title_id}/episodes/{episode_id}/settings")
async def update_settings(
    title_id:     int,
    episode_id:   int,
    settings:     EpisodePublishSettings,
    current_user: User = Depends(get_current_user)
):
    user_dir = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    settings_path = user_dir / f"episode_{episode_id}_settings.json"

    existing = {}
    if settings_path.exists():
        with open(settings_path, "r") as f:
            existing = json.load(f)

    existing.update({
        "status":          settings.status,
        "access_level":    settings.access_level,
        "price":           settings.price,
        "title_name":      settings.title_name,
        "episode_name":    settings.episode_name,
        "caption":         settings.caption,
        "comment":         settings.comment,
        "note":            settings.note,
        "scrambled":       settings.scrambled,
        "tile_size":       settings.tile_size,
        "webhook_enabled": settings.webhook_enabled,
        "author_name":     current_user.username,
        "author_id":       current_user.id,
        "title_id":        title_id,
        "episode_id":      episode_id,
        "updated_at":      time.time(),
    })
    if "created_at" not in existing:
        existing["created_at"] = time.time()

    with open(settings_path, "w") as f:
        json.dump(existing, f, indent=4, ensure_ascii=False)

    return {"status": "success"}

# ---------------------------------------------------------------------------
# 作者：エピソード削除
# ---------------------------------------------------------------------------

@app.delete("/api/author/titles/{title_id}/episodes/{episode_id}")
async def delete_episode(
    title_id:     int,
    episode_id:   int,
    current_user: User = Depends(get_current_user)
):
    user_dir      = FINAL_ZIP_DIR / f"user_{current_user.id}" / f"title_{title_id}"
    cbz_path      = user_dir / f"episode_{episode_id}.cbz"
    settings_path = user_dir / f"episode_{episode_id}_settings.json"
    cover_path    = user_dir / f"episode_{episode_id}_cover.jpg"
    thumb_dir     = user_dir / f"episode_{episode_id}_thumbs"

    deleted = []
    for path in [cbz_path, settings_path, cover_path]:
        if path.exists():
            path.unlink()
            deleted.append(path.name)
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)
        deleted.append("thumbs/")

    if not deleted:
        raise HTTPException(404, "削除対象のファイルが見つかりません")

    if user_dir.exists() and not any(user_dir.iterdir()):
        user_dir.rmdir()

    return {"status": "success", "deleted": deleted}

# ---------------------------------------------------------------------------
# 作者：プレビュー用CBZ
# ---------------------------------------------------------------------------

@app.get("/api/author/preview/{title_id}/{episode_id}.cbz")
async def preview_cbz(
    title_id:   int,
    episode_id: int,
    token:      str,
    db:         Session = Depends(get_db)
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンが無効または期限切れです")

    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")

    cbz_path = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}" / f"episode_{episode_id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")

    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"preview_t{title_id}_ep{episode_id}.cbz",
        headers={"Cache-Control": "no-store"},
    )

# ---------------------------------------------------------------------------
# 作者：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/author/thumb/{title_id}/{episode_id}/{index}")
async def author_thumb(
    title_id:   int,
    episode_id: int,
    index:      int,
    token:      str,
    db:         Session = Depends(get_db)
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except JWTError:
        raise HTTPException(status_code=401, detail="トークンが無効です")

    thumb_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_thumbs" / f"thumb_{index:04d}.jpg"
    )
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")

    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 公開：CBZ配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cbz/{user_id}/{title_id}/{episode_id}.cbz")
@limiter.limit("10/minute")
async def serve_public_cbz(
    request:    Request,
    user_id:    int,
    title_id:   int,
    episode_id: int,
):
    settings_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_settings.json"
    )
    if not settings_path.exists():
        raise HTTPException(status_code=404, detail="作品が見つかりません")

    with open(settings_path, "r") as f:
        meta = json.load(f)

    if meta.get("status") != "published":
        raise HTTPException(status_code=403, detail="この作品は非公開です")

    cbz_path = FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}" / f"episode_{episode_id}.cbz"
    if not cbz_path.exists():
        raise HTTPException(status_code=404, detail="CBZファイルが見つかりません")

    return FileResponse(
        path=str(cbz_path),
        media_type="application/zip",
        filename=f"title{title_id}_ep{episode_id}.cbz",
        headers={"Cache-Control": "public, max-age=3600"},
    )

# ---------------------------------------------------------------------------
# 公開：サムネイル配信
# ---------------------------------------------------------------------------

@app.get("/api/public/thumb/{user_id}/{title_id}/{episode_id}/{index}")
async def public_thumb(user_id: int, title_id: int, episode_id: int, index: int):
    settings_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_settings.json"
    )
    if settings_path.exists():
        with open(settings_path, "r") as f:
            meta = json.load(f)
        if meta.get("status") != "published":
            raise HTTPException(status_code=403, detail="非公開です")

    thumb_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_thumbs" / f"thumb_{index:04d}.jpg"
    )
    if not thumb_path.exists():
        raise HTTPException(status_code=404, detail="サムネイルが見つかりません")

    return FileResponse(str(thumb_path), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

# ---------------------------------------------------------------------------
# 公開：カタログ（cbz_url・cover_url付き、noteは除外）
# ---------------------------------------------------------------------------

@app.get("/api/public/catalog")
@limiter.limit("5/minute")
async def public_catalog(request: Request):
    base_url       = str(request.base_url).rstrip("/")
    published_list = []
    for settings_path in FINAL_ZIP_DIR.glob("user_*/title_*/episode_*_settings.json"):
        try:
            with open(settings_path, "r") as f:
                meta = json.load(f)
            if meta.get("status") != "published":
                continue
            uid = meta["author_id"]
            tid = meta["title_id"]
            eid = meta["episode_id"]
            meta["cbz_url"]   = f"{base_url}/api/public/cbz/{uid}/{tid}/{eid}.cbz"
            # カバー画像URL
            cover_path = settings_path.parent / f"episode_{eid}_cover.jpg"
            meta["cover_url"] = (
                f"{base_url}/api/public/cover/{uid}/{tid}/{eid}"
                if cover_path.exists() else None
            )
            # noteは公開APIから除外
            meta.pop("note", None)
            published_list.append(meta)
        except Exception:
            continue
    return published_list

# ---------------------------------------------------------------------------
# 公開：カバー画像配信
# ---------------------------------------------------------------------------

@app.get("/api/public/cover/{user_id}/{title_id}/{episode_id}")
async def public_cover(user_id: int, title_id: int, episode_id: int):
    cover_path = (
        FINAL_ZIP_DIR / f"user_{user_id}" / f"title_{title_id}"
        / f"episode_{episode_id}_cover.jpg"
    )
    if not cover_path.exists():
        raise HTTPException(status_code=404, detail="カバー画像が見つかりません")
    return FileResponse(str(cover_path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

# ---------------------------------------------------------------------------
# 設定情報（JSがプラットフォームURLを知るため）
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {"platform_base_url": base_url}

# ---------------------------------------------------------------------------
# 画面配信
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open(STATIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()

# ---------------------------------------------------------------------------
# UI HTML
# ---------------------------------------------------------------------------

index_html_content = r'''<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>マンガプラットフォーム</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:"Noto Sans JP",sans-serif;background:#f3f4f6;color:#333;min-height:100vh}
    nav{background:#1e293b;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
    nav .logo{color:#fff;font-weight:700;font-size:1.1rem;margin-right:8px}
    nav a{color:#94a3b8;font-size:.85rem;text-decoration:none;cursor:pointer;padding:4px 10px;border-radius:4px;transition:background .15s}
    nav a:hover{background:#334155;color:#fff}
    #login-status{margin-left:auto;color:#38bdf8;font-size:.8rem}
    .container{max-width:860px;margin:28px auto;background:#fff;padding:28px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.07)}
    .page{display:none}.page.active{display:block}
    h2{font-size:1.15rem;margin-bottom:18px;color:#1e293b}
    h3{font-size:.95rem;margin-bottom:12px;color:#334155}
    .form-group{margin-bottom:14px}
    .form-group label{display:block;font-size:.82rem;font-weight:600;margin-bottom:4px;color:#475569}
    .form-group input,.form-group select,.form-group textarea{width:100%;padding:8px 10px;border:1px solid #cbd5e1;border-radius:4px;font-size:.88rem;font-family:inherit}
    .form-group textarea{resize:vertical;min-height:60px}
    .btn{display:inline-flex;align-items:center;gap:.3em;padding:8px 18px;border:none;border-radius:4px;font-size:.84rem;font-weight:600;cursor:pointer;transition:background .15s}
    .btn-primary{background:#2563eb;color:#fff}.btn-primary:hover{background:#1d4ed8}
    .btn-success{background:#10b981;color:#fff}.btn-success:hover{background:#059669}
    .btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
    .btn-ghost{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}.btn-ghost:hover{background:#e2e8f0}
    .btn:disabled{background:#9ca3af;cursor:not-allowed}
    .btn-block{width:100%;justify-content:center}
    .drop-zone{border:2px dashed #cbd5e1;padding:24px;text-align:center;border-radius:6px;cursor:pointer;background:#f8fafc;font-size:.88rem;color:#64748b;transition:background .15s}
    .drop-zone:hover{background:#f1f5f9}
    .badge{display:inline-block;padding:2px 8px;font-size:.72rem;font-weight:700;border-radius:9999px;color:#fff}
    .badge-published{background:#10b981}.badge-draft{background:#94a3b8}
    .badge-free{background:#3b82f6}.badge-premium{background:#f59e0b}
    .work-card{border:1px solid #e2e8f0;border-radius:6px;padding:16px;margin-bottom:12px;background:#fafafa}
    .work-card-header{display:flex;align-items:flex-start;gap:12px}
    .work-card-cover{width:60px;height:90px;object-fit:cover;border-radius:4px;background:#e2e8f0;flex-shrink:0}
    .work-card-cover-placeholder{width:60px;height:90px;background:#e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:1.5rem;flex-shrink:0}
    .work-card-info{flex:1;min-width:0}
    .work-card-title{font-size:.95rem;font-weight:700;color:#1e293b;margin-bottom:3px}
    .work-card-sub{font-size:.77rem;color:#64748b;margin-top:4px}
    .work-card-caption{font-size:.8rem;color:#475569;margin-top:6px;line-height:1.5}
    .work-card-note{font-size:.78rem;color:#92400e;background:#fef3c7;border:1px solid #fde68a;border-radius:4px;padding:4px 8px;margin-top:6px}
    .work-card-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
    .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000;align-items:center;justify-content:center;overflow-y:auto;padding:20px}
    .modal-overlay.open{display:flex}
    .modal{background:#fff;border-radius:8px;padding:24px;width:min(560px,92vw);box-shadow:0 8px 32px rgba(0,0,0,.18)}
    .modal h3{margin-bottom:16px}
    .modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}
    .msg{font-size:.82rem;padding:8px 12px;border-radius:4px;margin-top:10px;display:block}
    .msg-error{background:#fef2f2;color:#dc2626;border:1px solid #fecaca}
    .msg-success{background:#f0fdf4;color:#16a34a;border:1px solid #bbf7d0}
    .msg-info{color:#64748b;font-size:.8rem;margin-top:6px}
    .divider{border:none;border-top:1px solid #e2e8f0;margin:20px 0}
    .empty-state{text-align:center;padding:40px;color:#94a3b8;font-size:.88rem;border:1px dashed #e2e8f0;border-radius:6px}
    .viewer-url-bar{background:#1e293b;color:#94a3b8;font-size:.78rem;padding:8px 20px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .viewer-url-bar input{flex:1;min-width:200px;padding:4px 8px;border-radius:4px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:.78rem}
    /* プログレスバー */
    .progress-wrap{background:#e2e8f0;border-radius:4px;height:8px;overflow:hidden;margin-top:8px}
    .progress-bar{height:100%;background:#2563eb;border-radius:4px;transition:width .2s ease;width:0%}
    .progress-label{font-size:.78rem;color:#475569;margin-top:4px}
    /* 公開作品カード */
    .catalog-card{border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:12px;background:#fff;display:flex;gap:14px;align-items:flex-start}
    .catalog-cover{width:80px;height:120px;object-fit:cover;border-radius:4px;background:#e2e8f0;flex-shrink:0}
    .catalog-cover-placeholder{width:80px;height:120px;background:#e2e8f0;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:2rem;flex-shrink:0}
    .catalog-info{flex:1;min-width:0}
    .catalog-title{font-size:1rem;font-weight:700;color:#1e293b;margin-bottom:4px}
    .catalog-sub{font-size:.78rem;color:#64748b;margin-bottom:6px}
    .catalog-caption{font-size:.82rem;color:#475569;line-height:1.6}
    /* スクランブル設定 */
    .scramble-option{display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #cbd5e1;border-radius:4px;cursor:pointer;transition:background .15s}
    .scramble-option:hover{background:#f8fafc}
    .scramble-option input{width:auto}
  </style>
</head>
<body>

<nav>
  <span class="logo">📚 マンガプラットフォーム</span>
  <a onclick="showPage('search-page'); loadPublishedManga();">🔍 公開作品</a>
  <a onclick="requireLogin('works-page'); loadMyWorks();">📋 マイ作品</a>
  <a onclick="showPage('upload-page')">📤 アップロード</a>
  <a onclick="showPage('auth-page')">🔐 アカウント</a>
  <span id="login-status">未ログイン</span>
</nav>

<div class="viewer-url-bar">
  <span>👁 ビューワーURL:</span>
  <input type="url" id="viewer-url-input" placeholder="https://xxxx.trycloudflare.com">
  <button class="btn btn-ghost" onclick="applyViewerUrl()" style="color:#e2e8f0;border-color:#475569">設定</button>
  <span id="viewer-url-status" style="color:#38bdf8"></span>
</div>

<!-- 公開作品一覧 -->
<div id="search-page" class="container page active">
  <h2>🔍 公開中の作品</h2>
  <button class="btn btn-ghost" onclick="loadPublishedManga()" style="margin-bottom:16px">🔄 更新</button>
  <div id="manga-list"><div class="empty-state">読み込み中…</div></div>
</div>

<!-- マイ作品管理 -->
<div id="works-page" class="container page">
  <h2>📋 マイ作品管理</h2>
  <button class="btn btn-ghost" onclick="loadMyWorks()" style="margin-bottom:16px">🔄 更新</button>
  <div id="works-list"><div class="empty-state">読み込み中…</div></div>
</div>

<!-- アップロード -->
<div id="upload-page" class="container page">
  <h2>📤 原稿アップロード</h2>
  <p id="upload-warn" class="msg msg-error" style="display:none">⚠️ 先にログインしてください</p>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>作品タイトル名</label>
      <input type="text" id="title_name" placeholder="例: 鬼の城">
    </div>
    <div class="form-group">
      <label>エピソード名</label>
      <input type="text" id="episode_name" placeholder="例: 第1話">
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>作品ID</label>
      <input type="number" id="title_id" value="1" min="1">
    </div>
    <div class="form-group">
      <label>エピソードID</label>
      <input type="number" id="episode_id" value="1" min="1">
    </div>
  </div>
  <div class="form-group">
    <label>キャプション <span style="font-weight:400;color:#94a3b8">（任意・公開）</span></label>
    <textarea id="caption" placeholder="作品の紹介文"></textarea>
  </div>
  <div class="form-group">
    <label>作者コメント <span style="font-weight:400;color:#94a3b8">（任意・公開）</span></label>
    <textarea id="comment" placeholder="読者へのコメント"></textarea>
  </div>
  <div class="form-group">
    <label>備考 <span style="font-weight:400;color:#92400e">（非公開・自分のみ）</span></label>
    <textarea id="note" placeholder="管理メモ、素材フォルダパスなど"></textarea>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group">
      <label>公開状態</label>
      <select id="status_select">
        <option value="draft">下書き（非公開）</option>
        <option value="published">公開</option>
      </select>
    </div>
    <div class="form-group">
      <label>アクセス権限</label>
      <select id="access_level_select">
        <option value="public">無料公開</option>
        <option value="premium">有料会員限定</option>
      </select>
    </div>
  </div>

  <!-- スクランブル設定 -->
  <div class="form-group">
    <label>🔒 画像スクランブル</label>
    <div style="display:flex;gap:8px;margin-top:4px">
      <label class="scramble-option" style="flex:1">
        <input type="radio" name="scramble" value="1" checked> する（推奨）
      </label>
      <label class="scramble-option" style="flex:1">
        <input type="radio" name="scramble" value="0"> しない
      </label>
    </div>
    <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
      <label style="font-size:.82rem;color:#475569;font-weight:600">タイルサイズ:</label>
      <select id="tile_size" style="width:auto;padding:4px 8px;border:1px solid #cbd5e1;border-radius:4px;font-size:.82rem">
        <option value="16" selected>16px（推奨）</option>
        <option value="8">8px（高強度）</option>
        <option value="32">32px（軽量）</option>
      </select>
    </div>
  </div>

  <!-- Webhook設定 -->
  <div class="form-group">
    <label>
      <input type="checkbox" id="webhook_enabled" style="width:auto;margin-right:6px">
      Discord Webhook送信を許可する
    </label>
  </div>

  <!-- バンプ画像 -->
  <div class="form-group">
    <label>🖼 バンプ画像（表紙）<span style="font-weight:400;color:#94a3b8"> 任意・5MBまで</span></label>
    <div class="drop-zone" id="cover-drop-zone">
      <p>表紙画像をドロップ、またはクリックして選択</p>
      <input type="file" id="cover-input" accept="image/*" style="display:none">
    </div>
    <div id="cover-preview" style="margin-top:8px"></div>
  </div>

  <!-- 原稿画像 -->
  <div class="form-group">
    <label>📄 原稿画像（複数可）<span style="font-weight:400;color:#94a3b8"> 1枚10MBまで・合計100MBまで</span></label>
    <div class="drop-zone" id="drop-zone">
      <p>原稿画像をドロップ、またはクリックして選択</p>
      <input type="file" id="file-input" multiple accept="image/*" style="display:none">
    </div>
    <div id="file-list" class="msg-info"></div>
  </div>

  <button id="upload-btn" class="btn btn-primary btn-block" onclick="startUpload()" disabled style="margin-top:14px">
    アップロード開始
  </button>

  <!-- プログレス表示 -->
  <div id="progress-area" style="display:none;margin-top:16px">
    <div id="progress-phase" style="font-size:.84rem;font-weight:600;color:#1e293b;margin-bottom:4px"></div>
    <div class="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    <div class="progress-label" id="progress-label"></div>
  </div>
  <div id="upload-status" style="margin-top:10px"></div>
</div>

<!-- アカウント -->
<div id="auth-page" class="container page">
  <h2>🔐 アカウント管理</h2>
  <div style="max-width:400px">
    <h3>ログイン</h3>
    <div class="form-group"><label>ユーザー名</label><input type="text" id="login-user"></div>
    <div class="form-group"><label>パスワード</label><input type="password" id="login-pass"></div>
    <button class="btn btn-primary btn-block" onclick="handleLogin()">ログインする</button>
    <div id="login-msg" style="margin-top:8px"></div>
    <hr class="divider">
    <h3>新規アカウント作成</h3>
    <div class="form-group"><label>ユーザー名</label><input type="text" id="reg-user" placeholder="taro_manga"></div>
    <div class="form-group"><label>メールアドレス</label><input type="email" id="reg-email" placeholder="test@example.com"></div>
    <div class="form-group"><label>パスワード</label><input type="password" id="reg-pass" placeholder="8文字以上"></div>
    <button class="btn btn-success btn-block" onclick="handleSignup()">アカウントを登録</button>
    <div id="signup-msg" style="margin-top:8px"></div>
  </div>
</div>

<!-- 編集モーダル -->
<div id="edit-modal" class="modal-overlay">
  <div class="modal">
    <h3>✏️ エピソード編集</h3>
    <input type="hidden" id="edit-title-id">
    <input type="hidden" id="edit-episode-id">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="form-group"><label>作品タイトル名</label><input type="text" id="edit-title-name"></div>
      <div class="form-group"><label>エピソード名</label><input type="text" id="edit-episode-name"></div>
    </div>
    <div class="form-group"><label>キャプション</label><textarea id="edit-caption"></textarea></div>
    <div class="form-group"><label>作者コメント</label><textarea id="edit-comment"></textarea></div>
    <div class="form-group"><label>備考（非公開）</label><textarea id="edit-note"></textarea></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div class="form-group">
        <label>公開状態</label>
        <select id="edit-status">
          <option value="draft">下書き（非公開）</option>
          <option value="published">公開</option>
        </select>
      </div>
      <div class="form-group">
        <label>アクセス権限</label>
        <select id="edit-access">
          <option value="public">無料公開</option>
          <option value="premium">有料会員限定</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label><input type="checkbox" id="edit-webhook" style="width:auto;margin-right:6px">Discord Webhook送信を許可する</label>
    </div>
    <div id="edit-msg" style="margin-top:8px"></div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeEditModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="saveEdit()">💾 保存</button>
    </div>
  </div>
</div>

<!-- 削除確認モーダル -->
<div id="delete-modal" class="modal-overlay">
  <div class="modal">
    <h3>🗑️ 削除の確認</h3>
    <p style="font-size:.88rem;color:#475569;margin-bottom:4px">以下のエピソードを削除します。この操作は取り消せません。</p>
    <p id="delete-target-name" style="font-weight:700;margin:12px 0;color:#1e293b"></p>
    <input type="hidden" id="delete-title-id">
    <input type="hidden" id="delete-episode-id">
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeDeleteModal()">キャンセル</button>
      <button class="btn btn-danger" onclick="confirmDelete()">削除する</button>
    </div>
  </div>
</div>

<script>
let token = "";
let selectedFiles = [];
let selectedCover = null;
let platformBaseUrl = "";
let viewerBaseUrl   = "";

/* ── 起動時設定取得 ── */
async function initConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    platformBaseUrl = cfg.platform_base_url.replace(/\/$/, "");
  } catch(e) {
    platformBaseUrl = window.location.origin;
  }
}

/* ── ビューワーURL設定 ── */
function applyViewerUrl() {
  const val = document.getElementById("viewer-url-input").value.trim().replace(/\/$/, "");
  if (!val) return;
  viewerBaseUrl = val;
  document.getElementById("viewer-url-status").textContent = "✅ 設定済み";
  loadPublishedManga();
  if (token) loadMyWorks();
}

/* ── ページ切替 ── */
function showPage(pageId) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(pageId).classList.add('active');
}
function requireLogin(pageId) {
  if (!token) { showPage('auth-page'); showMsg('login-msg','error','ログインが必要です'); return; }
  showPage(pageId);
}

/* ── メッセージ ── */
function showMsg(id, type, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `msg msg-${type}`;
  el.textContent = text;
  el.style.display = 'block';
}

/* ── 認証 ── */
async function handleLogin() {
  const fd = new FormData();
  fd.append("username", document.getElementById("login-user").value);
  fd.append("password", document.getElementById("login-pass").value);
  try {
    const res  = await fetch("/api/auth/token", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "ログイン失敗");
    token = data.access_token;
    document.getElementById("login-status").textContent = `👤 ${data.username}`;
    document.getElementById("upload-warn").style.display = "none";
    showMsg('login-msg','success', `${data.username} としてログインしました`);
    showPage('works-page');
    loadMyWorks();
  } catch(e) { showMsg('login-msg','error', e.message); }
}

async function handleSignup() {
  const payload = {
    username: document.getElementById("reg-user").value,
    email:    document.getElementById("reg-email").value,
    password: document.getElementById("reg-pass").value,
  };
  try {
    const res  = await fetch("/api/auth/signup", {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "登録エラー");
    showMsg('signup-msg','success', data.message);
  } catch(e) { showMsg('signup-msg','error', e.message); }
}

/* ── 公開作品一覧 ── */
async function loadPublishedManga() {
  const el = document.getElementById("manga-list");
  el.innerHTML = '<div class="empty-state">読み込み中…</div>';
  try {
    const res   = await fetch("/api/public/catalog");
    const items = await res.json();
    if (!items.length) { el.innerHTML = '<div class="empty-state">公開中の作品はありません</div>'; return; }
    el.innerHTML = items.map(m => {
      const title   = esc(m.title_name   || `作品${m.title_id}`);
      const episode = esc(m.episode_name || `エピソード${m.episode_id}`);
      const canRead = viewerBaseUrl && m.cbz_url;
      const coverHtml = m.cover_url
        ? `<img class="catalog-cover" src="${esc(m.cover_url)}" alt="cover">`
        : `<div class="catalog-cover-placeholder">📖</div>`;
      return `
        <div class="catalog-card">
          ${coverHtml}
          <div class="catalog-info">
            <div class="catalog-title">${title} — ${episode}</div>
            <div class="catalog-sub">
              👤 ${esc(m.author_name)} &nbsp;
              <span class="badge ${m.access_level==='public'?'badge-free':'badge-premium'}">
                ${m.access_level==='public'?'無料':'PREMIUM'}
              </span>
            </div>
            ${m.caption ? `<div class="catalog-caption">${esc(m.caption)}</div>` : ''}
            ${canRead ? `<button class="btn btn-primary" style="margin-top:10px" onclick="openViewer('${esc(m.cbz_url)}','${esc(JSON.stringify(m))}')">📖 読む</button>`
                      : `<span style="font-size:.75rem;color:#94a3b8;margin-top:8px;display:block">ビューワーURL未設定</span>`}
          </div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<div class="empty-state">読み込みエラー: ${e.message}</div>`; }
}

/* ── マイ作品一覧 ── */
async function loadMyWorks() {
  if (!token) return;
  const el = document.getElementById("works-list");
  el.innerHTML = '<div class="empty-state">読み込み中…</div>';
  try {
    const res   = await fetch("/api/author/works", { headers: {"Authorization":`Bearer ${token}`} });
    if (!res.ok) throw new Error("取得失敗");
    const works = await res.json();
    if (!works.length) {
      el.innerHTML = '<div class="empty-state">まだ作品がありません。アップロードページから投稿してください。</div>';
      return;
    }
    el.innerHTML = works.map(w => {
      const titleLabel   = esc(w.title_name   || `作品${w.title_id}`);
      const episodeLabel = esc(w.episode_name || `エピソード${w.episode_id}`);
      const isPublished  = w.status === 'published';
      const previewUrl   = `${platformBaseUrl}/api/author/preview/${w.title_id}/${w.episode_id}.cbz?token=${encodeURIComponent(token)}`;
      const canPreview   = viewerBaseUrl && w.has_cbz;
      const coverHtml    = w.has_cover
        ? `<img class="work-card-cover" src="${platformBaseUrl}/api/public/cover/${w.author_id}/${w.title_id}/${w.episode_id}" alt="cover">`
        : `<div class="work-card-cover-placeholder">📖</div>`;
      return `
        <div class="work-card">
          <div class="work-card-header">
            ${coverHtml}
            <div class="work-card-info">
              <div class="work-card-title">${titleLabel} — ${episodeLabel}</div>
              <div class="work-card-sub">
                <span class="badge ${isPublished?'badge-published':'badge-draft'}">${isPublished?'公開中':'下書き'}</span>
                &nbsp;
                <span class="badge ${w.access_level==='public'?'badge-free':'badge-premium'}">${w.access_level==='public'?'無料':'PREMIUM'}</span>
                ${w.scrambled ? ' &nbsp;<span style="font-size:.72rem;color:#7c3aed">🔒 スクランブル済</span>' : ''}
                ${!w.has_cbz  ? ' &nbsp;<span style="color:#ef4444;font-size:.75rem">⚠ CBZ未生成</span>' : ''}
              </div>
              ${w.caption ? `<div class="work-card-caption">${esc(w.caption)}</div>` : ''}
              ${w.note    ? `<div class="work-card-note">📝 ${esc(w.note)}</div>` : ''}
            </div>
          </div>
          <div class="work-card-actions">
            <button class="btn btn-ghost" onclick='openEditModal(${w.title_id},${w.episode_id},${JSON.stringify(w)})'>✏️ 編集</button>
            ${canPreview ? `<button class="btn btn-ghost" onclick="openPreview('${previewUrl}')">👁 プレビュー</button>` : ''}
            <button class="btn btn-danger" style="margin-left:auto" onclick="openDeleteModal(${w.title_id},${w.episode_id},'${titleLabel} — ${episodeLabel}')">🗑 削除</button>
          </div>
        </div>`;
    }).join('');
  } catch(e) { el.innerHTML = `<div class="empty-state">エラー: ${e.message}</div>`; }
}

/* ── ビューワー連携 ── */
function openViewer(cbzUrl) {
  if (!viewerBaseUrl) { alert("ビューワーURLを設定してください"); return; }
  window.open(`${viewerBaseUrl}/reader?${new URLSearchParams({url:cbzUrl})}`, '_blank');
}
function openPreview(previewUrl) {
  if (!viewerBaseUrl) { alert("ビューワーURLを設定してください"); return; }
  window.open(`${viewerBaseUrl}/reader?${new URLSearchParams({url:previewUrl})}`, '_blank');
}

/* ── 編集モーダル ── */
function openEditModal(titleId, episodeId, w) {
  document.getElementById('edit-title-id').value     = titleId;
  document.getElementById('edit-episode-id').value   = episodeId;
  document.getElementById('edit-title-name').value   = w.title_name   || '';
  document.getElementById('edit-episode-name').value = w.episode_name || '';
  document.getElementById('edit-caption').value      = w.caption      || '';
  document.getElementById('edit-comment').value      = w.comment      || '';
  document.getElementById('edit-note').value         = w.note         || '';
  document.getElementById('edit-status').value       = w.status;
  document.getElementById('edit-access').value       = w.access_level;
  document.getElementById('edit-webhook').checked    = !!w.webhook_enabled;
  document.getElementById('edit-msg').textContent    = '';
  document.getElementById('edit-modal').classList.add('open');
}
function closeEditModal() { document.getElementById('edit-modal').classList.remove('open'); }

async function saveEdit() {
  const titleId   = document.getElementById('edit-title-id').value;
  const episodeId = document.getElementById('edit-episode-id').value;
  const payload = {
    title_name:      document.getElementById('edit-title-name').value,
    episode_name:    document.getElementById('edit-episode-name').value,
    caption:         document.getElementById('edit-caption').value,
    comment:         document.getElementById('edit-comment').value,
    note:            document.getElementById('edit-note').value,
    status:          document.getElementById('edit-status').value,
    access_level:    document.getElementById('edit-access').value,
    webhook_enabled: document.getElementById('edit-webhook').checked,
    price: 0,
    scrambled: true, tile_size: 16, // 編集時は既存値を維持（変更不可）
  };
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/episodes/${episodeId}/settings`, {
      method:'PATCH', headers:{'Content-Type':'application/json','Authorization':`Bearer ${token}`},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '保存失敗');
    showMsg('edit-msg','success','保存しました');
    setTimeout(() => { closeEditModal(); loadMyWorks(); }, 800);
  } catch(e) { showMsg('edit-msg','error', e.message); }
}

/* ── 削除モーダル ── */
function openDeleteModal(titleId, episodeId, label) {
  document.getElementById('delete-title-id').value   = titleId;
  document.getElementById('delete-episode-id').value = episodeId;
  document.getElementById('delete-target-name').textContent = label;
  document.getElementById('delete-modal').classList.add('open');
}
function closeDeleteModal() { document.getElementById('delete-modal').classList.remove('open'); }

async function confirmDelete() {
  const titleId   = document.getElementById('delete-title-id').value;
  const episodeId = document.getElementById('delete-episode-id').value;
  try {
    const res  = await fetch(`/api/author/titles/${titleId}/episodes/${episodeId}`, {
      method:'DELETE', headers:{'Authorization':`Bearer ${token}`}
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '削除失敗');
    closeDeleteModal();
    loadMyWorks();
  } catch(e) { alert('削除エラー: ' + e.message); }
}

/* ── ファイル選択（原稿） ── */
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', e => handleFiles(e.target.files));
dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.style.background='#e2e8f0'; });
dropZone.addEventListener('dragleave', () => dropZone.style.background='#f8fafc');
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.style.background='#f8fafc'; handleFiles(e.dataTransfer.files); });

const MAX_PAGE_MB  = 10;
const MAX_TOTAL_MB = 100;

function handleFiles(files) {
  const imgs = Array.from(files).filter(f => f.type.startsWith('image/'));
  let totalSize = 0;
  const errors  = [];

  for (const f of imgs) {
    if (f.size > MAX_PAGE_MB * 1024 * 1024) {
      errors.push(`${f.name} が ${MAX_PAGE_MB}MB を超えています`);
    }
    totalSize += f.size;
  }
  if (totalSize > MAX_TOTAL_MB * 1024 * 1024) {
    errors.push(`合計サイズが ${MAX_TOTAL_MB}MB を超えています`);
  }

  if (errors.length) {
    document.getElementById('file-list').innerHTML =
      `<span style="color:#dc2626">${errors.join('<br>')}</span>`;
    selectedFiles = [];
    document.getElementById('upload-btn').disabled = true;
    return;
  }

  selectedFiles = imgs;
  document.getElementById('file-list').textContent =
    `${imgs.length} 枚 / 合計 ${(totalSize/1024/1024).toFixed(1)}MB`;
  document.getElementById('upload-btn').disabled = imgs.length === 0 || !token;
}

/* ── ファイル選択（バンプ画像） ── */
const coverDropZone = document.getElementById('cover-drop-zone');
const coverInput    = document.getElementById('cover-input');
coverDropZone.addEventListener('click', () => coverInput.click());
coverInput.addEventListener('change', e => handleCover(e.target.files[0]));
coverDropZone.addEventListener('dragover',  e => { e.preventDefault(); coverDropZone.style.background='#e2e8f0'; });
coverDropZone.addEventListener('dragleave', () => coverDropZone.style.background='#f8fafc');
coverDropZone.addEventListener('drop', e => { e.preventDefault(); coverDropZone.style.background='#f8fafc'; handleCover(e.dataTransfer.files[0]); });

function handleCover(file) {
  if (!file || !file.type.startsWith('image/')) return;
  if (file.size > 5 * 1024 * 1024) {
    document.getElementById('cover-preview').innerHTML =
      '<span style="color:#dc2626;font-size:.82rem">バンプ画像が5MBを超えています</span>';
    selectedCover = null;
    return;
  }
  selectedCover = file;
  const url = URL.createObjectURL(file);
  document.getElementById('cover-preview').innerHTML =
    `<img src="${url}" style="height:90px;border-radius:4px;border:1px solid #e2e8f0">
     <span style="font-size:.78rem;color:#475569;margin-left:8px">${file.name}</span>`;
}

/* ── アップロード（ポーリング版） ── */
async function startUpload() {
  if (!token) { showPage('auth-page'); return; }
  const tId      = document.getElementById('title_id').value;
  const eId      = document.getElementById('episode_id').value;
  const scramble = document.querySelector('input[name="scramble"]:checked').value;
  const tileSize = document.getElementById('tile_size').value;
  const statusEl = document.getElementById('upload-status');
  const progArea = document.getElementById('progress-area');

  statusEl.textContent = '';
  statusEl.className   = '';
  progArea.style.display = 'block';
  document.getElementById('upload-btn').disabled = true;
  updateProgressUI({phase:'queued', current:0, total:selectedFiles.length});

  try {
    const fd = new FormData();
    selectedFiles.forEach(f => fd.append('files', f));
    if (selectedCover) fd.append('cover', selectedCover);

    const uploadUrl = `/api/author/titles/${tId}/episodes/${eId}/upload-and-zip`
                    + `?do_scramble=${scramble==='1'}&tile_size=${tileSize}`;

    const upRes  = await fetch(uploadUrl, {
      method: 'POST',
      headers: {"Authorization": `Bearer ${token}`},
      body: fd
    });
    const upData = await upRes.json();
    if (!upRes.ok) throw new Error(upData.detail || 'アップロード失敗');

    // ポーリングで進捗監視
    await pollProgress(upData.job_id);

    // 設定保存
    const setRes = await fetch(`/api/author/titles/${tId}/episodes/${eId}/settings`, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json', 'Authorization':`Bearer ${token}`},
      body: JSON.stringify({
        status:          document.getElementById('status_select').value,
        access_level:    document.getElementById('access_level_select').value,
        title_name:      document.getElementById('title_name').value,
        episode_name:    document.getElementById('episode_name').value,
        caption:         document.getElementById('caption').value,
        comment:         document.getElementById('comment').value,
        note:            document.getElementById('note').value,
        webhook_enabled: document.getElementById('webhook_enabled').checked,
        scrambled:       scramble === '1',
        tile_size:       parseInt(tileSize),
        price: 0,
      })
    });
    if (!setRes.ok) throw new Error('設定保存失敗');

    showMsg('upload-status', 'success', '✅ アップロード完了！');
    selectedFiles = [];
    selectedCover = null;
    document.getElementById('file-list').textContent    = '';
    document.getElementById('cover-preview').innerHTML  = '';

  } catch(e) {
    showMsg('upload-status', 'error', '❌ ' + e.message);
  } finally {
    document.getElementById('upload-btn').disabled = selectedFiles.length === 0;
  }
}

/* ── ユーティリティ ── */
function esc(str) {
  return String(str||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/* ── 進捗ポーリング ── */
async function pollProgress(jobId) {
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/author/upload-progress/${jobId}`, {
          headers: {"Authorization": `Bearer ${token}`}
        });
        if (!res.ok) { clearInterval(interval); reject(new Error('進捗取得失敗')); return; }
        const p = await res.json();
        updateProgressUI(p);
        if (p.phase === 'done')  { clearInterval(interval); resolve(p); }
        if (p.phase === 'error') { clearInterval(interval); reject(new Error(p.message)); }
      } catch(e) { clearInterval(interval); reject(e); }
    }, 400);
  });
}

const PHASE_LABELS = {
  queued:   '待機中…',
  resize:   'リサイズ処理中',
  scramble: 'スクランブル処理中',
  zip:      'CBZ作成中',
  done:     '完了',
  error:    'エラー',
};

function updateProgressUI(p) {
  document.getElementById('progress-phase').textContent = PHASE_LABELS[p.phase] || p.phase;
  const pct = p.total > 0 ? Math.round(p.current / p.total * 100) : 0;
  document.getElementById('progress-bar').style.width  = pct + '%';
  document.getElementById('progress-label').textContent =
    p.total > 0 ? `${p.current} / ${p.total} (${pct}%)` : '';
}

/* 起動 */
initConfig();
loadPublishedManga();
</script>
</body>
</html>
'''

with open(STATIC_DIR / "index.html", "w", encoding="utf-8") as f:
    f.write(index_html_content)
