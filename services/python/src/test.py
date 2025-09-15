from services.python.src.database.pool import Db
import asyncio


async def testpool():
    db = Db()
    conn = await db.create_pool()

    print(conn)

asyncio.run(testpool())
