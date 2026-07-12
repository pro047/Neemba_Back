# 인시던트: TLS 인증서 만료로 전체 서비스 접속 불가 (2026-07-12)

## 요약

Let's Encrypt 인증서가 2026-07-11 만료됐다. certbot 자동 갱신이 **약 한 달간
조용히 실패**해왔기 때문이다. 앱·브라우저는 TLS 검증 실패로 요청을 서버에
보내지도 못했고("create session failed", `ERR_CERT_DATE_INVALID`), 서버 로그엔
아무것도 남지 않아 진단이 어려웠다. 수동 갱신으로 복구 완료 (신규 만료:
2026-10-10).

## 타임라인

| 시각 | 사건 |
|---|---|
| 2026-04-12 | 인증서 발급 (90일, neemba.app + www.neemba.app) |
| ~2026-06 중순부터 | certbot 12시간 주기 갱신 시도가 매번 `Some challenges have failed`로 실패 — **알림이 없어 아무도 모름** |
| 2026-07-11 01:13 UTC | 인증서 만료 |
| 2026-07-12 | 앱 start 실패 신고 → 진단 → 수동 갱신으로 복구 |

## 근본 원인 (3겹)

1. **바인드 마운트 분리(직접 원인)**: certbot과 nginx가 같은 호스트 폴더
   (`infra/nginx/html`)를 공유해야 챌린지 토큰이 전달되는데, 컨테이너가 3개월
   전에 뜬 상태에서 배포(rm -rf 후 재추출)로 호스트 폴더가 재생성되자
   **기존 컨테이너의 마운트는 삭제된 옛 폴더(inode)를 계속 참조**했다.
   certbot이 쓴 토큰을 nginx가 못 봐서 HTTP-01 검증이 매번 실패.
   - 검증 방법이었던 것: `docker exec certbot sh -c 'echo x > /var/www/html/.well-known/acme-challenge/probe'`
     후 `curl http://neemba.app/.well-known/acme-challenge/probe` → 404면 분리 확정.
2. **갱신 실패 무알림**: 한 달간 로그에 실패가 찍혔지만(`docker logs certbot`)
   아무도 보지 않았다. 만료 D-14 경보가 있었다면 사전에 잡혔다.
3. **행(hang) 시 무증상**: 복구 중 entrypoint의 renew 프로세스가 락을 쥔 채
   멈춰 수동 갱신을 전부 차단했다. 컨테이너는 `Up`이라 헬스체크로는 안 보인다.

## 복구 절차 (재사용 가능)

```bash
# 1. 마운트 재정렬 (컨테이너를 현재 호스트 폴더에 다시 바인드)
cd /srv/neemba && docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --force-recreate certbot nginx

# 2. 토큰 왕복 확인 (hello-123이 보여야 함)
docker exec certbot sh -c 'mkdir -p /var/www/html/.well-known/acme-challenge && echo hello-123 > /var/www/html/.well-known/acme-challenge/probe'
curl -s http://neemba.app/.well-known/acme-challenge/probe

# 3. 자체 루프가 락을 쥐고 있으면 일회용 컨테이너로 우회 갱신
docker stop certbot
docker run --rm -v /srv/neemba/infra/letsencrypt:/etc/letsencrypt \
  -v /srv/neemba/infra/nginx/html:/var/www/html \
  certbot/certbot renew --webroot -w /var/www/html
docker start certbot

# 4. 반영·확인·정리
docker exec nginx nginx -s reload
echo | openssl s_client -connect neemba.app:443 -servername neemba.app 2>/dev/null | openssl x509 -noout -dates
docker exec certbot rm -f /var/www/html/.well-known/acme-challenge/probe
```

## 재발 방지

- [x] nginx 12시간 자체 reload — develop PR #13 (갱신 "반영" 누락 방지, 배포 대기)
- [ ] **인증서 만료 감시**: 만료 D-14 미만이면 경보하는 체크(cron 또는 외부
      모니터링). 이번 사건의 가장 싼 예방책 — 백로그 최상단
- [ ] 배포 후 컨테이너 재생성 정책: rm-rf 배포와 장수 컨테이너 조합이 마운트
      분리를 만든다. 배포 시 `--force-recreate` 또는 배포 방식 자체 개선
- [ ] prod 알림 체계 (감사 P3 — Prometheus/alerting 부재의 실제 비용 사례)

## 진단 시 배운 것

- 앱만 실패하고 서버 로그가 조용하면 **TLS/DNS 등 "서버 도달 전" 구간**부터
  의심 — 폰 브라우저로 열어보는 게 가장 빠른 검증이었다.
- `curl -k`는 만료 인증서를 통과시키므로 인증서 문제 진단에 쓰면 오판한다.
  검증 포함 확인: `openssl s_client ... | openssl x509 -noout -dates`.
- 진단 중 세션을 여는 API 호출(start)은 반드시 짝 stop으로 정리.
