"""Topics: hold everything about a subject (sheet, searchable pages, CSV) and rebuild it on demand."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import topics
from app.jobs.runner import runner

router = APIRouter(prefix="/api/topics")


@router.get("")
async def list_topics() -> list[dict]:
    return topics.list_all()


class NewTopic(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    question: str = Field("", max_length=500)
    urls: list[str] = Field(default_factory=list, max_length=12)
    build: bool = True


@router.post("", status_code=201)
async def create_topic(body: NewTopic) -> dict:
    try:
        t = topics.create(body.name, body.question, body.urls)
    except topics.TopicError as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.build:
        t["job_id"] = runner.submit("topic", f"Topic: {t['name']}", topics.build_job(t["id"]), lane="background")
    return t


@router.get("/{tid}")
async def get_topic(tid: str) -> dict:
    try:
        return topics.get(tid)
    except KeyError:
        raise HTTPException(404, "Topic not found") from None


@router.post("/{tid}/build", status_code=202)
async def build_topic(tid: str) -> dict:
    try:
        t = topics.get(tid)
    except KeyError:
        raise HTTPException(404, "Topic not found") from None
    if t["status"] == "building":
        raise HTTPException(409, "This topic is already being built")
    return {"job_id": runner.submit("topic", f"Topic: {t['name']}", topics.build_job(tid), lane="background")}


@router.delete("/{tid}", status_code=204)
async def delete_topic(tid: str, remove_data: bool = False) -> None:
    if not await topics.delete(tid, remove_data):
        raise HTTPException(404, "Topic not found")
