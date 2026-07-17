from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Union, Dict, Any
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.get("/")
async def root():
    return FileResponse("index.html")