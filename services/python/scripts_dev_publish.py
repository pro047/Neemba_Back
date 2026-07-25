"""dev 검증용 발행 헬퍼(가짜 발행자): NATS 에 정상 자막 또는 깨진 페이로드를 쏜다.

python 컨테이너 안에서 실행 (/app/.env 의 NATS_URL 사용):
  docker compose -f docker-compose.dev.yml exec python \
    python3 scripts_dev_publish.py normal|garbage <count>
"""
import asyncio
import json
import sys
import time


async def main() -> None:
    kind, count = sys.argv[1], int(sys.argv[2])
    url = None
    with open('/app/.env') as f:
        for line in f:
            if line.startswith('NATS_URL'):
                url = line.split('=', 1)[1].strip().strip('"').strip("'")
    import nats
    nc = await nats.connect(url)
    js = nc.jetstream()
    base = int(time.time())  # sequence 를 시각 기반으로 → 중복/워터마크 회피
    for i in range(count):
        if kind == 'garbage':
            await nc.publish('transcript.session.hb1', b'not-json-garbage')
        else:
            await js.publish('transcript.session.hb1', json.dumps({
                'sessionId': 'hb1', 'segmentId': 1, 'sequence': base + i,
                'transcriptText': f'배터리 문장 {base + i}입니다.',
                'targetLanguage': 'en-US', 'sourceLanguage': 'ko-KR',
            }).encode())
    await nc.close()
    print(f'published {count} {kind}')


asyncio.run(main())
