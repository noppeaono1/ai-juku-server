from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Union, Dict, Any, Optional
import httpx
import os
import sqlite3
import stripe
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Stripe設定
# ============================================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "price_1TuBBA4TqZ7NhzVJ1dy5agyR")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# 決済完了後・キャンセル時に戻ってくるURL(自分のサービスのURLに変更する)
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://ai-juku-server.onrender.com")

# ============================================================
# 軽量DB(SQLite)。誰が課金済みかをここで管理する
# ============================================================
DB_PATH = Path(__file__).parent / "subscribers.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            email TEXT PRIMARY KEY,
            stripe_customer_id TEXT,
            subscription_id TEXT,
            status TEXT DEFAULT 'inactive',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


init_db()


def upsert_subscriber(email: str, customer_id: str = None, subscription_id: str = None, status: str = None):
    conn = get_db()
    existing = conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE subscribers SET
                stripe_customer_id = COALESCE(?, stripe_customer_id),
                subscription_id = COALESCE(?, subscription_id),
                status = COALESCE(?, status),
                updated_at = CURRENT_TIMESTAMP
               WHERE email = ?""",
            (customer_id, subscription_id, status, email),
        )
    else:
        conn.execute(
            "INSERT INTO subscribers (email, stripe_customer_id, subscription_id, status) VALUES (?, ?, ?, ?)",
            (email, customer_id, subscription_id, status or "inactive"),
        )
    conn.commit()
    conn.close()


def get_subscriber(email: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ============================================================
# 既存: Claude APIチャットエンドポイント
# ============================================================

class Message(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]  # 画像付きメッセージは配列で来るため文字列限定にしない

class ChatRequest(BaseModel):
    messages: List[Message]
    system: str

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            res = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.getenv("ANTHROPIC_API_KEY"),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1000,
                    "system": req.system,
                    "messages": [m.dict() for m in req.messages],
                }
            )
            return res.json()
    except httpx.TimeoutException:
        return {"error": {"message": "AIの応答がタイムアウトしました。もう一度試してね。"}}
    except httpx.RequestError:
        return {"error": {"message": "通信エラーが発生しました。ネット接続を確認してもう一度試してね。"}}
    except Exception:
        return {"error": {"message": "予期しないエラーが発生しました。もう一度試してね。"}}


# ============================================================
# Stripe: サブスクリプション状態の確認
# ============================================================

@app.get("/check-subscription")
async def check_subscription(email: str):
    sub = get_subscriber(email)
    is_active = bool(sub and sub["status"] == "active")
    return {"email": email, "active": is_active}


# ============================================================
# Stripe: Checkoutセッションの作成(月額課金の決済画面へ誘導)
# ============================================================

class CheckoutRequest(BaseModel):
    email: str

@app.post("/create-checkout-session")
async def create_checkout_session(req: CheckoutRequest):
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=req.email,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=f"{APP_BASE_URL}/?checkout=success",
            cancel_url=f"{APP_BASE_URL}/?checkout=cancel",
            metadata={"email": req.email},
        )
        # 決済前の段階として、メールアドレスをDBに仮登録(未課金状態)しておく
        upsert_subscriber(req.email, status=None)
        return {"url": session.url}
    except Exception as e:
        print(f"[create-checkout-session エラー] {repr(e)}")
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================
# Stripe: カスタマーポータル(解約・支払い方法の変更など)
# ============================================================

class PortalRequest(BaseModel):
    email: str

@app.post("/create-portal-session")
async def create_portal_session(req: PortalRequest):
    sub = get_subscriber(req.email)
    if not sub or not sub.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="加入情報が見つかりません")
    try:
        session = stripe.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url=f"{APP_BASE_URL}/",
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================
# Stripe: Webhook(決済完了・解約などのイベントを受け取る)
# ============================================================

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="不正なWebhookリクエストです")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        email = data.get("customer_email") or (data.get("metadata") or {}).get("email")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        if email:
            upsert_subscriber(email, customer_id=customer_id, subscription_id=subscription_id, status="active")

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = data.get("customer")
        status = "active" if data.get("status") == "active" else "inactive"
        conn = get_db()
        row = conn.execute("SELECT email FROM subscribers WHERE stripe_customer_id = ?", (customer_id,)).fetchone()
        conn.close()
        if row:
            upsert_subscriber(row["email"], status=status)

    elif event_type == "invoice.payment_failed":
        customer_id = data.get("customer")
        conn = get_db()
        row = conn.execute("SELECT email FROM subscribers WHERE stripe_customer_id = ?", (customer_id,)).fetchone()
        conn.close()
        if row:
            upsert_subscriber(row["email"], status="inactive")

    return {"received": True}


# ============================================================
# トップページ
# ============================================================

@app.get("/")
async def root():
    return FileResponse(
        "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
