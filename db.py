import time

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_URI

_db = AsyncIOMotorClient(MONGO_URI)["terabox_bot"] if MONGO_URI else None


async def add(kind: str, _id: int):
    if _db is not None:
        await _db[kind].update_one({"_id": _id}, {"$set": {"seen": time.time()}}, upsert=True)


async def count(kind: str) -> int:
    return await _db[kind].count_documents({}) if _db is not None else 0


async def ids(kind: str):
    if _db is None:
        return []
    return [d["_id"] async for d in _db[kind].find({}, {"_id": 1})]
